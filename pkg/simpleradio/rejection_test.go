package simpleradio

import (
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/coalitions"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

func TestPacket108RadioRejectionReasons(t *testing.T) {
	for _, tc := range []struct {
		want   string
		mutate func(*Client, *Transmission)
	}{
		{"radio_mvp_disabled", func(c *Client, _ *Transmission) { c.mvp = false }},
		{"radio_transport_unavailable", func(c *Client, _ *Transmission) { c.mvpWriter = nil }},
		{"radio_route_missing", func(_ *Client, tr *Transmission) { tr.route.used = nil }},
		{"radio_epoch_mismatch", func(c *Client, _ *Transmission) { c.mvpEpoch++ }},
		{"radio_origin_missing", func(_ *Client, tr *Transmission) { tr.route.origin = "" }},
		{"radio_origin_mismatch", func(_ *Client, tr *Transmission) { tr.route.origin = types.NewGUID() }},
		{"radio_channel_mismatch", func(_ *Client, tr *Transmission) { tr.Radio.Frequency++ }},
		{"radio_radio_configuration", func(c *Client, _ *Transmission) { c.clientInfo.RadioInfo.Radios = nil }},
		{"radio_pilot_missing", func(c *Client, _ *Transmission) { c.clients = nil }},
		{"radio_pilot_ambiguous", func(c *Client, tr *Transmission) {
			p := c.clients[tr.route.origin]
			p.GUID = types.NewGUID()
			c.clients[p.GUID] = p
		}},
		{"radio_pilot_guid_missing", func(c *Client, tr *Transmission) {
			p := c.clients[tr.route.origin]
			p.GUID = ""
			c.clients[tr.route.origin] = p
		}},
		{"radio_pilot_is_self", func(c *Client, tr *Transmission) {
			p := c.clients[tr.route.origin]
			p.GUID = c.clientInfo.GUID
			c.clients[tr.route.origin] = p
		}},
		{"radio_pilot_coalition_mismatch", func(c *Client, tr *Transmission) {
			p := c.clients[tr.route.origin]
			p.Coalition = coalitions.Red
			c.clients[tr.route.origin] = p
		}},
	} {
		t.Run(tc.want, func(t *testing.T) {
			c, tr, _ := mvpTestClient()
			if c.MVPRejection(tr) != "" || !c.ValidMVP(tr) {
				t.Fatal("positive control failed")
			}
			tc.mutate(c, &tr)
			if reason := c.MVPRejection(tr); reason != tc.want || c.ValidMVP(tr) {
				t.Fatalf("got %q want %q", reason, tc.want)
			}
		})
	}
	// 2026-11-07 hotfix: the pilot's SRS radio list can lag the actual transmission, so
	// identity no longer requires an exact radio match (frequency is checked per transmission).
	t.Run("pilot_radio_list_mismatch_accepted", func(t *testing.T) {
		c, tr, _ := mvpTestClient()
		p := c.clients[tr.route.origin]
		p.RadioInfo.Radios = nil
		c.clients[tr.route.origin] = p
		if reason := c.MVPRejection(tr); reason != "" || !c.ValidMVP(tr) {
			t.Fatalf("got %q want accepted", reason)
		}
	})
}

func TestPacket108MissingRouteRetainsPacketAdmissionReason(t *testing.T) {
	for _, tc := range []struct {
		want   string
		mutate func(*Client, *voice.Packet, *uint64)
	}{
		{"packet_epoch_mismatch", func(_ *Client, _ *voice.Packet, epoch *uint64) { *epoch = 0 }},
		{"transport_unavailable", func(c *Client, _ *voice.Packet, _ *uint64) { c.mvpWriter = nil }},
		{"radio_configuration", func(c *Client, _ *voice.Packet, _ *uint64) { c.clientInfo.RadioInfo.Radios = nil }},
		{"pilot_missing", func(c *Client, _ *voice.Packet, _ *uint64) { c.clients = nil }},
		{"self_origin", func(c *Client, p *voice.Packet, _ *uint64) { p.OriginGUID = []byte(c.clientInfo.GUID) }},
		{"self_relay", func(c *Client, p *voice.Packet, _ *uint64) { p.RelayGUID = []byte(c.clientInfo.GUID) }},
		{"origin_mismatch", func(_ *Client, p *voice.Packet, _ *uint64) { p.OriginGUID = []byte(types.NewGUID()) }},
		{"packet_radio_mismatch", func(_ *Client, p *voice.Packet, _ *uint64) { p.Frequencies[0].Frequency++ }},
		{"packet_replayed", func(c *Client, p *voice.Packet, _ *uint64) {
			c.mvpLastOrigin, c.mvpLastPacket = types.GUID(p.OriginGUID), p.PacketID
		}},
	} {
		t.Run(tc.want, func(t *testing.T) {
			c, tr, _ := mvpTestClient()
			p := voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: tr.Radio.Frequency}}, 123, 1, 0, []byte(tr.route.origin), []byte(tr.route.origin))
			epoch := c.mvpEpoch
			tc.mutate(c, &p, &epoch)
			route, ok := c.admitMVPPacket(&p, epoch)
			if ok || route.used != nil || route.rejection != tc.want {
				t.Fatalf("ok=%t route=%+v", ok, route)
			}
		})
	}
}

func TestPacket108DeliveryInterlocksHaveSpecificReasons(t *testing.T) {
	for _, mute := range []bool{false, true} {
		c, tr, w := mvpTestClient()
		c.mute, c.liveTX = mute, mute
		want := "live_tx_disabled"
		if mute {
			want = "muted"
		}
		d := c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
		if d.Outcome != "interlocked" || d.Reason != want || w.writes != 0 {
			t.Fatalf("%+v", d)
		}
	}
}
