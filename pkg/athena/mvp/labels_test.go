package mvp

import (
	"context"
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// Phase 1: every reply record shows the adapter's route and model, never a
// blanket "LLM"; a gateway fallback is labelled fallback.
func labelled(route, model string, speech ...string) streamFunc {
	return func(_ context.Context, req bridge.Request, emit func(bridge.Response) error) error {
		for _, s := range speech {
			if err := emit(bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: s, IntentClass: bridge.IntentFactual, State: bridge.StateOK, Route: route, Model: model}); err != nil {
				return err
			}
		}
		return nil
	}
}

func byKind(records []transcript.Record) map[string][]transcript.Record {
	out := map[string][]transcript.Record{}
	for _, r := range records {
		out[r.Kind] = append(out[r.Kind], r)
	}
	return out
}

func TestReplyRecordsCarryAdapterRouteAndModel(t *testing.T) {
	for _, tc := range []struct{ route, model string }{{"fixed", ""}, {"llm", "gpt-6-luna"}, {"debrief", ""}, {"fallback", ""}} {
		t.Run(tc.route, func(t *testing.T) {
			r, tr, _, timings, _ := streamFixture(t)
			r.Transcript = &transcript.Writer{Root: t.TempDir()}
			r.Bridge = labelled(tc.route, tc.model, "Prometheus, Athena, copy.")
			r.Process(t.Context(), tr)
			got := byKind(records(t, r.Transcript.Root))
			for _, kind := range []string{"reply_intended", "delivery", "exchange_outcome"} {
				if len(got[kind]) != 1 || got[kind][0].Route != tc.route || got[kind][0].Model != tc.model {
					t.Fatalf("%s: %+v", kind, got[kind])
				}
			}
			if got["reply_intended"][0].Timings["reply_received"] < 0 || (*timings)[0].Route != tc.route {
				t.Fatalf("latency/timing: %+v %+v", got["reply_intended"][0], (*timings)[0])
			}
		})
	}
}

func TestUnlabelledFrameKeepsLegacyLabel(t *testing.T) {
	r, tr, _, _, _ := streamFixture(t)
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	r.Bridge = replyParts("Prometheus, Athena, copy.")
	r.Process(t.Context(), tr)
	got := byKind(records(t, r.Transcript.Root))
	if got["delivery"][0].Route != "LLM" || got["exchange_outcome"][0].Route != "LLM" || got["delivery"][0].Model != "" {
		t.Fatalf("%+v", got)
	}
}

func TestGatewayFallbackIsLabelledFallbackWithoutTheAdapterModel(t *testing.T) {
	r, tr, _, timings, spoken := streamFixture(t)
	r.Transcript = &transcript.Writer{Root: t.TempDir()}
	r.Bridge = labelled("llm", "gpt-6-luna", "Prometheus, a very long reply.")
	r.Synthesize = func(_ context.Context, s string) (simpleradio.Audio, error) {
		*spoken = append(*spoken, s)
		if s == TooLongLine {
			return make(simpleradio.Audio, 16000), nil
		}
		return make(simpleradio.Audio, 40*16000), nil
	}
	r.Process(t.Context(), tr)
	got := byKind(records(t, r.Transcript.Root))
	out := got["exchange_outcome"][0]
	if out.Route != "fallback" || out.Model != "" || (*timings)[0].Outcome != "too_long_fallback_sent" {
		t.Fatalf("%+v %+v", out, (*timings)[0])
	}
	if got["reply_intended"][0].Route != "llm" || got["reply_intended"][0].Model != "gpt-6-luna" {
		t.Fatalf("%+v", got["reply_intended"])
	}
}
