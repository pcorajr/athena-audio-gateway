// Package bridge defines the text-only boundary between the Athena Audio
// Gateway and Hermes.
//
// Governing decision: ADR 0014 (Project Athena) — Server-Side SRS Audio Gateway
// Boundary. Specification: docs/architecture/dcs-srs-audio-gateway-architecture.md §5.
//
// The Gateway owns the entire audio path: SRS, Opus, admission, speech-to-text,
// speech synthesis, and transmission. Hermes is a consumer of language. It
// receives a transcript and returns response text. It never receives PCM, never
// opens an audio device, and never speaks directly.
//
// That boundary is enforced structurally: Request has no audio-carrying field,
// so PCM cannot cross it even by mistake.
package bridge

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
)

// Modulation is the radio modulation a transmission arrived with.
type Modulation string

const (
	// ModulationAM is amplitude modulation.
	ModulationAM Modulation = "AM"
	// ModulationFM is frequency modulation.
	ModulationFM Modulation = "FM"
)

// IntentClass is how Hermes classified the transcript.
type IntentClass string

const (
	// IntentFactual is a read-only question about mission state. These may be
	// answered on the command frequency.
	IntentFactual IntentClass = "factual"
	// IntentCue is a Mission Director cue delivery.
	IntentCue IntentClass = "cue"
	// IntentActionPreview is action language. It produces a non-executing
	// preview and is never dispatched to DCS or MOOSE.
	IntentActionPreview IntentClass = "action_preview"
	// IntentUnrecognized means Hermes could not classify the transcript.
	IntentUnrecognized IntentClass = "unrecognized"
)

// State carries Athena's freshness and confidence qualification through to the
// spoken response. A degraded or stale result must never be spoken as clean
// fact.
type State string

const (
	// StateOK means the backing facts were fresh and unambiguous.
	StateOK State = "ok"
	// StateStale means the backing facts exceeded their freshness budget.
	StateStale State = "stale"
	// StateDegraded means some sources were unavailable or partial.
	StateDegraded State = "degraded"
	// StateUnavailable means the required facts could not be obtained.
	StateUnavailable State = "unavailable"
	// StateOwnshipAmbiguous means the player's own aircraft could not be
	// resolved unambiguously.
	StateOwnshipAmbiguous State = "ownship_ambiguous"
)

// Request is what the Gateway sends to Hermes after a transmission has been
// admitted and transcribed.
//
// There is deliberately no audio field of any kind: no PCM, no file path, no
// Opus bytes, no audio hash. The absence is the contract.
type Request struct {
	// TransmissionID correlates every stage of one exchange.
	TransmissionID string `json:"transmission_id"`
	// Transcript is the speech-to-text output.
	Transcript string `json:"transcript"`
	// Speaker is the SRS client name resolved from live metadata.
	Speaker string `json:"speaker"`
	// Addressee is the persona the pilot addressed, e.g. "athena" for mission
	// questions or "hermes" for development and system work. Empty means the
	// transmission was not addressed to anyone and must not be routed.
	//
	// This is distinct from Speaker: Speaker is who talked, Addressee is who
	// was talked to. Admission screens the former; routing uses the latter.
	Addressee string `json:"addressee,omitempty"`
	// FrequencyHz is the command frequency the utterance arrived on.
	FrequencyHz uint64 `json:"frequency_hz"`
	// Modulation is the modulation the utterance arrived with.
	Modulation Modulation `json:"modulation"`
	// ReceivedAt is when the Gateway received the transmission.
	ReceivedAt time.Time `json:"received_at"`
	// TranscribedAt is when speech recognition completed.
	TranscribedAt time.Time `json:"transcribed_at"`
}

// Response is Hermes's answer. The Gateway synthesizes Speech verbatim; it does
// not reword, summarize, or expand. All judgment lives in Hermes.
type Response struct {
	// TransmissionID echoes the request.
	TransmissionID string `json:"transmission_id"`
	// Deliverable reports whether anything may be transmitted. False means
	// transmit nothing.
	Deliverable bool `json:"deliverable"`
	// Speech is the concise radio wording. Empty when not deliverable.
	Speech string `json:"speech"`
	// IntentClass is how Hermes classified the transcript.
	IntentClass IntentClass `json:"intent_class"`
	// State is the freshness/confidence qualification.
	State State `json:"state"`
	// SuppressionReason explains a non-deliverable response.
	SuppressionReason string `json:"suppression_reason,omitempty"`
}

