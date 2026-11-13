// Package mvp implements the serial, PTT-only audio exchange. Backends are
// injected; this package never loads a model or opens a network connection.
package mvp

import (
	"context"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
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
	// Route and Model are the adapter's labels of the reply (empty: none sent).
	Route, Model string
}

func LogTiming(t Timing) {
	log.Info().Str("transmission_id", t.TransmissionID).
		Int64("received", t.Received).Int64("stt_done", t.STTDone).
		Int64("reply_received", t.ReplyReceived).Int64("tts_done", t.TTSDone).
		Int64("first_send", t.FirstSend).Int64("last_send", t.LastSend).
		Str("outcome", t.Outcome).Msg("srs_mvp_timing_ms")
}

// Runner must be configured before use. Capture stays separate from reply
// admission: in Run, busy arrivals are queued and answered in order; the
// synchronous Process API still answers only one exchange at a time.
type Runner struct {
	Unprompted         bool
	activeThreat       atomic.Bool
	lastMu             sync.RWMutex
	lastRadio          simpleradio.Transmission
	Gate               *admission.Gate
	Bridge             bridge.Client
	Radio              Radio
	MaxAge             time.Duration
	MaxSpeech          int
	Recognize          func(context.Context, simpleradio.Audio) (string, error)
	Synthesize         func(context.Context, string) (simpleradio.Audio, error)
	Record             func(Timing)
	Transcript         *transcript.Writer
	busy               atomic.Bool
	lastTransmissionID string // guarded by busy; transport also consumes its route token
	// testPilotAt is when the last deliverable adapter frame said test_pilot
	// (unix nanoseconds); 0 = the last frame did not say so, or none was seen.
	testPilotAt atomic.Int64
}

func (r *Runner) Run(ctx context.Context, in <-chan simpleradio.Transmission) {
	r.runCapture(ctx, in)
}

// Process captures one transmission and, if idle/admitted, owns its reply.
// Detailed text goes only to the private transcript writer, not general logs.
func (r *Runner) Process(ctx context.Context, t simpleradio.Transmission) {
	owned := r.busy.CompareAndSwap(false, true)
	text, outcome := r.observe(ctx, t)
	if !owned {
		r.record(t, "route", "", "busy", "ignored", 0, nil)
		return
	}
	defer r.busy.Store(false)
	r.reply(ctx, t, text, outcome)
}

func (r *Runner) reply(ctx context.Context, t simpleradio.Transmission, text, recognition string) {
	r.replyAt(ctx, t, text, recognition, t.ReceivedAt)
}

