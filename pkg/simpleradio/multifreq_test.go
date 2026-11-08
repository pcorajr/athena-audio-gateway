package simpleradio

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

// Multi-frequency (2026-11-07): 133.0 AM (A-10C VHF AM), 251.0 AM (UHF, F-4E
// and A-10C), 33.0 FM (A-10C VHF FM). The gateway listens on all of them and
// replies on the channel the pilot transmitted on.
var (
	vhfAM = types.Radio{Frequency: 133000000, Modulation: types.ModulationAM}
	uhfAM = types.Radio{Frequency: 251000000, Modulation: types.ModulationAM}
	vhfFM = types.Radio{Frequency: 33000000, Modulation: types.ModulationFM}
)

func multiFreqClient(t *testing.T) (*Client, Transmission, *mvpWriterFake) {
	t.Helper()
	c, tr, w := mvpTestClient()
	radios := []types.Radio{vhfAM, uhfAM, vhfFM}
	c.clientInfo.RadioInfo.Radios = radios
	c.receivers = map[types.Radio]*receiver{}
	for _, r := range radios {
		c.receivers[r] = &receiver{radio: r}
	}
	c.transcript = &transcript.Writer{Root: t.TempDir()}
	return c, tr, w
}

// capturePilot feeds a >1 s pilot transmission on the given packet
// frequencies through the real assembly path and returns what it captured.
func capturePilot(t *testing.T, c *Client, origin types.GUID, firstID uint64, freqs ...types.Radio) []transmissionPackets {
	t.Helper()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	in := make(chan voiceDatagram, 64)
	out := make(chan transmissionPackets, 4)
	done := make(chan struct{})
	go func() { defer close(done); c.receiveMVPVoice(ctx, in, out) }()
	frequencies := make([]voice.Frequency, 0, len(freqs))
	for _, f := range freqs {
		frequencies = append(frequencies, voice.Frequency{Frequency: f.Frequency, Modulation: byte(f.Modulation)})
	}
	for i := range 30 {
		p := voice.NewPacket([]byte{1}, frequencies, 1, firstID+uint64(i), 0, []byte(origin), []byte(origin))
		in <- voiceDatagram{data: p.Encode(), epoch: c.mvpEpoch}
	}
	var got []transmissionPackets
	deadline := time.After(2 * time.Second)
	for {
		select {
		case tr := <-out:
			got = append(got, tr)
		case <-deadline:
			cancel()
			<-done
			return got
		}
		// Let any second capture (there must be none) surface.
		if len(got) == 1 {
			deadline = time.After(500 * time.Millisecond)
		}
	}
}

func replyFor(base Transmission, got transmissionPackets) Transmission {
	reply := base
	reply.Radio, reply.route = got.radio, got.route
	reply.Audio = make(Audio, frameSize*2)
	return reply
}

func assertTXOn(t *testing.T, w *mvpWriterFake, want types.Radio) {
	t.Helper()
	if len(w.packets) == 0 {
		t.Fatal("nothing transmitted")
	}
	for _, p := range w.packets {
		if len(p.Frequencies) != 1 || p.Frequencies[0].Frequency != want.Frequency ||
			types.Modulation(p.Frequencies[0].Modulation) != want.Modulation || p.Frequencies[0].Encryption != 0 {
			t.Fatalf("reply transmitted on %+v, want %.0f mod %d", p.Frequencies, want.Frequency, want.Modulation)
		}
	}
}

func TestMultiFreqReplyOnCallingChannel(t *testing.T) {
	for _, radio := range []types.Radio{vhfAM, uhfAM, vhfFM} {
		t.Run(fmt.Sprintf("%.3f_%s", radio.Frequency/1e6, mvpModulationName(radio.Modulation)), func(t *testing.T) {
			c, tr, w := multiFreqClient(t)
			got := capturePilot(t, c, tr.route.origin, 1, radio)
			if len(got) != 1 || !exactMVPRadio(got[0].radio, radio) || !exactMVPRadio(got[0].route.radio, radio) {
				t.Fatalf("captured %+v, want one capture on %+v", got, radio)
			}
			d := c.TransmitMVP(t.Context(), replyFor(tr, got[0]), time.Now().Add(2*time.Second))
			if d.Outcome != "sent" {
				t.Fatalf("delivery %+v", d)
			}
			assertTXOn(t, w, radio)
		})
	}
}

