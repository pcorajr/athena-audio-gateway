package simpleradio

import (
	"context"
	"errors"
	"math"
	"strings"
	"sync/atomic"
	"time"

	"github.com/dharmab/skyeye/pkg/pcm/rate"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
	"gopkg.in/hraban/opus.v2"
)

type voiceDatagram struct {
	data  []byte
	epoch uint64
}

type mvpRoute struct {
	origin types.GUID
	epoch  uint64
	used   *atomic.Bool
}

type mvpIdentity struct {
	guid   types.GUID
	unitID uint64
	unit   string
	seat   int
}

func validateMVPConfig(cfg types.ClientConfiguration) error {
	if !cfg.SRSMVP {
		return nil
	}
	if len(cfg.Radios) != 1 || strings.TrimSpace(cfg.PilotName) == "" || strings.TrimSpace(cfg.ClientName) == strings.TrimSpace(cfg.PilotName) {
		return errors.New("MVP requires one radio and a distinct pilot name")
	}
	r := cfg.Radios[0]
	if r.Frequency <= 0 || math.IsNaN(r.Frequency) || math.IsInf(r.Frequency, 0) || r.Frequency != math.Trunc(r.Frequency) ||
		(r.Modulation != types.ModulationAM && r.Modulation != types.ModulationFM) || r.IsEncrypted || r.GuardFrequency != 0 || r.ShouldRetransmit {
		return errors.New("MVP requires one unencrypted AM/FM command radio without guard or retransmit")
	}
	return nil
}

// Caller holds clientsLock. Count names across the entire live snapshot, not
// just on-frequency clients: a duplicate display name is ambiguous.
func (c *Client) mvpIdentityLocked() mvpIdentity {
	if !c.mvp || len(c.clientInfo.RadioInfo.Radios) != 1 {
		return mvpIdentity{}
	}
	var pilot types.ClientInfo
	count := 0
	for _, peer := range c.clients {
		if strings.TrimSpace(peer.Name) == strings.TrimSpace(c.pilotName) {
			count++
			pilot = peer
		}
	}
	if count != 1 || pilot.GUID == "" || pilot.GUID == c.clientInfo.GUID || pilot.Coalition != c.clientInfo.Coalition {
		return mvpIdentity{}
	}
	for _, radio := range pilot.RadioInfo.Radios {
		if exactMVPRadio(radio, c.clientInfo.RadioInfo.Radios[0]) {
			return mvpIdentity{guid: pilot.GUID, unitID: pilot.RadioInfo.UnitID, unit: pilot.RadioInfo.Unit, seat: pilot.Seat}
		}
	}
	return mvpIdentity{}
}

func exactMVPRadio(a, b types.Radio) bool {
	return a.Frequency == b.Frequency && a.Modulation == b.Modulation && !a.IsEncrypted && !b.IsEncrypted
}

func (c *Client) syncMVPClients(peers []types.ClientInfo) {
	c.clientsLock.Lock()
	defer c.clientsLock.Unlock()
	before := c.mvpIdentityLocked()
	c.clients = make(map[types.GUID]types.ClientInfo, len(peers))
	for _, peer := range peers {
		if peer.GUID != c.clientInfo.GUID {
			c.clients[peer.GUID] = peer
		}
	}
	if before != c.mvpIdentityLocked() {
		c.mvpEpoch++
	}
}

func (c *Client) syncMVPClient(peer types.ClientInfo) {
	c.clientsLock.Lock()
	defer c.clientsLock.Unlock()
	before := c.mvpIdentityLocked()
	if peer.GUID != c.clientInfo.GUID {
		c.clients[peer.GUID] = peer
	}
	if before != c.mvpIdentityLocked() {
		c.mvpEpoch++
	}
}

func (c *Client) invalidateMVP() {
	if !c.mvp {
		return
	}
	c.clientsLock.Lock()
	defer c.clientsLock.Unlock()
	c.mvpEpoch++
	c.clients = make(map[types.GUID]types.ClientInfo)
	c.mvpWriter = nil
	c.mvpLastOrigin, c.mvpLastPacket = "", 0
}

