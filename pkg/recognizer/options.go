package recognizer

type recognizerOptions struct {
	locations []string
	// athenaMode selects the Athena conversational prompt instead of the
	// upstream AWACS GCI prompt. The two prime for very different speech and
	// using the wrong one measurably degrades accuracy.
	athenaMode bool
	// promptOverride replaces the generated prompt entirely when non-empty.
	promptOverride string
}

// Option configures a recognizer.
type Option func(*recognizerOptions)

// WithLocations provides location names to include in the recognizer's prompt.
func WithLocations(locations []string) Option {
	return func(o *recognizerOptions) {
		o.locations = locations
	}
}

// WithAthenaMode selects the Athena conversational prompt.
//
// Upstream's prompt primes for AWACS brevity codes; the Athena command channel
// carries ordinary spoken English. Priming for the wrong register biases the
// model away from what it actually hears.
func WithAthenaMode() Option {
	return func(o *recognizerOptions) {
		o.athenaMode = true
	}
}

// WithPromptOverride replaces the generated prompt with the given text.
//
// Which vocabulary transcribes best is empirical and speaker-dependent, so the
// prompt is tunable without a rebuild. An empty string leaves the generated
// prompt in place.
func WithPromptOverride(prompt string) Option {
	return func(o *recognizerOptions) {
		o.promptOverride = prompt
	}
}

// initialPrompt returns the prompt text for this recognizer's configuration.
func (o recognizerOptions) initialPrompt(callsign string) string {
	if o.promptOverride != "" {
		return o.promptOverride
	}
	if o.athenaMode {
		return athenaPrompt(callsign, o.locations, "")
	}
	return prompt(callsign, o.locations)
}
