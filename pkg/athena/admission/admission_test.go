package admission

import (
	"errors"
	"testing"
	"time"
)

// testConfig is the Athena MVP shape with a concrete frequency and pilot.
func testConfig() Config {
	c := DefaultConfig()
	c.FrequencyHz = 30_000_000 // 30.0 MHz FM
	c.PilotName = "Prometheus"
	return c
}

func newTestGate(t *testing.T) *Gate {
	t.Helper()
	g, err := NewGate(testConfig())
	if err != nil {
		t.Fatalf("NewGate: %v", err)
	}
	return g
}

// validCandidate is an utterance that should be admitted. Individual tests
// mutate one field to prove that field is what causes rejection.
func validCandidate() Candidate {
	return Candidate{
		SpeakerName: "Prometheus",
		FrequencyHz: 30_000_000,
		Modulation:  ModulationFM,
		SampleCount: 16000 * 3,
		Duration:    3 * time.Second,
	}
}

func TestAdmitsConfiguredPilotOnCommandChannel(t *testing.T) {
	t.Parallel()

	g := newTestGate(t)
	got := g.Admit(validCandidate())
	if !got.Admitted {
		t.Fatalf("expected admission, got %s", got)
	}
	if got.Rejection != RejectNone {
		t.Errorf("admitted decision should carry no rejection, got %q", got.Rejection)
	}
}

// The core safety property: everything that is not the exact configured
// channel + exact configured speaker must be refused.
func TestRejectsEverythingElse(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*Candidate)
		want   Rejection
	}{
		{
			name:   "different frequency",
			mutate: func(c *Candidate) { c.FrequencyHz = 251_000_000 },
			want:   RejectFrequency,
		},
		{
			name:   "adjacent frequency outside tolerance",
			mutate: func(c *Candidate) { c.FrequencyHz = 30_000_101 },
			want:   RejectFrequency,
		},
		{
			name:   "right frequency wrong modulation",
			mutate: func(c *Candidate) { c.Modulation = ModulationAM },
			want:   RejectModulation,
		},
		{
			name:   "unresolved speaker",
			mutate: func(c *Candidate) { c.SpeakerName = "" },
			want:   RejectUnresolvedSpeaker,
		},
		{
			name:   "whitespace-only speaker",
			mutate: func(c *Candidate) { c.SpeakerName = "   " },
			want:   RejectUnresolvedSpeaker,
		},
		{
			name:   "different pilot",
			mutate: func(c *Candidate) { c.SpeakerName = "Viper 1-1" },
			want:   RejectSpeakerNotAuthorized,
		},
		{
			name:   "case-mismatched pilot name",
			mutate: func(c *Candidate) { c.SpeakerName = "prometheus" },
			want:   RejectSpeakerNotAuthorized,
		},
		{
			name:   "pilot name substring",
			mutate: func(c *Candidate) { c.SpeakerName = "Prometheus 2" },
			want:   RejectSpeakerNotAuthorized,
		},
		{
			name:   "empty utterance",
			mutate: func(c *Candidate) { c.SampleCount = 0 },
			want:   RejectEmpty,
		},
		{
			name: "below minimum duration",
			mutate: func(c *Candidate) {
				c.Duration = 500 * time.Millisecond
				c.SampleCount = 8000
			},
			want: RejectTooShort,
		},
		{
			name: "exceeds duration cap",
			mutate: func(c *Candidate) {
				c.Duration = 31 * time.Second
				c.SampleCount = 16000 * 20
			},
			want: RejectTooLong,
		},
		{
			name:   "exceeds sample cap",
			mutate: func(c *Candidate) { c.SampleCount = 16000*30 + 1 },
			want:   RejectTooLarge,
		},
	}

	g := newTestGate(t)
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			c := validCandidate()
			tc.mutate(&c)
			got := g.Admit(c)
			if got.Admitted {
				t.Fatalf("expected rejection %q, but transmission was admitted", tc.want)
			}
			if got.Rejection != tc.want {
				t.Errorf("rejection = %q, want %q", got.Rejection, tc.want)
			}
		})
	}
}

// Channel checks must run before identity checks so that traffic on other
// frequencies is discarded without consulting the speaker at all.
func TestChannelCheckedBeforeIdentity(t *testing.T) {
	t.Parallel()

	g := newTestGate(t)
	c := validCandidate()
	c.FrequencyHz = 251_000_000
	c.SpeakerName = "" // would also fail identity

	got := g.Admit(c)
	if got.Rejection != RejectFrequency {
		t.Errorf("expected frequency rejection to take precedence, got %q", got.Rejection)
	}
}

// A reconnect reassigns SRS GUIDs. Because identity is passed per call rather
// than cached, a resolved name admits and an unresolvable one fails closed.
func TestIdentityIsResolvedPerCallNotCached(t *testing.T) {
	t.Parallel()

	g := newTestGate(t)

	before := g.Admit(validCandidate())
	if !before.Admitted {
		t.Fatalf("pre-reconnect: expected admission, got %s", before)
	}

	// Immediately after reconnect the client map has not repopulated, so the
	// origin GUID resolves to nothing.
	during := validCandidate()
	during.SpeakerName = ""
	if got := g.Admit(during); got.Admitted {
		t.Error("during reconnect: unresolved speaker must fail closed")
	} else if got.Rejection != RejectUnresolvedSpeaker {
		t.Errorf("during reconnect: rejection = %q, want %q", got.Rejection, RejectUnresolvedSpeaker)
	}

	// Once the map repopulates, the same pilot is admitted again without any
	// gate mutation.
	after := g.Admit(validCandidate())
	if !after.Admitted {
		t.Errorf("post-reconnect: expected admission, got %s", after)
	}
}

