package recognizer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/dharmab/skyeye/pkg/pcm/rate"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/ggerganov/whisper.cpp/bindings/go/pkg/whisper"
	"github.com/rs/zerolog/log"
)

type whisperRecognizer struct {
	model    whisper.Model
	callsign string
	recognizerOptions
}

var _ Recognizer = &whisperRecognizer{}

// NewWhisperRecognizer creates a new recognizer using OpenAI Whisper.
func NewWhisperRecognizer(model *whisper.Model, callsign string, opts ...Option) Recognizer {
	r := &whisperRecognizer{
		model:    *model,
		callsign: callsign,
	}
	for _, opt := range opts {
		opt(&r.recognizerOptions)
	}
	return r
}

// leadInSilence is prepended to every sample: radio audio starts abruptly and
// whisper tends to drop the first word without it.
const leadInSilence = 400 * time.Millisecond

// padLeadIn returns a new slice with leadInSilence of zero samples (16 kHz)
// followed by sample. The input slice is not modified.
func padLeadIn(sample []float32) []float32 {
	n := int(leadInSilence.Seconds() * float64(rate.Wideband.Hertz()))
	padded := make([]float32, n, n+len(sample))
	return append(padded, sample...)
}

// Recognize implements [Recognizer.Recognize] using whisper.cpp.
func (r *whisperRecognizer) Recognize(ctx context.Context, sample []float32, enableTranscriptionLogging bool) (string, error) {
	maxSize := int(simpleradio.MaxTransmissionDuration.Seconds()) * int(rate.Wideband.Hertz())
	if len(sample) > maxSize {
		log.Warn().Int("length", len(sample)).Int("maxLength", maxSize).Msg("clamping sample to maximum size")
		sample = sample[:maxSize]
	}

	sample = padLeadIn(sample)

	wCtx, err := r.model.NewContext()
	if err != nil {
		return "", fmt.Errorf("error creating whisper context: %w", err)
	}

	wCtx.SetInitialPrompt(r.initialPrompt(r.callsign))

	if wCtx.IsMultilingual() {
		_ = wCtx.SetLanguage("en")
	}

	err = wCtx.Process(
		sample,
		nil,
		func(segment whisper.Segment) {
			event := log.Debug()
			if enableTranscriptionLogging {
				event = event.Str("text", segment.Text)
			}
			event.Msg("processing segment")
		},
		nil,
	)
	if err != nil {
		return "", fmt.Errorf("error processing sample: %w", err)
	}

	var textBuilder strings.Builder
	for {
		select {
		case <-ctx.Done():
			log.Warn().Msg("returning early from speech recognition due to context cancellation")
			return textBuilder.String(), nil
		default:
			segment, err := wCtx.NextSegment()
			if errors.Is(err, io.EOF) {
				return textBuilder.String(), nil
			}
			if err != nil {
				return textBuilder.String(), fmt.Errorf("error processing segment: %w", err)
			}
			if _, err := textBuilder.WriteString(stripNonSpeech(segment.Text)); err != nil {
				return textBuilder.String(), fmt.Errorf("error writing segment text: %w", err)
			}
		}
	}
}
