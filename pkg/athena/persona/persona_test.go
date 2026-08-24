package persona

import (
	"strings"
	"testing"
)

// testRegistry mirrors the pilot's real setup: Athena is mission, Hermes is dev
// and system.
func testRegistry(t *testing.T) *Registry {
	t.Helper()
	r, err := NewRegistry([]Persona{
		{Name: "athena", Aliases: []string{"athena"}, Endpoint: "http://mission"},
		{Name: "hermes", Aliases: []string{"hermes"}, Endpoint: "http://dev"},
	}, DefaultMaxDistance)
	if err != nil {
		t.Fatalf("NewRegistry: %v", err)
	}
	return r
}

// The most important property: an unaddressed transmission resolves to nobody.
// Guessing would let mission chatter reach a lane that can mutate a repository.
func TestUnaddressedResolvesToNobody(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	for _, transcript := range []string{
		"",
		"   ",
		"what is near me",
		"give me a status update on the battlefield",
		"say again",
		"one two three four five",
		// A persona named mid-sentence is not an address.
		"tell hermes I said hello",
		"I was talking to athena earlier",
	} {
		got := r.Resolve(transcript)
		if got.Addressed() {
			t.Errorf("Resolve(%q) resolved to %q; want nobody",
				transcript, got.Persona.Name)
		}
		if got.Remainder != transcript {
			t.Errorf("Resolve(%q) altered the transcript to %q; an unresolved "+
				"transcript must pass through unchanged", transcript, got.Remainder)
		}
	}
}

func TestResolvesTheRealPilotPhrasings(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	// Both of these were transcribed verbatim from live radio during testing.
	tests := []struct {
		transcript string
		want       string
		remainder  string
	}{
		{
			"Athena, give me a status update on the battlefield.",
			"athena",
			"give me a status update on the battlefield.",
		},
		{
			"Hermes, let's adjust the spawn rate.",
			"hermes",
			"let's adjust the spawn rate.",
		},
	}

	for _, tc := range tests {
		got := r.Resolve(tc.transcript)
		if !got.Addressed() {
			t.Fatalf("Resolve(%q) resolved to nobody", tc.transcript)
		}
		if got.Persona.Name != tc.want {
			t.Errorf("Resolve(%q) = %q, want %q", tc.transcript, got.Persona.Name, tc.want)
		}
		if got.Remainder != tc.remainder {
			t.Errorf("Resolve(%q) remainder = %q, want %q",
				tc.transcript, got.Remainder, tc.remainder)
		}
		if got.Fuzzy {
			t.Errorf("Resolve(%q) used fuzzy matching on an exact phrasing", tc.transcript)
		}
	}
}

func TestAddressIsStrippedFromRemainder(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	// The persona should never see its own name as part of the request.
	got := r.Resolve("Athena, what is near me?")
	if got.Remainder != "what is near me?" {
		t.Errorf("remainder = %q, want the address removed", got.Remainder)
	}
}

func TestCaseAndPunctuationInsensitive(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	for _, transcript := range []string{
		"Athena, status",
		"athena status",
		"ATHENA, status",
		"Athena. status",
		"Athena: status",
		"  Athena,   status",
	} {
		got := r.Resolve(transcript)
		if !got.Addressed() || got.Persona.Name != "athena" {
			t.Errorf("Resolve(%q) failed to resolve athena", transcript)
		}
	}
}

// Accented speech is a first-class requirement, not an edge case. A single
// clean live sample is not proof against a noisy transmission.
func TestFuzzyMatchToleratesTranscriptionSlips(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	for _, tc := range []struct {
		transcript string
		want       string
	}{
		{"Athen, what is near me", "athena"},   // dropped character
		{"Athenna, what is near me", "athena"}, // doubled character
		{"Athina, what is near me", "athena"},  // substitution
		{"Hermez, run the tests", "hermes"},    // substitution
	} {
		got := r.Resolve(tc.transcript)
		if !got.Addressed() {
			t.Errorf("Resolve(%q) resolved to nobody; a one-character slip should match", tc.transcript)
			continue
		}
		if got.Persona.Name != tc.want {
			t.Errorf("Resolve(%q) = %q, want %q", tc.transcript, got.Persona.Name, tc.want)
		}
		if !got.Fuzzy {
			t.Errorf("Resolve(%q) should report Fuzzy so near-misses can be reviewed", tc.transcript)
		}
		if got.Distance != 1 {
			t.Errorf("Resolve(%q) distance = %d, want 1", tc.transcript, got.Distance)
		}
	}
}

// Fuzzy matching must not become a licence to guess.
func TestFuzzyMatchDoesNotOverreach(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	for _, transcript := range []string{
		"Alpha, what is near me",   // unrelated word
		"Athenaeum, what is there", // too far
		"the, quick brown fox",     // short common word
		"and, another thing",
	} {
		if got := r.Resolve(transcript); got.Addressed() {
			t.Errorf("Resolve(%q) matched %q at distance %d; should not have",
				transcript, got.Persona.Name, got.Distance)
		}
	}
}

