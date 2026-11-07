package mvp

import (
	"context"
	"errors"
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
	if kind == "inbound" && t.RecordReception(text, outcome, timings) {
		return
	}
	mod := "AM"
	if t.Radio.Modulation == 1 {
		mod = "FM"
	}
	speaker := t.ClientName
	if kind == "reply_intended" || kind == "delivery" {
		speaker = "Athena"
	}
	r.Transcript.Log(transcript.Record{Kind: kind, ID: t.TraceID, FrequencyHz: uint64(t.Radio.Frequency), Modulation: mod, Speaker: speaker, Text: text, Outcome: outcome, Route: route, Part: part, Timings: timings})
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

func (r *Runner) streamReply(ctx, workCtx context.Context, t simpleradio.Transmission, req bridge.Request, expires time.Time, valid func() bool, client bridge.StreamClient, timing *Timing) {
	radio, ok := r.Radio.(interface {
		BeginMVPReply(simpleradio.Transmission) func(context.Context, simpleradio.Audio, time.Time) simpleradio.Delivery
	})
	if !ok {
		timing.Outcome = "stream_transport_unavailable"
		return
	}
	send := radio.BeginMVPReply(t)
	part := 0
	err := client.ExchangeStream(workCtx, req, func(resp bridge.Response) error {
		if !resp.Deliverable {
			timing.Outcome = "suppressed"
			return nil
		}
		part++
		r.record(t, "reply_intended", resp.Speech, "generated", "LLM", part, nil)
		timing.ReplyReceived = time.Since(t.ReceivedAt).Milliseconds()
		failure := func(reason string) error {
			timing.Outcome = reason
			r.record(t, "delivery", resp.Speech, reason, "LLM", part, nil)
			return errors.New(reason)
		}
		if !valid() {
			return failure(timing.Outcome)
		}
		audio, err := r.Synthesize(workCtx, resp.Speech)
		timing.TTSDone = time.Since(t.ReceivedAt).Milliseconds()
		if err != nil || len(audio) == 0 {
			return failure("tts_failed_or_empty")
		}
		if !valid() {
			return failure(timing.Outcome)
		}
		d := send(ctx, audio, expires)
		first, last := int64(-1), int64(-1)
		if !d.FirstSend.IsZero() {
			first = d.FirstSend.Sub(t.ReceivedAt).Milliseconds()
			if timing.FirstSend < 0 {
				timing.FirstSend = first
			}
		}
		if !d.LastSend.IsZero() {
			last = d.LastSend.Sub(t.ReceivedAt).Milliseconds()
			timing.LastSend = last
		}
		timing.Outcome = deliveryOutcome(d)
		r.record(t, "delivery", resp.Speech, timing.Outcome, "LLM", part, map[string]int64{"first_send": first, "last_send": last})
		if d.Outcome != "sent" {
			return errors.New("delivery_failed")
		}
		return nil
	})
	if err != nil {
		r.record(t, "stream_error", "", "bridge_or_delivery_failed", "LLM", part, nil)
		if timing.Outcome == "bridge_no_response" {
			timing.Outcome = "bridge_failed"
		}
	}
}
