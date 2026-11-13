package mvp

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

func requireTextTiming(t *testing.T, root string, rec transcript.Record, keys ...string) {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(root, "*.txt"))
	if err != nil || len(paths) != 1 {
		t.Fatalf("TXT paths=%v err=%v", paths, err)
	}
	data, err := os.ReadFile(paths[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.Contains(line, "["+rec.Kind+" "+rec.ID+" ") {
			continue
		}
		if rec.Text == "" || !strings.Contains(line, rec.Text) {
			t.Errorf("missing speech in TXT/JSONL: %+v TXT=%q", rec, line)
		}
		for _, key := range keys {
			value, ok := rec.Timings[key]
			if !ok || !strings.Contains(line, fmt.Sprintf("%q:%d", key, value)) {
				t.Errorf("missing/mismatched %s timing: JSONL=%v TXT=%q", key, rec.Timings, line)
			}
		}
		return
	}
	t.Fatalf("missing TXT row %s/%s", rec.Kind, rec.ID)
}

// N2 ports the reviewer failure and checks each final TXT row against JSONL.
func TestUnpromptedTextTimingRegression(t *testing.T) {
	r, tr, _, _ := runnerFixture(t)
	r.Radio = &priorityRadio{sent: make(chan struct{})}
	r.Bridge = &threatBridge{}
	r.lastRadio = tr
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	r.playThreat(t.Context(), threatJob{last: tr, frame: bridge.Response{TransmissionID: "unprompted-timing", Speech: "SA-13 launch.", Priority: "launch"}})
	found := 0
	for _, rec := range records(t, r.Transcript.Root) {
		if rec.Kind == "unprompted_delivery" || rec.Kind == "exchange_outcome" {
			found++
			if rec.Outcome != "sent" || rec.Text != "SA-13 launch." {
				t.Errorf("unexpected record %+v", rec)
			}
			requireTextTiming(t, r.Transcript.Root, rec, "tts_done", "first_send", "last_send", "audio_ms")
		}
	}
	if found != 2 {
		t.Fatalf("final rows=%d", found)
	}
}

type interruptedRecordRadio struct{ cancel context.CancelCauseFunc }

func (*interruptedRecordRadio) ValidMVP(simpleradio.Transmission) bool { return true }
func (r *interruptedRecordRadio) TransmitMVP(_ context.Context, _ simpleradio.Transmission, _ time.Time) simpleradio.Delivery {
	first := time.Now()
	r.cancel(errLaunchInterrupt)
	return simpleradio.Delivery{Outcome: "canceled", FirstSend: first, LastSend: first}
}
func (r *interruptedRecordRadio) BeginMVPReply(tr simpleradio.Transmission) replySender {
	return func(ctx context.Context, audio simpleradio.Audio, expires time.Time) simpleradio.Delivery {
		tr.Audio = audio
		return r.TransmitMVP(ctx, tr, expires)
	}
}

func TestInterruptedTextTimingRegression(t *testing.T) {
	for _, streaming := range []bool{false, true} {
		t.Run(fmt.Sprintf("streaming_%v", streaming), func(t *testing.T) {
			r, tr, _, _ := runnerFixture(t)
			ctx, cancel := context.WithCancelCause(t.Context())
			defer cancel(nil)
			r.Radio = &interruptedRecordRadio{cancel: cancel}
			r.Transcript = &transcript.Writer{Root: t.TempDir()}
			const speech = "Synthetic interrupted reply."
			r.Bridge = bridgeFunc(func(_ context.Context, req bridge.Request) (bridge.Response, error) {
				return bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: speech, State: bridge.StateOK, IntentClass: bridge.IntentFactual, Priority: "normal"}, nil
			})
			if streaming {
				r.Bridge = replyParts(speech)
			}
			r.reply(ctx, tr, "Athena, radio check", "recognized")
			found := false
			for _, rec := range records(t, r.Transcript.Root) {
				if rec.Kind != "exchange_outcome" {
					continue
				}
				found = true
				if rec.Outcome != "interrupted" || rec.Text != speech {
					t.Errorf("interrupted record loses speech: %+v", rec)
				}
				requireTextTiming(t, r.Transcript.Root, rec, "tts_done", "first_send", "last_send")
			}
			if !found {
				t.Fatal("missing interrupted outcome")
			}
		})
	}
}

type protectedKillBridge struct {
	threatBridge
	started <-chan struct{}
}

func (b *protectedKillBridge) Exchange(_ context.Context, req bridge.Request) (bridge.Response, error) {
	return bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Prometheus, Athena, Confirmed, splash one Ural truck.", State: bridge.StateOK, IntentClass: bridge.IntentFactual, Route: "fixed", Priority: "threat"}, nil
}
func (b *protectedKillBridge) Poll(ctx context.Context, id, last string) (bridge.Response, error) {
	select {
	case <-b.started:
		return b.threatBridge.Poll(ctx, id, last)
	default:
		return bridge.Response{}, nil
	}
}

// M7: the original reviewer probe is immutable external evidence, not a valid
// protected-priority regression: it hardcodes normal and never releases the
// fake radio, so even protected playback is canceled by its shutdown timeout.
// Here the producer supplies threat; normal playback completion is released
// before shutdown. Go must honor priority, not interpret the speech text.
func TestLaunchDoesNotInterruptProtectedKillConfirmation(t *testing.T) {
	r, tr, _, _ := runnerFixture(t)
	radio := &priorityRadio{started: make(chan struct{}), release: make(chan struct{}), sent: make(chan struct{})}
	b := &protectedKillBridge{threatBridge: threatBridge{polled: make(chan struct{})}, started: radio.started}
	r.Radio, r.Bridge, r.Unprompted = radio, b, true
	r.Record = func(Timing) {}
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	in := make(chan simpleradio.Transmission, 1)
	in <- tr
	done := make(chan struct{})
	go func() { defer close(done); r.Run(ctx, in) }()
	defer func() { cancel(); <-done }()
	select {
	case <-b.polled:
	case <-ctx.Done():
		t.Fatal("launch was not polled during protected playback")
	}
	select {
	case <-radio.sent:
		t.Fatal("launch interrupted protected speech")
	case <-time.After(150 * time.Millisecond):
	}
	close(radio.release)
	select {
	case <-radio.sent:
	case <-ctx.Done():
		t.Fatal("launch did not follow completed protected speech")
	}
	close(in) // Gracefully finish both jobs, without cancellation.
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("scheduler did not finish normally")
	}
	radio.mu.Lock()
	cut := radio.interrupted
	radio.mu.Unlock()
	if cut {
		t.Fatal("protected speech was canceled")
	}
	outcomes := 0
	for _, rec := range records(t, r.Transcript.Root) {
		if rec.Kind == "exchange_outcome" {
			outcomes++
			if rec.Outcome != "sent" {
				t.Errorf("job did not complete normally: %+v", rec)
			}
		}
	}
	if outcomes != 2 {
		t.Fatalf("completed jobs=%d", outcomes)
	}
}
