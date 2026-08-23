package bridge

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
)

func validRequest() Request {
	received := time.Date(2026, 8, 22, 12, 0, 0, 0, time.UTC)
	return Request{
		TransmissionID: "abc123",
		Transcript:     "athena, nearest threat",
		Speaker:        "Prometheus",
		FrequencyHz:    30_000_000,
		Modulation:     ModulationFM,
		ReceivedAt:     received,
		TranscribedAt:  received.Add(400 * time.Millisecond),
	}
}

func validResponse() Response {
	return Response{
		TransmissionID: "abc123",
		Deliverable:    true,
		Speech:         "Nearest threat bullseye 270 for 40, medium.",
		IntentClass:    IntentFactual,
		State:          StateOK,
	}
}

// The contract's central guarantee: audio cannot cross this boundary. This is
// asserted structurally rather than by convention, so a future field addition
// that carries PCM fails the build's test gate.
func TestRequestCannotCarryAudio(t *testing.T) {
	t.Parallel()

	forbidden := []string{"audio", "pcm", "opus", "sample", "waveform", "path", "hash", "bytes"}
	rt := reflect.TypeFor[Request]()
	for field := range rt.Fields() {
		lower := strings.ToLower(field.Name)
		for _, bad := range forbidden {
			if strings.Contains(lower, bad) {
				t.Errorf("Request.%s looks audio-derived; audio must not cross the bridge", field.Name)
			}
		}
		if kind := field.Type.Kind(); kind == reflect.Slice || kind == reflect.Array {
			t.Errorf("Request.%s is a %s; the bridge carries text only", field.Name, kind)
		}
	}
}

func TestRequestValidation(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*Request)
		ok     bool
	}{
		{"valid", func(*Request) {}, true},
		{"missing id", func(r *Request) { r.TransmissionID = "" }, false},
		{"blank id", func(r *Request) { r.TransmissionID = "  " }, false},
		{"missing transcript", func(r *Request) { r.Transcript = "" }, false},
		{"blank transcript", func(r *Request) { r.Transcript = "   " }, false},
		{"missing speaker", func(r *Request) { r.Speaker = "" }, false},
		{"missing frequency", func(r *Request) { r.FrequencyHz = 0 }, false},
		{"unknown modulation", func(r *Request) { r.Modulation = Modulation("SSB") }, false},
		{"empty modulation", func(r *Request) { r.Modulation = "" }, false},
		{"AM accepted", func(r *Request) { r.Modulation = ModulationAM }, true},
		{"missing received_at", func(r *Request) { r.ReceivedAt = time.Time{} }, false},
		{"missing transcribed_at", func(r *Request) { r.TranscribedAt = time.Time{} }, false},
		{"transcribed before received", func(r *Request) {
			r.TranscribedAt = r.ReceivedAt.Add(-time.Second)
		}, false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			req := validRequest()
			tc.mutate(&req)
			err := req.Validate()
			if tc.ok && err != nil {
				t.Fatalf("expected valid, got %v", err)
			}
			if !tc.ok {
				if err == nil {
					t.Fatal("expected validation error, got nil")
				}
				if !errors.Is(err, ErrInvalidRequest) {
					t.Errorf("error should wrap ErrInvalidRequest, got %v", err)
				}
			}
		})
	}
}

func TestResponseValidation(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*Response)
		ok     bool
	}{
		{"valid deliverable", func(*Response) {}, true},
		{"missing id", func(r *Response) { r.TransmissionID = "" }, false},
		{"unknown intent", func(r *Response) { r.IntentClass = IntentClass("chatter") }, false},
		{"empty intent", func(r *Response) { r.IntentClass = "" }, false},
		{"unknown state", func(r *Response) { r.State = State("fine") }, false},
		{"empty state", func(r *Response) { r.State = "" }, false},
		{"deliverable without speech", func(r *Response) { r.Speech = "" }, false},
		{"deliverable with blank speech", func(r *Response) { r.Speech = "   " }, false},
		{"deliverable with suppression reason", func(r *Response) {
			r.SuppressionReason = "why"
		}, false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			resp := validResponse()
			tc.mutate(&resp)
			err := resp.Validate()
			if tc.ok && err != nil {
				t.Fatalf("expected valid, got %v", err)
			}
			if !tc.ok {
				if err == nil {
					t.Fatal("expected validation error, got nil")
				}
				if !errors.Is(err, ErrInvalidResponse) {
					t.Errorf("error should wrap ErrInvalidResponse, got %v", err)
				}
			}
		})
	}
}

