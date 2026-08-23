// Package admission implements the Athena command-channel admission gate.
//
// Governing decision: ADR 0014 (Project Athena) — Server-Side SRS Audio Gateway
// Boundary. Specification: docs/architecture/dcs-srs-audio-gateway-architecture.md §4.
//
// A transmission is admitted for speech recognition only when ALL of the
// following hold:
//
//  1. it arrived on the configured command frequency and modulation;
//  2. its origin resolves to the configured pilot name;
//  3. that name was resolved from LIVE SRS client metadata for the current
//     session.
//
// Anything failing admission is discarded BEFORE speech-to-text runs. No
// transcript is produced, no content is logged, and no audio is retained.
//
// The gate is deliberately free of SRS networking, Opus, and speech
// dependencies so it can be exercised deterministically.
package admission

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// Rejection enumerates why a transmission was not admitted. Values are stable
// and safe to log: none of them carry transcript or audio-derived content.
type Rejection string

const (
	// RejectNone indicates the transmission was admitted.
	RejectNone Rejection = ""
	// RejectFrequency means the transmission did not arrive on the configured
	// command frequency.
	RejectFrequency Rejection = "frequency_mismatch"
	// RejectModulation means the frequency matched but the modulation did not.
	RejectModulation Rejection = "modulation_mismatch"
	// RejectUnresolvedSpeaker means the origin GUID did not resolve to any
	// known SRS client. Identity could not be established, so the transmission
	// fails closed.
	RejectUnresolvedSpeaker Rejection = "speaker_unresolved"
	// RejectSpeakerNotAuthorized means the origin resolved to a client that is
	// not the configured pilot.
	RejectSpeakerNotAuthorized Rejection = "speaker_not_authorized"
	// RejectTooShort means the utterance was below the minimum duration.
	RejectTooShort Rejection = "utterance_too_short"
	// RejectTooLong means the utterance exceeded the hard duration cap.
	RejectTooLong Rejection = "utterance_too_long"
	// RejectTooLarge means the utterance exceeded the hard sample-count cap.
	RejectTooLarge Rejection = "utterance_too_large"
	// RejectEmpty means the utterance contained no audio samples.
	RejectEmpty Rejection = "utterance_empty"
)

// Modulation mirrors the SRS modulation values relevant to the command channel.
type Modulation int

const (
	// ModulationAM is amplitude modulation.
	ModulationAM Modulation = 0
	// ModulationFM is frequency modulation. This is the Athena MVP default.
	ModulationFM Modulation = 1
)

func (m Modulation) String() string {
	switch m {
	case ModulationAM:
		return "AM"
	case ModulationFM:
		return "FM"
	default:
		return fmt.Sprintf("unknown(%d)", int(m))
	}
}

// Config is the command-channel configuration. Frequency and modulation are
// configuration rather than constants because airframe radio ranges differ: the
// F-16 tunes 225.000-399.975 on COM1 and 108.000-151.975 on COM2, while the
// F-4E primary radio is limited to 225.0-399.95 AM.
type Config struct {
	// FrequencyHz is the single command frequency, in hertz.
	FrequencyHz uint64
	// Modulation is the command-channel modulation.
	Modulation Modulation
	// FrequencyToleranceHz absorbs the small rounding differences seen between
	// SRS clients reporting the same tuned frequency. Zero means exact match.
	FrequencyToleranceHz uint64
	// PilotName is the exact SRS client name permitted to issue commands.
	// Comparison is case-sensitive after trimming surrounding whitespace.
	PilotName string
	// MinUtterance rejects transmissions too short to carry a command. Values
	// below whisper.cpp's 1s floor cause recognizer errors.
	MinUtterance time.Duration
	// MaxUtterance is the hard duration cap. Exceeding it fails closed.
	MaxUtterance time.Duration
	// MaxSamples is the hard sample-count cap. Exceeding it fails closed.
	MaxSamples int
}

// DefaultConfig returns the Athena MVP defaults. FrequencyHz and PilotName have
// no safe default and must be supplied by the operator.
func DefaultConfig() Config {
	return Config{
		Modulation:           ModulationFM,
		FrequencyToleranceHz: 100,
		MinUtterance:         1 * time.Second,
		MaxUtterance:         30 * time.Second,
		MaxSamples:           16000 * 30,
	}
}

// ErrInvalidConfig is returned by Validate when the configuration cannot
// safely gate a command channel.
var ErrInvalidConfig = errors.New("invalid admission config")

