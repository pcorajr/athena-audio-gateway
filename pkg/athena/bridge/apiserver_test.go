package bridge

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// exchangeRequest is a valid request for transport tests.
func apiExchangeRequest() Request {
	received := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	return Request{
		TransmissionID: "tx-http-1",
		Transcript:     "what am I flying",
		Speaker:        "Prometheus",
		Addressee:      "athena",
		FrequencyHz:    133_000_000,
		Modulation:     ModulationAM,
		ReceivedAt:     received,
		TranscribedAt:  received.Add(time.Second),
	}
}

// okReply builds an API-server envelope carrying the given assistant text.
func okReply(t *testing.T, text string) []byte {
	t.Helper()
	body := map[string]any{
		"id":     "resp_1",
		"object": "response",
		"status": "completed",
		"output": []map[string]any{{
			"type": "message",
			"role": "assistant",
			"content": []map[string]any{
				{"type": "output_text", "text": text},
			},
		}},
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return raw
}

func newTestClient(t *testing.T, srv *httptest.Server) *APIServerClient {
	t.Helper()
	c, err := NewAPIServerClient(APIServerConfig{
		Endpoint: srv.URL,
		APIKey:   "test-key",
		Timeout:  5 * time.Second,
	})
	if err != nil {
		t.Fatalf("NewAPIServerClient: %v", err)
	}
	return c
}

func TestExchangeReturnsDeliverableSpeech(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(okReply(t, `{"speech":"A-10C two.","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	got, err := newTestClient(t, srv).Exchange(t.Context(), apiExchangeRequest())
	if err != nil {
		t.Fatalf("Exchange: %v", err)
	}
	if !got.Deliverable || got.Speech != "A-10C two." {
		t.Errorf("got %+v, want deliverable speech", got)
	}
	if got.TransmissionID != "tx-http-1" {
		t.Errorf("transmission_id = %q, want the request's", got.TransmissionID)
	}
}

// The persona's conversation is what keeps mission and development context
// apart. It must reach the API server on every exchange.
func TestExchangeSendsThePersonaConversation(t *testing.T) {
	t.Parallel()

	var seen responsesRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&seen)
		_, _ = w.Write(okReply(t, `{"speech":"ok","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	req := apiExchangeRequest()
	req.Addressee = "hermes"
	if _, err := newTestClient(t, srv).Exchange(t.Context(), req); err != nil {
		t.Fatalf("Exchange: %v", err)
	}
	if seen.Conversation != "hermes" {
		t.Errorf("conversation = %q, want %q", seen.Conversation, "hermes")
	}
	if !seen.Store {
		t.Error("store must be true or conversation continuity is lost")
	}
	if !strings.Contains(seen.Input, "what am I flying") {
		t.Errorf("input %q should carry the transcript", seen.Input)
	}
	if !strings.Contains(seen.Input, "JSON object") {
		t.Error("the reply-shape contract must travel in the turn's input")
	}
}

// Without a default, an unaddressed request would open a fresh session every
// time, which looks like the model forgetting rather than a config error.
func TestExchangeFallsBackToTheDefaultConversation(t *testing.T) {
	t.Parallel()

	var seen responsesRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&seen)
		_, _ = w.Write(okReply(t, `{"speech":"ok","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	c, err := NewAPIServerClient(APIServerConfig{
		Endpoint:            srv.URL,
		APIKey:              "test-key",
		DefaultConversation: "athena",
	})
	if err != nil {
		t.Fatalf("NewAPIServerClient: %v", err)
	}

	req := apiExchangeRequest()
	req.Addressee = "" // single-persona deployment
	if _, err := c.Exchange(t.Context(), req); err != nil {
		t.Fatalf("Exchange: %v", err)
	}
	if seen.Conversation != "athena" {
		t.Errorf("conversation = %q, want the default %q", seen.Conversation, "athena")
	}
}

func TestExchangeSendsBearerAuth(t *testing.T) {
	t.Parallel()

	var auth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		_, _ = w.Write(okReply(t, `{"speech":"ok","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	if _, err := newTestClient(t, srv).Exchange(t.Context(), apiExchangeRequest()); err != nil {
		t.Fatalf("Exchange: %v", err)
	}
	if auth != "Bearer test-key" {
		t.Errorf("Authorization = %q, want a bearer token", auth)
	}
}

// Every transport failure must suppress rather than error, so a caller can
// never mistake a failed exchange for permission to transmit.
func TestTransportFailuresSuppress(t *testing.T) {
	t.Parallel()

	tests := map[string]http.HandlerFunc{
		"500": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte(`{"error":"boom"}`))
		},
		"401": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":"bad key"}`))
		},
		"malformed envelope": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`not json`))
		},
		"empty body": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write(nil)
		},
		"no output items": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"output":[]}`))
		},
	}

	for name, handler := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv := httptest.NewServer(handler)
			defer srv.Close()

			got, err := newTestClient(t, srv).Exchange(t.Context(), apiExchangeRequest())
			if err != nil {
				t.Fatalf("transport failure must suppress, not error: %v", err)
			}
			assertSuppressed(t, got, "")
		})
	}
}

// An unreachable Hermes must suppress, not hang or error.
func TestUnreachableHermesSuppresses(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // nothing is listening now

	c, err := NewAPIServerClient(APIServerConfig{
		Endpoint: url, APIKey: "k", Timeout: time.Second,
	})
	if err != nil {
		t.Fatalf("NewAPIServerClient: %v", err)
	}
	got, err := c.Exchange(t.Context(), apiExchangeRequest())
	if err != nil {
		t.Fatalf("unreachable hermes must suppress, not error: %v", err)
	}
	assertSuppressed(t, got, "unreachable")
}

// A slow answer on a radio is worse than no answer. The exchange is bounded and
// never retried.
func TestSlowHermesSuppressesAndDoesNotRetry(t *testing.T) {
	t.Parallel()

	// The handler runs on the server's goroutine while the client times out on
	// this one, so the counter needs synchronisation of its own.
	var mu sync.Mutex
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		calls++
		mu.Unlock()
		time.Sleep(300 * time.Millisecond)
		_, _ = w.Write(okReply(t, `{"speech":"late","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	c, err := NewAPIServerClient(APIServerConfig{
		Endpoint: srv.URL, APIKey: "k", Timeout: 50 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("NewAPIServerClient: %v", err)
	}
	got, err := c.Exchange(t.Context(), apiExchangeRequest())
	if err != nil {
		t.Fatalf("timeout must suppress, not error: %v", err)
	}
	assertSuppressed(t, got, "")

	mu.Lock()
	seen := calls
	mu.Unlock()
	if seen != 1 {
		t.Errorf("server called %d times; the exchange must never be retried", seen)
	}
}

