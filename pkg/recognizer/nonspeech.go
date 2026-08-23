package recognizer

import (
	"regexp"
	"strings"
)

// nonSpeechMarker matches Whisper's bracketed annotations for audio it did not
// interpret as speech: [MUSIC], [BLANK_AUDIO], (wind blowing), *static*, and
// similar.
//
// These are not transcripts. In live testing a keyed microphone carrying
// little speech produced the transcript "[MUSIC]", which the gateway then
// treated as a genuine utterance and forwarded. Anything built on top would
// have to guess whether a bracketed token was something the pilot said or
// something the model invented.
//
// Matching is deliberately structural -- any fully bracketed or asterisked
// span -- rather than a list of known markers, because the set varies by model
// and language and a missed marker is worse than an over-eager strip. Real
// speech does not arrive wrapped in brackets.
var nonSpeechMarker = regexp.MustCompile(`\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+[^♪]*♪+|♪+`)

// stripNonSpeech removes non-speech annotations from a transcript segment.
//
// A segment consisting only of markers collapses to the empty string, which
// the caller treats as silence. That is the correct outcome: the gateway must
// fail quiet rather than forward a marker as if the pilot had said it.
func stripNonSpeech(text string) string {
	if text == "" {
		return ""
	}
	cleaned := nonSpeechMarker.ReplaceAllString(text, " ")
	// Collapse the whitespace left behind so a stripped marker does not show
	// up as a gap mid-sentence.
	return strings.Join(strings.Fields(cleaned), " ")
}

// IsSilence reports whether a transcript carries no usable speech.
//
// Callers use this to decide whether to proceed. An empty or marker-only
// transcript means the recognizer heard audio but no words, and nothing should
// be sent downstream.
func IsSilence(text string) bool {
	return strings.TrimSpace(stripNonSpeech(text)) == ""
}
