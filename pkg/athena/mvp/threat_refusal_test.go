package mvp

import (
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

type refusingRadio struct{ priorityRadio }

func (*refusingRadio) UnpromptedMVP(simpleradio.Transmission, string) (simpleradio.Transmission, bool) {
	return simpleradio.Transmission{}, false
}
func (*refusingRadio) UnpromptedRejection(simpleradio.Transmission) string {
	return "radio_pilot_channel_mismatch"
}

// Flight 2026-11-13 (r12.9): 27 unprompted calls were refused by the radio layer and
// left no trace. Every refusal is now a transcript row with its reason and text.
func TestUnpromptedRefusalIsRecorded(t *testing.T) {
	for name, tc := range map[string]struct {
		supersede bool
		want      string
	}{"radio": {false, "radio_pilot_channel_mismatch"}, "superseded": {true, "route_superseded"}} {
		t.Run(name, func(t *testing.T) {
			r, tr, _, _ := runnerFixture(t)
			r.Radio = &refusingRadio{}
			r.Bridge = &threatBridge{}
			r.lastRadio = tr
			if tc.supersede {
				r.lastRadio.TraceID = "a-newer-pilot-call"
			}
			r.Transcript = &transcript.Writer{Root: t.TempDir()}
			r.playThreat(t.Context(), threatJob{last: tr, frame: bridge.Response{TransmissionID: "unprompted-refused", Speech: "SA-13 launch.", Priority: "launch"}})
			found := 0
			for _, rec := range records(t, r.Transcript.Root) {
				if rec.Kind == "unprompted_refused" {
					found++
					if rec.Outcome != tc.want || rec.Text != "SA-13 launch." || rec.Route != "unprompted" {
						t.Errorf("unexpected record %+v", rec)
					}
				}
				if rec.Kind == "unprompted_intended" || rec.Kind == "unprompted_delivery" {
					t.Errorf("refused call reached the radio path: %+v", rec)
				}
			}
			if found != 1 {
				t.Fatalf("refusal rows=%d", found)
			}
		})
	}
}
