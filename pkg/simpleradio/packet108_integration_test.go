package simpleradio_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

func TestPacket108CaptureToBridge(t *testing.T) {
	for _, prefix := range []string{"", "stale_epoch", "self_relay"} {
		name := "admitted"
		if prefix != "" {
			name = prefix
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			writer := &transcript.Writer{Root: t.TempDir()}
			radio, captured, err := simpleradio.CaptureSRSMVPFixture(ctx, writer, prefix)
			if err != nil {
				cancel()
				t.Fatal(err)
			}
			defer func() { cancel(); <-captured }()
			cfg := admission.DefaultConfig()
			cfg.FrequencyHz, cfg.Modulation, cfg.PilotName, cfg.FrequencyToleranceHz = 133000000, admission.ModulationAM, "Prometheus", 0
			gate, err := admission.NewGate(cfg)
			if err != nil {
				t.Fatal(err)
			}
			called := make(chan bridge.Request, 1)
			outcome := make(chan mvp.Timing, 1)
			r := &mvp.Runner{Gate: gate, Radio: radio, MaxAge: 10 * time.Second, Transcript: writer,
				Recognize: func(_ context.Context, audio simpleradio.Audio) (string, error) {
					if prefix != "self_relay" && len(audio) != 32000 {
						t.Errorf("untrusted prefix retained: samples=%d", len(audio))
					}
					return "Athena, test.", nil
				},
				Synthesize: func(context.Context, string) (simpleradio.Audio, error) {
					t.Error("suppressed bridge must not synthesize")
					return nil, nil
				},
				Bridge: bridgeFunc(func(_ context.Context, req bridge.Request) (bridge.Response, error) {
					called <- req
					return bridge.Suppressed(req.TransmissionID, bridge.IntentUnrecognized, bridge.StateOK, "synthetic"), nil
				}), Record: func(timing mvp.Timing) { outcome <- timing },
			}
			done := make(chan struct{})
			go func() { defer close(done); r.Run(ctx, radio.Receive()) }()
			defer func() { cancel(); <-done }()
			if prefix == "self_relay" {
				select {
				case timing := <-outcome:
					if timing.Outcome != "rejected:radio_route_missing:self_relay" {
						t.Fatalf("%+v", timing)
					}
				case <-time.After(2 * time.Second):
					t.Fatal("missing rejected capture outcome")
				}
				if len(called) != 0 {
					t.Fatal("untrusted prefix was promoted to a command")
				}
				paths, _ := filepath.Glob(filepath.Join(writer.Root, "*.jsonl"))
				raw, err := os.ReadFile(paths[0])
				if err != nil || !strings.Contains(string(raw), "rejected:radio_route_missing:self_relay") {
					t.Fatal("missing specific transcript reason")
				}
				return
			}
			select {
			case req := <-called:
				if req.Speaker != "Prometheus" || req.FrequencyHz != 133000000 || req.Modulation != bridge.ModulationAM || req.ReceivedAt.IsZero() {
					t.Fatalf("incorrect captured request: %+v", req)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("admissible capture did not reach bridge")
			}
		})
	}
}
