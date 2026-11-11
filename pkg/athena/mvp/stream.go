package mvp

import (
	"context"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"strings"
	"sync"
	"time"
)

// Intake hands every capture to the serial STT worker. Recognized commands are
// answered strictly in arrival order by a single reply goroutine: a command
// that arrives while a reply is in flight is queued and answered next, never
// dropped (Athena Eye, 2026-11-07). Its expiry clock starts when its turn comes.
func (r *Runner) runCapture(ctx context.Context, in <-chan simpleradio.Transmission) {
	type job struct {
		t             simpleradio.Transmission
		text, outcome string
		ready         time.Time
	}
	jobs := make(chan simpleradio.Transmission, 16)
	answers := make(chan job, 16)
	var worker, replier sync.WaitGroup
	worker.Go(func() {
		defer close(answers)
		for t := range jobs {
			text, outcome := r.observe(ctx, t)
			if ctx.Err() != nil {
				continue
			}
			if r.busy.Load() || len(answers) > 0 {
				r.record(t, "route", "", "queued_behind_reply", "pending", 0, nil)
			}
			select {
			case answers <- job{t: t, text: text, outcome: outcome, ready: time.Now()}:
			default:
				r.record(t, "route", "", "reply_queue_full", "ignored", 0, nil)
			}
		}
	})
	replier.Go(func() {
		for j := range answers {
			if ctx.Err() != nil {
				continue
			}
			r.busy.Store(true)
			r.replyAt(ctx, j.t, j.text, j.outcome, j.t.ReceivedAt.Add(time.Since(j.ready)))
			r.busy.Store(false)
		}
	})
	defer func() { close(jobs); worker.Wait(); replier.Wait() }()
	defer func() {
		// Never wait for an arbitrary producer or drain an unbounded stream.
		// Production receipts independently cover sends racing this snapshot.
		for range cap(in) {
			select {
			case t, ok := <-in:
				if !ok {
					return
				}
				r.record(t, "inbound", "", "capture_canceled", "ignored", 0, nil)
			default:
				return
			}
		}
	}()
	for {
		if ctx.Err() != nil {
			return
		}
		select {
		case <-ctx.Done():
			return
		case t, ok := <-in:
			if !ok {
				return
			}
			if ctx.Err() != nil {
				r.record(t, "inbound", "", "capture_canceled", "ignored", 0, nil)
				return
			}
			select {
			case jobs <- t:
			default:
				r.record(t, "inbound", "", "recognition_capacity", "ignored", 0, nil)
			}
		}
	}
}

func (r *Runner) record(t simpleradio.Transmission, kind, text, outcome, route string, part int, timings map[string]int64) {
	r.log(t, transcript.Record{Kind: kind, Text: text, Outcome: outcome, Route: route, Part: part, Timings: timings})
}

// recordFrame records one reply frame with the adapter's labels (route, model).
func (r *Runner) recordFrame(t simpleradio.Transmission, kind string, resp bridge.Response, outcome string, part int, timings map[string]int64) {
	r.log(t, transcript.Record{Kind: kind, Text: resp.Speech, Outcome: outcome, Route: frameRoute(resp), Model: resp.Model, Part: part, Timings: timings})
}

func (r *Runner) log(t simpleradio.Transmission, rec transcript.Record) {
	if rec.Kind == "inbound" && t.RecordReception(rec.Text, rec.Outcome, rec.Timings) {
		return
	}
	rec.Modulation = "AM"
	if t.Radio.Modulation == 1 {
		rec.Modulation = "FM"
	}
	rec.Speaker = t.ClientName
	if rec.Kind == "reply_intended" || rec.Kind == "delivery" {
		rec.Speaker = "Athena"
	}
	rec.ID, rec.FrequencyHz = t.TraceID, uint64(t.Radio.Frequency)
	r.Transcript.Log(rec)
}

func (r *Runner) observe(ctx context.Context, t simpleradio.Transmission) (string, string) {
	text, outcome := "", "recognized"
	start := time.Now()
	if ctx.Err() != nil {
		outcome = "capture_canceled"
	} else if t.ReceptionError != "" {
		outcome = t.ReceptionError
	} else if r.Recognize == nil || len(t.Audio) == 0 || len(t.Audio) > 16000*30 {
		outcome = "audio_unavailable_or_oversize"
	} else {
		budget := r.MaxAge
		if budget <= 0 {
			budget = DefaultMaxAge
		}
		sttCtx, cancel := context.WithTimeout(ctx, budget)
		var err error
		text, err = r.Recognize(sttCtx, t.Audio)
		if err != nil || sttCtx.Err() != nil {
			text, outcome = "", "stt_failed"
		} else if strings.TrimSpace(text) == "" {
			outcome = "stt_empty"
		}
		cancel()
	}
	if ctx.Err() != nil {
		text, outcome = "", "capture_canceled"
	}
	r.record(t, "inbound", text, outcome, "pending", 0, map[string]int64{"stt": time.Since(start).Milliseconds()})
	return text, outcome
}

// Fixed spoken lines used instead of silence when a reply cannot be spoken.
// Both must stay far below the audio limit (phase 0, 2026-11-10).
const (
	TooLongLine  = "Prometheus, Athena, say again, my answer was too long."
	SayAgainLine = "Prometheus, Athena, say again."
	// continuationGrace lets part 2 follow part 1 even when part 1 itself ran
	// past the first-send expiry: expiry gates starting a reply, not finishing it.
	continuationGrace = 5 * time.Second
	fallbackTimeout   = 5 * time.Second
)

type replySender = func(context.Context, simpleradio.Audio, time.Time) simpleradio.Delivery

