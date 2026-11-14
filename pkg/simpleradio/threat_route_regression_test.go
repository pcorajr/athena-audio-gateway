package simpleradio

import (
	"context"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/simpleradio/types"
)

// M3: an admitted prompted route is not authority for new unsolicited speech
// after the pilot leaves the channel, including after minting or during audio.
func TestUnpromptedCurrentPilotRadio(t *testing.T) {
	for _, change := range []string{"retuned", "modulation", "encrypted"} {
		for _, phase := range []string{"mint", "before_send", "during_send", "prompted"} {
			t.Run(change+"/"+phase, func(t *testing.T) {
				c, last, w := mvpTestClient()
				peer := c.clients[last.route.origin]
				radio := last.Radio
				switch change {
				case "retuned":
					radio.Frequency = 251000000
				case "modulation":
					radio.Modulation = types.ModulationFM
				case "encrypted":
					radio.IsEncrypted = true
				}
				peer.RadioInfo.Radios = []types.Radio{radio}
				if change == "unknown" {
					peer.RadioInfo.Radios = nil
				}
				if phase == "mint" || phase == "prompted" {
					c.syncMVPClient(peer)
				}
				if phase == "prompted" {
					if d := c.TransmitMVP(t.Context(), last, time.Now().Add(time.Second)); d.Outcome != "sent" || w.writes != 2 {
						t.Fatalf("prompted metadata tolerance lost: %+v writes=%d", d, w.writes)
					}
					return
				}
				alert, ok := c.UnpromptedMVP(last, "unprompted-retune")
				if phase == "mint" {
					if ok || w.writes != 0 {
						t.Fatalf("stale/unknown pilot radio minted=%v writes=%d", ok, w.writes)
					}
					return
				}
				if !ok {
					t.Fatal("current channel did not mint")
				}
				alert.Audio = make(Audio, frameSize*10)
				if phase == "before_send" {
					c.syncMVPClient(peer)
					d := c.TransmitMVP(t.Context(), alert, time.Now().Add(time.Second))
					if d.Outcome == "sent" || w.writes != 0 {
						t.Fatalf("stale minted route sent: %+v writes=%d", d, w.writes)
					}
					return
				}
				ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
				defer cancel()
				notify := &notifyingWriter{mvpWriterFake: w, first: make(chan struct{})}
				c.mvpWriter = notify
				done := make(chan Delivery, 1)
				go func() { done <- c.TransmitMVP(ctx, alert, time.Now().Add(time.Second)) }()
				select {
				case <-notify.first:
				case <-ctx.Done():
					t.Fatal("first packet never sent")
				}
				c.syncMVPClient(peer)
				c.clientsLock.Lock()
				before := w.writes
				c.clientsLock.Unlock()
				select {
				case d := <-done:
					if d.Outcome != "invalidated" || w.writes != before {
						t.Fatalf("packets after retune: %+v before=%d after=%d", d, before, w.writes)
					}
				case <-ctx.Done():
					t.Fatal("retune did not stop send")
				}
			})
		}
	}
}

// Flight 2026-11-13 (r12.9): 27 of 28 unprompted calls were refused because live SRS
// updates without a radio list erased the pilot's list. A list-less update keeps the
// known list; a pilot whose list was never received is trusted on the channel he was
// heard on; small SRS frequency offsets are the same channel.
func TestUnpromptedSurvivesListlessPilotUpdates(t *testing.T) {
	t.Run("listless_update_keeps_known_list", func(t *testing.T) {
		c, last, w := mvpTestClient()
		peer := c.clients[last.route.origin]
		peer.RadioInfo.Radios = []types.Radio{last.Radio}
		c.syncMVPClient(peer)
		peer.RadioInfo.Radios = nil
		c.syncMVPClient(peer)
		c.syncMVPClients([]types.ClientInfo{peer})
		if got := c.clients[last.route.origin].RadioInfo.Radios; len(got) != 1 {
			t.Fatalf("known radio list erased: %v", got)
		}
		alert, ok := c.UnpromptedMVP(last, "unprompted-listless")
		if !ok {
			t.Fatalf("refused: %s", c.UnpromptedRejection(last))
		}
		alert.Audio = make(Audio, frameSize)
		if d := c.TransmitMVP(t.Context(), alert, time.Now().Add(time.Second)); d.Outcome != "sent" || w.writes != 1 {
			t.Fatalf("not sent: %+v writes=%d", d, w.writes)
		}
	})
	t.Run("retune_after_listless_update_still_refused", func(t *testing.T) {
		c, last, _ := mvpTestClient()
		peer := c.clients[last.route.origin]
		peer.RadioInfo.Radios = nil
		c.syncMVPClient(peer)
		peer.RadioInfo.Radios = []types.Radio{{Frequency: 251000000, Modulation: types.ModulationAM}}
		c.syncMVPClient(peer)
		if _, ok := c.UnpromptedMVP(last, "unprompted-retuned"); ok || c.UnpromptedRejection(last) != "radio_pilot_channel_mismatch" {
			t.Fatalf("retuned pilot minted; reason=%q", c.UnpromptedRejection(last))
		}
	})
	t.Run("never_received_list_is_trusted", func(t *testing.T) {
		c, last, _ := mvpTestClient()
		peer := c.clients[last.route.origin]
		peer.RadioInfo.Radios = nil
		c.clients[last.route.origin] = peer
		if _, ok := c.UnpromptedMVP(last, "unprompted-nolist"); !ok {
			t.Fatalf("refused: %s", c.UnpromptedRejection(last))
		}
	})
	t.Run("small_frequency_offset_is_same_channel", func(t *testing.T) {
		c, last, _ := mvpTestClient()
		peer := c.clients[last.route.origin]
		r := last.Radio
		r.Frequency += 300
		peer.RadioInfo.Radios = []types.Radio{r}
		c.syncMVPClient(peer)
		if _, ok := c.UnpromptedMVP(last, "unprompted-offset"); !ok {
			t.Fatalf("refused: %s", c.UnpromptedRejection(last))
		}
	})
}

func TestUnpromptedCurrentPilotSecondaryRadio(t *testing.T) {
	c, last, w := mvpTestClient()
	peer := c.clients[last.route.origin]
	peer.RadioInfo.Radios = []types.Radio{{Frequency: 251000000, Modulation: types.ModulationAM}, last.Radio}
	c.syncMVPClient(peer)
	alert, ok := c.UnpromptedMVP(last, "unprompted-secondary")
	if !ok {
		t.Fatal("pilot still has selected channel on second radio")
	}
	alert.Audio = make(Audio, frameSize)
	if d := c.TransmitMVP(t.Context(), alert, time.Now().Add(time.Second)); d.Outcome != "sent" || w.writes != 1 {
		t.Fatalf("current secondary radio rejected: %+v writes=%d", d, w.writes)
	}
}
