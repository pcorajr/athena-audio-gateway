package simpleradio

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
)

func captureRecords(t *testing.T, root string) []transcript.Record {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(root, "*.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	var records []transcript.Record
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
			records = append(records, rec)
		}
	}
	return records
}

func TestCancellationAccountsForBufferedDecode(t *testing.T) {
	c, tr, _ := mvpTestClient()
	c.transcript = &transcript.Writer{Root: t.TempDir()}
	in := make(chan transmissionPackets, 8)
	for i := 0; i < cap(in); i++ {
		received := transmissionPackets{traceID: fmt.Sprintf("decode-%d", i), origin: tr.route.origin, radio: tr.Radio}
		c.captureRecord(received, "received", "assembling")
		in <- received
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	c.decodeVoice(ctx, in)
	counts := map[string]int{}
	for _, rec := range captureRecords(t, c.transcript.Root) {
		if rec.Kind == "inbound" {
			counts[rec.ID]++
			if rec.Text != "" || rec.Outcome != "capture_canceled" {
				t.Errorf("not canceled: %+v", rec)
			}
		}
	}
	if len(counts) != cap(in) {
		t.Fatalf("terminal IDs=%d want=%d", len(counts), cap(in))
	}
	for id, n := range counts {
		if n != 1 {
			t.Errorf("%s terminal outcomes=%d", id, n)
		}
	}
}
