package application

import (
	"context"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/composer"
	"github.com/dharmab/skyeye/pkg/traces"
	"github.com/rs/zerolog/log"
)

// athenaObserveOnly consumes transcripts and discards them.
//
// This runs when an Athena command channel is configured but no Hermes bridge
// is: the gateway admits, transcribes, and logs, but has nobody to ask and
// nothing to say. It exists so receive-only validation can exercise the SRS
// connection, admission gate, and speech recognition without the GCI brain
// running behind it.
//
// It must consume from the channel rather than leaving it unread, or the
// recognition goroutine would block once the buffer filled.
func (*Application) athenaObserveOnly(ctx context.Context, in <-chan Message[string]) {
	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("stopping Athena observe-only routine due to context cancellation")
			return
		case message := <-in:
			// The transcript itself is not logged here. Whether transcripts may
			// be recorded at all is a separate operator decision governed by
			// enable-transcription-logging.
			log.Info().
				Str("transmissionID", traces.GetTraceID(message.Context)).
				Msg("admitted transmission transcribed; no Hermes bridge configured, transmitting nothing")
		}
	}
}

// athenaCommandLane replaces the GCI controller lane (parse -> control ->
// compose) when Athena is configured.
//
// The Gateway does not interpret the transcript. It hands the text to Hermes
// and synthesizes whatever comes back, verbatim. All judgment -- intent
// classification, tool selection, wording, and whether to answer at all --
// lives in Hermes. See ADR 0014 and docs/architecture/dcs-srs-audio-gateway-architecture.md §5.
//
// Every failure path is silent. Silence on the radio is always correct when the
// system is uncertain; a wrong or invented transmission is not.
func (a *Application) athenaCommandLane(
	ctx context.Context,
	in <-chan Message[string],
	out chan<- Message[composer.NaturalLanguageResponse],
) {
	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("stopping Athena command lane due to context cancellation")
			return
		case message := <-in:
			a.exchangeWithHermes(ctx, message, out)
		}
	}
}

// exchangeWithHermes performs one transcript-for-speech exchange.
func (a *Application) exchangeWithHermes(
	ctx context.Context,
	message Message[string],
	out chan<- Message[composer.NaturalLanguageResponse],
) {
	rCtx := message.Context
	transcript := message.Data
	transmissionID := traces.GetTraceID(rCtx)
	logger := log.With().Str("transmissionID", transmissionID).Logger()

	// An empty transcript means speech recognition produced nothing usable.
	// Never guess at what the pilot said.
	if transcript == "" {
		logger.Info().Msg("empty transcript; transmitting nothing")
		return
	}

	// Resolve who was addressed before anything else. Athena is mission,
	// Hermes is dev and system; they run in separate sessions because they
	// differ in authority, not merely topic. An unaddressed transmission is
	// not routed at all -- guessing could send a mission question into a lane
	// that can mutate a repository.
	//
	// A nil registry means persona routing is not configured; the transcript
	// goes to the single configured bridge unchanged. This keeps a
	// single-persona deployment working without forcing the pilot to say a
	// name on every call.
	transcriptForHermes := transcript
	addressee := ""
	if a.personas != nil {
		resolution := a.personas.Resolve(transcript)
		if !resolution.Addressed() {
			// The transcript is logged at debug so that, after a week of
			// flying, we can see what unaddressed traffic actually looks like
			// and decide on evidence whether a default persona is warranted.
			logger.Info().Msg("transmission addressed to no persona; transmitting nothing")
			logger.Debug().Str("transcript", transcript).Msg("unaddressed transcript")
			return
		}
		if resolution.Fuzzy {
			// Surfaced deliberately: a persona name that repeatedly needs
			// fuzzy matching for this speaker is a naming problem, not a
			// transient slip.
			logger.Info().
				Str("persona", resolution.Persona.Name).
				Str("heard", resolution.Matched).
				Int("distance", resolution.Distance).
				Msg("persona resolved by near-match")
		}
		transcriptForHermes = resolution.Remainder
		addressee = resolution.Persona.Name
	}

	receivedAt := traces.GetReceivedAt(rCtx)
	if receivedAt.IsZero() {
		receivedAt = time.Now()
	}
	transcribedAt := traces.GetRecognizedAt(rCtx)
	if transcribedAt.IsZero() || transcribedAt.Before(receivedAt) {
		transcribedAt = receivedAt
	}

	req := bridge.Request{
		TransmissionID: transmissionID,
		// The remainder, not the raw transcript: a persona should not receive
		// its own name as part of the request.
		Transcript:    transcriptForHermes,
		Addressee:     addressee,
		Speaker:       traces.GetClientName(rCtx),
		FrequencyHz:   a.athenaFrequencyHz,
		Modulation:    a.athenaModulation,
		ReceivedAt:    receivedAt,
		TranscribedAt: transcribedAt,
	}

	resp, err := a.hermesBridge.Exchange(ctx, req)
	if err != nil {
		// Bounded error code only. The transcript is not logged here: whether
		// transcripts may be logged at all is a separate operator decision.
		logger.Warn().Err(err).Msg("bridge exchange failed; transmitting nothing")
		a.trace(traces.WithRequestError(rCtx, err))
		return
	}

	if !resp.Deliverable {
		logger.Info().
			Str("reason", resp.SuppressionReason).
			Str("state", string(resp.State)).
			Msg("Hermes suppressed the response; transmitting nothing")
		return
	}

	logger.Info().
		Str("intent", string(resp.IntentClass)).
		Str("state", string(resp.State)).
		Msg("Hermes returned a deliverable response")

	// Speech is synthesized verbatim. The Gateway does not reword, summarize,
	// or expand it.
	out <- AsMessage(
		traces.WithHandledAt(rCtx, time.Now()),
		composer.NaturalLanguageResponse{
			Subtitle: resp.Speech,
			Speech:   resp.Speech,
		},
	)
}
