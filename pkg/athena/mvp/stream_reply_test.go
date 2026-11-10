package mvp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// streamRadio is a fake transport with the real sender contract: expiry gates
// each part, and a failed part closes the sender.
type streamRadio struct {
	fakeRadio
	mu      sync.Mutex
	sent    []int // sample counts of sent parts
	play    time.Duration
	refused bool
}

func (r *streamRadio) BeginMVPReply(simpleradio.Transmission) replySender {
	closed := false
	return func(_ context.Context, audio simpleradio.Audio, expires time.Time) simpleradio.Delivery {
		r.mu.Lock()
		defer r.mu.Unlock()
		if closed {
			return simpleradio.Delivery{Outcome: "invalid_or_replayed"}
		}
		if !time.Now().Before(expires) {
			closed = true
			return simpleradio.Delivery{Outcome: "expired"}
		}
		if r.refused {
			closed = true
			return simpleradio.Delivery{Outcome: "invalidated", Reason: "channel_busy"}
		}
		first := time.Now()
		time.Sleep(r.play)
		r.sent = append(r.sent, len(audio))
		return simpleradio.Delivery{Outcome: "sent", FirstSend: first, LastSend: time.Now()}
	}
}

type streamFunc func(context.Context, bridge.Request, func(bridge.Response) error) error

func (f streamFunc) Exchange(context.Context, bridge.Request) (bridge.Response, error) {
	return bridge.Response{}, errors.New("unused")
}

func (f streamFunc) ExchangeStream(ctx context.Context, req bridge.Request, emit func(bridge.Response) error) error {
	return f(ctx, req, emit)
}

func replyParts(speech ...string) streamFunc {
	return func(_ context.Context, req bridge.Request, emit func(bridge.Response) error) error {
		for _, s := range speech {
			if err := emit(bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: s, IntentClass: bridge.IntentFactual, State: bridge.StateOK}); err != nil {
				return err
			}
		}
		return nil
	}
}

func streamFixture(t *testing.T) (*Runner, simpleradio.Transmission, *streamRadio, *[]Timing, *[]string) {
	t.Helper()
	r, tr, _, timings := runnerFixture(t)
	radio := &streamRadio{}
	radio.valid.Store(true)
	r.Radio = radio
	spoken := &[]string{}
	var mu sync.Mutex
	r.Synthesize = func(_ context.Context, s string) (simpleradio.Audio, error) {
		mu.Lock()
		defer mu.Unlock()
		*spoken = append(*spoken, s)
		return make(simpleradio.Audio, 16000), nil // 1 s per part
	}
	return r, tr, radio, timings, spoken
}

func TestStreamReplySendsAllPartsThenSent(t *testing.T) {
	r, tr, radio, timings, _ := streamFixture(t)
	r.Bridge = replyParts("Prometheus, part one.", "Part two.")
	r.Process(t.Context(), tr)
	if len(radio.sent) != 2 || (*timings)[0].Outcome != "sent" {
		t.Fatalf("sent=%v timing=%+v", radio.sent, (*timings)[0])
	}
}

func TestStreamReplyTooLongSpeaksFallback(t *testing.T) {
	r, tr, radio, timings, spoken := streamFixture(t)
	r.Bridge = replyParts("Prometheus, a very long debrief.", "And more.")
	r.Synthesize = func(_ context.Context, s string) (simpleradio.Audio, error) {
		*spoken = append(*spoken, s)
		if s == TooLongLine {
			return make(simpleradio.Audio, 3*16000), nil
		}
		return make(simpleradio.Audio, 20*16000), nil // 40 s total > 30 s
	}
	r.Process(t.Context(), tr)
	if got := (*timings)[0].Outcome; got != "too_long_fallback_sent" {
		t.Fatalf("outcome=%q", got)
	}
	if len(radio.sent) != 1 || radio.sent[0] != 3*16000 || (*spoken)[len(*spoken)-1] != TooLongLine {
		t.Fatalf("sent=%v spoken=%v", radio.sent, *spoken)
	}
	if 3*16000 > simpleradio.MaxMVPReplySamples/5 {
		t.Fatal("fallback must stay short")
	}
}