func TestFrequencyTolerance(t *testing.T) {
	t.Parallel()

	g := newTestGate(t)
	tests := []struct {
		name string
		hz   uint64
		want bool
	}{
		{"exact", 30_000_000, true},
		{"just below within tolerance", 29_999_950, true},
		{"just above within tolerance", 30_000_050, true},
		{"at tolerance boundary below", 29_999_900, true},
		{"at tolerance boundary above", 30_000_100, true},
		{"outside tolerance below", 29_999_899, false},
		{"outside tolerance above", 30_000_101, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			c := validCandidate()
			c.FrequencyHz = tc.hz
			if got := g.Admit(c).Admitted; got != tc.want {
				t.Errorf("admitted = %v, want %v for %d Hz", got, tc.want, tc.hz)
			}
		})
	}
}

// Guards against unsigned underflow when the received frequency is below the
// configured one.
func TestFrequencyBelowConfiguredDoesNotUnderflow(t *testing.T) {
	t.Parallel()

	cfg := testConfig()
	cfg.FrequencyHz = 1000
	cfg.FrequencyToleranceHz = 10
	g, err := NewGate(cfg)
	if err != nil {
		t.Fatalf("NewGate: %v", err)
	}

	c := validCandidate()
	c.FrequencyHz = 1
	if got := g.Admit(c); got.Admitted {
		t.Error("a far-below frequency must not be admitted via underflow")
	}
}

func TestConfigValidation(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*Config)
		ok     bool
	}{
		{"valid", func(*Config) {}, true},
		{"missing frequency", func(c *Config) { c.FrequencyHz = 0 }, false},
		{"missing pilot", func(c *Config) { c.PilotName = "" }, false},
		{"whitespace pilot", func(c *Config) { c.PilotName = "  " }, false},
		{"invalid modulation", func(c *Config) { c.Modulation = Modulation(9) }, false},
		{"zero minimum", func(c *Config) { c.MinUtterance = 0 }, false},
		{"max below min", func(c *Config) { c.MaxUtterance = c.MinUtterance }, false},
		{"zero sample cap", func(c *Config) { c.MaxSamples = 0 }, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := testConfig()
			tc.mutate(&cfg)
			err := cfg.Validate()
			if tc.ok && err != nil {
				t.Fatalf("expected valid config, got %v", err)
			}
			if !tc.ok {
				if err == nil {
					t.Fatal("expected validation error, got nil")
				}
				if !errors.Is(err, ErrInvalidConfig) {
					t.Errorf("error should wrap ErrInvalidConfig, got %v", err)
				}
				if _, gerr := NewGate(cfg); gerr == nil {
					t.Error("NewGate must reject an invalid config")
				}
			}
		})
	}
}

// The MVP default must be FM, and no frequency or pilot may be assumed.
func TestDefaultConfigIsFMAndRequiresOperatorInput(t *testing.T) {
	t.Parallel()

	d := DefaultConfig()
	if d.Modulation != ModulationFM {
		t.Errorf("default modulation = %v, want FM", d.Modulation)
	}
	if d.FrequencyHz != 0 {
		t.Error("default config must not assume a command frequency")
	}
	if d.PilotName != "" {
		t.Error("default config must not assume a pilot name")
	}
	if err := d.Validate(); err == nil {
		t.Error("bare default config must fail validation")
	}
}

// Decision strings are logged, so they must never carry content.
func TestDecisionStringIsContentFree(t *testing.T) {
	t.Parallel()

	if got := (Decision{Admitted: true}).String(); got != "admitted" {
		t.Errorf("admitted string = %q", got)
	}
	got := reject(RejectSpeakerNotAuthorized).String()
	if got != "rejected:speaker_not_authorized" {
		t.Errorf("rejected string = %q", got)
	}
}

func TestModulationString(t *testing.T) {
	t.Parallel()

	if ModulationAM.String() != "AM" {
		t.Errorf("AM string = %q", ModulationAM.String())
	}
	if ModulationFM.String() != "FM" {
		t.Errorf("FM string = %q", ModulationFM.String())
	}
	if got := Modulation(7).String(); got != "unknown(7)" {
		t.Errorf("unknown string = %q", got)
	}
}

// The gate is consulted from the SRS receive path; concurrent use must be safe.
func TestGateIsConcurrencySafe(t *testing.T) {
	t.Parallel()

	g := newTestGate(t)
	done := make(chan bool, 64)
	for i := range 64 {
		go func(i int) {
			c := validCandidate()
			if i%2 == 0 {
				c.SpeakerName = "Intruder"
			}
			done <- g.Admit(c).Admitted
		}(i)
	}
	admitted := 0
	for range 64 {
		if <-done {
			admitted++
		}
	}
	if admitted != 32 {
		t.Errorf("admitted %d of 64, want exactly 32", admitted)
	}
}
