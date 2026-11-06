package mvp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
)

type bridgeFunc func(context.Context, bridge.Request) (bridge.Response, error)

func (f bridgeFunc) Exchange(ctx context.Context, r bridge.Request) (bridge.Response, error) {
	return f(ctx, r)
}

type fakeRadio struct {
	valid atomic.Bool
	sends int
}

func (r *fakeRadio) ValidMVP(simpleradio.Transmission) bool { return r.valid.Load() }
func (r *fakeRadio) TransmitMVP(_ context.Context, _ simpleradio.Transmission, _ time.Time) simpleradio.Delivery {
	r.sends++
	now := time.Now()
	return simpleradio.Delivery{FirstSend: now, LastSend: now, Outcome: "sent"}
}

func runnerFixture(t *testing.T) (*Runner, simpleradio.Transmission, *fakeRadio, *[]Timing) {
	t.Helper()
	cfg := admission.DefaultConfig()
	cfg.FrequencyHz = 133000000
	cfg.Modulation = admission.ModulationAM
	cfg.PilotName = "Prometheus"
	cfg.FrequencyToleranceHz = 0
	gate, err := admission.NewGate(cfg)
	if err != nil {
		t.Fatal(err)
	}
	radio := &fakeRadio{}
	radio.valid.Store(true)
	timings := &[]Timing{}
	r := &Runner{Gate: gate, MaxAge: DefaultMaxAge, Radio: radio,
		Recognize:  func(context.Context, simpleradio.Audio) (string, error) { return "What is my status?", nil },
		Synthesize: func(context.Context, string) (simpleradio.Audio, error) { return make(simpleradio.Audio, 640), nil },
		Record:     func(timing Timing) { *timings = append(*timings, timing) },
		Bridge: bridgeFunc(func(_ context.Context, req bridge.Request) (bridge.Response, error) {
			return bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Synthetic status unavailable.", IntentClass: bridge.IntentFactual, State: bridge.StateUnavailable}, nil
		}),
	}
	tr := simpleradio.Transmission{TraceID: "synthetic-1", ClientName: "Prometheus", Radio: types.Radio{Frequency: 133000000, Modulation: types.ModulationAM}, Audio: make(simpleradio.Audio, 32000), ReceivedAt: time.Now()}
	return r, tr, radio, timings
}

func TestSRSMVPPTTOnlyAndTiming(t *testing.T) {
	r, tr, radio, timings := runnerFixture(t)
	r.Bridge = bridgeFunc(func(_ context.Context, req bridge.Request) (bridge.Response, error) {
		if req.Addressee != "" || req.Transcript != "What is my status?" || req.Speaker != "Prometheus" || req.FrequencyHz != 133000000 || req.Modulation != bridge.ModulationAM {
			t.Fatalf("wrong request: %+v", req)
		}
		return bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Synthetic status unavailable.", IntentClass: bridge.IntentFactual, State: bridge.StateUnavailable}, nil
	})
	r.Process(t.Context(), tr)
	if radio.sends != 1 || len(*timings) != 1 {
		t.Fatalf("sends=%d timings=%d", radio.sends, len(*timings))
	}
	got := (*timings)[0]
	if got.Received != 0 || got.STTDone < 0 || got.ReplyReceived < got.STTDone || got.TTSDone < got.ReplyReceived || got.FirstSend < got.TTSDone || got.LastSend < got.FirstSend || got.Outcome != "sent" {
		t.Fatalf("bad timings: %+v", got)
	}
}

