package mvp

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
)

// channelRadio records the channel each reply was handed to the transport on.
type channelRadio struct {
	mu   sync.Mutex
	sent []types.Radio
	ids  []string
}

func (*channelRadio) ValidMVP(simpleradio.Transmission) bool { return true }
func (r *channelRadio) TransmitMVP(_ context.Context, t simpleradio.Transmission, _ time.Time) simpleradio.Delivery {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sent = append(r.sent, t.Radio)
	r.ids = append(r.ids, t.TraceID)
	now := time.Now()
	return simpleradio.Delivery{FirstSend: now, LastSend: now, Outcome: "sent"}
}

// Multi-frequency (2026-11-07): calls on 251.0 AM and 33.0 FM are answered in
// arrival order, each on its own channel; a call on an unlisted channel is
// never answered. Transcript lines carry each call's channel.
func TestMultiFreqRunnerRepliesOnCallingChannelInOrder(t *testing.T) {
	cfg := admission.DefaultConfig()
	cfg.PilotName = "Prometheus"
	cfg.FrequencyToleranceHz = 0
	cfg.Channels = []admission.Channel{
		{FrequencyHz: 133000000, Modulation: admission.ModulationAM},
		{FrequencyHz: 251000000, Modulation: admission.ModulationAM},
		{FrequencyHz: 33000000, Modulation: admission.ModulationFM},
	}
	cfg.FrequencyHz, cfg.Modulation = 133000000, admission.ModulationAM
	gate, err := admission.NewGate(cfg)
	if err != nil {
		t.Fatal(err)
	}
	radio := &channelRadio{}
	var mu sync.Mutex
	var requests []bridge.Request
	root := filepath.Join(t.TempDir(), "transcript")
	r := &Runner{Gate: gate, MaxAge: DefaultMaxAge, Radio: radio, Transcript: &transcript.Writer{Root: root},
		Recognize:  func(context.Context, simpleradio.Audio) (string, error) { return "Athena, radio check.", nil },
		Synthesize: func(context.Context, string) (simpleradio.Audio, error) { return make(simpleradio.Audio, 640), nil },
		Record:     func(Timing) {},
		Bridge: bridgeFunc(func(_ context.Context, req bridge.Request) (bridge.Response, error) {
			mu.Lock()
			requests = append(requests, req)
			mu.Unlock()
			return bridge.Response{TransmissionID: req.TransmissionID, Deliverable: true, Speech: "Loud and clear.", IntentClass: bridge.IntentFactual, State: bridge.StateUnavailable}, nil
		}),
	}
	uhf := types.Radio{Frequency: 251000000, Modulation: types.ModulationAM}
	fm := types.Radio{Frequency: 33000000, Modulation: types.ModulationFM}
	unlisted := types.Radio{Frequency: 40000000, Modulation: types.ModulationFM}
	in := make(chan simpleradio.Transmission, 4)
	now := time.Now()
	for i, radio := range []types.Radio{uhf, fm, unlisted} {
		in <- simpleradio.Transmission{TraceID: []string{"call-251", "call-33", "call-40"}[i], ClientName: "Prometheus", Radio: radio, Audio: make(simpleradio.Audio, 32000), ReceivedAt: now}
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan struct{})
	go func() { defer close(done); r.Run(ctx, in) }()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		radio.mu.Lock()
		n := len(radio.sent)
		radio.mu.Unlock()
		if n >= 2 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(100 * time.Millisecond) // let the unlisted call be (not) answered
	cancel()
	<-done
	radio.mu.Lock()
	defer radio.mu.Unlock()
	if len(radio.sent) != 2 || radio.ids[0] != "call-251" || radio.ids[1] != "call-33" {
		t.Fatalf("replies %v on %+v, want call-251 then call-33", radio.ids, radio.sent)
	}
	if radio.sent[0] != uhf || radio.sent[1] != fm {
		t.Fatalf("cross-talk: replies on %+v", radio.sent)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(requests) != 2 || requests[0].FrequencyHz != 251000000 || requests[0].Modulation != bridge.ModulationAM ||
		requests[1].FrequencyHz != 33000000 || requests[1].Modulation != bridge.ModulationFM {
		t.Fatalf("bridge requests %+v", requests)
	}
	paths, _ := filepath.Glob(filepath.Join(root, "*.txt"))
	if len(paths) != 1 {
		t.Fatalf("transcript files %v", paths)
	}
	raw, err := os.ReadFile(paths[0])
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	for _, want := range []string{" 251.000 AM Athena [delivery call-251", " 33.000 FM Athena [delivery call-33", " 40.000 FM Prometheus [exchange_outcome call-40"} {
		if !strings.Contains(text, want) {
			t.Errorf("transcript missing %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "[delivery call-40") {
		t.Fatal("unlisted channel call was answered")
	}
}
