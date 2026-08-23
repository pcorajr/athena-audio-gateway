package application

import (
	"context"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/composer"
	"github.com/dharmab/skyeye/pkg/traces"
	"github.com/rs/zerolog/log"
)

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
		Transcript:     transcript,
		Speaker:        traces.GetClientName(rCtx),
		FrequencyHz:    a.athenaFrequencyHz,
		Modulation:     a.athenaModulation,
		ReceivedAt:     receivedAt,
		TranscribedAt:  transcribedAt,
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
