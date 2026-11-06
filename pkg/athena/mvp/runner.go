// Package mvp implements the serial, PTT-only audio exchange. Backends are
// injected; this package never loads a model or opens a network connection.
package mvp

import (
	"context"
	"strings"
	"sync/atomic"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/rs/zerolog/log"
)

const DefaultMaxAge = 10 * time.Second

// Radio is the synchronous identity-bound transport seam.
type Radio interface {
	ValidMVP(simpleradio.Transmission) bool
	TransmitMVP(context.Context, simpleradio.Transmission, time.Time) simpleradio.Delivery
}

// Timing contains monotonic millisecond offsets from assembled reception.
// Missing stages are -1; received=0 is not physical PTT release or audibility.
type Timing struct {
	TransmissionID                                                 string
	Received, STTDone, ReplyReceived, TTSDone, FirstSend, LastSend int64
	Outcome                                                        string
}

func LogTiming(t Timing) {
	log.Info().Str("transmission_id", t.TransmissionID).
		Int64("received", t.Received).Int64("stt_done", t.STTDone).
		Int64("reply_received", t.ReplyReceived).Int64("tts_done", t.TTSDone).
		Int64("first_send", t.FirstSend).Int64("last_send", t.LastSend).
		Str("outcome", t.Outcome).Msg("srs_mvp_timing_ms")
}

// Runner must be configured before use. Process rejects concurrent callers;
// the production receive channel also drops arrivals instead of queueing them.
type Runner struct {
	Gate               *admission.Gate
	Bridge             bridge.Client
	Radio              Radio
	MaxAge             time.Duration
	MaxSpeech          int
	Recognize          func(context.Context, simpleradio.Audio) (string, error)
	Synthesize         func(context.Context, string) (simpleradio.Audio, error)
	Record             func(Timing)
	busy               atomic.Bool
	lastTransmissionID string // guarded by busy; transport also consumes its route token
}

func (r *Runner) Run(ctx context.Context, in <-chan simpleradio.Transmission) {
	for {
		select {
		case <-ctx.Done():
			return
		case t, ok := <-in:
			if !ok {
				return
			}
			r.Process(ctx, t)
		}
	}
}

// Process owns one exchange through final write. Errors are never converted
// into substitute speech, retried, or logged with transcript/response contents.
func (r *Runner) Process(ctx context.Context, t simpleradio.Transmission) {
	if !r.busy.CompareAndSwap(false, true) {
		return
	}
	defer r.busy.Store(false)
	if t.TraceID == "" || t.TraceID == r.lastTransmissionID {
		return
	}
	r.lastTransmissionID = t.TraceID
	timing := Timing{TransmissionID: t.TraceID, Received: 0, STTDone: -1, ReplyReceived: -1, TTSDone: -1, FirstSend: -1, LastSend: -1, Outcome: "rejected"}
	defer func() {
		if r.Record != nil {
			r.Record(timing)
		} else {
			LogTiming(timing)
		}
	}()
	if r.Gate == nil || r.Radio == nil || r.Bridge == nil || r.Recognize == nil || r.Synthesize == nil || r.MaxAge <= 0 || t.ReceivedAt.IsZero() || t.ReceivedAt.After(time.Now()) {
		return
	}
	candidate := admission.Candidate{SpeakerName: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: admission.Modulation(t.Radio.Modulation), SampleCount: len(t.Audio), Duration: time.Duration(len(t.Audio)) * time.Second / 16000}
	if !r.Gate.Admit(candidate).Admitted || !r.Radio.ValidMVP(t) {
		return
	}
	expires := t.ReceivedAt.Add(r.MaxAge)
	workCtx, cancel := context.WithDeadline(ctx, expires)
	defer cancel()
	valid := func() bool { return workCtx.Err() == nil && time.Now().Before(expires) && r.Radio.ValidMVP(t) }
	if !valid() {
		timing.Outcome = "expired_or_invalidated"
		return
	}
	text, err := r.Recognize(workCtx, t.Audio)
	timing.STTDone = time.Since(t.ReceivedAt).Milliseconds()
	if err != nil || strings.TrimSpace(text) == "" {
		timing.Outcome = "stt_failed_or_empty"
		return
	}
	if !valid() {
		timing.Outcome = "expired_or_invalidated"
		return
	}
	modulation := bridge.ModulationAM
	if t.Radio.Modulation == 1 {
		modulation = bridge.ModulationFM
	}
	req := bridge.Request{TransmissionID: t.TraceID, Transcript: text, Speaker: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: modulation, ReceivedAt: t.ReceivedAt.UTC(), TranscribedAt: time.Now().UTC()}
	resp, err := r.Bridge.Exchange(workCtx, req)
	if err == nil {
		timing.ReplyReceived = time.Since(t.ReceivedAt).Milliseconds()
	}
	if err != nil || resp.ValidateMVP(req.TransmissionID, r.MaxSpeech) != nil {
		timing.Outcome = "bridge_failed"
		return
	}
	if !valid() {
		timing.Outcome = "expired_or_invalidated"
		return
	}
	if !resp.Deliverable {
		timing.Outcome = "suppressed"
		return
	}
	audio, err := r.Synthesize(workCtx, resp.Speech)
	timing.TTSDone = time.Since(t.ReceivedAt).Milliseconds()
	if err != nil || len(audio) == 0 {
		timing.Outcome = "tts_failed_or_empty"
		return
	}
	if !valid() {
		timing.Outcome = "expired_or_invalidated"
		return
	}
	t.Audio = audio
	// Use the process context here: expiry gates first send, not a cutoff
	// midway through a spoken qualification. Radio still guards each write.
	delivery := r.Radio.TransmitMVP(ctx, t, expires)
	if !delivery.FirstSend.IsZero() {
		timing.FirstSend = delivery.FirstSend.Sub(t.ReceivedAt).Milliseconds()
	}
	if !delivery.LastSend.IsZero() {
		timing.LastSend = delivery.LastSend.Sub(t.ReceivedAt).Milliseconds()
	}
	timing.Outcome = delivery.Outcome
}
