package simpleradio

import (
	"testing"
	"time"
)

func TestUnpromptedRouteReusesIdentityNotConsumedToken(t *testing.T) {
	c, last, w := mvpTestClient()
	if d := c.TransmitMVP(t.Context(), last, time.Now().Add(time.Second)); d.Outcome != "sent" {
		t.Fatal(d)
	}
	alert, ok := c.UnpromptedMVP(last, "unprompted-1")
	if !ok || alert.Radio != last.Radio || alert.route.used == last.route.used || len(alert.Audio) != 0 {
		t.Fatal("bad minted route")
	}
	alert.Audio = make(Audio, frameSize)
	if d := c.TransmitMVP(t.Context(), alert, time.Now().Add(time.Second)); d.Outcome != "sent" {
		t.Fatal(d)
	}
	before := w.writes
	c.TransmitMVP(t.Context(), alert, time.Now().Add(time.Second))
	if w.writes != before {
		t.Fatal("replayed unprompted token")
	}
}

func TestUnpromptedRouteGuards(t *testing.T) {
	for _, kind := range []string{"no_reception", "epoch", "channel", "identity", "mute", "live_off", "empty_id"} {
		t.Run(kind, func(t *testing.T) {
			c, last, w := mvpTestClient()
			id := "unprompted"
			switch kind {
			case "no_reception":
				last = Transmission{}
			case "epoch":
				c.invalidateMVP()
			case "channel":
				last.Radio.Frequency++
			case "identity":
				delete(c.clients, last.route.origin)
			case "mute":
				c.mute = true
			case "live_off":
				c.liveTX = false
			case "empty_id":
				id = ""
			}
			if _, ok := c.UnpromptedMVP(last, id); ok {
				t.Fatal("invalid route minted")
			}
			if w.writes != 0 {
				t.Fatal("guard wrote packets")
			}
		})
	}
}