func (c *Client) admitMVPPacket(packet *voice.Packet, epoch uint64) (mvpRoute, bool) {
	c.clientsLock.Lock()
	defer c.clientsLock.Unlock()
	identity := c.mvpIdentityLocked()
	origin := types.GUID(packet.OriginGUID)
	if epoch != c.mvpEpoch || c.mvpWriter == nil || len(c.clientInfo.RadioInfo.Radios) != 1 {
		return mvpRoute{}, false
	}
	matched := false
	for _, f := range packet.Frequencies {
		if f.Encryption == 0 && exactMVPRadio(types.Radio{Frequency: f.Frequency, Modulation: types.Modulation(f.Modulation)}, c.clientInfo.RadioInfo.Radios[0]) {
			matched = true
		}
	}
	if matched {
		c.mvpChannelUntil = time.Now().Add(maxRxGap)
	}
	if identity.guid == "" || identity.guid != origin || origin == c.clientInfo.GUID || types.GUID(packet.RelayGUID) == c.clientInfo.GUID {
		return mvpRoute{}, false
	}
	if !matched || (origin == c.mvpLastOrigin && packet.PacketID <= c.mvpLastPacket) {
		return mvpRoute{}, false
	}
	c.mvpLastOrigin, c.mvpLastPacket = origin, packet.PacketID
	return mvpRoute{origin: origin, epoch: epoch, used: &atomic.Bool{}}, true
}

func (c *Client) validMVPLocked(t Transmission) bool {
	return c.mvp && c.mvpWriter != nil && t.route.used != nil && t.route.epoch == c.mvpEpoch &&
		t.route.origin != "" && t.route.origin == c.mvpIdentityLocked().guid &&
		len(c.clientInfo.RadioInfo.Radios) == 1 && exactMVPRadio(t.Radio, c.clientInfo.RadioInfo.Radios[0])
}

// ValidMVP revalidates the original connection, speaker and command route.
func (c *Client) ValidMVP(t Transmission) bool {
	c.clientsLock.RLock()
	defer c.clientsLock.RUnlock()
	return c.validMVPLocked(t)
}

// Delivery is a local socket-write result, never proof of audibility.
type Delivery struct {
	FirstSend time.Time
	LastSend  time.Time
	Outcome   string
}

// TransmitMVP is synchronous: no pending reply queue, no retry, no replay.
// Expiry bounds waiting and the first write. Once started, finish the short
// response unless canceled, identity changes, or the transport fails.
func (c *Client) TransmitMVP(ctx context.Context, t Transmission, expires time.Time) Delivery {
	c.txLock.Lock()
	defer c.txLock.Unlock()
	if !c.mvp || t.route.used == nil || t.route.used.Swap(true) {
		return Delivery{Outcome: "invalid_or_replayed"}
	}
	if c.mute || !c.liveTX {
		return Delivery{Outcome: "interlocked"}
	}
	if !c.ValidMVP(t) {
		return Delivery{Outcome: "identity_changed"}
	}
	if ctx.Err() != nil || !time.Now().Before(expires) {
		return Delivery{Outcome: "expired"}
	}
	packets, err := c.encodeMVP(t.Audio)
	if err != nil {
		return Delivery{Outcome: "encode_failed"}
	}
	return c.sendMVP(ctx, t, expires, packets)
}

