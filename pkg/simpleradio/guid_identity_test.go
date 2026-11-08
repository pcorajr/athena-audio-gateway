package simpleradio

import (
	"sync/atomic"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

// Round 2 (2026-11-07 play session): single-player SRS flips the client name
// "player" <-> "Prometheus" on the same connection (same GUID). That flip
// bumped the epoch and dropped ~11 replies in 30 minutes
// (radio_epoch_mismatch). Identity is now keyed on the SRS GUID.
func guidTestClient(t *testing.T) (*Client, Transmission, *mvpWriterFake, types.ClientInfo) {
	t.Helper()
	c, tr, w := mvpTestClient()
	c.pilotName = "Prometheus,Player,Client"
	peer := c.clients[tr.route.origin]
	c.syncMVPClient(peer) // establishes the sticky pilot GUID
	tr.route.epoch = c.mvpEpoch
	return c, tr, w, peer
}

func TestGUIDKeyedIdentityNameFlip(t *testing.T) {
	c, tr, _, peer := guidTestClient(t)
	epoch := c.mvpEpoch
	for i, name := range []string{"player", "Prometheus", "PLAYER", "Client", "Prometheus"} {
		// Live: the in-game name ("player") comes with the DCS unit; the SRS
		// standalone name comes without it. Both flip together.
		peer.Name = name
		if i%2 == 0 {
			peer.RadioInfo.UnitID, peer.RadioInfo.Unit = 16778752, "A-10C_2"
		} else {
			peer.RadioInfo.UnitID, peer.RadioInfo.Unit = 0, ""
		}
		if i%2 == 0 {
			c.syncMVPClient(peer)
		} else {
			c.syncMVPClients([]types.ClientInfo{peer})
		}
		if c.mvpEpoch != epoch {
			t.Fatalf("name flip to %q bumped epoch %d -> %d", name, epoch, c.mvpEpoch)
		}
		if reason := c.MVPRejection(tr); reason != "" {
			t.Fatalf("name flip to %q rejected reply: %s", name, reason)
		}
	}
}

func TestGUIDKeyedIdentityUnitSeatChangeKeepsReply(t *testing.T) {
	c, tr, _, peer := guidTestClient(t)
	peer.RadioInfo.UnitID++
	peer.RadioInfo.Unit = "A-10C_2"
	peer.Seat++
	c.syncMVPClient(peer)
	if reason := c.MVPRejection(tr); reason != "" {
		t.Fatalf("unit/seat change on same GUID rejected reply: %s", reason)
	}
}

func TestGUIDKeyedIdentityStickyAgainstBetterRank(t *testing.T) {
	c, tr, _, peer := guidTestClient(t)
	peer.Name = "player"
	c.syncMVPClient(peer)
	other := peer
	other.GUID, other.Name = "AAAAother0000000000000", "Prometheus"
	c.syncMVPClient(other)
	if reason := c.MVPRejection(tr); reason != "" {
		t.Fatalf("a second, better-ranked name stole the identity: %s", reason)
	}
	// The pilot leaves: the other listed client becomes the pilot (new epoch).
	c.removeClient(peer)
	if c.ValidMVP(tr) {
		t.Fatal("reply for a disconnected pilot GUID still valid")
	}
}

func TestGUIDKeyedIdentityNameOutsideListInvalidates(t *testing.T) {
	c, tr, _, peer := guidTestClient(t)
	peer.Name = "someone-else"
	c.syncMVPClient(peer)
	if c.ValidMVP(tr) {
		t.Fatal("name outside the pilot list must invalidate")
	}
}

// Flip between receive and send: packets admitted before the flip, reply sent
// after it.
func TestGUIDKeyedIdentityFlipBetweenReceiveAndSend(t *testing.T) {
	c, tr, w, peer := guidTestClient(t)
	epoch := c.mvpEpoch
	radio := c.clientInfo.RadioInfo.Radios[0]
	freq := []voice.Frequency{{Frequency: radio.Frequency, Modulation: byte(radio.Modulation)}}
	p := voice.NewPacket([]byte{1}, freq, 123, 7, 0, []byte(peer.GUID), []byte(peer.GUID))
	route, ok := c.admitMVPPacket(&p, epoch)
	if !ok {
		t.Fatalf("packet rejected before flip: %s", route.rejection)
	}
	peer.Name, peer.RadioInfo.Unit = "player", "A-10C_2"
	c.syncMVPClient(peer)
	// A packet that was stamped with the pre-flip epoch is still admitted.
	p2 := voice.NewPacket([]byte{1}, freq, 123, 8, 0, []byte(peer.GUID), []byte(peer.GUID))
	if r2, ok := c.admitMVPPacket(&p2, epoch); !ok {
		t.Fatalf("packet after flip rejected: %s", r2.rejection)
	}
	c.mvpChannelUntil = time.Time{}
	tr.route = route
	d := c.TransmitMVP(t.Context(), tr, time.Now().Add(2*time.Second))
	if d.Outcome != "sent" || w.writes == 0 {
		t.Fatalf("reply after name flip not sent: %+v", d)
	}
}

// Flip mid-reply: the name flips after the first packet is written; the rest
// of the reply must still go out.
func TestGUIDKeyedIdentityFlipMidReply(t *testing.T) {
	c, tr, w, peer := guidTestClient(t)
	tr.Audio = make(Audio, frameSize*10)
	tr.route.used = &atomic.Bool{}
	notify := &notifyingWriter{mvpWriterFake: w, first: make(chan struct{})}
	c.mvpWriter = notify
	done := make(chan Delivery, 1)
	go func() { done <- c.TransmitMVP(t.Context(), tr, time.Now().Add(2*time.Second)) }()
	<-notify.first
	peer.Name, peer.RadioInfo.UnitID = "player", 16778752
	c.syncMVPClient(peer)
	peer.Name, peer.RadioInfo.UnitID = "Prometheus", 0
	c.syncMVPClients([]types.ClientInfo{peer})
	select {
	case d := <-done:
		if d.Outcome != "sent" || w.writes != 10 {
			t.Fatalf("reply cut off by name flip: %+v writes=%d", d, w.writes)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("reply did not finish")
	}
}

// A real reconnect (new TCP session) still invalidates everything.
func TestGUIDKeyedIdentityReconnectStillInvalidates(t *testing.T) {
	c, tr, w, peer := guidTestClient(t)
	c.invalidateMVP()
	c.syncMVPClients([]types.ClientInfo{peer})
	c.mvpWriter = w
	if c.ValidMVP(tr) {
		t.Fatal("reconnect must invalidate old routes")
	}
}
