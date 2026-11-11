package bridge

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Phase 1: the adapter may label a reply frame with route and model; frames
// without labels stay valid, bad labels are refused like any malformed frame.
func TestStreamAcceptsOptionalRouteAndModelLabels(t *testing.T) {
	now := time.Now().UTC()
	req := Request{TransmissionID: "labels", Transcript: "Athena, picture.", Speaker: "Prometheus", FrequencyHz: 133000000, Modulation: ModulationAM, ReceivedAt: now, TranscribedAt: now}
	base := `{"transmission_id":"labels","deliverable":true,"speech":"Prometheus, Athena, copy.","intent_class":"factual","state":"ok"`
	for _, tc := range []struct {
		name, extra, route, model string
		bad                       bool
	}{
		{"unlabelled", ``, "", "", false},
		{"fixed", `,"route":"fixed"`, "fixed", "", false},
		{"llm_model", `,"route":"llm","model":"gpt-6-luna"`, "llm", "gpt-6-luna", false},
		{"fallback", `,"route":"fallback"`, "fallback", "", false},
		{"unknown_route", `,"route":"guess"`, "", "", true},
		{"model_without_route", `,"model":"gpt-6-luna"`, "", "", true},
		{"null_route", `,"route":null`, "", "", true},
		{"long_model", `,"route":"llm","model":"` + strings.Repeat("m", MaxModelChars+1) + `"`, "", "", true},
		{"unknown_key", `,"author":"x"`, "", "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/x-ndjson")
				fmt.Fprint(w, base+tc.extra+"}\n[DONE]\n")
			}))
			defer server.Close()
			client, err := NewMVPClient(server.URL+"/radio/v1/exchange", time.Second, 350)
			if err != nil {
				t.Fatal(err)
			}
			var got []Response
			err = client.ExchangeStream(t.Context(), req, func(r Response) error { got = append(got, r); return nil })
			if (err != nil) != tc.bad {
				t.Fatalf("err=%v", err)
			}
			if !tc.bad && (len(got) != 1 || got[0].Route != tc.route || got[0].Model != tc.model) {
				t.Fatalf("got=%+v", got)
			}
		})
	}
}

func TestSuppressedFrameMayNotCarryARoute(t *testing.T) {
	r := Suppressed("x", IntentFactual, StateUnavailable, "unaddressed")
	r.Route = "llm"
	if r.ValidateMVP("x", 0) == nil {
		t.Fatal("a suppressed frame has no author")
	}
}
