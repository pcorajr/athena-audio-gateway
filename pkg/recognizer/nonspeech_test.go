package recognizer

import "testing"

func TestStripNonSpeech(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		in   string
		want string
	}{
		{"plain speech untouched", "Athena, nearest threat", "Athena, nearest threat"},
		{"empty", "", ""},

		// The exact failure observed live: a keyed mic with little speech.
		{"music marker alone", "[MUSIC]", ""},
		{"blank audio marker", "[BLANK_AUDIO]", ""},
		{"lowercase marker", "[music]", ""},
		{"parenthesised", "(wind blowing)", ""},
		{"asterisked", "*static*", ""},
		{"musical notes", "♪♪♪", ""},

		{"marker before speech", "[MUSIC] Athena, say bullseye", "Athena, say bullseye"},
		{"marker after speech", "Athena, say bullseye [MUSIC]", "Athena, say bullseye"},
		{"marker mid sentence", "Athena, [cough] nearest threat", "Athena, nearest threat"},
		{"multiple markers", "[MUSIC] one [BLANK_AUDIO] two [MUSIC]", "one two"},

		// Whitespace left by a stripped marker must not survive as a gap.
		{"collapses whitespace", "Athena,   [MUSIC]   nearest threat", "Athena, nearest threat"},
		{"whitespace only", "   ", ""},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := stripNonSpeech(tc.in); got != tc.want {
				t.Errorf("stripNonSpeech(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestIsSilence(t *testing.T) {
	t.Parallel()

	silent := []string{"", "   ", "[MUSIC]", "[BLANK_AUDIO]", "(wind)", "*static*", "[MUSIC] [MUSIC]"}
	for _, s := range silent {
		if !IsSilence(s) {
			t.Errorf("IsSilence(%q) = false, want true", s)
		}
	}

	speech := []string{"Athena", "nearest threat", "[MUSIC] Athena"}
	for _, s := range speech {
		if IsSilence(s) {
			t.Errorf("IsSilence(%q) = true, want false", s)
		}
	}
}

func TestAthenaPromptDiffersFromGCIPrompt(t *testing.T) {
	t.Parallel()

	gci := prompt("Athena", nil)
	athena := athenaPrompt("Athena", nil, "")

	if gci == athena {
		t.Fatal("the Athena prompt must not be the AWACS GCI prompt")
	}

	// The GCI prompt primes for brevity codes that bias against natural speech.
	for _, brevity := range []string{"ANYFACE", "SNAPLOCK", "SPIKED"} {
		if contains(athena, brevity) {
			t.Errorf("Athena prompt should not prime for GCI brevity term %q", brevity)
		}
	}

	// It must prime for the number forms that failed live: "angels one five"
	// was transcribed as "ANGER 1-5".
	for _, want := range []string{"niner", "Angels", "point"} {
		if !contains(athena, want) {
			t.Errorf("Athena prompt should include aviation number form %q", want)
		}
	}

	// And for conversational phrasing rather than terse codes.
	if !contains(athena, "what is near me") {
		t.Error("Athena prompt should include a natural-language example")
	}
}

func TestAthenaPromptIncludesCallsignAndLocations(t *testing.T) {
	t.Parallel()

	p := athenaPrompt("Athena", []string{"Nellis", "Creech"}, "")
	if !contains(p, "Athena") {
		t.Error("prompt should name the callsign")
	}
	if !contains(p, "Nellis") || !contains(p, "Creech") {
		t.Error("prompt should include configured locations")
	}

	// An empty callsign must not produce a malformed prompt.
	if p := athenaPrompt("", nil, ""); !contains(p, "Athena") {
		t.Error("empty callsign should fall back to Athena")
	}
}

func TestPromptOverrideWins(t *testing.T) {
	t.Parallel()

	o := recognizerOptions{athenaMode: true, promptOverride: "custom vocabulary"}
	if got := o.initialPrompt("Athena"); got != "custom vocabulary" {
		t.Errorf("override should replace the generated prompt, got %q", got)
	}

	// Athena mode without an override uses the Athena prompt, not the GCI one.
	o = recognizerOptions{athenaMode: true}
	if got := o.initialPrompt("Athena"); got == prompt("Athena", nil) {
		t.Error("athena mode should not use the GCI prompt")
	}

	// Default stays on upstream behaviour.
	o = recognizerOptions{}
	if got := o.initialPrompt("Athena"); got != prompt("Athena", nil) {
		t.Error("default mode should preserve the upstream GCI prompt")
	}
}

func TestLoadPromptFile(t *testing.T) {
	t.Parallel()

	// No path is the normal case and must not be an error.
	got, err := LoadPromptFile("")
	if err != nil || got != "" {
		t.Errorf("empty path = (%q, %v), want (\"\", nil)", got, err)
	}

	if _, err := LoadPromptFile("/nonexistent/prompt.txt"); err == nil {
		t.Error("a missing prompt file should be an error, not silently ignored")
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
