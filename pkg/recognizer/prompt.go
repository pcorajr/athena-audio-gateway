package recognizer

import (
	"fmt"
	"os"
	"strings"
)

// prompt constructs a prompt for OpenAI's audio transcription models. See https://platform.openai.com/docs/guides/speech-to-text#prompting
func prompt(callsign string, locations []string) string {
	s := fmt.Sprintf("Either ANYFACE or %s, PILOT CALLSIGN, DIGITS, one of 'RADIO' 'ALPHA' 'BOGEY' 'PICTURE' 'DECLARE' 'SNAPLOCK' 'SPIKED', ARGUMENTS such as BULLSEYE, BRAA, numbers or digits.", callsign)
	if len(locations) > 0 {
		s += " Locations: " + strings.Join(locations, ", ") + "."
	}
	return s
}

// athenaPrompt constructs the initial prompt for the Athena command channel.
//
// This is deliberately different from prompt() above, which primes the model
// for AWACS GCI brevity (ANYFACE, SNAPLOCK, SPIKED, BRAA). That vocabulary is
// correct for upstream SkyEye and wrong here: the Athena channel carries
// ordinary conversational English, and priming for terse brevity codes biases
// the model away from the natural speech it actually receives.
//
// A Whisper initial prompt is not a dictionary or a grammar. It is prior
// context: the model is told "the audio continues from this text" and adjusts
// its expectations for style, vocabulary, and formatting. So the prompt is
// written as a plausible sample of the traffic rather than as a word list.
//
// Two things it must handle that plain English does not cover:
//
//   - Aviation number words. "niner" for nine, "tree" for three, "fife" for
//     five, and "angels one five" meaning fifteen thousand feet. Without
//     priming these are transcribed phonetically, e.g. "angels one five"
//     became "ANGER 1-5" in live testing.
//   - Accented speech. The pilot is a native Spanish speaker. Showing the
//     model well-formed English sentences of the kind it will hear helps more
//     than a bare keyword list, which tends to produce keyword-shaped guesses.
func athenaPrompt(callsign string, locations []string, extra string) string {
	if callsign == "" {
		callsign = "Athena"
	}

	var b strings.Builder

	// Conversational examples first. These set the expectation that the audio
	// is a person speaking in full sentences, not a stream of brevity codes.
	// strings.Builder never returns an error.
	_, _ = fmt.Fprintf(&b,
		"A pilot talking to %s, an AI copilot, over the radio in natural English. "+
			"%s, what is near me? "+
			"%s, where is the nearest threat? "+
			"%s, I want to land at the airport, which way should I turn and for how long? "+
			"%s, where is the target relative to my heading? "+
			"%s, what am I flying? "+
			"%s, say again. ",
		callsign, callsign, callsign, callsign, callsign, callsign, callsign,
	)

	// Aviation number forms, shown in use rather than listed.
	b.WriteString(
		"Heading two seven zero. Angels one five. Bullseye zero niner zero for forty. " +
			"Fuel state four point two. Flight level two fife zero. ")

	// Domain nouns the model would otherwise guess at.
	b.WriteString(
		"Terms: bullseye, bogey, bandit, friendly, contact, threat, SAM, " +
			"heading, bearing, range, altitude, airbase, waypoint, sector, tanker. ")

	if len(locations) > 0 {
		b.WriteString("Locations: " + strings.Join(locations, ", ") + ". ")
	}

	if extra = strings.TrimSpace(extra); extra != "" {
		b.WriteString(extra)
		if !strings.HasSuffix(extra, ".") {
			b.WriteString(".")
		}
		b.WriteString(" ")
	}

	return strings.TrimSpace(b.String())
}

// LoadPromptFile reads a prompt override from disk.
//
// The vocabulary that works best is an empirical question -- it depends on the
// speaker, the mission, and the model. Keeping it in a file means it can be
// tuned against the accuracy harness without rebuilding the binary.
//
// An empty path returns an empty string, not an error: no override is the
// normal case.
func LoadPromptFile(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("failed to read prompt file %q: %w", path, err)
	}
	return strings.TrimSpace(string(b)), nil
}
