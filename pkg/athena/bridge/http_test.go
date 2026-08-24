package bridge

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// encodeOrFail writes a JSON body from a test HTTP handler.
//
// It deliberately does NOT call t.Errorf. A handler runs on the server's
// goroutine and, in timeout tests, outlives the test function: reporting
// through *testing.T from there is a data race that -race reports against
// whichever test happens to be running. A write failure here means the client
// already gave up, which is exactly what those tests are asserting, so it is
// not a fault worth reporting.
func encodeOrFail(_ *testing.T, w http.ResponseWriter, v any) {
	if err := json.NewEncoder(w).Encode(v); err != nil {
		// The client has already gone away, which is what the timeout tests
		// are asserting. Reporting it through *testing.T from this goroutine
		// would be the data race this function exists to avoid.
		_ = err
	}
}

func exchangeRequest() Request {
	received := time.Date(2026, 8, 22, 12, 0, 0, 0, time.UTC)
	return Request{
		TransmissionID: "tx-1",
		Transcript:     "athena, nearest threat",
		Speaker:        "Prometheus",
		FrequencyHz:    30_000_000,
		Modulation:     ModulationFM,
		ReceivedAt:     received,
		TranscribedAt:  received.Add(400 * time.Millisecond),
	}
}

func serve(t *testing.T, handler http.HandlerFunc) *HTTPClient {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	client, err := NewHTTPClient(server.URL, time.Second)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}
	return client
}

func TestExchangeReturnsHermesResponse(t *testing.T) {
	t.Parallel()

	client := serve(t, func(w http.ResponseWriter, r *http.Request) {
		var got Request
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("server could not decode request: %v", err)
		}
		if got.Transcript != "athena, nearest threat" {
			t.Errorf("server saw transcript %q", got.Transcript)
		}
		encodeOrFail(t, w, Response{
			TransmissionID: "tx-1",
			Deliverable:    true,
			Speech:         "Bullseye 270 for 40.",
			IntentClass:    IntentFactual,
			State:          StateOK,
		})
	})

	resp, err := client.Exchange(t.Context(), exchangeRequest())
	if err != nil {
		t.Fatalf("Exchange: %v", err)
	}
	if !resp.Deliverable || resp.Speech != "Bullseye 270 for 40." {
		t.Errorf("unexpected response: %+v", resp)
	}
}

// A reply carrying a different transmission ID would speak one pilot's answer
// over another's question. It must be refused.
func TestExchangeRejectsMismatchedTransmissionID(t *testing.T) {
	t.Parallel()

	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		encodeOrFail(t, w, Response{
			TransmissionID: "some-other-transmission",
			Deliverable:    true,
			Speech:         "Answer to a different question.",
			IntentClass:    IntentFactual,
			State:          StateOK,
		})
	})

	if _, err := client.Exchange(t.Context(), exchangeRequest()); err == nil {
		t.Fatal("expected a mismatched transmission_id to be rejected")
	}
}

func TestExchangeErrorPaths(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		handler http.HandlerFunc
	}{
		{
			name: "server error",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusInternalServerError)
			},
		},
		{
			name: "malformed json",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				_, _ = w.Write([]byte("{not json"))
			},
		},
		{
			name: "invalid envelope: deliverable without speech",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				encodeOrFail(t, w, Response{
					TransmissionID: "tx-1",
					Deliverable:    true,
					IntentClass:    IntentFactual,
					State:          StateOK,
				})
			},
		},
		{
			name: "invalid envelope: unknown state",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				encodeOrFail(t, w, Response{
					TransmissionID: "tx-1",
					Deliverable:    true,
					Speech:         "something",
					IntentClass:    IntentFactual,
					State:          State("great"),
				})
			},
		},
		{
			name: "suppressed response smuggling speech",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				encodeOrFail(t, w, Response{
					TransmissionID:    "tx-1",
					Deliverable:       false,
					Speech:            "should never be transmitted",
					IntentClass:       IntentFactual,
					State:             StateStale,
					SuppressionReason: "stale",
				})
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			client := serve(t, tc.handler)
			resp, err := client.Exchange(t.Context(), exchangeRequest())
			if err == nil {
				t.Fatal("expected an error")
			}
			if resp.Deliverable {
				t.Error("an errored exchange must never return a deliverable response")
			}
		})
	}
}

// A slow Hermes must not hold the radio open indefinitely.
func TestExchangeTimesOut(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(300 * time.Millisecond)
		encodeOrFail(t, w, Response{
			TransmissionID: "tx-1",
			Deliverable:    true,
			Speech:         "too late",
			IntentClass:    IntentFactual,
			State:          StateOK,
		})
	}))
	t.Cleanup(server.Close)

	client, err := NewHTTPClient(server.URL, 30*time.Millisecond)
	if err != nil {
		t.Fatalf("NewHTTPClient: %v", err)
	}
	if _, err := client.Exchange(t.Context(), exchangeRequest()); err == nil {
		t.Fatal("expected a timeout error")
	}
}

// A cancelled context must abort the exchange rather than transmit late.
func TestExchangeHonoursContextCancellation(t *testing.T) {
	t.Parallel()

	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(200 * time.Millisecond)
		encodeOrFail(t, w, Response{TransmissionID: "tx-1"})
	})

	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := client.Exchange(ctx, exchangeRequest()); err == nil {
		t.Fatal("expected cancellation to abort the exchange")
	}
}

// An invalid request must never reach the network.
func TestExchangeValidatesRequestBeforeSending(t *testing.T) {
	t.Parallel()

	called := false
	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		called = true
		encodeOrFail(t, w, Response{TransmissionID: "tx-1"})
	})

	bad := exchangeRequest()
	bad.Transcript = ""
	if _, err := client.Exchange(t.Context(), bad); err == nil {
		t.Fatal("expected request validation to fail")
	}
	if called {
		t.Error("an invalid request must not be sent to Hermes")
	}
}

// An oversized reply is a malfunction, not a long answer; it must not be
// consumed without bound.
func TestExchangeBoundsResponseSize(t *testing.T) {
	t.Parallel()

	client := serve(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"transmission_id":"tx-1","speech":"`))
		_, _ = w.Write([]byte(strings.Repeat("A", maxResponseBytes+1024)))
		_, _ = w.Write([]byte(`"}`))
	})

	if _, err := client.Exchange(t.Context(), exchangeRequest()); err == nil {
		t.Fatal("expected an oversized response to be rejected")
	}
}

func TestNewHTTPClientValidation(t *testing.T) {
	t.Parallel()

	if _, err := NewHTTPClient("", time.Second); err == nil {
		t.Error("an empty endpoint must be rejected")
	}
	if _, err := NewHTTPClient("   ", time.Second); err == nil {
		t.Error("a blank endpoint must be rejected")
	}
	client, err := NewHTTPClient("http://localhost:9/bridge", 0)
	if err != nil {
		t.Fatalf("a zero timeout should fall back to the default: %v", err)
	}
	if client.http.Timeout != DefaultTimeout {
		t.Errorf("timeout = %v, want the default %v", client.http.Timeout, DefaultTimeout)
	}
}

// HTTPClient must satisfy the Client interface the Gateway depends on.
var _ Client = (*HTTPClient)(nil)
