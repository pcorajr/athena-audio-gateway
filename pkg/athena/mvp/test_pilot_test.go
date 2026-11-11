package mvp

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// Re-review minor: while the adapter says the TEST stand-in is the pilot
// (frame field test_pilot), the gateway's own "say again" carries the same
// "Test pilot." marker. Never seen, false, or stale: no marker.
const (
	plainSayAgain  = "Prometheus, Athena, say again."
	markedSayAgain = "Prometheus, Athena, Test pilot. say again."
)

func pilotFrame(test bool) streamFunc {
	return func(_ context.Context, req bridge.Request, emit func(bridge.Response) error) error {
		return emit(bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Prometheus, Athena, copy.", IntentClass: bridge.IntentFactual, State: bridge.StateOK, Route: "fixed", TestPilot: test})
	}
}

func pilotSays(speech string) streamFunc {
	return func(_ context.Context, req bridge.Request, emit func(bridge.Response) error) error {
		return emit(bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: speech, IntentClass: bridge.IntentFactual, State: bridge.StateOK, Route: "llm", Model: "m", TestPilot: true})
	}
}

func failing(_ context.Context, _ bridge.Request, _ func(bridge.Response) error) error {
	return errors.New("adapter timeout")
}

var calls int

func lastSpoken(t *testing.T, r *Runner, tr simpleradio.Transmission, spoken *[]string, bridgeNow streamFunc) string {
	t.Helper()
	*spoken = nil
	calls++
	tr.TraceID = fmt.Sprintf("tp-%d", calls)
	r.Bridge = bridgeNow
	r.Process(t.Context(), tr)
	if len(*spoken) == 0 {
		t.Fatal("nothing spoken")
	}
	return (*spoken)[len(*spoken)-1]
}

func TestSayAgainCarriesTheTestPilotMarkerOnlyWhileTheAdapterSaysSo(t *testing.T) {
	r, tr, _, _, spoken := streamFixture(t)
	if got := lastSpoken(t, r, tr, spoken, failing); got != plainSayAgain {
		t.Fatalf("never seen a frame: %q", got)
	}
	lastSpoken(t, r, tr, spoken, pilotFrame(true))
	if got := lastSpoken(t, r, tr, spoken, failing); got != markedSayAgain {
		t.Fatalf("stand-in frame seen: %q", got)
	}
	lastSpoken(t, r, tr, spoken, pilotFrame(false)) // a real pilot is back
	if got := lastSpoken(t, r, tr, spoken, failing); got != plainSayAgain {
		t.Fatalf("real pilot frame seen: %q", got)
	}
}

func TestStaleTestPilotFrameGivesNoMarker(t *testing.T) {
	r, tr, _, _, spoken := streamFixture(t)
	lastSpoken(t, r, tr, spoken, pilotFrame(true))
	r.testPilotAt.Store(time.Now().Add(-testPilotFresh - time.Second).UnixNano())
	if got := lastSpoken(t, r, tr, spoken, failing); got != plainSayAgain {
		t.Fatalf("stale: %q", got)
	}
}

func TestTooLongFallbackAndUndeliverableFrameAreMarkedToo(t *testing.T) {
	r, tr, _, _, spoken := streamFixture(t)
	lastSpoken(t, r, tr, spoken, pilotFrame(true))
	r.Synthesize = func(_ context.Context, s string) (simpleradio.Audio, error) {
		*spoken = append(*spoken, s)
		if s == "Prometheus, Athena, Test pilot. say again, my answer was too long." {
			return make(simpleradio.Audio, 16000), nil
		}
		return make(simpleradio.Audio, 40*16000), nil
	}
	if got := lastSpoken(t, r, tr, spoken, pilotSays("Prometheus, a very long reply.")); got != "Prometheus, Athena, Test pilot. say again, my answer was too long." {
		t.Fatalf("too long: %q", got)
	}
}

func TestBridgeAcceptsTestPilotOnlyOnADeliverableFrame(t *testing.T) {
	ok := bridge.Response{TransmissionID: "x", Deliverable: true, Speech: "Prometheus, Athena, copy.", IntentClass: bridge.IntentFactual, State: bridge.StateOK, TestPilot: true}
	if err := ok.ValidateMVP("x", 350); err != nil {
		t.Fatal(err)
	}
	bad := bridge.Suppressed("x", bridge.IntentFactual, bridge.StateUnavailable, "unaddressed")
	bad.TestPilot = true
	if bad.ValidateMVP("x", 350) == nil {
		t.Fatal("a suppressed frame says nothing about the pilot")
	}
}
