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

// Intake reserves reply admission immediately, independently of the bounded
// serial STT queue. A busy capture never becomes a command later when idle.
func (r *Runner) runCapture(ctx context.Context, in <-chan simpleradio.Transmission) {
	type job struct {
		t     simpleradio.Transmission
		owned bool
	}
	jobs := make(chan job, 16)
	var worker, replies sync.WaitGroup
	worker.Go(func() {
		for j := range jobs {
			text, outcome := r.observe(ctx, j.t)
			if !j.owned {
				r.record(j.t, "route", "", "busy", "ignored", 0, nil)
				continue
			}
			replies.Go(func() { defer r.busy.Store(false); r.reply(ctx, j.t, text, outcome) })
		}
	})
	defer func() { close(jobs); worker.Wait(); replies.Wait() }()
	for {
		select {
		case <-ctx.Done():
			return
		case t, ok := <-in:
			if !ok {
				return
			}
			owned := r.busy.CompareAndSwap(false, true)
			select {
			case jobs <- job{t, owned}:
			default:
				if owned {
					r.busy.Store(false)
				}
				r.record(t, "inbound", "", "recognition_capacity", "ignored", 0, nil)
			}
		}
	}
}

func (r *Runner) record(t simpleradio.Transmission, kind, text, outcome, route string, part int, timings map[string]int64) {
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
	if t.ReceptionError != "" {
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
			return failure("expired_or_invalidated")
		}
		audio, err := r.Synthesize(workCtx, resp.Speech)
		timing.TTSDone = time.Since(t.ReceivedAt).Milliseconds()
		if err != nil || len(audio) == 0 {
			return failure("tts_failed_or_empty")
		}
		if !valid() {
			return failure("expired_or_invalidated")
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
		timing.Outcome = d.Outcome
		r.record(t, "delivery", resp.Speech, d.Outcome, "LLM", part, map[string]int64{"first_send": first, "last_send": last})
		if d.Outcome != "sent" {
			return errors.New("delivery_failed")
		}
		return nil
	})
	if err != nil {
		r.record(t, "stream_error", "", "bridge_or_delivery_failed", "LLM", part, nil)
		if timing.Outcome == "rejected" {
			timing.Outcome = "bridge_failed"
		}
	}
}
