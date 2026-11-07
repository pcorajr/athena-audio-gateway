package mvp

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

func TestCancellationAccountsForBufferedReceive(t *testing.T) {
	r, tr, radio, _ := runnerFixture(t)
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	r.Recognize = func(context.Context, simpleradio.Audio) (string, error) {
		t.Error("canceled reception entered STT")
		return "Athena, synthetic", nil
	}
	in := make(chan simpleradio.Transmission, 8)
	for i := 0; i < cap(in); i++ {
		tr.TraceID = fmt.Sprintf("buffered-%d", i)
		in <- tr
		r.Transcript.Log(transcript.Record{Kind: "received", ID: tr.TraceID, Outcome: "assembling"})
	}
	// Intentionally open: cancellation must not wait for an arbitrary producer.
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	r.Run(ctx, in)
	assertCanceled(t, r.Transcript.Root, cap(in))
	if radio.sends != 0 {
		t.Fatal("canceled backlog replied")
	}
}

func TestCancellationAccountsForBufferedIntake(t *testing.T) {
	r, tr, radio, _ := runnerFixture(t)
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	entered := make(chan struct{})
	var once sync.Once
	r.Recognize = func(ctx context.Context, _ simpleradio.Audio) (string, error) {
		if ctx.Err() != nil {
			t.Error("queued canceled reception entered STT")
		}
		once.Do(func() { close(entered) })
		<-ctx.Done()
		return "must not retain canceled text", nil
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	in := make(chan simpleradio.Transmission)
	done := make(chan struct{})
	go func() { defer close(done); r.Run(ctx, in) }()
	in <- tr
	<-entered
	for i := 0; i < 8; i++ {
		tr.TraceID = fmt.Sprintf("intake-%d", i)
		in <- tr // handshake proves accepted into the intake while STT is blocked
	}
	cancel()
	<-done
	assertCanceled(t, r.Transcript.Root, 9)
	if radio.sends != 0 {
		t.Fatal("canceled intake replied")
	}
}

func assertCanceled(t *testing.T, root string, want int) {
	t.Helper()
	counts := map[string]int{}
	for _, record := range records(t, root) {
		if record.Kind != "inbound" {
			continue
		}
		counts[record.ID]++
		if record.Text != "" || record.Outcome != "capture_canceled" {
			t.Errorf("not an explicit blank cancellation: %+v", record)
		}
	}
	if len(counts) != want {
		t.Fatalf("terminal IDs=%d want=%d", len(counts), want)
	}
	for id, count := range counts {
		if count != 1 {
			t.Errorf("%s terminal outcomes=%d", id, count)
		}
	}
}
