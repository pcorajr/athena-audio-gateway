package admission

import (
	"errors"
	"testing"
	"time"
)

// Multi-frequency (2026-11-07): 133.0 AM, 251.0 AM, 33.0 FM.
func multiFreqGate(t *testing.T) *Gate {
	t.Helper()
	cfg := DefaultConfig()
	cfg.PilotName = "Prometheus,Player"
	cfg.FrequencyToleranceHz = 0
	cfg.Channels = []Channel{
		{FrequencyHz: 133_000_000, Modulation: ModulationAM},
		{FrequencyHz: 251_000_000, Modulation: ModulationAM},
		{FrequencyHz: 33_000_000, Modulation: ModulationFM},
	}
	cfg.FrequencyHz, cfg.Modulation = cfg.Channels[0].FrequencyHz, cfg.Channels[0].Modulation
	g, err := NewGate(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func TestMultiChannelAdmissionPerChannel(t *testing.T) {
	g := multiFreqGate(t)
	for _, tc := range []struct {
		hz   uint64
		mod  Modulation
		want Rejection
	}{
		{133_000_000, ModulationAM, RejectNone},
		{251_000_000, ModulationAM, RejectNone},
		{33_000_000, ModulationFM, RejectNone},
		{133_000_000, ModulationFM, RejectModulation},
		{251_000_000, ModulationFM, RejectModulation},
		{33_000_000, ModulationAM, RejectModulation},
		{40_000_000, ModulationFM, RejectFrequency},
		{243_000_000, ModulationAM, RejectFrequency},
		{251_000_001, ModulationAM, RejectFrequency},
	} {
		d := g.Admit(Candidate{SpeakerName: "Player", FrequencyHz: tc.hz, Modulation: tc.mod, SampleCount: 32000, Duration: 2 * time.Second})
		if d.Rejection != tc.want || d.Admitted != (tc.want == RejectNone) {
			t.Errorf("%d %s: got %s want %q", tc.hz, tc.mod, d, tc.want)
		}
		if g.Matches(tc.hz, tc.mod) != (tc.want == RejectNone) {
			t.Errorf("%d %s: Matches disagrees with Admit", tc.hz, tc.mod)
		}
	}
	// Identity is still enforced on every channel.
	if d := g.Admit(Candidate{SpeakerName: "Other", FrequencyHz: 251_000_000, Modulation: ModulationAM, SampleCount: 32000, Duration: 2 * time.Second}); d.Rejection != RejectSpeakerNotAuthorized {
		t.Fatalf("other speaker on 251: %s", d)
	}
}

func TestMultiChannelConfigValidation(t *testing.T) {
	base := DefaultConfig()
	base.PilotName = "Prometheus"
	for name, channels := range map[string][]Channel{
		"zero frequency": {{FrequencyHz: 133_000_000}, {FrequencyHz: 0, Modulation: ModulationAM}},
		"bad modulation": {{FrequencyHz: 133_000_000}, {FrequencyHz: 251_000_000, Modulation: 2}},
		"duplicate":      {{FrequencyHz: 133_000_000}, {FrequencyHz: 133_000_000}},
	} {
		cfg := base
		cfg.Channels = channels
		if err := cfg.Validate(); !errors.Is(err, ErrInvalidConfig) {
			t.Errorf("%s accepted: %v", name, err)
		}
	}
	cfg := base
	cfg.Channels = []Channel{{FrequencyHz: 133_000_000, Modulation: ModulationAM}, {FrequencyHz: 133_000_000, Modulation: ModulationFM}}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("same frequency, different modulation rejected: %v", err)
	}
	if got := (Channel{FrequencyHz: 33_000_000, Modulation: ModulationFM}).String(); got != "33.000 FM" {
		t.Fatalf("channel string %q", got)
	}
}
