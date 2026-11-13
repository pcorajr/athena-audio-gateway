package mvp

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

type threatBridge struct {
	priority string
	once     atomic.Bool
	polled   chan struct{}
}

func (b *threatBridge) Exchange(_ context.Context, req bridge.Request) (bridge.Response, error) {
	return bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Synthetic ordinary reply.", State: bridge.StateOK, IntentClass: bridge.IntentFactual, Priority: b.priority}, nil
}
func (*threatBridge) Remember(context.Context, bridge.Request) error { return nil }
func (b *threatBridge) Poll(_ context.Context, id, last string) (bridge.Response, error) {
	if b.once.Swap(true) {
		return bridge.Response{}, nil
	}
	close(b.polled)
	return bridge.Response{TransmissionID: id, Deliverable: true, Speech: "SA-13 launch, five o'clock, one mile.", State: bridge.StateOK, IntentClass: bridge.IntentFactual, Priority: "launch", Route: "unprompted"}, nil
}
func (*threatBridge) Receipt(context.Context, string, string) (bool, error) { return true, nil }

type priorityRadio struct {
	started, release, sent chan struct{}
	mu                     sync.Mutex
	interrupted            bool
	frequencies            []float64
}

func (*priorityRadio) ValidMVP(simpleradio.Transmission) bool { return true }
func (*priorityRadio) UnpromptedMVP(last simpleradio.Transmission, id string) (simpleradio.Transmission, bool) {
	last.TraceID = id
	last.ReceivedAt = time.Now()
	return last, true
}
func (r *priorityRadio) TransmitMVP(ctx context.Context, t simpleradio.Transmission, _ time.Time) simpleradio.Delivery {
	if strings.HasPrefix(t.TraceID, "unprompted-") {
		r.mu.Lock()
		r.frequencies = append(r.frequencies, t.Radio.Frequency)
		r.mu.Unlock()
		close(r.sent)
		return simpleradio.Delivery{Outcome: "sent", FirstSend: time.Now(), LastSend: time.Now()}
	}
	close(r.started)
	select {
	case <-ctx.Done():
		r.mu.Lock()
		r.interrupted = true
		r.mu.Unlock()
		return simpleradio.Delivery{Outcome: "canceled"}
	case <-r.release:
		return simpleradio.Delivery{Outcome: "sent", FirstSend: time.Now(), LastSend: time.Now()}
	}
}

func TestLaunchPriorityAndTranscript(t *testing.T) {
	for _, priority := range []string{"normal", "threat", ""} {
		t.Run("priority_"+priority, func(t *testing.T) {
			r, tr, _, _ := runnerFixture(t)
			radio := &priorityRadio{started: make(chan struct{}), release: make(chan struct{}), sent: make(chan struct{})}
			b := &threatBridge{priority: priority, polled: make(chan struct{})}
			r.Radio = radio
			r.Bridge = b
			r.Unprompted = true
			r.Record = nil
			r.Transcript = &transcript.Writer{Root: t.TempDir()}
			ctx, cancel := context.WithTimeout(t.Context(), 4*time.Second)
			defer cancel()
			in := make(chan simpleradio.Transmission, 1)
			in <- tr
			done := make(chan struct{})
			go func() { defer close(done); r.Run(ctx, in) }()
			select {
			case <-radio.started:
			case <-ctx.Done():
				t.Fatal("ordinary reply never started")
			}
			select {
			case <-b.polled:
			case <-ctx.Done():
				t.Fatal("no poll")
			}
			if priority != "normal" {
				select {
				case <-radio.sent:
					t.Fatal("cut a protected threat")
				case <-time.After(100 * time.Millisecond):
				}
				close(radio.release)
			}
			select {
			case <-radio.sent:
			case <-ctx.Done():
				t.Fatal("launch not delivered")
			}
			cancel()
			<-done
			radio.mu.Lock()
			defer radio.mu.Unlock()
			if radio.interrupted != (priority == "normal") {
				t.Fatalf("interrupted=%v priority=%q", radio.interrupted, priority)
			}
			if len(radio.frequencies) != 1 || radio.frequencies[0] != tr.Radio.Frequency {
				t.Fatal("wrong last channel")
			}
			intended, delivered, interrupted := false, false, false
			for _, rec := range records(t, r.Transcript.Root) {
				intended = intended || (rec.Kind == "unprompted_intended" && rec.Route == "unprompted" && rec.Text != "" && rec.FrequencyHz == uint64(tr.Radio.Frequency))
				delivered = delivered || (rec.Kind == "unprompted_delivery" && rec.Outcome == "sent")
				interrupted = interrupted || (rec.Kind == "exchange_outcome" && rec.ID == tr.TraceID && rec.Outcome == "interrupted")
			}
			if !intended || !delivered || interrupted != (priority == "normal") {
				t.Fatalf("journal intended=%v delivered=%v interrupted=%v", intended, delivered, interrupted)
			}
		})
	}
}

func TestOldOrMalformedAdapterIsSilent(t *testing.T) {
	for _, kind := range []string{"old_header", "missing_priority", "unknown_field", "bad_priority", "good"} {
		t.Run(kind, func(t *testing.T) {
			r, tr, radio, _, _ := streamFixture(t)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				if strings.HasSuffix(req.URL.Path, "/delivery") {
					http.NotFound(w, req)
					return
				}
				w.Header().Set("Content-Type", "application/x-ndjson")
				if kind != "old_header" {
					w.Header().Set("X-Athena-Protocol", "15")
				}
				frame := map[string]any{"transmission_id": tr.TraceID, "deliverable": true, "speech": "Synthetic reply.", "intent_class": "factual", "state": "ok", "priority": "normal"}
				if kind == "missing_priority" {
					delete(frame, "priority")
				}
				if kind == "unknown_field" {
					frame["future_field"] = true
				}
				if kind == "bad_priority" {
					frame["priority"] = "urgent"
				}
				json.NewEncoder(w).Encode(frame)
				fmt.Fprint(w, "[DONE]\n")
			}))
			defer srv.Close()
			client, err := bridge.NewMVPClient(srv.URL+"/radio/v1/exchange", time.Second, 280)
			if err != nil {
				t.Fatal(err)
			}
			client.RequireProtocol15 = true
			r.Bridge = client
			r.Process(t.Context(), tr)
			want := 0
			if kind == "good" {
				want = 1
			}
			if len(radio.sent) != want {
				t.Fatalf("sends=%d want=%d", len(radio.sent), want)
			}
		})
	}
}
