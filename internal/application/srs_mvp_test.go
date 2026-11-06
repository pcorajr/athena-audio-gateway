package application

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/dharmab/skyeye/internal/conf"
	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/martinlindhe/unit"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
)

func validSRSMVPConfig() conf.Configuration {
	mod := admission.ModulationAM
	return conf.Configuration{
		AthenaSRSMVP: true, AthenaResponseMaxAge: 10 * time.Second,
		AthenaCommandFrequencyHz: 133000000, AthenaCommandModulation: &mod,
		AthenaPilotName: "Prometheus", AthenaHermesEndpoint: "http://127.0.0.1:1234/radio/v1/exchange",
		SRSFrequencies: []simpleradio.RadioFrequency{{Frequency: 133000000 * unit.Hertz, Modulation: 0}},
		Recognizer:     conf.WhisperLocal,
	}
}

func TestSRSMVPInvalidStartupBeforeNetwork(t *testing.T) {
	for name, mutate := range map[string]func(*conf.Configuration){
		"missing frequency":  func(c *conf.Configuration) { c.AthenaCommandFrequencyHz = 0 },
		"missing modulation": func(c *conf.Configuration) { c.AthenaCommandModulation = nil },
		"missing pilot":      func(c *conf.Configuration) { c.AthenaPilotName = "" },
		"missing bridge":     func(c *conf.Configuration) { c.AthenaHermesEndpoint = "" },
		"remote bridge":      func(c *conf.Configuration) { c.AthenaHermesEndpoint = "http://192.0.2.1:1234/radio/v1/exchange" },
		"missing radio":      func(c *conf.Configuration) { c.SRSFrequencies = nil },
		"multiple radios":    func(c *conf.Configuration) { c.SRSFrequencies = append(c.SRSFrequencies, c.SRSFrequencies[0]) },
		"wrong radio":        func(c *conf.Configuration) { c.SRSFrequencies[0].Frequency++ },
		"wrong modulation":   func(c *conf.Configuration) { c.SRSFrequencies[0].Modulation = 1 },
		"expiry zero":        func(c *conf.Configuration) { c.AthenaResponseMaxAge = 0 },
		"expiry negative":    func(c *conf.Configuration) { c.AthenaResponseMaxAge = -time.Second },
		"speech limit":       func(c *conf.Configuration) { c.AthenaHermesMaxSpeech = 351 },
		"timeout negative":   func(c *conf.Configuration) { c.AthenaHermesTimeout = -time.Second },
		"diagnostic WAV":     func(c *conf.Configuration) { c.AthenaDebugAudioDir = "not-created" },
		"persona":            func(c *conf.Configuration) { c.AthenaPersonas = []string{"hermes"} },
		"cloud STT":          func(c *conf.Configuration) { c.Recognizer = conf.WhisperAPI },
		"gRPC":               func(c *conf.Configuration) { c.EnableGRPC = true },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := validSRSMVPConfig()
			mutate(&cfg)
			// The address is intentionally absent; invalid configuration must
			// be rejected without trying to dial, load Whisper or start Piper.
			if cfg.ValidateSRSMVP() == nil {
				t.Fatal("configuration accepted")
			}
			if app, err := NewApplication(cfg); err == nil || app != nil {
				t.Fatal("startup accepted invalid MVP config")
			}
		})
	}
}

type mvpRecognizerFake struct{ logging bool }

func (r *mvpRecognizerFake) Recognize(_ context.Context, _ []float32, boolFlag bool) (string, error) {
	r.logging = boolFlag
	return "synthetic transcript sentinel", nil
}

type mvpSpeakerFake struct{ speech string }

func (s *mvpSpeakerFake) Say(speech string) ([]float32, error) {
	return s.SayContext(context.Background(), speech)
}
func (s *mvpSpeakerFake) SayContext(_ context.Context, speech string) ([]float32, error) {
	s.speech = speech
	return []float32{0.5}, nil
}

type mvpRadioFake struct{ audio simpleradio.Audio }

func (*mvpRadioFake) ValidMVP(simpleradio.Transmission) bool { return true }
func (r *mvpRadioFake) TransmitMVP(_ context.Context, t simpleradio.Transmission, _ time.Time) simpleradio.Delivery {
	r.audio = t.Audio
	return simpleradio.Delivery{Outcome: "interlocked"}
}

func TestSRSMVPApplicationUsesLocalBackendsAndLoggingChoice(t *testing.T) {
	for _, logging := range []bool{false, true} {
		recognizer, speaker, radio := &mvpRecognizerFake{}, &mvpSpeakerFake{}, &mvpRadioFake{}
		cfg := admission.DefaultConfig()
		cfg.FrequencyHz = 133000000
		cfg.Modulation = admission.ModulationAM
		cfg.PilotName = "Prometheus"
		gate, err := admission.NewGate(cfg)
		if err != nil {
			t.Fatal(err)
		}
		app := &Application{athenaSRSMVP: true, athenaResponseMaxAge: 10 * time.Second, admissionGate: gate, recognizer: recognizer, speaker: speaker, volume: 0.5, enableTranscriptionLogging: logging,
			hermesBridge: &fakeBridge{resp: bridge.Response{TransmissionID: "synthetic-app", Deliverable: true, Speech: "Synthetic unavailable answer.", IntentClass: bridge.IntentFactual, State: bridge.StateUnavailable}},
		}
		var output bytes.Buffer
		original := log.Logger
		log.Logger = zerolog.New(&output)
		runner := app.srsMVPRunner()
		runner.Radio = radio
		runner.Record = func(mvp.Timing) {}
		runner.Process(t.Context(), simpleradio.Transmission{TraceID: "synthetic-app", ClientName: "Prometheus", Radio: types.Radio{Frequency: 133000000}, Audio: make(simpleradio.Audio, 32000), ReceivedAt: time.Now()})
		log.Logger = original
		if recognizer.logging != logging || strings.Contains(output.String(), "synthetic transcript sentinel") != logging {
			t.Fatal("transcript logging choice ignored")
		}
		if speaker.speech != "Synthetic unavailable answer." || len(radio.audio) != 1 || radio.audio[0] != 0.25 {
			t.Fatal("local synthesis/volume path not used")
		}
	}
}

func TestSRSMVPConfigAllowsTranscriptLoggingAndMute(t *testing.T) {
	cfg := validSRSMVPConfig()
	for _, enabled := range []bool{false, true} {
		cfg.EnableTranscriptionLogging = enabled
		cfg.Mute = true
		if err := cfg.ValidateSRSMVP(); err != nil {
			t.Fatal(err)
		}
	}
	if cfg.AthenaLiveTX {
		t.Fatal("live TX must default off")
	}
	cfg.AthenaSRSMVP = false
	cfg.AthenaHermesEndpoint = ""
	if err := cfg.ValidateSRSMVP(); err != nil {
		t.Fatal("legacy config changed")
	}
}
