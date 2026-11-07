package application

import (
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
)

// Use the actual Runner factory: a missing application client is a typed nil
// interface, not an ordinary nil Radio. It must fail closed with a reason.
func TestPacket108ApplicationFactoryMissingRadio(t *testing.T) {
	t.Setenv("ATHENA_RADIO_TRANSCRIPT_DIR", t.TempDir())
	cfg := admission.DefaultConfig()
	cfg.FrequencyHz, cfg.Modulation, cfg.PilotName = 133000000, admission.ModulationAM, "Prometheus"
	gate, err := admission.NewGate(cfg)
	if err != nil {
		t.Fatal(err)
	}
	app := &Application{admissionGate: gate, athenaResponseMaxAge: 10 * time.Second,
		recognizer: &mvpRecognizerFake{}, speaker: &mvpSpeakerFake{},
		hermesBridge: &fakeBridge{resp: bridge.Response{}},
	}
	runner := app.srsMVPRunner()
	var got mvp.Timing
	runner.Record = func(timing mvp.Timing) { got = timing }
	runner.Process(t.Context(), simpleradio.Transmission{TraceID: "synthetic-missing-radio", ClientName: "Prometheus",
		Radio: types.Radio{Frequency: 133000000}, Audio: make(simpleradio.Audio, 32000), ReceivedAt: time.Now()})
	if got.Outcome != "rejected:radio_unavailable" {
		t.Fatalf("%+v", got)
	}
}
