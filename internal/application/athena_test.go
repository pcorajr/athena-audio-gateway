package application

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/composer"
	"github.com/dharmab/skyeye/pkg/traces"
)

// fakeBridge is a scripted Hermes stand-in. It records what it was asked so a
// test can assert the request envelope, and returns whatever the test set up.
type fakeBridge struct {
	resp     bridge.Response
	err      error
	requests []bridge.Request
}

func (f *fakeBridge) Exchange(_ context.Context, req bridge.Request) (bridge.Response, error) {
	f.requests = append(f.requests, req)
	if f.err != nil {
		return bridge.Response{}, f.err
	}
	return f.resp, nil
}

func laneApplication(fake *fakeBridge) *Application {
	return &Application{
		hermesBridge:      fake,
		athenaFrequencyHz: 30_000_000,
		athenaModulation:  bridge.ModulationFM,
	}
}

// requestContext mirrors what recognize() builds before handing off.
func requestContext(transcript string) Message[string] {
	received := time.Now().Add(-2 * time.Second)
	ctx := context.Background()
	ctx = traces.WithTraceID(ctx, "tx-1")
	ctx = traces.WithClientName(ctx, "Prometheus")
	ctx = traces.WithReceivedAt(ctx, received)
	ctx = traces.WithRecognizedAt(ctx, received.Add(500*time.Millisecond))
	return AsMessage(ctx, transcript)
}

// collect drains one message if the lane produced one, without blocking.
func collect(out chan Message[composer.NaturalLanguageResponse]) (composer.NaturalLanguageResponse, bool) {
	select {
	case msg := <-out:
		return msg.Data, true
	default:
		return composer.NaturalLanguageResponse{}, false
	}
}

func TestCommandLaneSynthesizesHermesSpeechVerbatim(t *testing.T) {
	t.Parallel()

	const speech = "Nearest threat bullseye 270 for 40, medium."
	fake := &fakeBridge{resp: bridge.Response{
		TransmissionID: "tx-1",
		Deliverable:    true,
		Speech:         speech,
		IntentClass:    bridge.IntentFactual,
		State:          bridge.StateOK,
	}}
	app := laneApplication(fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext("athena, nearest threat"), out)

	got, ok := collect(out)
	if !ok {
		t.Fatal("expected a response to be queued for synthesis")
	}
	if got.Speech != speech {
		t.Errorf("speech = %q, want verbatim %q", got.Speech, speech)
	}
	if got.Subtitle != speech {
		t.Errorf("subtitle = %q, want %q", got.Subtitle, speech)
	}
}

// Every uncertainty path must produce silence rather than an invented or stale
// transmission.
func TestCommandLaneFailsQuiet(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		transcript string
		fake       *fakeBridge
	}{
		{
			name:       "empty transcript",
			transcript: "",
			fake:       &fakeBridge{},
		},
		{
			name:       "bridge error",
			transcript: "athena, status",
			fake:       &fakeBridge{err: errors.New("hermes unreachable")},
		},
		{
			name:       "hermes suppressed the response",
			transcript: "athena, status",
			fake: &fakeBridge{resp: bridge.Response{
				TransmissionID:    "tx-1",
				Deliverable:       false,
				IntentClass:       bridge.IntentFactual,
				State:             bridge.StateStale,
				SuppressionReason: "mission state is stale",
			}},
		},
		{
			name:       "unrecognized intent suppressed",
			transcript: "unintelligible chatter",
			fake: &fakeBridge{resp: bridge.Response{
				TransmissionID:    "tx-1",
				Deliverable:       false,
				IntentClass:       bridge.IntentUnrecognized,
				State:             bridge.StateOK,
				SuppressionReason: "could not classify",
			}},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			app := laneApplication(tc.fake)
			out := make(chan Message[composer.NaturalLanguageResponse], 1)

			app.exchangeWithHermes(t.Context(), requestContext(tc.transcript), out)

			if _, ok := collect(out); ok {
				t.Error("expected silence, but a transmission was queued")
			}
		})
	}
}

// An empty transcript must not even reach Hermes: there is nothing to ask.
func TestCommandLaneDoesNotCallHermesOnEmptyTranscript(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{}
	app := laneApplication(fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext(""), out)

	if len(fake.requests) != 0 {
		t.Errorf("Hermes was called %d times for an empty transcript; want 0", len(fake.requests))
	}
}

// The request envelope must carry the correlation ID, speaker, and observed
// channel — and nothing audio-derived.
func TestCommandLaneBuildsCorrelatedRequest(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{resp: bridge.Response{
		TransmissionID: "tx-1",
		Deliverable:    true,
		Speech:         "Copy.",
		IntentClass:    bridge.IntentFactual,
		State:          bridge.StateOK,
	}}
	app := laneApplication(fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext("athena, status"), out)

	if len(fake.requests) != 1 {
		t.Fatalf("Hermes called %d times, want 1", len(fake.requests))
	}
	req := fake.requests[0]
	if req.TransmissionID != "tx-1" {
		t.Errorf("transmission_id = %q, want tx-1", req.TransmissionID)
	}
	if req.Speaker != "Prometheus" {
		t.Errorf("speaker = %q, want Prometheus", req.Speaker)
	}
	if req.FrequencyHz != 30_000_000 {
		t.Errorf("frequency_hz = %d, want 30000000", req.FrequencyHz)
	}
	if req.Modulation != bridge.ModulationFM {
		t.Errorf("modulation = %q, want FM", req.Modulation)
	}
	if err := req.Validate(); err != nil {
		t.Errorf("lane built an invalid request: %v", err)
	}
}

// Timestamps must be coherent even when tracing data is missing or skewed, or
// the request fails validation at the bridge and the pilot gets silence for a
// bookkeeping reason.
func TestCommandLaneRepairsMissingTimestamps(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{resp: bridge.Response{
		TransmissionID: "tx-1",
		Deliverable:    true,
		Speech:         "Copy.",
		IntentClass:    bridge.IntentFactual,
		State:          bridge.StateOK,
	}}
	app := laneApplication(fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	// No ReceivedAt or RecognizedAt on the context at all.
	ctx := context.Background()
	ctx = traces.WithTraceID(ctx, "tx-1")
	ctx = traces.WithClientName(ctx, "Prometheus")

	app.exchangeWithHermes(t.Context(), AsMessage(ctx, "athena, status"), out)

	if len(fake.requests) != 1 {
		t.Fatalf("Hermes called %d times, want 1", len(fake.requests))
	}
	if err := fake.requests[0].Validate(); err != nil {
		t.Errorf("request with missing timestamps should still validate: %v", err)
	}
}

// Action language is carried as an inert preview, and its speech is still
// synthesized verbatim without any dispatch occurring.
func TestCommandLaneCarriesActionPreviewInertly(t *testing.T) {
	t.Parallel()

	const speech = "That would task two aircraft. Not executing."
	fake := &fakeBridge{resp: bridge.Response{
		TransmissionID: "tx-1",
		Deliverable:    true,
		Speech:         speech,
		IntentClass:    bridge.IntentActionPreview,
		State:          bridge.StateOK,
	}}
	app := laneApplication(fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext("athena, engage that group"), out)

	got, ok := collect(out)
	if !ok {
		t.Fatal("action preview should still be spoken")
	}
	if got.Speech != speech {
		t.Errorf("speech = %q, want %q", got.Speech, speech)
	}
}
