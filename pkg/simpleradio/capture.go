package simpleradio

import (
	"context"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
	"github.com/lithammer/shortuuid/v3"
	"time"
)

func (c *Client) captureIdentity(t transmissionPackets) transcript.Record {
	name, _ := c.getPeerName(t.origin)
	modulation := "AM"
	if t.radio.Modulation == types.ModulationFM {
		modulation = "FM"
	}
	return transcript.Record{ID: t.traceID, FrequencyHz: uint64(t.radio.Frequency), Modulation: modulation, Speaker: name, Route: "ignored"}
}

func (c *Client) captureRecord(t transmissionPackets, kind, reason string) {
	if kind == "inbound" && t.receipt != nil {
		t.receipt.finish("", reason, nil)
		return
	}
	record := c.captureIdentity(t)
	record.Kind, record.Outcome = kind, reason
	c.transcript.Log(record)
}
func (c *Client) captureFailure(t transmissionPackets, reason string) {
	c.captureRecord(t, "inbound", reason)
}

// Assembly is independent per heard origin and command channel. Only
// admitMVPPacket can mint a reply route; observing a nonpilot/self/ambiguous
// packet cannot grant one. Each capture carries the channel it was heard on
// so the reply goes back on that channel (multi-frequency, 2026-11-07).
func (c *Client) receiveMVPVoice(ctx context.Context, in <-chan voiceDatagram, out chan<- transmissionPackets) {
	type capture struct {
		receiver *receiver
		id       string
		origin   types.GUID
		receipt  *captureReceipt
	}
	type captureKey struct {
		origin types.GUID
		radio  types.Radio
	}
	ledger := &captureLedger{ctx: ctx, writer: c.transcript, pending: make(map[*captureReceipt]transcript.Record)}
	defer ledger.cancel()
	active := map[captureKey]*capture{}
	ticker := time.NewTicker(frameLength)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil {
			return
		}
		select {
		case <-ctx.Done():
			return
		case b, ok := <-in:
			if !ok {
				return
			}
			if ctx.Err() != nil {
				return
			}
			packet, err := voice.Decode(b.data)
			if err != nil {
				for _, a := range active {
					a.receiver.rejectCurrent()
				}
				c.transcript.Log(transcript.Record{Kind: "reception_error", ID: shortuuid.New(), Outcome: "malformed_datagram", Route: "ignored"})
				continue
			}
			route, _ := c.admitMVPPacket(packet, b.epoch)
			if radio, matched := c.mvpPacketRadio(packet); matched {
				origin := types.GUID(packet.OriginGUID)
				// receiveUDP snapshots the epoch before blocking Read. A stale
				// datagram must not start assembly and poison admitted packets.
				// Discard it as the legacy command path did; never promote its
				// audio or replace an existing capture's route token.
				if route.rejection == "packet_epoch_mismatch" {
					c.captureFailure(transmissionPackets{radio: radio, origin: origin, traceID: shortuuid.New()}, "packet_epoch_mismatch")
					continue
				}
				key := captureKey{origin: origin, radio: mvpChannel(radio)}
				a := active[key]
				if a == nil {
					if len(active) >= 64 {
						c.captureFailure(transmissionPackets{radio: radio, origin: origin, traceID: shortuuid.New()}, "assembly_capacity_fragment")
						continue
					}
					a = &capture{receiver: &receiver{radio: radio}, id: shortuuid.New(), origin: origin}
					active[key] = a
					a.receipt = ledger.begin(c.captureIdentity(transmissionPackets{radio: radio, origin: origin, traceID: a.id}))
				}
				// Keep observation epoch even when no reply token was minted.
				if route.used == nil {
					route.epoch = b.epoch
				}
				if a.receiver.route.used != nil && route.used == nil {
					a.receiver.rejectCurrent()
				}
				a.receiver.receive(packet, route)
			}
		case <-ticker.C:
			for key, a := range active {
				if !a.receiver.hasTransmission() {
					continue
				}
				tr, valid := a.receiver.takeCompleted()
				tr.traceID, tr.origin = a.id, a.origin
				tr.receipt = a.receipt
				delete(active, key)
				if !valid {
					c.captureFailure(tr, "short_overflow_or_invalid_audio")
					continue
				}
				select {
				case out <- tr:
				default:
					c.captureFailure(tr, "decode_queue_full")
				}
			}
		}
	}
}
