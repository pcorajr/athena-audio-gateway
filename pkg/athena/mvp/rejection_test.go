package mvp

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

func TestPacket108RejectionReasons(t *testing.T) {
	for _, tc := range []struct {
		want   string
		mutate func(*Runner, *simpleradio.Transmission)
	}{
		{"gate_unavailable", func(r *Runner, _ *simpleradio.Transmission) { r.Gate = nil }},
		{"radio_unavailable", func(r *Runner, _ *simpleradio.Transmission) { r.Radio = nil }},
		{"radio_unavailable", func(r *Runner, _ *simpleradio.Transmission) { r.Radio = (*simpleradio.Client)(nil) }},
		{"bridge_unavailable", func(r *Runner, _ *simpleradio.Transmission) { r.Bridge = nil }},
		{"recognizer_unavailable", func(r *Runner, _ *simpleradio.Transmission) { r.Recognize = nil }},
		{"synthesizer_unavailable", func(r *Runner, _ *simpleradio.Transmission) { r.Synthesize = nil }},
		{"invalid_max_age", func(r *Runner, _ *simpleradio.Transmission) { r.MaxAge = 0 }},
		{"received_at_missing", func(_ *Runner, tr *simpleradio.Transmission) { tr.ReceivedAt = time.Time{} }},
		{"received_at_future", func(_ *Runner, tr *simpleradio.Transmission) { tr.ReceivedAt = time.Now().Add(time.Hour) }},
		{"admission_frequency_mismatch", func(_ *Runner, tr *simpleradio.Transmission) { tr.Radio.Frequency++ }},
		{"admission_modulation_mismatch", func(_ *Runner, tr *simpleradio.Transmission) { tr.Radio.Modulation = 1 }},
		{"admission_speaker_unresolved", func(_ *Runner, tr *simpleradio.Transmission) { tr.ClientName = "" }},
		{"admission_speaker_not_authorized", func(_ *Runner, tr *simpleradio.Transmission) { tr.ClientName = "other" }},
		{"admission_utterance_empty", func(_ *Runner, tr *simpleradio.Transmission) { tr.Audio = nil }},
		{"admission_utterance_too_short", func(_ *Runner, tr *simpleradio.Transmission) { tr.Audio = tr.Audio[:1] }},
		{"admission_utterance_too_large", func(_ *Runner, tr *simpleradio.Transmission) { tr.Audio = make(simpleradio.Audio, 480001) }},
		{"radio_invalid", func(r *Runner, _ *simpleradio.Transmission) { r.Radio.(*fakeRadio).valid.Store(false) }},
	} {
		t.Run(tc.want, func(t *testing.T) {
			r, tr, radio, timings := runnerFixture(t)
			r.Transcript = &transcript.Writer{Root: t.TempDir()}
			calls := 0
			r.Bridge = bridgeFunc(func(context.Context, bridge.Request) (bridge.Response, error) { calls++; return bridge.Response{}, nil })
			tc.mutate(r, &tr)
			r.Process(t.Context(), tr)
			want := "rejected:" + tc.want
			if calls != 0 || radio.sends != 0 || len(*timings) != 1 || (*timings)[0].Outcome != want {
				t.Fatalf("calls=%d sends=%d timings=%+v want=%s", calls, radio.sends, *timings, want)
			}
			paths, _ := filepath.Glob(filepath.Join(r.Transcript.Root, "*.jsonl"))
			f, err := os.Open(paths[0])
			if err != nil {
				t.Fatal(err)
			}
			defer f.Close()
			decoder := json.NewDecoder(f)
			var in, out transcript.Record
			if decoder.Decode(&in) != nil || decoder.Decode(&out) != nil || out.Kind != "exchange_outcome" || out.Outcome != want {
				t.Fatalf("missing specific transcript rejection: %+v", out)
			}
		})
	}
}

func TestPacket108LaterInvalidationReason(t *testing.T) {
	for _, stage := range []string{"expired", "canceled", "radio_invalid"} {
		t.Run(stage, func(t *testing.T) {
			r, tr, radio, timings := runnerFixture(t)
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			base := r.Bridge
			r.Bridge = bridgeFunc(func(ctx context.Context, req bridge.Request) (bridge.Response, error) {
				switch stage {
				case "expired":
					<-ctx.Done()
				case "canceled":
					cancel()
				case "radio_invalid":
					radio.valid.Store(false)
				}
				return base.Exchange(ctx, req)
			})
			if stage == "expired" {
				r.MaxAge = 20 * time.Millisecond
			}
			r.Process(ctx, tr)
			if radio.sends != 0 || (*timings)[0].Outcome != stage {
				t.Fatalf("%+v", *timings)
			}
		})
	}
}
