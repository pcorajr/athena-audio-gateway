package simpleradio_test

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

func TestCancellationLateProducerAfterRunnerExit(t *testing.T) {
	_, tr, writes := simpleradio.NewSRSMVPFixtureClient(false, true)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	writer := &transcript.Writer{Root: t.TempDir()}
	observe, stopCapture := simpleradio.ObserveSRSMVPFixture(ctx, writer)
	defer stopCapture()
	var observed []simpleradio.Transmission
	for i := 0; i < 8; i++ {
		tr.TraceID = fmt.Sprintf("late-producer-%d", i)
		observed = append(observed, observe(tr))
	}
	in := make(chan simpleradio.Transmission, len(observed))
	for _, tr := range observed[:3] {
		in <- tr
	}
	cancel()
	runner := &mvp.Runner{Transcript: writer,
		Recognize: func(context.Context, simpleradio.Audio) (string, error) {
			t.Error("canceled queue invoked STT")
			return "", nil
		},
	}
	runner.Run(ctx, in)
	// Force the race that a nonblocking channel drain alone cannot solve:
	// the producer publishes already-observed work AFTER the consumer exits.
	for _, tr := range observed[3:] {
		in <- tr
	}
	stopCapture()
	// Late downstream completion cannot append a duplicate or resurrect text.
	for _, tr := range observed {
		tr.RecordReception("late STT", "recognized", nil)
	}
	paths, err := filepath.Glob(filepath.Join(writer.Root, "*.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	counts := map[string]int{}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			var rec transcript.Record
			if err := json.Unmarshal([]byte(line), &rec); err != nil {
				t.Fatal(err)
			}
			if rec.Kind != "inbound" {
				continue
			}
			counts[rec.ID]++
			if rec.Text != "" || rec.Outcome != "capture_canceled" {
				t.Fatalf("not canceled: %+v", rec)
			}
		}
	}
	if len(counts) != len(observed) {
		t.Fatalf("terminal IDs=%d want=%d", len(counts), len(observed))
	}
	for id, count := range counts {
		if count != 1 {
			t.Fatalf("%s terminal outcomes=%d", id, count)
		}
	}
	if writes() != 0 {
		t.Fatal("canceled traffic transmitted")
	}
}
