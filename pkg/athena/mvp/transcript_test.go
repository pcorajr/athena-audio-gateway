package mvp

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

func records(t *testing.T, root string) []transcript.Record {
	t.Helper()
	paths, _ := filepath.Glob(filepath.Join(root, "*.jsonl"))
	var result []transcript.Record
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			var record transcript.Record
			if json.Unmarshal([]byte(line), &record) != nil {
				t.Fatal("bad record")
			}
			result = append(result, record)
		}
	}
	return result
}

func TestTranscriptBeforeAdmissionAndSTTFailure(t *testing.T) {
	for _, mode := range []string{"normal", "other", "empty", "stt_error", "tts_error", "bridge_error", "invalid_route"} {
		t.Run(mode, func(t *testing.T) {
			r, tr, radio, _ := runnerFixture(t)
			r.Transcript = &transcript.Writer{Root: t.TempDir()}
			switch mode {
			case "other":
				tr.ClientName = "Other pilot"
			case "empty":
				r.Recognize = func(context.Context, simpleradio.Audio) (string, error) { return "", nil }
			case "stt_error":
				r.Recognize = func(context.Context, simpleradio.Audio) (string, error) { return "", errors.New("secret-error-body") }
			case "tts_error":
				r.Synthesize = func(context.Context, string) (simpleradio.Audio, error) { return nil, errors.New("secret-error-body") }
			case "bridge_error":
				r.Bridge = bridgeFunc(func(context.Context, bridge.Request) (bridge.Response, error) {
					return bridge.Response{}, errors.New("secret-error-body")
				})
			case "invalid_route":
				radio.valid.Store(false)
			}
			r.Process(t.Context(), tr)
			got := records(t, r.Transcript.Root)
			inbound := 0
			for _, record := range got {
				if record.Kind == "inbound" {
					inbound++
					if mode == "other" && record.Text == "" {
						t.Fatal("nonpilot STT skipped")
					}
				}
				raw, _ := json.Marshal(record)
				if strings.Contains(string(raw), "secret-error-body") {
					t.Fatal("error body leaked")
				}
			}
			if inbound != 1 {
				t.Fatalf("inbound=%d", inbound)
			}
			if mode != "normal" && radio.sends != 0 {
				t.Fatal("failed or ignored input spoke")
			}
		})
	}
}

func TestBusyReceptionSupersedesIdenticalQueuedAsk(t *testing.T) {
	r, tr, radio, _ := runnerFixture(t)
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var once sync.Once
	var recognized atomic.Int32
	r.Recognize = func(context.Context, simpleradio.Audio) (string, error) {
		recognized.Add(1)
		return "Athena, synthetic", nil
	}
	base := r.Bridge
	var mu sync.Mutex
	var order []string
	r.Bridge = bridgeFunc(func(ctx context.Context, req bridge.Request) (bridge.Response, error) {
		mu.Lock()
		order = append(order, req.TransmissionID)
		mu.Unlock()
		once.Do(func() { close(entered) })
		<-release
		return base.Exchange(ctx, req)
	})
	in := make(chan simpleradio.Transmission)
	go func() { defer close(done); r.Run(t.Context(), in) }()
	in <- tr
	<-entered
	tr.TraceID = "busy-second"
	in <- tr
	tr.TraceID = "busy-third"
	in <- tr
	deadline := time.After(time.Second)
	for recognized.Load() != 3 {
		select {
		case <-deadline:
			t.Fatal("busy traffic not transcribed")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	time.Sleep(20 * time.Millisecond)
	close(release)
	close(in)
	<-done
	if radio.sends != 2 {
		t.Fatalf("queued commands not answered: sends=%d", radio.sends)
	}
	if strings.Join(order, ",") != "synthetic-1,busy-third" {
		t.Fatalf("answer order %v", order)
	}
	inbound, queued, superseded := 0, 0, 0
	for _, record := range records(t, r.Transcript.Root) {
		if record.ID == "busy-second" && record.Outcome == "superseded" {
			superseded++
		}
		if record.Kind == "inbound" {
			inbound++
		}
		if record.Outcome == "queued_behind_reply" {
			queued++
		}
	}
	if inbound != 3 || queued != 2 || superseded != 1 {
		t.Fatalf("inbound=%d queued=%d", inbound, queued)
	}
}
