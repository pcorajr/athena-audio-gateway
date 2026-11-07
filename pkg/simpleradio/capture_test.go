package simpleradio

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

func TestCaptureBeforeAdmissionOtherSpeakerAndShortAudio(t *testing.T) {
	for _, other := range []bool{false, true} {
		c, tr, _ := mvpTestClient()
		c.transcript = &transcript.Writer{Root: t.TempDir()}
		origin := tr.route.origin
		if other {
			origin = types.NewGUID()
			c.clients[origin] = types.ClientInfo{GUID: origin, Name: "Other pilot"}
		}
		ctx, cancel := context.WithCancel(t.Context())
		in := make(chan voiceDatagram, 100)
		out := make(chan transmissionPackets, 2)
		done := make(chan struct{})
		go func() { defer close(done); c.receiveMVPVoice(ctx, in, out) }()
		for i := 0; i < 30; i++ {
			p := voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: tr.Radio.Frequency, Modulation: byte(tr.Radio.Modulation)}}, 1, uint64(i+1), 0, []byte(origin), []byte(origin))
			in <- voiceDatagram{data: p.Encode(), epoch: 1}
		}
		select {
		case got := <-out:
			if got.traceID == "" || got.origin != origin {
				t.Fatal("missing reception identity")
			}
			observed := Transmission{Radio: got.radio, route: got.route}
			if c.ValidMVP(observed) == other {
				t.Fatal("capture changed reply permission")
			}
		case <-time.After(2 * time.Second):
			t.Fatal("nonpilot reception was lost")
		}
		// A sub-second transmission is explicitly recorded as unrecognized.
		p := voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: tr.Radio.Frequency, Modulation: byte(tr.Radio.Modulation)}}, 1, 50, 0, []byte(origin), []byte(origin))
		in <- voiceDatagram{data: p.Encode(), epoch: 1}
		time.Sleep(400 * time.Millisecond)
		cancel()
		<-done
		paths, _ := filepath.Glob(filepath.Join(c.transcript.Root, "*.jsonl"))
		raw, _ := os.ReadFile(paths[0])
		if !strings.Contains(string(raw), "short_overflow_or_invalid_audio") {
			t.Fatal("short reception silently lost")
		}
		for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			var rec transcript.Record
			if json.Unmarshal([]byte(line), &rec) != nil {
				t.Fatal("bad transcript")
			}
		}
	}
}

func TestStreamRouteOneShotAndFailureStopsParts(t *testing.T) {
	for _, mode := range []string{"normal", "mute", "cancel", "identity", "oversize"} {
		t.Run(mode, func(t *testing.T) {
			c, tr, w := mvpTestClient()
			send := c.BeginMVPReply(tr)
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			switch mode {
			case "mute":
				c.mute = true
			case "cancel":
				cancel()
			case "identity":
				c.invalidateMVP()
			}
			audio := tr.Audio
			if mode == "oversize" {
				audio = make(Audio, 16000*9)
			}
			first := send(ctx, audio, time.Now().Add(time.Second))
			second := send(ctx, tr.Audio, time.Now().Add(time.Second))
			third := send(ctx, tr.Audio, time.Now().Add(time.Second))
			if third.Outcome != "invalid_or_replayed" {
				t.Fatal("third part allowed")
			}
			if c.TransmitMVP(ctx, tr, time.Now().Add(time.Second)).Outcome != "invalid_or_replayed" {
				t.Fatal("stream token replay")
			}
			if mode == "normal" {
				if first.Outcome != "sent" || second.Outcome != "sent" {
					t.Fatal(first, second)
				}
			} else if !first.FirstSend.IsZero() || !second.FirstSend.IsZero() {
				t.Fatal("unsafe stream write")
			}
			_ = w
		})
	}
}
