package recognizer

type recognizerOptions struct {
	locations []string
	// athenaMode selects the Athena conversational prompt instead of the
	// upstream AWACS GCI prompt. The two prime for very different speech and
	// using the wrong one measurably degrades accuracy.
	athenaMode bool
	// promptOverride replaces the generated prompt entirely when non-empty.
	promptOverride string
	// dynamic, when set, appends live vocabulary written by the adapter.
	dynamic *dynamicPrompt
	// threads is the whisper.cpp CPU thread count; 0 keeps the library default (4).
	threads uint
}

// Option configures a recognizer.
type Option func(*recognizerOptions)

// WithLocations provides location names to include in the recognizer's prompt.
func WithLocations(locations []string) Option {
	return func(o *recognizerOptions) {
		o.locations = locations
	}
}

// WithThreads sets the whisper.cpp CPU thread count. 0 keeps the default.
func WithThreads(n uint) Option {
	return func(o *recognizerOptions) {
		o.threads = n
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
		return withDynamic(o.promptOverride, o.dynamic)
	}
	if o.athenaMode {
		return withDynamic(athenaPrompt(callsign, o.locations, ""), o.dynamic)
	}
	return prompt(callsign, o.locations)
}
