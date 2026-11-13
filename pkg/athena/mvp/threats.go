package mvp

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

var errLaunchInterrupt = errors.New("new_launch_interrupt")

type replyJob struct {
	t             simpleradio.Transmission
	text, outcome string
	ready         time.Time
}
type threatJob struct {
	last  simpleradio.Transmission
	frame bridge.Response
}

func sameAsk(text string) string {
	text = strings.ToLower(text)
	text = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			return r
		}
		return ' '
	}, text)
	text = strings.Join(strings.Fields(strings.TrimPrefix(strings.TrimSpace(text), "athena")), " ")
	if text == "sam launch sam launch" {
		return "sam launch"
	}
	return text
}

// rememberRoute observes an already admitted, recognized pilot transmission.
// The adapter binds it to its DCS session; no synthetic request opens the gate.
func (r *Runner) rememberRoute(ctx context.Context, t simpleradio.Transmission, text, outcome string) {
	client, ok := r.Bridge.(bridge.ThreatClient)
	if !r.Unprompted || !ok || outcome != "recognized" || strings.TrimSpace(text) == "" || r.configurationRejection(t) != "" || r.radioRejection(t) != "" {
		return
	}
	candidate := admission.Candidate{SpeakerName: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: admission.Modulation(t.Radio.Modulation), SampleCount: len(t.Audio), Duration: time.Duration(len(t.Audio)) * time.Second / 16000}
	if !r.Gate.Admit(candidate).Admitted {
		return
	}
	mod := bridge.ModulationAM
	if t.Radio.Modulation == 1 {
		mod = bridge.ModulationFM
	}
	req := bridge.Request{TransmissionID: t.TraceID, Transcript: text, Speaker: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: mod, ReceivedAt: t.ReceivedAt.UTC(), TranscribedAt: time.Now().UTC()}
	short, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if client.Remember(short, req) != nil {
		return
	}
	r.lastMu.Lock()
	r.lastRadio = t
	r.lastMu.Unlock()
}

func (r *Runner) pollThreats(ctx context.Context, out chan<- threatJob) {
	client, ok := r.Bridge.(bridge.ThreatClient)
	if !r.Unprompted || !ok {
		return
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.lastMu.RLock()
			last := r.lastRadio
			r.lastMu.RUnlock()
			if last.TraceID == "" || r.radioRejection(last) != "" {
				continue
			}
			id := fmt.Sprintf("unprompted-%d", time.Now().UnixNano())
			short, cancel := context.WithTimeout(ctx, time.Second)
			frame, err := client.Poll(short, id, last.TraceID)
			cancel()
			if err != nil || !frame.Deliverable {
				continue
			}
			select {
			case out <- threatJob{last, frame}:
			case <-ctx.Done():
				r.receipt(id, "failed")
				return
			}
		}
	}
}

func (r *Runner) receipt(id, outcome string) {
	if client, ok := r.Bridge.(bridge.ThreatClient); ok {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_, _ = client.Receipt(ctx, id, outcome)
	}
}

