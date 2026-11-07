package simpleradio

import (
	"context"
	"errors"
	"math"
	"strings"
	"sync/atomic"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
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
	// Content-free packet admission reason for observed non-command audio.
	rejection string
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
	identity, _ := c.mvpIdentityReasonLocked()
	return identity
}

func (c *Client) mvpIdentityReasonLocked() (mvpIdentity, string) {
	if !c.mvp || len(c.clientInfo.RadioInfo.Radios) != 1 {
		return mvpIdentity{}, "radio_configuration"
	}
	var pilot types.ClientInfo
	count := 0
	for _, peer := range c.clients {
		if strings.TrimSpace(peer.Name) == strings.TrimSpace(c.pilotName) {
			count++
			pilot = peer
		}
	}
	if count == 0 {
		return mvpIdentity{}, "pilot_missing"
	}
	if count != 1 {
		return mvpIdentity{}, "pilot_ambiguous"
	}
	if pilot.GUID == "" {
		return mvpIdentity{}, "pilot_guid_missing"
	}
	if pilot.GUID == c.clientInfo.GUID {
		return mvpIdentity{}, "pilot_is_self"
	}
	if pilot.Coalition != c.clientInfo.Coalition {
		return mvpIdentity{}, "pilot_coalition_mismatch"
	}
	for _, radio := range pilot.RadioInfo.Radios {
		if exactMVPRadio(radio, c.clientInfo.RadioInfo.Radios[0]) {
			return mvpIdentity{guid: pilot.GUID, unitID: pilot.RadioInfo.UnitID, unit: pilot.RadioInfo.Unit, seat: pilot.Seat}, ""
		}
	}
	// Hotfix: peer radio list can lag/differ; the transmission frequency is checked separately.
	return mvpIdentity{guid: pilot.GUID, unitID: pilot.RadioInfo.UnitID, unit: pilot.RadioInfo.Unit, seat: pilot.Seat}, ""
}

func exactMVPRadio(a, b types.Radio) bool {
	return a.Frequency == b.Frequency && a.Modulation == b.Modulation && !a.IsEncrypted && !b.IsEncrypted
}

func (c *Client) syncMVPClients(peers []types.ClientInfo) {
	c.clientsLock.Lock()
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
	record := c.mvpReadinessLocked()
	c.clientsLock.Unlock()
	c.transcript.Log(record)
}

func (c *Client) syncMVPClient(peer types.ClientInfo) {
	c.clientsLock.Lock()
	before := c.mvpIdentityLocked()
	beforeReason := c.mvpReadinessLocked().Outcome
	if peer.GUID != c.clientInfo.GUID {
		c.clients[peer.GUID] = peer
	}
	changed := before != c.mvpIdentityLocked()
	if changed {
		c.mvpEpoch++
	}
	record := c.mvpReadinessLocked()
	c.clientsLock.Unlock()
	if changed || beforeReason != record.Outcome {
		c.transcript.Log(record)
	}
}

// Diagnostic guard state only, not authority to transmit a future reception.
// Never include peer names, GUIDs, credentials or packets.
func (c *Client) mvpReadinessLocked() transcript.Record {
	_, reason := c.mvpIdentityReasonLocked()
	if c.mvpWriter == nil {
		reason = "transport_unavailable"
	}
	if reason == "" {
		reason = "ready"
	}
	r := transcript.Record{Kind: "radio_readiness", Speaker: "Athena", Route: "ignored", Outcome: reason}
	if len(c.clientInfo.RadioInfo.Radios) == 1 {
		radio := c.clientInfo.RadioInfo.Radios[0]
		r.FrequencyHz, r.Modulation = uint64(radio.Frequency), "AM"
		if radio.Modulation == types.ModulationFM {
			r.Modulation = "FM"
		}
	}
	return r
}

func (c *Client) invalidateMVP() {
	if !c.mvp {
		return
	}
	c.clientsLock.Lock()
	c.mvpEpoch++
	c.clients = make(map[types.GUID]types.ClientInfo)
	c.mvpWriter = nil
	c.mvpLastOrigin, c.mvpLastPacket = "", 0
	record := c.mvpReadinessLocked()
	c.clientsLock.Unlock()
	c.transcript.Log(record)
}

