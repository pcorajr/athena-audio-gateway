package application

import (
	"strings"
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/persona"
	"github.com/dharmab/skyeye/pkg/composer"
)

// personaLaneApplication builds a command lane with persona routing enabled.
func personaLaneApplication(t *testing.T, b *fakeBridge) *Application {
	t.Helper()
	app := laneApplication(b)
	reg, err := persona.NewRegistry(persona.DefaultPersonas(), persona.DefaultMaxDistance)
	if err != nil {
		t.Fatalf("NewRegistry: %v", err)
	}
	app.personas = reg
	return app
}

// The safety property: an unaddressed transmission must not reach any persona.
// Guessing could route a mission question into a lane that can mutate a
// repository.
func TestUnaddressedTransmissionNeverReachesHermes(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{resp: bridge.Response{Deliverable: true, Speech: "should not happen"}}
	app := personaLaneApplication(t, fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext("what is near me"), out)

	if len(fake.requests) != 0 {
		t.Errorf("bridge called %d times for an unaddressed transmission; want 0", len(fake.requests))
	}
	if _, ok := collect(out); ok {
		t.Error("an unaddressed transmission must not produce speech")
	}
}

// A persona named mid-sentence is not an address.
func TestPersonaNameMidSentenceIsNotAnAddress(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{resp: bridge.Response{Deliverable: true, Speech: "x"}}
	app := personaLaneApplication(t, fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext("tell hermes I said hello"), out)

	if len(fake.requests) != 0 {
		t.Errorf("bridge called %d times; a mid-sentence name is not an address", len(fake.requests))
	}
}

// The persona must not receive its own name as part of the request.
func TestAddressIsStrippedBeforeReachingHermes(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{resp: bridge.Response{Deliverable: true, Speech: "ok"}}
	app := personaLaneApplication(t, fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(),
		requestContext("Athena, give me a status update on the battlefield."), out)

	if len(fake.requests) != 1 {
		t.Fatalf("bridge called %d times, want 1", len(fake.requests))
	}
	if got, want := fake.requests[0].Transcript, "give me a status update on the battlefield."; got != want {
		t.Errorf("transcript = %q, want the address stripped: %q", got, want)
	}
	if got := fake.requests[0].Addressee; !strings.HasPrefix(got, "athena") {
		t.Errorf("addressee = %q, want it to identify the athena persona", got)
	}
}

// Athena is mission; Hermes is dev and system. They must not be conflated.
func TestPersonasRouteToDistinctAddressees(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct{ transcript, addressee, remainder string }{
		{"Athena, what is near me?", "athena", "what is near me?"},
		{"Hermes, let's adjust the spawn rate.", "hermes", "let's adjust the spawn rate."},
	} {
		fake := &fakeBridge{resp: bridge.Response{Deliverable: true, Speech: "ok"}}
		app := personaLaneApplication(t, fake)
		out := make(chan Message[composer.NaturalLanguageResponse], 1)

		app.exchangeWithHermes(t.Context(), requestContext(tc.transcript), out)

		if len(fake.requests) != 1 {
			t.Fatalf("%q: bridge called %d times, want 1", tc.transcript, len(fake.requests))
		}
		if got := fake.requests[0].Addressee; !strings.HasPrefix(got, tc.addressee) {
			t.Errorf("%q: addressee = %q, want it to identify %q", tc.transcript, got, tc.addressee)
		}
		if got := fake.requests[0].Transcript; got != tc.remainder {
			t.Errorf("%q: transcript = %q, want %q", tc.transcript, got, tc.remainder)
		}
	}
}

// A one-character transcription slip must still route. The pilot is a native
// Spanish speaker and accented speech is a first-class requirement.
func TestNearMissAddressStillRoutes(t *testing.T) {
	t.Parallel()

	fake := &fakeBridge{resp: bridge.Response{Deliverable: true, Speech: "ok"}}
	app := personaLaneApplication(t, fake)
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext("Athenna, nearest threat"), out)

	if len(fake.requests) != 1 {
		t.Fatalf("bridge called %d times, want 1; a near-miss should still route", len(fake.requests))
	}
	if got := fake.requests[0].Addressee; !strings.HasPrefix(got, "athena") {
		t.Errorf("addressee = %q, want it to identify the athena persona", got)
	}
}

// With no registry configured, the lane behaves as a single-persona deployment:
// the transcript passes through unchanged and no address is required.
func TestNilRegistryPassesTranscriptThrough(t *testing.T) {
	t.Parallel()

	const transcript = "what is near me"
	fake := &fakeBridge{resp: bridge.Response{Deliverable: true, Speech: "ok"}}
	app := laneApplication(fake) // no persona registry
	out := make(chan Message[composer.NaturalLanguageResponse], 1)

	app.exchangeWithHermes(t.Context(), requestContext(transcript), out)

	if len(fake.requests) != 1 {
		t.Fatalf("bridge called %d times, want 1", len(fake.requests))
	}
	if got := fake.requests[0].Transcript; got != transcript {
		t.Errorf("transcript = %q, want unchanged %q", got, transcript)
	}
	if got := fake.requests[0].Addressee; got != "" {
		t.Errorf("addressee = %q, want empty when routing is not configured", got)
	}
}