// schedule serializes actual speech. A launch jumps the queue and cancels only
// a positively classified non-threat reply. Unknown/old frames are protected.
func (r *Runner) schedule(ctx context.Context, answers <-chan replyJob) {
	polling, stop := context.WithCancel(ctx)
	defer stop()
	alerts := make(chan threatJob, 1)
	pollDone := make(chan struct{})
	go func() { defer close(pollDone); r.pollThreats(polling, alerts) }()
	defer func() { stop(); <-pollDone }()
	queue := []replyJob{}
	threats := []threatJob{}
	done := make(chan struct{}, 1)
	var cancel context.CancelCauseFunc
	tick := time.NewTicker(20 * time.Millisecond)
	defer tick.Stop()
	drop := func(j replyJob, why string) { r.record(j.t, "exchange_outcome", j.text, why, "ignored", 0, nil) }
	for {
		// Drain ready launches before starting ANY queued pilot reply.
		select {
		case a := <-alerts:
			threats = append(threats, a)
		default:
		}
		sort.SliceStable(threats, func(i, j int) bool {
			return threats[i].frame.Priority == "launch" && threats[j].frame.Priority != "launch"
		})
		if ctx.Err() != nil {
			if cancel != nil {
				cancel(ctx.Err())
				<-done
			}
			for _, j := range queue {
				drop(j, "canceled")
			}
			for _, a := range threats {
				r.receipt(a.frame.TransmissionID, "failed")
			}
			return
		}
		if cancel != nil && len(threats) > 0 && threats[0].frame.Priority == "launch" && !r.activeThreat.Load() {
			cancel(errLaunchInterrupt)
		}
		if cancel == nil && (len(queue) > 0 || len(threats) > 0) {
			jobCtx, stopJob := context.WithCancelCause(ctx)
			cancel = stopJob
			r.activeThreat.Store(true)
			r.busy.Store(true)
			if len(threats) > 0 {
				a := threats[0]
				threats = threats[1:]
				go func() { r.playThreat(jobCtx, a); done <- struct{}{} }()
			} else {
				j := queue[0]
				queue = queue[1:]
				go func() {
					r.replyAt(jobCtx, j.t, j.text, j.outcome, j.t.ReceivedAt.Add(time.Since(j.ready)))
					done <- struct{}{}
				}()
			}
		}
		if answers == nil && cancel == nil && len(queue) == 0 && len(threats) == 0 {
			return
		}
		select {
		case <-ctx.Done():
		case <-tick.C:
		case <-done:
			cancel(nil)
			cancel = nil
			r.busy.Store(false)
		case a := <-alerts:
			threats = append(threats, a)
		case j, ok := <-answers:
			if !ok {
				answers = nil
				continue
			}
			key := sameAsk(j.text)
			kept := queue[:0]
			for _, old := range queue {
				if key != "" && key == sameAsk(old.text) && j.outcome == "recognized" && old.outcome == "recognized" && j.t.Radio == old.t.Radio && j.t.ClientName == old.t.ClientName {
					drop(old, "superseded")
				} else {
					kept = append(kept, old)
				}
			}
			queue = kept
			if len(queue) >= 16 {
				drop(j, "reply_queue_full")
			} else {
				queue = append(queue, j)
			}
		}
	}
}

func (r *Runner) playThreat(ctx context.Context, j threatJob) {
	frame := j.frame
	outcome := "failed"
	defer func() { r.receipt(frame.TransmissionID, outcome) }()
	radio, ok := r.Radio.(interface {
		UnpromptedMVP(simpleradio.Transmission, string) (simpleradio.Transmission, bool)
	})
	client, hasClient := r.Bridge.(bridge.ThreatClient)
	if !ok || !hasClient {
		return
	}
	r.lastMu.RLock()
	latest := r.lastRadio
	r.lastMu.RUnlock()
	if latest.TraceID != j.last.TraceID {
		return
	}
	t, ok := radio.UnpromptedMVP(j.last, frame.TransmissionID)
	if !ok {
		return
	}
	start := time.Now()
	timing := map[string]int64{"received": 0, "reply_received": 0, "tts_done": -1, "first_send": -1, "last_send": -1}
	defer func() {
		r.log(t, transcript.Record{Kind: "exchange_outcome", Route: "unprompted", Text: frame.Speech, Outcome: outcome, Timings: timing})
	}()
	if r.Transcript == nil {
		return
	}
	mod := "AM"
	if t.Radio.Modulation == 1 {
		mod = "FM"
	}
	// A failed transcript means silence; never knowingly put unrecorded speech on air.
	if r.Transcript.Append(transcript.Record{Kind: "unprompted_intended", ID: t.TraceID, Speaker: "Athena", FrequencyHz: uint64(t.Radio.Frequency), Modulation: mod, Route: "unprompted", Text: frame.Speech, Outcome: "reserved", Timings: timing}) != nil {
		return
	}
	work, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	audio, err := r.Synthesize(work, frame.Speech)
	timing["tts_done"] = time.Since(start).Milliseconds()
	if err != nil || len(audio) == 0 || len(audio) > simpleradio.MaxMVPReplySamples || work.Err() != nil {
		return
	}
	valid, err := client.Receipt(work, t.TraceID, "check")
	if err != nil || !valid {
		return
	}
	t.Audio = audio
	d := r.Radio.TransmitMVP(ctx, t, time.Now().Add(time.Second))
	outcome = deliveryOutcome(d)
	timing["first_send"], timing["last_send"] = sinceMs(t, d.FirstSend), sinceMs(t, d.LastSend)
	timing["audio_ms"] = int64(len(audio)) * 1000 / 16000
	r.record(t, "unprompted_delivery", frame.Speech, outcome, "unprompted", 1, timing)
	if outcome != "sent" {
		outcome = "failed"
	}
}
