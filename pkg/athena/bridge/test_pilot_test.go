package bridge

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// The adapter may mark a reply frame test_pilot (the TEST stand-in is the
// pilot). The v13 gateway refuses unknown keys, so this field exists only
// from this version on; see contract.py for the deploy order.
func TestStreamAcceptsOptionalTestPilotFlag(t *testing.T) {
	now := time.Now().UTC()
	req := Request{TransmissionID: "tp", Transcript: "Athena, picture.", Speaker: "Prometheus", FrequencyHz: 133000000, Modulation: ModulationAM, ReceivedAt: now, TranscribedAt: now}
	base := `{"transmission_id":"tp","deliverable":true,"speech":"Prometheus, Athena, copy.","intent_class":"factual","state":"ok","route":"fixed"`
	for _, tc := range []struct {
		name, extra string
		want        bool
		bad         bool
	}{
		{"absent", ``, false, false},
		{"true", `,"test_pilot":true`, true, false},
		{"false", `,"test_pilot":false`, false, false},
		{"string", `,"test_pilot":"yes"`, false, true},
		{"null", `,"test_pilot":null`, false, true},
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
			if !tc.bad && (len(got) != 1 || got[0].TestPilot != tc.want) {
				t.Fatalf("got=%+v", got)
			}
		})
	}
}