// replyAt answers t with its MaxAge expiry measured from expiryBase (the
// reception time, shifted by any time spent queued behind a previous reply).
func (r *Runner) replyAt(ctx context.Context, t simpleradio.Transmission, text, recognition string, expiryBase time.Time) {
	if t.TraceID == "" {
		r.record(t, "route", "", "missing_transmission_id", "ignored", 0, nil)
		return
	}
	if t.TraceID == r.lastTransmissionID {
		r.record(t, "route", "", "transmission_replayed", "ignored", 0, nil)
		return
	}
	r.lastTransmissionID = t.TraceID
	var speech string // Private transcript only, never the content-free timing log.
	timing := Timing{TransmissionID: t.TraceID, Received: 0, STTDone: -1, ReplyReceived: -1, TTSDone: -1, FirstSend: -1, LastSend: -1, Outcome: "bridge_no_response"}
	defer func() {
		if context.Cause(ctx) == errLaunchInterrupt {
			timing.Outcome = "interrupted"
		}
		route, model := exchangeRoute(timing), timing.Model
		if route != timing.Route && timing.Route != "" {
			model = "" // a gateway fallback: the adapter's words were not spoken
		}
		r.log(t, transcript.Record{Kind: "exchange_outcome", Text: speech, Outcome: timing.Outcome, Route: route, Model: model, Timings: map[string]int64{"stt_done": timing.STTDone, "reply_received": timing.ReplyReceived, "tts_done": timing.TTSDone, "first_send": timing.FirstSend, "last_send": timing.LastSend}})
		outcome := "failed"
		if timing.Outcome == "sent" || timing.Outcome == "interrupted" {
			outcome = timing.Outcome
		}
		r.receipt(t.TraceID, outcome)
		if r.Record != nil {
			r.Record(timing)
		} else {
			LogTiming(timing)
		}
	}()
	if reason := r.configurationRejection(t); reason != "" {
		timing.Outcome = "rejected:" + reason
		return
	}
	candidate := admission.Candidate{SpeakerName: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: admission.Modulation(t.Radio.Modulation), SampleCount: len(t.Audio), Duration: time.Duration(len(t.Audio)) * time.Second / 16000}
	if decision := r.Gate.Admit(candidate); !decision.Admitted {
		timing.Outcome = "rejected:admission_" + string(decision.Rejection)
		return
	}
	if reason := r.radioRejection(t); reason != "" {
		timing.Outcome = "rejected:" + reason
		return
	}
	expires := expiryBase.Add(r.MaxAge)
	workCtx, cancel := context.WithDeadline(ctx, expires)
	defer cancel()
	valid := func() bool {
		switch {
		case ctx.Err() != nil:
			timing.Outcome = "canceled"
		case workCtx.Err() != nil || !time.Now().Before(expires):
			timing.Outcome = "expired"
		default:
			if reason := r.radioRejection(t); reason != "" {
				timing.Outcome = reason
			} else {
				return true
			}
		}
		return false
	}
	if !valid() {
		return
	}
	timing.STTDone = time.Since(t.ReceivedAt).Milliseconds()
	if recognition != "recognized" || strings.TrimSpace(text) == "" {
		timing.Outcome = "stt_failed_or_empty"
		return
	}
	if !valid() {
		return
	}
	modulation := bridge.ModulationAM
	if t.Radio.Modulation == 1 {
		modulation = bridge.ModulationFM
	}
	req := bridge.Request{TransmissionID: t.TraceID, Transcript: text, Speaker: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: modulation, ReceivedAt: t.ReceivedAt.UTC(), TranscribedAt: time.Now().UTC()}
	if streaming, ok := r.Bridge.(bridge.StreamClient); ok {
		r.streamReply(ctx, workCtx, t, req, expires, valid, streaming, &timing, &speech)
		return
	}
	resp, err := r.Bridge.Exchange(workCtx, req)
	if err == nil {
		timing.ReplyReceived = time.Since(t.ReceivedAt).Milliseconds()
	}
	if err != nil || resp.ValidateMVP(req.TransmissionID, r.MaxSpeech) != nil {
		timing.Outcome = "bridge_failed"
		return
	}
	if !valid() {
		return
	}
	if !resp.Deliverable {
		timing.Outcome = "suppressed"
		return
	}
	speech = resp.Speech
	timing.Route, timing.Model = frameRoute(resp), resp.Model
	r.activeThreat.Store(resp.Priority != "normal")
	r.recordFrame(t, "reply_intended", resp, "generated", 1, map[string]int64{"reply_received": timing.ReplyReceived})
	audio, err := r.Synthesize(workCtx, resp.Speech)
	timing.TTSDone = time.Since(t.ReceivedAt).Milliseconds()
	if err != nil || len(audio) == 0 {
		timing.Outcome = "tts_failed_or_empty"
		return
	}
	if !valid() {
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
	timing.Outcome = deliveryOutcome(delivery)
	r.recordFrame(t, "delivery", resp, timing.Outcome, 1, map[string]int64{"first_send": timing.FirstSend, "last_send": timing.LastSend})
}

// exchangeRoute labels the exchange_outcome record. A spoken (or attempted)
// "say again" / "too long" fallback is never "ignored": the pilot heard Athena.
func exchangeRoute(t Timing) string {
	switch {
	case strings.HasPrefix(t.Outcome, "say_again_fallback"), strings.HasPrefix(t.Outcome, "too_long_fallback"):
		return "fallback"
	case t.ReplyReceived >= 0 && t.Route != "":
		return t.Route
	case t.ReplyReceived >= 0:
		return "LLM"
	}
	return "ignored"
}

// frameRoute is the adapter's route label of a reply frame (fixed, llm,
// debrief or fallback); a frame from an adapter that sends none stays "LLM".
func frameRoute(resp bridge.Response) string {
	if resp.Route == "" {
		return "LLM"
	}
	return resp.Route
}
func deliveryOutcome(d simpleradio.Delivery) string {
	if d.Reason != "" {
		return d.Outcome + ":" + d.Reason
	}
	return d.Outcome
}