func TestMultiFreqNeverTransmitsOnOtherOrUnlistedChannel(t *testing.T) {
	c, tr, w := multiFreqClient(t)
	got := capturePilot(t, c, tr.route.origin, 1, uhfAM)
	if len(got) != 1 {
		t.Fatalf("captures=%d", len(got))
	}
	// Re-pointing the reply at another listed channel breaks the binding.
	moved := replyFor(tr, got[0])
	moved.Radio = vhfAM
	if reason := c.MVPRejection(moved); reason != "radio_channel_mismatch" {
		t.Fatalf("moved reply reason %q", reason)
	}
	if d := c.TransmitMVP(t.Context(), moved, time.Now().Add(time.Second)); d.Outcome == "sent" {
		t.Fatal("reply moved to another channel was sent")
	}
	// An unlisted channel is never encodable or transmittable.
	unlisted := replyFor(tr, got[0])
	unlisted.Radio = types.Radio{Frequency: 40000000, Modulation: types.ModulationFM}
	unlisted.route.radio = unlisted.Radio
	if reason := c.MVPRejection(unlisted); reason != "radio_channel_mismatch" {
		t.Fatalf("unlisted reason %q", reason)
	}
	if _, err := c.encodeMVP(unlisted.Audio, unlisted.Radio); err == nil {
		t.Fatal("encoded on an unlisted channel")
	}
	send := c.BeginMVPReply(unlisted)
	if d := send(t.Context(), unlisted.Audio, time.Now().Add(time.Second)); d.Outcome == "sent" {
		t.Fatal("stream reply on an unlisted channel was sent")
	}
	// Same frequency, other modulation: also unlisted.
	wrongMod := replyFor(tr, got[0])
	wrongMod.Radio = types.Radio{Frequency: 33000000, Modulation: types.ModulationAM}
	wrongMod.route.radio = wrongMod.Radio
	if c.ValidMVP(wrongMod) {
		t.Fatal("33.0 AM accepted while only 33.0 FM is listed")
	}
	if w.writes != 0 {
		t.Fatalf("writer called %d times", w.writes)
	}
}

// Calls on two channels: each is captured separately, carries its own channel
// and is answered on it, in order, without cross-talk.
func TestMultiFreqConcurrentCallsNoCrossTalk(t *testing.T) {
	c, tr, w := multiFreqClient(t)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	in := make(chan voiceDatagram, 128)
	out := make(chan transmissionPackets, 4)
	done := make(chan struct{})
	go func() { defer close(done); c.receiveMVPVoice(ctx, in, out) }()
	origin := []byte(tr.route.origin)
	id := uint64(1)
	for range 30 {
		for _, r := range []types.Radio{vhfAM, vhfFM} {
			p := voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: r.Frequency, Modulation: byte(r.Modulation)}}, 1, id, 0, origin, origin)
			id++
			in <- voiceDatagram{data: p.Encode(), epoch: c.mvpEpoch}
		}
	}
	var got []transmissionPackets
	timeout := time.After(3 * time.Second)
	for len(got) < 2 {
		select {
		case g := <-out:
			got = append(got, g)
		case <-timeout:
			t.Fatalf("captures=%d, want 2", len(got))
		}
	}
	cancel()
	<-done
	seen := map[types.Radio]bool{}
	for _, g := range got {
		seen[mvpChannel(g.radio)] = true
		if !exactMVPRadio(g.radio, g.route.radio) || g.route.used == nil {
			t.Fatalf("capture radio %+v route %+v", g.radio, g.route)
		}
	}
	if !seen[vhfAM] || !seen[vhfFM] {
		t.Fatalf("captured channels %+v", seen)
	}
	for _, g := range got {
		w.packets, w.writes = nil, 0
		d := c.TransmitMVP(t.Context(), replyFor(tr, g), time.Now().Add(2*time.Second))
		if d.Outcome != "sent" {
			t.Fatalf("delivery on %+v: %+v", g.radio, d)
		}
		assertTXOn(t, w, g.radio)
	}
}