func TestStreamReplyFailuresSaySayAgain(t *testing.T) {
	for _, stage := range []string{"bridge_error", "tts_error"} {
		t.Run(stage, func(t *testing.T) {
			r, tr, radio, timings, spoken := streamFixture(t)
			r.Bridge = replyParts("Prometheus, part one.")
			switch stage {
			case "bridge_error":
				r.Bridge = streamFunc(func(context.Context, bridge.Request, func(bridge.Response) error) error {
					return errors.New("synthetic")
				})
			case "tts_error":
				synth := r.Synthesize
				r.Synthesize = func(ctx context.Context, s string) (simpleradio.Audio, error) {
					if s != SayAgainLine {
						return nil, errors.New("synthetic")
					}
					return synth(ctx, s)
				}
			}
			r.Process(t.Context(), tr)
			if got := (*timings)[0].Outcome; got != "say_again_fallback_sent" || len(radio.sent) != 1 || (*spoken)[len(*spoken)-1] != SayAgainLine {
				t.Fatalf("outcome=%q sent=%v spoken=%v", got, radio.sent, *spoken)
			}
		})
	}
}

func TestStreamReplySuppressedAndRefusedStayHonest(t *testing.T) {
	r, tr, radio, timings, _ := streamFixture(t)
	r.Bridge = streamFunc(func(_ context.Context, req bridge.Request, emit func(bridge.Response) error) error {
		return emit(bridge.Response{TransmissionID: req.TransmissionID, Deliverable: false, IntentClass: bridge.IntentFactual, State: bridge.StateOK})
	})
	r.Process(t.Context(), tr)
	if len(radio.sent) != 0 || (*timings)[0].Outcome != "suppressed" {
		t.Fatalf("sent=%v timing=%+v", radio.sent, (*timings)[0])
	}

	r, tr, radio, timings, _ = streamFixture(t)
	radio.refused = true
	r.Bridge = replyParts("Prometheus, part one.", "Part two.")
	r.Process(t.Context(), tr)
	if len(radio.sent) != 0 || (*timings)[0].Outcome != "invalidated:channel_busy" {
		t.Fatalf("sent=%v timing=%+v", radio.sent, (*timings)[0])
	}
}

// F2: the bridge HTTP timeout must not cover playback, and part 2 must follow
// a part 1 that plays past both the HTTP timeout and the first-send expiry.
func TestStreamReplyLongPartOneDoesNotKillPartTwo(t *testing.T) {
	r, tr, radio, timings, _ := streamFixture(t)
	r.MaxAge = 2 * time.Second
	radio.play = 700 * time.Millisecond // longer than the HTTP timeout below
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		var in bridge.Request
		_ = json.NewDecoder(req.Body).Decode(&in)
		w.Header().Set("Content-Type", "application/x-ndjson")
		for _, s := range []string{"Prometheus, part one.", "Part two."} {
			raw, _ := json.Marshal(bridge.Response{TransmissionID: in.TransmissionID, Deliverable: true, Speech: s, IntentClass: bridge.IntentFactual, State: bridge.StateOK})
			fmt.Fprintf(w, "%s\n", raw)
		}
		fmt.Fprint(w, "[DONE]\n")
	}))
	defer server.Close()
	client, err := bridge.NewMVPClient(server.URL+"/radio/v1/exchange", 500*time.Millisecond, 350)
	if err != nil {
		t.Fatal(err)
	}
	r.Bridge = client
	tr.ReceivedAt = time.Now().Add(-1500 * time.Millisecond) // part 1 ends past expiry
	r.Process(t.Context(), tr)
	if len(radio.sent) != 2 || (*timings)[0].Outcome != "sent" {
		t.Fatalf("sent=%v timing=%+v", radio.sent, (*timings)[0])
	}
}
