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

// Assembly is independent per heard origin. Only admitMVPPacket can mint a
// reply route; observing a nonpilot/self/ambiguous packet cannot grant one.
func (c *Client) receiveMVPVoice(ctx context.Context, in <-chan voiceDatagram, out chan<- transmissionPackets) {
	type capture struct {
		receiver *receiver
		id       string
		origin   types.GUID
		receipt  *captureReceipt
	}
	ledger := &captureLedger{ctx: ctx, writer: c.transcript, pending: make(map[*captureReceipt]transcript.Record)}
	defer ledger.cancel()
	active := map[types.GUID]*capture{}
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
			for radio := range c.receivers {
				matched := false
				for _, f := range packet.Frequencies {
					matched = matched || exactMVPRadio(types.Radio{Frequency: f.Frequency, Modulation: types.Modulation(f.Modulation), IsEncrypted: f.Encryption != 0}, radio)
				}
				if !matched {
					continue
				}
				origin := types.GUID(packet.OriginGUID)
				a := active[origin]
				if a == nil {
					if len(active) >= 64 {
						c.captureFailure(transmissionPackets{radio: radio, origin: origin, traceID: shortuuid.New()}, "assembly_capacity_fragment")
						continue
					}
					a = &capture{receiver: &receiver{radio: radio}, id: shortuuid.New(), origin: origin}
					active[origin] = a
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
			for origin, a := range active {
				if !a.receiver.hasTransmission() {
					continue
				}
				tr, valid := a.receiver.takeCompleted()
				tr.traceID, tr.origin = a.id, origin
				tr.receipt = a.receipt
				delete(active, origin)
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
