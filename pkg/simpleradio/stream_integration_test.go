package simpleradio_test

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRadioV2PythonStreamingBeforeFinalOutput(t *testing.T) {
	if os.Getenv("SRSMVP_TEXT_ADAPTER_ROOT") == "" {
		t.Skip("explicit composition paths required")
	}
	for _, mode := range []string{"normal", "muted", "live_tx_off", "tts_error", "cancel", "unaddressed", "duplicate"} {
		t.Run(mode, func(t *testing.T) {
			ack := filepath.Join(t.TempDir(), "tts-entered")
			root := t.TempDir()
			url := startTextFixtureWithAck(t, "streaming", ack, root)
			client, err := bridge.NewMVPClient(url, 4*time.Second, 350)
			if err != nil {
				t.Fatal(err)
			}
			radio, tr, writes := simpleradio.NewSRSMVPFixtureClient(mode == "muted", mode != "live_tx_off")
			cfg := admission.DefaultConfig()
			cfg.FrequencyHz = 133000000
			cfg.Modulation = admission.ModulationAM
			cfg.PilotName = "Prometheus"
			cfg.FrequencyToleranceHz = 0
			gate, err := admission.NewGate(cfg)
			if err != nil {
				t.Fatal(err)
			}
			text := "Athena, tell me the picture naturally."
			if mode == "unaddressed" {
				text = "Other pilot, picture."
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			syntheses := 0
			r := &mvp.Runner{Gate: gate, Bridge: client, Radio: radio, MaxAge: 10 * time.Second, Transcript: &transcript.Writer{Root: root},
				Recognize: func(context.Context, simpleradio.Audio) (string, error) { return text, nil },
				Synthesize: func(_ context.Context, s string) (simpleradio.Audio, error) {
					syntheses++
					if syntheses == 1 && s != "Prometheus, synthetic first sentence." {
						t.Errorf("first speech=%q", s)
					}
					if err := os.WriteFile(ack, []byte("synthetic TTS entered"), 0600); err != nil {
						t.Fatal(err)
					}
					if mode == "tts_error" {
						return nil, errors.New("synthetic private error")
					}
					if mode == "cancel" {
						cancel()
					}
					return make(simpleradio.Audio, 1280), nil
				}, Record: func(mvp.Timing) {},
			}
			r.Process(ctx, tr)
			if mode == "duplicate" {
				r.Process(ctx, tr)
			}
			if mode == "normal" || mode == "duplicate" {
				if syntheses != 2 || writes() != 4 {
					t.Fatalf("synthesis=%d writes=%d", syntheses, writes())
				}
			} else if writes() != 0 {
				t.Fatal("interlocked stream wrote radio")
			}
			if mode == "unaddressed" && syntheses != 0 {
				t.Fatal("noncommand got speech")
			}
			if mode == "normal" {
				counts := map[string]int{}
				paths, err := filepath.Glob(filepath.Join(root, "*.jsonl"))
				if err != nil {
					t.Fatal(err)
				}
				for _, path := range paths {
					raw, err := os.ReadFile(path)
					if err != nil {
						t.Fatal(err)
					}
					for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
						var rec transcript.Record
						if err := json.Unmarshal([]byte(line), &rec); err != nil {
							t.Fatal(err)
						}
						wantSpeaker := "Prometheus"
						switch rec.Kind {
						case "reply_generated", "reply_intended", "delivery":
							wantSpeaker = "Athena"
						}
						if rec.Speaker != wantSpeaker || rec.ID != tr.TraceID || rec.FrequencyHz != 133000000 || rec.Modulation != "AM" {
							t.Fatalf("incorrect transcript identity: %+v", rec)
						}
						if rec.Kind == "reply_generated" && rec.Outcome != "intended_not_sent" || rec.Kind == "delivery" && rec.Outcome != "sent" {
							t.Fatalf("generated/delivery conflated: %+v", rec)
						}
						counts[rec.Kind]++
					}
				}
				for kind, want := range map[string]int{"inbound": 1, "adapter_ingress": 1, "reply_generated": 2, "reply_intended": 2, "delivery": 2} {
					if counts[kind] != want {
						t.Fatalf("%s count=%d want=%d", kind, counts[kind], want)
					}
				}
			}
		})
	}
}