// A packet keyed on two of our channels at once is captured, and so answered,
// exactly once, on the first configured channel.
func TestMultiFreqPacketOnTwoChannelsCapturedOnce(t *testing.T) {
	c, tr, _ := multiFreqClient(t)
	got := capturePilot(t, c, tr.route.origin, 1, vhfFM, uhfAM)
	if len(got) != 1 || !exactMVPRadio(got[0].radio, uhfAM) {
		t.Fatalf("captures %+v, want one on 251.0 AM", got)
	}
}

// Audio heard on one channel holds replies on that channel only.
func TestMultiFreqBusyIsPerChannel(t *testing.T) {
	c, tr, w := multiFreqClient(t)
	got := capturePilot(t, c, tr.route.origin, 1, uhfAM)
	if len(got) != 1 {
		t.Fatalf("captures=%d", len(got))
	}
	c.clientsLock.Lock()
	c.mvpChannelUntil = map[types.Radio]time.Time{mvpChannel(vhfAM): time.Now().Add(time.Hour)}
	c.clientsLock.Unlock()
	if d := c.TransmitMVP(t.Context(), replyFor(tr, got[0]), time.Now().Add(2*time.Second)); d.Outcome != "sent" {
		t.Fatalf("251 reply blocked by 133 traffic: %+v", d)
	}
	assertTXOn(t, w, uhfAM)
	got = capturePilot(t, c, tr.route.origin, 100, uhfAM)
	if len(got) != 1 {
		t.Fatalf("captures=%d", len(got))
	}
	c.clientsLock.Lock()
	c.mvpChannelUntil = map[types.Radio]time.Time{mvpChannel(uhfAM): time.Now().Add(time.Hour)}
	c.clientsLock.Unlock()
	w.packets, w.writes = nil, 0
	if d := c.TransmitMVP(t.Context(), replyFor(tr, got[0]), time.Now().Add(100*time.Millisecond)); d.Outcome == "sent" || w.writes != 0 {
		t.Fatalf("reply stepped on a busy channel: %+v", d)
	}
}

func TestMultiFreqConfigAndReadiness(t *testing.T) {
	ok := types.ClientConfiguration{SRSMVP: true, ClientName: "Athena", PilotName: "Prometheus", Radios: []types.Radio{vhfAM, uhfAM, vhfFM}}
	if err := validateMVPConfig(ok); err != nil {
		t.Fatal(err)
	}
	eleven := ok
	eleven.Radios = nil
	for i := range 11 {
		eleven.Radios = append(eleven.Radios, types.Radio{Frequency: float64(100000000 + i*1000000)})
	}
	for _, bad := range [][]types.Radio{
		nil, {vhfAM, uhfAM, vhfAM}, eleven.Radios,
		{vhfAM, {Frequency: 251000000, Modulation: types.ModulationAM, IsEncrypted: true}},
		{vhfAM, {Frequency: 251000000, Modulation: types.ModulationAM, ShouldRetransmit: true}},
		{vhfAM, {Frequency: 251000000, Modulation: types.ModulationMIDS}},
	} {
		cfg := ok
		cfg.Radios = bad
		if validateMVPConfig(cfg) == nil {
			t.Fatalf("invalid radios accepted: %+v", bad)
		}
	}
	c, _, _ := multiFreqClient(t)
	c.clientsLock.Lock()
	r := c.mvpReadinessLocked()
	c.clientsLock.Unlock()
	if r.Outcome != "ready" || r.FrequencyHz != 133000000 || r.Modulation != "AM" ||
		r.Text != "channels=133.000 AM,251.000 AM,33.000 FM" {
		t.Fatalf("readiness %+v", r)
	}
}