// ErrInvalidRequest and ErrInvalidResponse wrap all envelope validation
// failures so callers can distinguish contract violations from transport
// errors.
var (
	ErrInvalidRequest  = errors.New("invalid bridge request")
	ErrInvalidResponse = errors.New("invalid bridge response")
)

// Validate reports whether the request is well formed.
func (r Request) Validate() error {
	if strings.TrimSpace(r.TransmissionID) == "" {
		return fmt.Errorf("%w: transmission_id is required", ErrInvalidRequest)
	}
	if strings.TrimSpace(r.Transcript) == "" {
		return fmt.Errorf("%w: transcript is required", ErrInvalidRequest)
	}
	if strings.TrimSpace(r.Speaker) == "" {
		return fmt.Errorf("%w: speaker is required", ErrInvalidRequest)
	}
	if r.FrequencyHz == 0 {
		return fmt.Errorf("%w: frequency_hz is required", ErrInvalidRequest)
	}
	if r.Modulation != ModulationAM && r.Modulation != ModulationFM {
		return fmt.Errorf("%w: modulation must be AM or FM", ErrInvalidRequest)
	}
	if r.ReceivedAt.IsZero() {
		return fmt.Errorf("%w: received_at is required", ErrInvalidRequest)
	}
	if r.TranscribedAt.IsZero() {
		return fmt.Errorf("%w: transcribed_at is required", ErrInvalidRequest)
	}
	if r.TranscribedAt.Before(r.ReceivedAt) {
		return fmt.Errorf("%w: transcribed_at precedes received_at", ErrInvalidRequest)
	}
	return nil
}

// Validate reports whether the response is well formed and internally
// consistent.
//
// The consistency rules matter as much as the field checks: a deliverable
// response with no speech would transmit silence, and a non-deliverable
// response carrying speech invites a caller to transmit something Hermes
// decided to suppress.
func (r Response) Validate() error {
	if strings.TrimSpace(r.TransmissionID) == "" {
		return fmt.Errorf("%w: transmission_id is required", ErrInvalidResponse)
	}
	switch r.IntentClass {
	case IntentFactual, IntentCue, IntentActionPreview, IntentUnrecognized:
	default:
		return fmt.Errorf("%w: unknown intent_class %q", ErrInvalidResponse, r.IntentClass)
	}
	switch r.State {
	case StateOK, StateStale, StateDegraded, StateUnavailable, StateOwnshipAmbiguous:
	default:
		return fmt.Errorf("%w: unknown state %q", ErrInvalidResponse, r.State)
	}
	if r.Deliverable {
		if strings.TrimSpace(r.Speech) == "" {
			return fmt.Errorf("%w: deliverable response requires speech", ErrInvalidResponse)
		}
		if r.SuppressionReason != "" {
			return fmt.Errorf("%w: deliverable response must not carry a suppression reason", ErrInvalidResponse)
		}
	} else {
		if strings.TrimSpace(r.Speech) != "" {
			return fmt.Errorf("%w: non-deliverable response must not carry speech", ErrInvalidResponse)
		}
		if strings.TrimSpace(r.SuppressionReason) == "" {
			return fmt.Errorf("%w: non-deliverable response requires a suppression reason", ErrInvalidResponse)
		}
	}
	return nil
}

// Client is the Gateway's view of Hermes.
type Client interface {
	// Exchange sends a transcript and returns Hermes's response. An error means
	// nothing may be transmitted.
	Exchange(ctx context.Context, req Request) (Response, error)
}

// Suppressed builds a valid non-deliverable response. Use it wherever the
// Gateway must fail quiet, so the silent path is as well formed as the
// speaking one.
func Suppressed(transmissionID string, intent IntentClass, state State, reason string) Response {
	if strings.TrimSpace(reason) == "" {
		reason = "unspecified"
	}
	return Response{
		TransmissionID:    transmissionID,
		Deliverable:       false,
		IntentClass:       intent,
		State:             state,
		SuppressionReason: reason,
	}
}
