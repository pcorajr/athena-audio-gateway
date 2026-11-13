package bridge

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestPythonThreatFrameMatchesGo(t *testing.T) {
	cmd := exec.Command("python3", "-B", "-c", `from types import SimpleNamespace
from integrations.hermes_radio.contract import response, encode_response
print(encode_response(response(SimpleNamespace(transmission_id='wire-check'),speech="SA-13 launch, five o'clock, one mile.",state='ok',route='unprompted',priority='launch')).decode())`)
	cmd.Dir = "../../.."
	raw, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	if err = validateMVPJSON(raw); err != nil {
		t.Fatal(err)
	}
	var frame Response
	if err = json.Unmarshal(raw, &frame); err != nil {
		t.Fatal(err)
	}
	if err = frame.ValidateMVP("wire-check", 280); err != nil {
		t.Fatal(err)
	}
	if frame.Priority != "launch" || frame.Route != "unprompted" {
		t.Fatalf("wrong frame %+v", frame)
	}
}

func TestThreatControlPlaneStrictness(t *testing.T) {
	for _, kind := range []string{"valid", "old_adapter", "unknown_field", "wrong_id", "wrong_priority", "missing_priority", "wrong_route", "wrong_state"} {
		t.Run(kind, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/radio/v1/unprompted" || r.Header.Get("X-Athena-Protocol") != "15" {
					t.Error("wrong endpoint/protocol")
				}
				w.Header().Set("Content-Type", "application/json")
				if kind != "old_adapter" {
					w.Header().Set("X-Athena-Protocol", "15")
				}
				frame := map[string]any{"transmission_id": "alert", "deliverable": true, "speech": "Missile launch.", "intent_class": "factual", "state": "ok", "route": "unprompted", "priority": "launch"}
				switch kind {
				case "unknown_field":
					frame["frequency"] = 133000000
				case "wrong_id":
					frame["transmission_id"] = "other"
				case "wrong_priority":
					frame["priority"] = "normal"
				case "missing_priority":
					delete(frame, "priority")
				case "wrong_route":
					frame["route"] = "llm"
				case "wrong_state":
					frame["state"] = "stale"
				}
				json.NewEncoder(w).Encode(frame)
			}))
			defer srv.Close()
			c, err := NewMVPClient(srv.URL+"/radio/v1/exchange", time.Second, 280)
			if err != nil {
				t.Fatal(err)
			}
			f, err := c.Poll(t.Context(), "alert", "pilot")
			if kind == "valid" {
				if err != nil || !f.Deliverable {
					t.Fatal(err)
				}
			} else if err == nil || f.Deliverable {
				t.Fatalf("accepted %s", kind)
			}
		})
	}
}

func TestThreatReceiptCannotSmuggleSpeech(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Athena-Protocol", "15")
		if !strings.HasSuffix(r.URL.Path, "/delivery") {
			t.Error("wrong endpoint")
		}
		json.NewEncoder(w).Encode(Response{TransmissionID: "alert", Deliverable: true, Speech: "Unsafe receipt speech.", State: StateOK, IntentClass: IntentFactual})
	}))
	defer srv.Close()
	c, _ := NewMVPClient(srv.URL+"/radio/v1/exchange", time.Second, 280)
	ok, err := c.Receipt(t.Context(), "alert", "sent")
	if err == nil || ok {
		t.Fatal("accepted speech on receipt")
	}
}
