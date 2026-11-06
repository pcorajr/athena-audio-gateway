package bridge

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func mvpRequest() Request {
	now := time.Now().UTC()
	return Request{TransmissionID: "synthetic-1", Transcript: "What is my current status?", Speaker: "Prometheus", FrequencyHz: 133000000, Modulation: ModulationAM, ReceivedAt: now, TranscribedAt: now}
}

func TestSRSMVPStrictResponse(t *testing.T) {
	base := `{"transmission_id":"synthetic-1","deliverable":true,"speech":"Synthetic status unavailable.","intent_class":"factual","state":"unavailable"}`
	tests := map[string]string{
		"wrong correlation":     strings.Replace(base, "synthetic-1", "other", 1),
		"too long":              strings.Replace(base, "Synthetic status unavailable.", strings.Repeat("x", 351), 1),
		"cue":                   strings.Replace(base, "factual", "cue", 1),
		"action":                strings.Replace(base, "factual", "action_preview", 1),
		"unrecognized":          strings.Replace(base, "factual", "unrecognized", 1),
		"unknown key":           strings.TrimSuffix(base, "}") + `,"audio":"no"}`,
		"duplicate key":         strings.TrimSuffix(base, "}") + `,"deliverable":false}`,
		"duplicate escaped key": strings.TrimSuffix(base, "}") + `,"\u0073tate":"ok"}`,
		"trailing object":       base + `{}`,
		"trailing junk":         base + `x`,
		"array":                 `[` + base + `]`,
		"null bool":             strings.Replace(base, "true", "null", 1),
		"string bool":           strings.Replace(base, "true", `"true"`, 1),
		"missing bool":          strings.Replace(base, `"deliverable":true,`, "", 1),
		"null speech":           strings.Replace(base, `"Synthetic status unavailable."`, "null", 1),
		"bad UTF8":              strings.Replace(base, "Synthetic", string([]byte{0xff}), 1),
		"unpaired surrogate":    strings.Replace(base, "Synthetic", `\ud800`, 1),
		"control character":     strings.Replace(base, "Synthetic", `\u0000`, 1),
		"oversized body":        base + strings.Repeat(" ", maxResponseBytes),
		"suppressed speech":     strings.Replace(base, "true", "false", 1),
	}
	for name, payload := range tests {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, payload) }))
			defer server.Close()
			client, err := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 0)
			if err != nil {
				t.Fatal(err)
			}
			resp, err := client.Exchange(t.Context(), mvpRequest())
			if err == nil || resp.Deliverable {
				t.Fatalf("accepted malformed response: %v", err)
			}
		})
	}
}

func TestSRSMVPNativeRequestAndFactualStates(t *testing.T) {
	for _, state := range []State{StateOK, StateStale, StateDegraded, StateUnavailable, StateOwnshipAmbiguous} {
		t.Run(string(state), func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.URL.Path != "/radio/v1/exchange" || r.Method != http.MethodPost || r.Header.Get("Authorization") != "" {
					t.Error("not a native bridge request")
				}
				var request Request
				if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
					t.Error(err)
				}
				if request.Addressee != "" || request.Transcript != mvpRequest().Transcript {
					t.Error("PTT transcript was rerouted or rewritten")
				}
				_ = json.NewEncoder(w).Encode(Response{TransmissionID: request.TransmissionID, Deliverable: true, Speech: strings.Repeat("é", 350), IntentClass: IntentFactual, State: state})
			}))
			defer server.Close()
			c, err := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 350)
			if err != nil {
				t.Fatal(err)
			}
			resp, err := c.Exchange(t.Context(), mvpRequest())
			if err != nil || !resp.Deliverable || resp.State != state || calls != 1 {
				t.Fatalf("valid factual response failed: %v", err)
			}
		})
	}
}

func TestSRSMVPEndpoints(t *testing.T) {
	for _, endpoint := range []string{"", "http://localhost:1234/radio/v1/exchange", "https://127.0.0.1:1234/radio/v1/exchange", "http://192.0.2.1:1234/radio/v1/exchange", "http://127.0.0.1/radio/v1/exchange", "http://127.0.0.1:0/radio/v1/exchange", "http://127.0.0.1:65536/radio/v1/exchange", "http://user@127.0.0.1:1234/radio/v1/exchange", "http://127.0.0.1:1234/v1/responses", "http://127.0.0.1:1234/radio/v1/exchange?next=x"} {
		if _, err := NewMVPClient(endpoint, time.Second, 0); err == nil {
			t.Errorf("accepted %q", endpoint)
		}
	}
}

func TestSRSMVPNoRedirectOrRetry(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		http.Redirect(w, r, "/elsewhere", http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	c, err := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Exchange(t.Context(), mvpRequest()); err == nil {
		t.Fatal("redirect accepted")
	}
	if calls != 1 {
		t.Fatalf("calls=%d", calls)
	}
}

func TestSRSMVPDeadlineAndInput(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(http.StatusServiceUnavailable) }))
	defer server.Close()
	c, _ := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 0)
	for _, mutate := range []func(*Request){func(r *Request) { r.Addressee = "hermes" }, func(r *Request) { r.Transcript = strings.Repeat("x", 32001) }, func(r *Request) { r.Transcript = string([]byte{0xff}) }} {
		r := mvpRequest()
		mutate(&r)
		if _, err := c.Exchange(t.Context(), r); err == nil {
			t.Fatal("invalid request accepted")
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := c.Exchange(ctx, mvpRequest()); err == nil {
		t.Fatal("canceled exchange accepted")
	}
	if calls != 0 {
		t.Fatal("invalid input reached HTTP")
	}
}
