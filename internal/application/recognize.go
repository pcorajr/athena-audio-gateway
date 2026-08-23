package application

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/pcm/rate"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/dharmab/skyeye/pkg/traces"
	"github.com/rs/zerolog/log"
)

// recognize runs speech recognition on audio received from SRS and forwards recognized text to the given channel.
//
// When an Athena command-channel gate is configured, every transmission is
// screened before recognition. Rejected transmissions are dropped here, so no
// transcript is ever produced for them and their audio is released without
// being read. See ADR 0014 and pkg/athena/admission.
func (a *Application) recognize(ctx context.Context, out chan<- Message[string]) {
	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("stopping speech recognition due to context cancellation")
			return
		case transmission := <-a.srsClient.Receive():
			if !a.admit(transmission) {
				continue
			}
			rCtx := context.Background()
			rCtx = traces.WithTraceID(rCtx, transmission.TraceID)
			rCtx = traces.WithClientName(rCtx, transmission.ClientName)
			rCtx = traces.WithReceivedAt(rCtx, time.Now())
			a.recognizeSample(ctx, rCtx, transmission.Audio, out)
		}
	}
}

// admit screens a transmission against the Athena command-channel gate.
//
// It returns true when the transmission may proceed to speech recognition. When
// no gate is configured the application is running as upstream SkyEye and every
// transmission proceeds, preserving existing behaviour.
//
// The log line is deliberately content-free: it records the stable rejection
// reason and the trace ID, never a transcript or any audio-derived value.
func (a *Application) admit(transmission simpleradio.Transmission) bool {
	if a.admissionGate == nil {
		return true
	}

	sampleCount := len(transmission.Audio)
	candidate := admission.Candidate{
		SpeakerName: transmission.ClientName,
		FrequencyHz: uint64(transmission.Radio.Frequency),
		Modulation:  admission.Modulation(transmission.Radio.Modulation),
		SampleCount: sampleCount,
		Duration:    sampleDuration(sampleCount),
	}

	decision := a.admissionGate.Admit(candidate)
	if !decision.Admitted {
		log.Info().
			Str("traceID", transmission.TraceID).
			Str("reason", string(decision.Rejection)).
			Msg("transmission rejected before speech recognition")
		return false
	}
	return true
}

// sampleDuration converts a PCM sample count to wall-clock duration at the SRS
// wideband sample rate.
func sampleDuration(samples int) time.Duration {
	if samples <= 0 {
		return 0
	}
	hz := rate.Wideband.Hertz()
	if hz <= 0 {
		return 0
	}
	return time.Duration(float64(samples) / hz * float64(time.Second))
}

// recognizeSample runs speech recognition on a single audio sample and forwards the recognized text to the output channel.
// The first context is the parent context of the process, and the second context is the context of the request.
// If the recognition process takes longer than 30 seconds, recognizeSample will log an error and return without publishing a message.
func (a *Application) recognizeSample(processCtx context.Context, requestCtx context.Context, audio simpleradio.Audio, out chan<- Message[string]) {
	recogizerCtx, cancel := context.WithTimeout(processCtx, 30*time.Second)
	defer func() {
		if recogizerCtx.Err() != nil && errors.Is(recogizerCtx.Err(), context.DeadlineExceeded) {
			a.trace(traces.WithRequestError(requestCtx, recogizerCtx.Err()))
		}
	}()
	defer cancel()

	if err := tryLock(processCtx, a.recognizerLock); err != nil {
		err := fmt.Errorf("unable to obtain recognizer lock: %w", err)
		log.Error().Err(err).Msg("error recognizing audio sample")
		a.trace(traces.WithRequestError(processCtx, err))
		return
	}

	defer unlock(a.recognizerLock)

	log.Info().Msg("recognizing audio sample")
	start := time.Now()
	text, err := a.recognizer.Recognize(recogizerCtx, audio, a.enableTranscriptionLogging)
	if err != nil {
		log.Error().Err(err).Msg("error recognizing audio sample")
		a.trace(traces.WithRequestError(processCtx, err))
		return
	}
	logger := log.With().Stringer("clockTime", time.Since(start)).Logger()

	requestCtx = traces.WithRecognizedAt(requestCtx, time.Now())
	requestCtx = traces.WithRequestText(requestCtx, text)
	if a.enableTranscriptionLogging {
		logger = logger.With().Str("text", text).Logger()
	}
	logger.Info().Msg("recognized audio")
	out <- AsMessage(requestCtx, text)
}