// streamReply reads the WHOLE bounded NDJSON reply first, so the bridge HTTP
// timeout covers only the HTTP exchange, never playback. It then synthesizes
// every part, checks the total audio length, and only then speaks. If the
// reply cannot be spoken (bridge error, TTS failure, too long) Athena says a
// short fixed line instead of going silent. Outcome is "sent" only when every
// part was sent.
func (r *Runner) streamReply(ctx, workCtx context.Context, t simpleradio.Transmission, req bridge.Request, expires time.Time, valid func() bool, client bridge.StreamClient, timing *Timing) {
	radio, ok := r.Radio.(interface {
		BeginMVPReply(simpleradio.Transmission) replySender
	})
	if !ok {
		timing.Outcome = "stream_transport_unavailable"
		return
	}
	send := radio.BeginMVPReply(t)
	var parts []bridge.Response
	err := client.ExchangeStream(workCtx, req, func(resp bridge.Response) error {
		parts = append(parts, resp)
		return nil
	})
	if err != nil {
		r.record(t, "stream_error", "", "bridge_or_delivery_failed", "LLM", len(parts), nil)
		timing.Outcome = "bridge_failed"
		r.fallback(ctx, t, send, expires, SayAgainLine, "say_again_fallback", timing)
		return
	}
	timing.ReplyReceived = time.Since(t.ReceivedAt).Milliseconds()
	if len(parts) == 0 || !parts[0].Deliverable {
		timing.Outcome = "suppressed"
		return
	}
	timing.Route, timing.Model = frameRoute(parts[0]), parts[0].Model
	for i, resp := range parts {
		r.recordFrame(t, "reply_intended", resp, "generated", i+1, map[string]int64{"reply_received": timing.ReplyReceived})
	}
	if !valid() {
		for i, resp := range parts {
			r.recordFrame(t, "delivery", resp, timing.Outcome, i+1, nil)
		}
		return
	}
	audios := make([]simpleradio.Audio, len(parts))
	samples := 0
	for i, resp := range parts {
		audio, err := r.Synthesize(workCtx, resp.Speech)
		timing.TTSDone = time.Since(t.ReceivedAt).Milliseconds()
		if err != nil || len(audio) == 0 {
			if ctx.Err() != nil {
				timing.Outcome = "canceled"
				return
			}
			timing.Outcome = "tts_failed_or_empty"
			r.recordFrame(t, "delivery", resp, timing.Outcome, i+1, nil)
			r.fallback(ctx, t, send, expires, SayAgainLine, "say_again_fallback", timing)
			return
		}
		audios[i] = audio
		samples += len(audio)
	}
	if samples > simpleradio.MaxMVPReplySamples {
		timing.Outcome = "speech_duration_exceeded"
		for i, resp := range parts {
			r.recordFrame(t, "delivery", resp, timing.Outcome, i+1, map[string]int64{"audio_ms": int64(len(audios[i])) * 1000 / 16000})
		}
		r.fallback(ctx, t, send, expires, TooLongLine, "too_long_fallback", timing)
		return
	}
	if !valid() {
		for i, resp := range parts {
			r.recordFrame(t, "delivery", resp, timing.Outcome, i+1, nil)
		}
		return
	}
	for i, resp := range parts {
		partExpires := expires
		if i > 0 {
			partExpires = later(expires, time.Now().Add(continuationGrace))
		}
		d := send(ctx, audios[i], partExpires)
		r.noteSend(t, d, timing)
		timing.Outcome = deliveryOutcome(d)
		r.recordFrame(t, "delivery", resp, timing.Outcome, i+1, map[string]int64{"first_send": sinceMs(t, d.FirstSend), "last_send": sinceMs(t, d.LastSend)})
		if d.Outcome != "sent" {
			// The sender is closed after a failed write; nothing more can be keyed.
			for j := i + 1; j < len(parts); j++ {
				r.recordFrame(t, "delivery", parts[j], "not_sent_after_failure", j+1, nil)
			}
			return
		}
	}
}

// fallback speaks one short fixed line on the still-unused reply sender. A
// "say again" is never stale, so it may start slightly after the expiry.
func (r *Runner) fallback(ctx context.Context, t simpleradio.Transmission, send replySender, expires time.Time, line, label string, timing *Timing) {
	if ctx.Err() != nil {
		timing.Outcome = "canceled"
		return
	}
	reason := timing.Outcome
	synthCtx, cancel := context.WithTimeout(ctx, fallbackTimeout)
	audio, err := r.Synthesize(synthCtx, line)
	cancel()
	if err != nil || len(audio) == 0 {
		timing.Outcome = label + "_tts_failed:" + reason
		r.record(t, "delivery", line, timing.Outcome, "fallback", 0, nil)
		return
	}
	d := send(ctx, audio, later(expires, time.Now().Add(continuationGrace)))
	r.noteSend(t, d, timing)
	if d.Outcome == "sent" {
		timing.Outcome = label + "_sent"
	} else {
		timing.Outcome = label + "_failed:" + deliveryOutcome(d)
	}
	r.record(t, "delivery", line, timing.Outcome+":"+reason, "fallback", 0, map[string]int64{"first_send": sinceMs(t, d.FirstSend), "last_send": sinceMs(t, d.LastSend)})
}

func (r *Runner) noteSend(t simpleradio.Transmission, d simpleradio.Delivery, timing *Timing) {
	if !d.FirstSend.IsZero() && timing.FirstSend < 0 {
		timing.FirstSend = sinceMs(t, d.FirstSend)
	}
	if !d.LastSend.IsZero() {
		timing.LastSend = sinceMs(t, d.LastSend)
	}
}

func sinceMs(t simpleradio.Transmission, at time.Time) int64 {
	if at.IsZero() {
		return -1
	}
	return at.Sub(t.ReceivedAt).Milliseconds()
}

func later(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}
