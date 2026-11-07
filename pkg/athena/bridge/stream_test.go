package bridge

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestStreamFramesAndFailureBoundaries(t *testing.T) {
	now := time.Now().UTC()
	req := Request{TransmissionID: "synthetic-stream", Transcript: "Athena, picture.", Speaker: "Prometheus", FrequencyHz: 133000000, Modulation: ModulationAM, ReceivedAt: now, TranscribedAt: now}
	response := Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Prometheus, synthetic.", IntentClass: IntentFactual, State: StateOK}
	raw, _ := json.Marshal(response)
	line := string(raw) + "\n"
	second := strings.Replace(line, "Prometheus, synthetic.", "Synthetic second sentence.", 1)
	for _, tc := range []struct {
		name, body string
		calls      int
		bad        bool
	}{
		{"two", line + second + "[DONE]\n", 2, false},
		{"repeated_sentence", line + line + "[DONE]\n", 1, true},
		{"missing_done", line, 1, true},
		{"third", line + second + line + "[DONE]\n", 2, true},
		{"wrong_id", strings.Replace(line, "synthetic-stream", "other", 1) + "[DONE]\n", 0, true},
		{"duplicate_key", strings.Replace(line, "{", "{\"deliverable\":true,", 1) + "[DONE]\n", 0, true},
		{"empty", "[DONE]\n", 0, true},
		{"trailing", line + "[DONE]\n" + line, 1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/x-ndjson")
				fmt.Fprint(w, tc.body)
			}))
			defer server.Close()
			client, err := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 350)
			if err != nil {
				t.Fatal(err)
			}
			calls := 0
			err = client.ExchangeStream(t.Context(), req, func(Response) error { calls++; return nil })
			if (err != nil) != tc.bad || calls != tc.calls {
				t.Fatalf("err=%v calls=%d", err, calls)
			}
		})
	}
}

func TestStreamEmitsBeforeFinalAndCancellationNeverRetries(t *testing.T) {
	now := time.Now().UTC()
	req := Request{TransmissionID: "stream", Transcript: "Athena, status", Speaker: "Prometheus", FrequencyHz: 133000000, Modulation: ModulationAM, ReceivedAt: now, TranscribedAt: now}
	first := Response{TransmissionID: "stream", Deliverable: true, Speech: "Prometheus, synthetic.", IntentClass: IntentFactual, State: StateOK}
	release := make(chan struct{})
	requests := make(chan struct{}, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests <- struct{}{}
		w.Header().Set("Content-Type", "application/x-ndjson")
		json.NewEncoder(w).Encode(first)
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		fmt.Fprint(w, "[DONE]\n")
	}))
	defer server.Close()
	client, _ := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 350)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	err := client.ExchangeStream(ctx, req, func(Response) error { cancel(); close(release); return nil })
	if err == nil || len(requests) != 1 {
		t.Fatalf("err=%v requests=%d", err, len(requests))
	}
}
