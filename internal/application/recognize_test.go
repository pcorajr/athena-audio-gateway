package application

import (
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	srs "github.com/dharmab/skyeye/pkg/simpleradio/types"
)

const (
	testCommandFrequency = 30_000_000
	testPilot            = "Prometheus"
	// wideband is 16kHz, so three seconds of audio is 48000 samples.
	threeSecondsOfSamples = 48000
)

func gatedApplication(t *testing.T) *Application {
	t.Helper()
	cfg := admission.DefaultConfig()
	cfg.FrequencyHz = testCommandFrequency
	cfg.PilotName = testPilot
	gate, err := admission.NewGate(cfg)
	if err != nil {
		t.Fatalf("NewGate: %v", err)
	}
	return &Application{admissionGate: gate}
}

func commandTransmission() simpleradio.Transmission {
	return simpleradio.Transmission{
		TraceID:    "trace-1",
		ClientName: testPilot,
		Radio: srs.Radio{
			Frequency:  testCommandFrequency,
			Modulation: srs.ModulationFM,
		},
		Audio: make(simpleradio.Audio, threeSecondsOfSamples),
	}
}

func TestAdmitAllowsPilotOnCommandChannel(t *testing.T) {
	t.Parallel()

	app := gatedApplication(t)
	if !app.admit(commandTransmission()) {
		t.Error("expected the configured pilot on the command channel to be admitted")
	}
}

func TestAdmitBlocksBeforeRecognition(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*simpleradio.Transmission)
	}{
		{
			name:   "other frequency",
			mutate: func(tr *simpleradio.Transmission) { tr.Radio.Frequency = 251_000_000 },
		},
		{
			name:   "wrong modulation",
			mutate: func(tr *simpleradio.Transmission) { tr.Radio.Modulation = srs.ModulationAM },
		},
		{
			name:   "unresolved speaker",
			mutate: func(tr *simpleradio.Transmission) { tr.ClientName = "" },
		},
		{
			name:   "different pilot",
			mutate: func(tr *simpleradio.Transmission) { tr.ClientName = "Viper 1-1" },
		},
		{
			name:   "empty audio",
			mutate: func(tr *simpleradio.Transmission) { tr.Audio = simpleradio.Audio{} },
		},
		{
			name: "below minimum duration",
			mutate: func(tr *simpleradio.Transmission) {
				tr.Audio = make(simpleradio.Audio, 8000) // 0.5s
			},
		},
	}

	app := gatedApplication(t)
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tr := commandTransmission()
			tc.mutate(&tr)
			if app.admit(tr) {
				t.Error("transmission should have been rejected before recognition")
			}
		})
	}
}

// With no gate configured the fork must behave exactly as upstream SkyEye.
func TestAdmitPassesEverythingWhenGateDisabled(t *testing.T) {
	t.Parallel()

	app := &Application{}
	tr := commandTransmission()
	tr.ClientName = "Anyone At All"
	tr.Radio.Frequency = 251_000_000
	if !app.admit(tr) {
		t.Error("with no gate configured every transmission must pass through")
	}
}

// The gate reads frequency and modulation from the transmission itself, so a
// transmission that arrived on a non-command receiver is rejected even though
// the speaker is authorized.
func TestAdmitUsesObservedRadioNotSpeakerTrust(t *testing.T) {
	t.Parallel()

	app := gatedApplication(t)
	tr := commandTransmission()
	tr.Radio.Frequency = 251_000_000
	if app.admit(tr) {
		t.Error("an authorized pilot on a non-command frequency must still be rejected")
	}
}

func TestSampleDuration(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		samples int
		want    time.Duration
	}{
		{"zero", 0, 0},
		{"negative", -1, 0},
		{"one second", 16000, time.Second},
		{"three seconds", 48000, 3 * time.Second},
		{"half second", 8000, 500 * time.Millisecond},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := sampleDuration(tc.samples)
			// Allow a millisecond of float conversion slack.
			delta := got - tc.want
			if delta < 0 {
				delta = -delta
			}
			if delta > time.Millisecond {
				t.Errorf("sampleDuration(%d) = %v, want %v", tc.samples, got, tc.want)
			}
		})
	}
}