// Validate reports whether the configuration is usable. An unconfigured
// frequency or pilot name is fatal: without both, the gate would admit
// everything or nothing in a way the operator did not intend.
func (c Config) Validate() error {
	if c.FrequencyHz == 0 {
		return fmt.Errorf("%w: command frequency must be set", ErrInvalidConfig)
	}
	if strings.TrimSpace(c.PilotName) == "" {
		return fmt.Errorf("%w: pilot name must be set", ErrInvalidConfig)
	}
	if c.Modulation != ModulationAM && c.Modulation != ModulationFM {
		return fmt.Errorf("%w: modulation must be AM or FM", ErrInvalidConfig)
	}
	if c.MinUtterance <= 0 {
		return fmt.Errorf("%w: minimum utterance must be positive", ErrInvalidConfig)
	}
	if c.MaxUtterance <= c.MinUtterance {
		return fmt.Errorf("%w: maximum utterance must exceed minimum", ErrInvalidConfig)
	}
	if c.MaxSamples <= 0 {
		return fmt.Errorf("%w: maximum samples must be positive", ErrInvalidConfig)
	}
	return nil
}

// Candidate is a received transmission being considered for admission. It
// carries no audio: only the sample count and duration needed to enforce the
// fail-closed limits.
type Candidate struct {
	// SpeakerName is the SRS client name resolved from live metadata for the
	// origin GUID. Empty means the origin could not be resolved.
	SpeakerName string
	// FrequencyHz is the frequency the transmission arrived on.
	FrequencyHz uint64
	// Modulation is the modulation the transmission arrived with.
	Modulation Modulation
	// SampleCount is the number of PCM samples in the assembled utterance.
	SampleCount int
	// Duration is the wall-clock length of the assembled utterance.
	Duration time.Duration
}

// Decision is the outcome of an admission check.
type Decision struct {
	// Admitted reports whether the transmission may proceed to speech
	// recognition.
	Admitted bool
	// Rejection explains why the transmission was refused. Empty when admitted.
	Rejection Rejection
}

// String renders a decision for logging. It never contains transcript or audio
// content.
func (d Decision) String() string {
	if d.Admitted {
		return "admitted"
	}
	return "rejected:" + string(d.Rejection)
}

// Gate decides which transmissions may reach speech recognition.
//
// A Gate is immutable and safe for concurrent use. Identity is supplied per
// call rather than cached, so a reconnect that reassigns SRS GUIDs is handled
// naturally: the caller resolves the name from the live client map each time,
// and a stale or unknown GUID resolves to empty and fails closed.
type Gate struct {
	cfg Config
}

// NewGate constructs a Gate. It returns an error if the configuration could not
// safely gate a command channel.
func NewGate(cfg Config) (*Gate, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &Gate{cfg: cfg}, nil
}

// Config returns the gate's configuration.
func (g *Gate) Config() Config { return g.cfg }

// Admit evaluates a candidate transmission.
//
// Checks run cheapest-first and, more importantly, in an order that avoids
// leaking information: channel checks precede identity checks, so traffic on
// other frequencies is discarded without ever consulting the speaker.
func (g *Gate) Admit(c Candidate) Decision {
	if !g.matchesFrequency(c.FrequencyHz) {
		return reject(RejectFrequency)
	}
	if c.Modulation != g.cfg.Modulation {
		return reject(RejectModulation)
	}

	name := strings.TrimSpace(c.SpeakerName)
	if name == "" {
		return reject(RejectUnresolvedSpeaker)
	}
	if name != strings.TrimSpace(g.cfg.PilotName) {
		return reject(RejectSpeakerNotAuthorized)
	}

	if c.SampleCount <= 0 {
		return reject(RejectEmpty)
	}
	if c.SampleCount > g.cfg.MaxSamples {
		return reject(RejectTooLarge)
	}
	if c.Duration < g.cfg.MinUtterance {
		return reject(RejectTooShort)
	}
	if c.Duration > g.cfg.MaxUtterance {
		return reject(RejectTooLong)
	}

	return Decision{Admitted: true, Rejection: RejectNone}
}

// matchesFrequency compares against the configured frequency within tolerance,
// using unsigned-safe subtraction.
func (g *Gate) matchesFrequency(hz uint64) bool {
	want := g.cfg.FrequencyHz
	if hz == want {
		return true
	}
	var delta uint64
	if hz > want {
		delta = hz - want
	} else {
		delta = want - hz
	}
	return delta <= g.cfg.FrequencyToleranceHz
}

func reject(r Rejection) Decision {
	return Decision{Admitted: false, Rejection: r}
}
