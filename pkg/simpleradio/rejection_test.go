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
	// 2026-11-07 hotfix: single-player SRS reports the pilot in a different coalition.
	t.Run("pilot_coalition_mismatch_accepted", func(t *testing.T) {
		c, tr, _ := mvpTestClient()
		p := c.clients[tr.route.origin]
		p.Coalition = coalitions.Red
		c.clients[tr.route.origin] = p
		if reason := c.MVPRejection(tr); reason != "" || !c.ValidMVP(tr) {
			t.Fatalf("got %q want accepted", reason)
		}
	})
	// A stale duplicate of the pilot name no longer blocks the radio: the peer on the
	// command radio (then lowest GUID) is chosen deterministically.
	t.Run("pilot_duplicate_resolved", func(t *testing.T) {
		c, tr, _ := mvpTestClient()
		p := c.clients[tr.route.origin]
		p.GUID = "AAAAduplicate000000000"
		p.RadioInfo.Radios = []types.Radio{{Frequency: 251000000, Modulation: types.ModulationAM}}
		c.clients[p.GUID] = p
		for range 20 {
			if reason := c.MVPRejection(tr); reason != "" || !c.ValidMVP(tr) {
				t.Fatalf("got %q want accepted", reason)
			}
		}
	})
}

// 2026-11-07 hotfix: the pilot setting is a comma-separated, case-insensitive list.
// The earliest list entry present wins, independent of map iteration order.
func TestPilotListIdentity(t *testing.T) {
	for _, tc := range []struct {
		name, list string
		want       int
	}{
		{"Prometheus", "Prometheus,Player,Client", 0},
		{"player", "Prometheus,Player,Client", 1},
		{" CLIENT ", "Prometheus, Player , Client", 2},
		{"Athena", "Prometheus,Player,Client", -1},
		{"", "Prometheus,,Client", -1},
		{"Players", "Prometheus,Player,Client", -1},
	} {
		if got := pilotListRank(tc.name, tc.list); got != tc.want {
			t.Errorf("pilotListRank(%q, %q) = %d, want %d", tc.name, tc.list, got, tc.want)
		}
		if got := pilotListMatch(tc.name, tc.list); got != (tc.want >= 0) {
			t.Errorf("pilotListMatch(%q, %q) = %v", tc.name, tc.list, got)
		}
	}

	t.Run("single_player_name", func(t *testing.T) {
		c, tr, _ := mvpTestClient()
		c.pilotName = "Prometheus,Player,Client"
		p := c.clients[tr.route.origin]
		p.Name = "player"
		c.clients[tr.route.origin] = p
		if reason := c.MVPRejection(tr); reason != "" || !c.ValidMVP(tr) {
			t.Fatalf("got %q want accepted", reason)
		}
	})
	t.Run("list_order_wins", func(t *testing.T) {
		c, tr, _ := mvpTestClient()
		c.pilotName = "Prometheus,Player,Client"
		other := c.clients[tr.route.origin]
		other.GUID, other.Name = "AAAAclient000000000000", "Client"
		c.clients[other.GUID] = other
		for range 20 {
			c.clientsLock.Lock()
			id, reason := c.mvpIdentityReasonLocked()
			c.clientsLock.Unlock()
			if reason != "" || id.guid != tr.route.origin {
				t.Fatalf("identity %q reason %q, want Prometheus peer", id.guid, reason)
			}
		}
	})
	t.Run("client_name_in_pilot_list_rejected", func(t *testing.T) {
		cfg := types.ClientConfiguration{SRSMVP: true, ClientName: "player", PilotName: "Prometheus,Player",
			Radios: []types.Radio{{Frequency: 133000000, Modulation: types.ModulationAM}}}
		if validateMVPConfig(cfg) == nil {
			t.Fatal("gateway name inside pilot list must be rejected")
		}
		cfg.ClientName = "Athena"
		if err := validateMVPConfig(cfg); err != nil {
			t.Fatal(err)
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