func TestFuzzyCanBeDisabled(t *testing.T) {
	t.Parallel()
	r, err := NewRegistry([]Persona{{Name: "athena"}}, 0)
	if err != nil {
		t.Fatalf("NewRegistry: %v", err)
	}
	if got := r.Resolve("Athen, status"); got.Addressed() {
		t.Error("maxDistance 0 must disable fuzzy matching entirely")
	}
	if got := r.Resolve("Athena, status"); !got.Addressed() {
		t.Error("exact matching must still work with fuzzy disabled")
	}
}

// Adding a persona must be a configuration change, not a code change.
func TestRegistryIsExtensible(t *testing.T) {
	t.Parallel()
	r, err := NewRegistry([]Persona{
		{Name: "athena"},
		{Name: "hermes"},
		{Name: "controller", Aliases: []string{"control", "tower"}},
	}, DefaultMaxDistance)
	if err != nil {
		t.Fatalf("NewRegistry: %v", err)
	}
	for _, tc := range []struct{ transcript, want string }{
		{"controller, spawn a tank", "controller"},
		{"control, spawn a tank", "controller"},
		{"tower, spawn a tank", "controller"},
	} {
		got := r.Resolve(tc.transcript)
		if !got.Addressed() || got.Persona.Name != tc.want {
			t.Errorf("Resolve(%q) failed to resolve %q", tc.transcript, tc.want)
		}
	}
}

// Ambiguity must fail at construction. Resolving it at runtime would silently
// prefer whichever persona happened to be registered first.
func TestDuplicateAliasIsRejected(t *testing.T) {
	t.Parallel()
	_, err := NewRegistry([]Persona{
		{Name: "athena", Aliases: []string{"boss"}},
		{Name: "hermes", Aliases: []string{"boss"}},
	}, DefaultMaxDistance)
	if err == nil {
		t.Fatal("a duplicate alias must be rejected at construction")
	}
}

func TestEmptyRegistryIsRejected(t *testing.T) {
	t.Parallel()
	// An inert registry would accept transmissions and route none, which looks
	// like a transport fault and misdirects debugging.
	if _, err := NewRegistry(nil, DefaultMaxDistance); err == nil {
		t.Error("an empty registry must be rejected")
	}
	if _, err := NewRegistry([]Persona{{Name: "  "}}, DefaultMaxDistance); err == nil {
		t.Error("a persona with an empty name must be rejected")
	}
	if _, err := NewRegistry([]Persona{{Name: "athena"}}, -1); err == nil {
		t.Error("a negative edit distance must be rejected")
	}
}

func TestCanonicalNameIsAlwaysAddressable(t *testing.T) {
	t.Parallel()
	// A persona declared with no explicit aliases must still answer to its name.
	r, err := NewRegistry([]Persona{{Name: "athena"}}, DefaultMaxDistance)
	if err != nil {
		t.Fatalf("NewRegistry: %v", err)
	}
	if got := r.Resolve("athena, status"); !got.Addressed() {
		t.Error("canonical name must be addressable without an explicit alias")
	}
}

func TestAddressOnlyTransmission(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)

	// Just the name, nothing else. Resolves, with an empty remainder.
	got := r.Resolve("Athena")
	if !got.Addressed() {
		t.Fatal("a bare persona name should resolve")
	}
	if got.Remainder != "" {
		t.Errorf("remainder = %q, want empty", got.Remainder)
	}
}

func TestPersonasReturnsACopy(t *testing.T) {
	t.Parallel()
	r := testRegistry(t)
	got := r.Personas()
	got[0].Name = "mutated"
	if r.Personas()[0].Name == "mutated" {
		t.Error("Personas() must not expose internal state for mutation")
	}
}

// A conversation accumulates history, and history is read as truth. A session
// that spent an evening answering "no telemetry" keeps saying "still no
// telemetry" after the data returns, because its own past turns are the most
// recent evidence it has. The epoch exists to abandon such a session.
func TestConversationNameCarriesTheEpoch(t *testing.T) {
	t.Parallel()

	p := Persona{Name: "athena"}
	got := p.ConversationName()
	if got == "athena" {
		t.Error("default conversation name should carry the epoch so a poisoned session can be abandoned")
	}
	if !strings.HasPrefix(got, "athena-") {
		t.Errorf("ConversationName() = %q, want it to start with the persona name", got)
	}
}

func TestExplicitConversationOverridesTheEpoch(t *testing.T) {
	t.Parallel()

	// An operator who names a conversation explicitly means that exact name.
	p := Persona{Name: "athena", Conversation: "mission-alpha"}
	if got := p.ConversationName(); got != "mission-alpha" {
		t.Errorf("ConversationName() = %q, want the explicit name verbatim", got)
	}
}

func TestPersonasGetDistinctConversations(t *testing.T) {
	t.Parallel()

	// The epoch must not collapse two personas into one session; mission and
	// development context staying separate is the whole point of the split.
	athena := Persona{Name: "athena"}.ConversationName()
	hermes := Persona{Name: "hermes"}.ConversationName()
	if athena == hermes {
		t.Fatalf("personas share conversation %q; they must stay isolated", athena)
	}
}