// encodeMVP uses exactly the sole configured command route, never the legacy
// all-radios frequency list. The caller holds txLock for packet numbering.
func (c *Client) encodeMVP(audio Audio) ([]voice.Packet, error) {
	if len(c.clientInfo.RadioInfo.Radios) != 1 || len(audio) == 0 {
		return nil, errors.New("invalid MVP audio or radio")
	}
	encoder, err := opus.NewEncoder(int(rate.Wideband.Hertz()), channels, opusApplicationVoIP)
	if err != nil {
		return nil, err
	}
	radio := c.clientInfo.RadioInfo.Radios[0]
	frequencies := []voice.Frequency{{Frequency: radio.Frequency, Modulation: byte(radio.Modulation)}}
	packets := make([]voice.Packet, 0)
	for i := 0; i < len(audio); i += int(frameSize) {
		frame := make([]float32, int(frameSize))
		copy(frame, audio[i:min(i+int(frameSize), len(audio))])
		encoded, err := encodeFrame(encoder, frame)
		if err != nil {
			return nil, err
		}
		guid := []byte(c.clientInfo.GUID)
		packets = append(packets, voice.NewPacket(encoded, frequencies, uint32(c.clientInfo.RadioInfo.UnitID), c.packetNumber, 0, guid, guid))
		c.packetNumber++
	}
	return packets, nil
}

func (c *Client) sendMVP(ctx context.Context, t Transmission, expires time.Time, packets []voice.Packet) Delivery {
	d := Delivery{Outcome: "expired"}
	waitCtx, cancel := context.WithDeadline(ctx, expires)
	defer cancel()
	for {
		if waitCtx.Err() != nil || !c.ValidMVP(t) {
			return d
		}
		c.clientsLock.RLock()
		busy := time.Now().Before(c.mvpChannelUntil)
		c.clientsLock.RUnlock()
		for _, r := range c.receivers {
			busy = busy || r.isReceivingTransmission()
		}
		if !busy {
			break
		}
		if !waitMVP(waitCtx, frameLength) {
			return d
		}
	}
	start := time.Now()
	for i, packet := range packets {
		if !waitMVP(ctx, time.Until(start.Add(time.Duration(i)*frameLength-frameLength/2))) {
			d.Outcome = "canceled"
			return d
		}
		// The lock linearizes identity invalidation and each socket write.
		// Interlocks and route are checked here, including the first write.
		c.clientsLock.RLock()
		if c.mute || !c.liveTX || !c.validMVPLocked(t) || c.mvpWriter == nil || ctx.Err() != nil ||
			(i == 0 && (!time.Now().Before(expires) || time.Now().Before(c.mvpChannelUntil))) {
			c.clientsLock.RUnlock()
			d.Outcome = "invalidated"
			return d
		}
		if len(packet.Frequencies) != 1 || !exactMVPRadio(types.Radio{Frequency: packet.Frequencies[0].Frequency, Modulation: types.Modulation(packet.Frequencies[0].Modulation), IsEncrypted: packet.Frequencies[0].Encryption != 0}, t.Radio) {
			c.clientsLock.RUnlock()
			d.Outcome = "route_mismatch"
			return d
		}
		b := packet.Encode()
		// UDP writes can otherwise block even after the reply expires.
		if writer, ok := c.mvpWriter.(interface{ SetWriteDeadline(time.Time) error }); ok {
			deadline := time.Now().Add(frameLength)
			if i == 0 && expires.Before(deadline) {
				deadline = expires
			}
			if err := writer.SetWriteDeadline(deadline); err != nil {
				c.clientsLock.RUnlock()
				d.Outcome = "write_failed"
				return d
			}
		}
		n, err := c.mvpWriter.Write(b)
		if err == nil && n == len(b) {
			now := time.Now()
			if d.FirstSend.IsZero() {
				d.FirstSend = now
			}
			d.LastSend = now
		}
		if writer, ok := c.mvpWriter.(interface{ SetWriteDeadline(time.Time) error }); ok {
			// Pings share the UDP socket; do not leave an expired deadline on it.
			if resetErr := writer.SetWriteDeadline(time.Time{}); err == nil {
				err = resetErr
			}
		}
		c.clientsLock.RUnlock()
		if err != nil || n != len(b) {
			d.Outcome = "write_failed"
			return d
		}
	}
	if len(packets) == 0 {
		d.Outcome = "empty_audio"
	} else {
		d.Outcome = "sent"
	}
	return d
}

func waitMVP(ctx context.Context, delay time.Duration) bool {
	if ctx.Err() != nil {
		return false
	}
	if delay <= 0 {
		return true
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