func TestCancelledContextSuppresses(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(200 * time.Millisecond)
		_, _ = w.Write(okReply(t, `{"speech":"ok","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	ctx, cancel := context.WithCancel(t.Context())
	cancel()

	got, err := newTestClient(t, srv).Exchange(ctx, apiExchangeRequest())
	if err != nil {
		t.Fatalf("cancellation must suppress, not error: %v", err)
	}
	assertSuppressed(t, got, "")
}

// An action-shaped answer must not reach the radio even when it survives the
// whole transport path intact.
func TestActionPreviewIsSuppressedEndToEnd(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(okReply(t,
			`{"speech":"Spawning a tank at sector alpha.","intent":"action_preview","state":"ok"}`))
	}))
	defer srv.Close()

	got, err := newTestClient(t, srv).Exchange(t.Context(), apiExchangeRequest())
	if err != nil {
		t.Fatalf("Exchange: %v", err)
	}
	assertSuppressed(t, got, "action")
}

// An invalid request is a programming fault the caller must fix, so it errors
// rather than silently suppressing.
func TestExchangeRejectsAnInvalidRequest(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("an invalid request must not reach the server")
	}))
	defer srv.Close()

	req := apiExchangeRequest()
	req.Transcript = ""
	if _, err := newTestClient(t, srv).Exchange(t.Context(), req); err == nil {
		t.Fatal("an invalid request must error")
	}
}

func TestNewAPIServerClientValidatesConfig(t *testing.T) {
	t.Parallel()

	if _, err := NewAPIServerClient(APIServerConfig{APIKey: "k"}); err == nil {
		t.Error("an empty endpoint must be rejected")
	}
	// A missing credential must fail at construction, not mid-flight.
	if _, err := NewAPIServerClient(APIServerConfig{Endpoint: "http://x"}); err == nil {
		t.Error("a missing API key must be rejected at construction")
	}

	c, err := NewAPIServerClient(APIServerConfig{Endpoint: "http://x/", APIKey: "k"})
	if err != nil {
		t.Fatalf("NewAPIServerClient: %v", err)
	}
	if strings.HasSuffix(c.endpoint, "/") {
		t.Error("a trailing slash must be trimmed so the path is not doubled")
	}
	if c.model != DefaultModel {
		t.Errorf("model = %q, want the default", c.model)
	}
	if c.maxSpeechChars != DefaultMaxSpeechChars {
		t.Errorf("maxSpeechChars = %d, want the default", c.maxSpeechChars)
	}
}

// The API server persists `instructions` into a stored conversation. Sending
// the reply-shape contract that way contaminated every later turn -- observed
// live, where a plain question came back wrapped in the radio JSON envelope.
func TestReplyShapeDoesNotLeakIntoTheStoredConversation(t *testing.T) {
	t.Parallel()

	var bodies []map[string]any
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		bodies = append(bodies, body)
		mu.Unlock()
		_, _ = w.Write(okReply(t, `{"speech":"ok","intent":"factual","state":"ok"}`))
	}))
	defer srv.Close()

	client := newTestClient(t, srv)

	// Two turns on the same conversation, as a real session would run.
	for i := range 2 {
		req := apiExchangeRequest()
		req.TransmissionID = fmt.Sprintf("tx-%d", i)
		if _, err := client.Exchange(t.Context(), req); err != nil {
			t.Fatalf("Exchange %d: %v", i, err)
		}
	}

	mu.Lock()
	defer mu.Unlock()
	if len(bodies) != 2 {
		t.Fatalf("captured %d requests, want 2", len(bodies))
	}
	for i, body := range bodies {
		if _, present := body["instructions"]; present {
			t.Errorf("request %d carries an `instructions` field; it would persist "+
				"into the stored conversation and contaminate every later turn", i)
		}
		input, _ := body["input"].(string)
		if !strings.Contains(input, "JSON object") {
			t.Errorf("request %d input lacks the reply-shape contract; it must "+
				"travel per turn, not be inherited from the session", i)
		}
	}
}