func (c *Client) admitMVPPacket(packet *voice.Packet, epoch uint64) (mvpRoute, bool) {
	c.clientsLock.Lock()
	defer c.clientsLock.Unlock()
	reject := func(reason string) (mvpRoute, bool) { return mvpRoute{rejection: reason}, false }
	identity, identityReason := c.mvpIdentityReasonLocked()
	origin := types.GUID(packet.OriginGUID)
	if epoch != c.mvpEpoch {
		return reject("packet_epoch_mismatch")
	}
	if c.mvpWriter == nil {
		return reject("transport_unavailable")
	}
	if len(c.clientInfo.RadioInfo.Radios) != 1 {
		return reject("radio_configuration")
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
	if identityReason != "" {
		return reject(identityReason)
	}
	if origin == c.clientInfo.GUID {
		return reject("self_origin")
	}
	if types.GUID(packet.RelayGUID) == c.clientInfo.GUID {
		return reject("self_relay")
	}
	if identity.guid != origin {
		return reject("origin_mismatch")
	}
	if !matched {
		return reject("packet_radio_mismatch")
	}
	if origin == c.mvpLastOrigin && packet.PacketID <= c.mvpLastPacket {
		return reject("packet_replayed")
	}
	c.mvpLastOrigin, c.mvpLastPacket = origin, packet.PacketID
	return mvpRoute{origin: origin, epoch: epoch, used: &atomic.Bool{}}, true
}

func (c *Client) validMVPLocked(t Transmission) bool {
	return c.mvpRejectionLocked(t) == ""
}

func (c *Client) mvpRejectionLocked(t Transmission) string {
	if !c.mvp {
		return "radio_mvp_disabled"
	}
	if c.mvpWriter == nil {
		return "radio_transport_unavailable"
	}
	if t.route.used == nil {
		if t.route.rejection != "" {
			return "radio_route_missing:" + t.route.rejection
		}
		return "radio_route_missing"
	}
	if t.route.epoch != c.mvpEpoch {
		return "radio_epoch_mismatch"
	}
	if t.route.origin == "" {
		return "radio_origin_missing"
	}
	identity, reason := c.mvpIdentityReasonLocked()
	if reason != "" {
		return "radio_" + reason
	}
	if t.route.origin != identity.guid {
		return "radio_origin_mismatch"
	}
	if !exactMVPRadio(t.Radio, c.clientInfo.RadioInfo.Radios[0]) {
		return "radio_channel_mismatch"
	}
	return ""
}

// MVPRejection uses exactly ValidMVP's guards without minting or consuming a
// token. Reasons are constants, never peer metadata or transcript content.
func (c *Client) MVPRejection(t Transmission) string {
	if c == nil {
		return "radio_unavailable"
	}
	c.clientsLock.RLock()
	defer c.clientsLock.RUnlock()
	return c.mvpRejectionLocked(t)
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
	Reason    string // bounded guard detail, never an error body
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
		return Delivery{Outcome: "interlocked", Reason: c.mvpInterlockReason()}
	}
	if reason := c.MVPRejection(t); reason != "" {
		return Delivery{Outcome: "identity_changed", Reason: reason}
	}
	if ctx.Err() != nil {
		return Delivery{Outcome: "canceled"}
	}
	if !time.Now().Before(expires) {
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
		if ctx.Err() != nil {
			d.Outcome = "canceled"
			return d
		}
		if waitCtx.Err() != nil {
			return d
		}
		if reason := c.MVPRejection(t); reason != "" {
			d.Outcome, d.Reason = "invalidated", reason
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
			if ctx.Err() != nil {
				d.Outcome = "canceled"
			}
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
		reason := c.mvpInterlockReason()
		if reason == "" {
			reason = c.mvpRejectionLocked(t)
		}
		if reason == "" && ctx.Err() != nil {
			reason = "canceled"
		}
		if reason == "" && i == 0 && !time.Now().Before(expires) {
			reason = "expired"
		}
		if reason == "" && i == 0 && time.Now().Before(c.mvpChannelUntil) {
			reason = "channel_busy"
		}
		if reason != "" {
			c.clientsLock.RUnlock()
			d.Outcome, d.Reason = "invalidated", reason
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

func (c *Client) mvpInterlockReason() string {
	if c.mute {
		return "muted"
	}
	if !c.liveTX {
		return "live_tx_disabled"
	}
	return ""
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