// A suppressed response must not smuggle speech through, or a caller could
// transmit something Hermes decided to withhold.
func TestNonDeliverableResponseRules(t *testing.T) {
	t.Parallel()

	withSpeech := Response{
		TransmissionID: "abc123",
		Deliverable:    false,
		Speech:         "this should never be transmitted",
		IntentClass:    IntentFactual,
		State:          StateStale,
	}
	if err := withSpeech.Validate(); err == nil {
		t.Error("a non-deliverable response carrying speech must be rejected")
	}

	noReason := Response{
		TransmissionID: "abc123",
		Deliverable:    false,
		IntentClass:    IntentFactual,
		State:          StateStale,
	}
	if err := noReason.Validate(); err == nil {
		t.Error("a non-deliverable response requires a suppression reason")
	}
}

func TestSuppressedBuildsValidResponse(t *testing.T) {
	t.Parallel()

	resp := Suppressed("abc123", IntentFactual, StateUnavailable, "athena unreachable")
	if err := resp.Validate(); err != nil {
		t.Fatalf("Suppressed must build a valid response, got %v", err)
	}
	if resp.Deliverable {
		t.Error("suppressed response must not be deliverable")
	}
	if resp.Speech != "" {
		t.Error("suppressed response must carry no speech")
	}

	// Even a caller that forgets the reason gets a valid envelope.
	blank := Suppressed("abc123", IntentUnrecognized, StateDegraded, "  ")
	if err := blank.Validate(); err != nil {
		t.Errorf("Suppressed must fill a missing reason, got %v", err)
	}
}

// Every state and intent must survive validation, so a caveat can always be
// carried rather than dropped.
func TestAllStatesAndIntentsAreCarryable(t *testing.T) {
	t.Parallel()

	states := []State{StateOK, StateStale, StateDegraded, StateUnavailable, StateOwnshipAmbiguous}
	intents := []IntentClass{IntentFactual, IntentCue, IntentActionPreview, IntentUnrecognized}

	for _, s := range states {
		for _, i := range intents {
			resp := validResponse()
			resp.State = s
			resp.IntentClass = i
			if err := resp.Validate(); err != nil {
				t.Errorf("state %q intent %q should validate, got %v", s, i, err)
			}
		}
	}
}

// Action language must be representable as non-executing, and the envelope
// carries no field that could dispatch anything.
func TestActionPreviewIsInert(t *testing.T) {
	t.Parallel()

	resp := Response{
		TransmissionID: "abc123",
		Deliverable:    true,
		Speech:         "Understood. That would task two aircraft. Not executing.",
		IntentClass:    IntentActionPreview,
		State:          StateOK,
	}
	if err := resp.Validate(); err != nil {
		t.Fatalf("action preview should validate, got %v", err)
	}

	rt := reflect.TypeFor[Response]()
	for field := range rt.Fields() {
		name := strings.ToLower(field.Name)
		for _, bad := range []string{"command", "execute", "dispatch", "action_id", "mutation"} {
			if strings.Contains(name, bad) {
				t.Errorf("Response.%s could imply execution; the bridge is inert",
					field.Name)
			}
		}
	}
}

func TestJSONRoundTrip(t *testing.T) {
	t.Parallel()

	req := validRequest()
	data, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	var gotReq Request
	if err := json.Unmarshal(data, &gotReq); err != nil {
		t.Fatalf("unmarshal request: %v", err)
	}
	if !gotReq.ReceivedAt.Equal(req.ReceivedAt) || gotReq.Transcript != req.Transcript {
		t.Error("request did not survive a JSON round trip")
	}
	if err := gotReq.Validate(); err != nil {
		t.Errorf("round-tripped request should validate, got %v", err)
	}

	resp := validResponse()
	data, err = json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal response: %v", err)
	}
	// A deliverable response must not emit an empty suppression_reason key.
	if strings.Contains(string(data), "suppression_reason") {
		t.Error("empty suppression_reason should be omitted from JSON")
	}
	var gotResp Response
	if err := json.Unmarshal(data, &gotResp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if err := gotResp.Validate(); err != nil {
		t.Errorf("round-tripped response should validate, got %v", err)
	}
}