func TestSRSMVPFailureMatrix(t *testing.T) {
	for _, stage := range []string{"oversize", "wrong_speaker", "wrong_frequency", "wrong_modulation", "stt_error", "stt_empty", "stt_late", "bridge_error", "wrong_id", "non_factual", "suppressed", "late_reply", "identity_changed", "tts_error", "tts_late", "canceled"} {
		t.Run(stage, func(t *testing.T) {
			r, tr, radio, timings := runnerFixture(t)
			synthCalls := 0
			synth := r.Synthesize
			r.Synthesize = func(ctx context.Context, s string) (simpleradio.Audio, error) { synthCalls++; return synth(ctx, s) }
			base := r.Bridge
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			switch stage {
			case "oversize":
				tr.Audio = make(simpleradio.Audio, 16000*30+1)
			case "wrong_speaker":
				tr.ClientName = "other"
			case "wrong_frequency":
				tr.Radio.Frequency++
			case "wrong_modulation":
				tr.Radio.Modulation = types.ModulationFM
			case "stt_error":
				r.Recognize = func(context.Context, simpleradio.Audio) (string, error) { return "", errors.New("synthetic") }
			case "stt_empty":
				r.Recognize = func(context.Context, simpleradio.Audio) (string, error) { return "  ", nil }
			case "stt_late":
				r.MaxAge = 10 * time.Millisecond
				r.Recognize = func(ctx context.Context, _ simpleradio.Audio) (string, error) { <-ctx.Done(); return "status", nil }
			case "bridge_error":
				r.Bridge = bridgeFunc(func(context.Context, bridge.Request) (bridge.Response, error) {
					return bridge.Response{}, errors.New("synthetic")
				})
			case "wrong_id", "non_factual", "suppressed", "identity_changed":
				r.Bridge = bridgeFunc(func(ctx context.Context, req bridge.Request) (bridge.Response, error) {
					resp, _ := base.Exchange(ctx, req)
					switch stage {
					case "wrong_id":
						resp.TransmissionID = "other"
					case "non_factual":
						resp.IntentClass = bridge.IntentActionPreview
					case "suppressed":
						resp = bridge.Suppressed(req.TransmissionID, bridge.IntentUnrecognized, bridge.StateOK, "synthetic")
					case "identity_changed":
						radio.valid.Store(false)
					}
					return resp, nil
				})
			case "late_reply":
				r.MaxAge = 10 * time.Millisecond
				r.Bridge = bridgeFunc(func(ctx context.Context, req bridge.Request) (bridge.Response, error) {
					<-ctx.Done()
					return base.Exchange(ctx, req)
				})
			case "tts_error":
				r.Synthesize = func(context.Context, string) (simpleradio.Audio, error) { return nil, errors.New("synthetic") }
			case "tts_late":
				r.MaxAge = 10 * time.Millisecond
				r.Synthesize = func(ctx context.Context, _ string) (simpleradio.Audio, error) {
					<-ctx.Done()
					return make(simpleradio.Audio, 640), nil
				}
			case "canceled":
				cancel()
			}
			r.Process(ctx, tr)
			if radio.sends != 0 || len(*timings) != 1 || (*timings)[0].FirstSend != -1 || (*timings)[0].LastSend != -1 {
				t.Fatalf("unsafe delivery or missing timing: sends=%d timings=%+v", radio.sends, *timings)
			}
			if stage != "tts_error" && stage != "tts_late" && synthCalls != 0 {
				t.Fatal("failed response reached TTS")
			}
		})
	}
}

func TestSRSMVPContentFreeTimingLog(t *testing.T) {
	r, tr, _, _ := runnerFixture(t)
	var output bytes.Buffer
	original := log.Logger
	log.Logger = zerolog.New(&output)
	t.Cleanup(func() { log.Logger = original })
	r.Record = nil
	r.Process(t.Context(), tr)
	r.Process(t.Context(), tr) // duplicate must not produce a second exchange/log
	if strings.Count(output.String(), "\n") != 1 {
		t.Fatal("expected exactly one timing line")
	}
	var record map[string]any
	if err := json.Unmarshal(output.Bytes(), &record); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"received", "stt_done", "reply_received", "tts_done", "first_send", "last_send"} {
		if _, ok := record[key].(float64); !ok {
			t.Fatalf("missing numeric ms clock %s", key)
		}
	}
	if len(record) != 10 || record["message"] != "srs_mvp_timing_ms" || strings.Contains(output.String(), "status") || strings.Contains(output.String(), "Prometheus") {
		t.Fatal("timing record contains unexpected/content fields")
	}
}

func TestSRSMVPOneExchangeInFlight(t *testing.T) {
	r, tr, radio, timings := runnerFixture(t)
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var calls atomic.Int32
	base := r.Bridge
	r.Bridge = bridgeFunc(func(ctx context.Context, req bridge.Request) (bridge.Response, error) {
		calls.Add(1)
		close(entered)
		<-release
		return base.Exchange(ctx, req)
	})
	go func() { defer close(done); r.Process(t.Context(), tr) }()
	<-entered
	r.Process(t.Context(), tr)
	close(release)
	<-done
	if calls.Load() != 1 || radio.sends != 1 || len(*timings) != 1 {
		t.Fatalf("queued or concurrent exchange: calls=%d sends=%d timings=%d", calls.Load(), radio.sends, len(*timings))
	}
}
