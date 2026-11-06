package simpleradio

import (
	"context"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/coalitions"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

type mvpWriterFake struct {
	writes  int
	packets []voice.Packet
	fail    bool
}

func (w *mvpWriterFake) Write(b []byte) (int, error) {
	w.writes++
	if w.fail {
		return 0, io.ErrClosedPipe
	}
	p, err := voice.Decode(b)
	if err != nil {
		return 0, err
	}
	w.packets = append(w.packets, *p)
	return len(b), nil
}

func mvpTestClient() (*Client, Transmission, *mvpWriterFake) {
	radio := types.Radio{Frequency: 133000000, Modulation: types.ModulationAM}
	peer := types.ClientInfo{GUID: "OriginGUID000000000000", Name: "Prometheus", Coalition: coalitions.Blue, RadioInfo: types.RadioInfo{UnitID: 123, Radios: []types.Radio{radio}}}
	writer := &mvpWriterFake{}
	c := &Client{mvp: true, liveTX: true, pilotName: "Prometheus", mvpEpoch: 1, mvpWriter: writer, packetNumber: 1,
		clientInfo: types.ClientInfo{GUID: "RelayGUID0000000000000", Name: "gateway", Coalition: coalitions.Blue, RadioInfo: types.RadioInfo{Radios: []types.Radio{radio}}},
		clients:    map[types.GUID]types.ClientInfo{peer.GUID: peer}, receivers: map[types.Radio]*receiver{radio: {radio: radio}}}
	transmission := Transmission{TraceID: "synthetic-1", ClientName: "Prometheus", Radio: radio, ReceivedAt: time.Now(), Audio: make(Audio, frameSize*2), route: mvpRoute{origin: peer.GUID, epoch: 1, used: &atomic.Bool{}}}
	return c, transmission, writer
}

func TestSRSMVPInterlocksNeverCallWriter(t *testing.T) {
	for _, test := range []struct {
		name       string
		mute, live bool
	}{{"muted", true, true}, {"default_off", false, false}, {"both", true, false}} {
		t.Run(test.name, func(t *testing.T) {
			c, tr, w := mvpTestClient()
			c.mute, c.liveTX = test.mute, test.live
			for _, phase := range []string{"connected", "failure", "reconnected"} {
				tr.route.used = &atomic.Bool{}
				if phase == "failure" {
					c.invalidateMVP()
				}
				if phase == "reconnected" {
					peer := types.ClientInfo{GUID: tr.route.origin, Name: "Prometheus", Coalition: coalitions.Blue, RadioInfo: types.RadioInfo{Radios: []types.Radio{tr.Radio}}}
					c.syncClients([]types.ClientInfo{peer})
					c.mvpWriter = w
					tr.route.epoch = c.mvpEpoch
				}
				c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
				// Exercise both alternate/legacy entrances as well.
				c.Transmit(tr)
				c.writePackets(nil)
				c.sendMVP(t.Context(), tr, time.Now().Add(time.Second), []voice.Packet{voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: tr.Radio.Frequency}}, 1, 1, 0, nil, nil)})
			}
			if w.writes != 0 {
				t.Fatalf("writer called %d times", w.writes)
			}
		})
	}
}

func TestSRSMVPOneRadioAndNoReplay(t *testing.T) {
	c, tr, w := mvpTestClient()
	d := c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
	if d.Outcome != "sent" || d.FirstSend.IsZero() || d.LastSend.Before(d.FirstSend) || w.writes != 2 {
		t.Fatalf("delivery=%+v writes=%d", d, w.writes)
	}
	for _, packet := range w.packets {
		if len(packet.Frequencies) != 1 || packet.Frequencies[0].Frequency != 133000000 || packet.Frequencies[0].Modulation != 0 {
			t.Fatalf("wrong TX route: %+v", packet.Frequencies)
		}
	}
	c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
	if w.writes != 2 {
		t.Fatal("replayed a transmission")
	}
	c.clientInfo.RadioInfo.Radios = append(c.clientInfo.RadioInfo.Radios, types.Radio{Frequency: 250000000})
	if _, err := c.encodeMVP(tr.Audio); err == nil {
		t.Fatal("encoded multiple radios")
	}
	ch := make(chan []voice.Packet, 1)
	c.encodeVoice(t.Context(), ch)
	if len(ch) != 0 {
		t.Fatal("legacy encoder reachable")
	}
}

func TestSRSMVPIdentityInvalidation(t *testing.T) {
	for _, kind := range []string{"reconnect", "rename", "duplicate", "tune_away", "modulation", "unit", "seat", "empty_radios", "snapshot_replaced", "disconnect"} {
		t.Run(kind, func(t *testing.T) {
			c, tr, w := mvpTestClient()
			peer := c.clients[tr.route.origin]
			switch kind {
			case "reconnect":
				c.invalidateMVP()
				c.syncClients([]types.ClientInfo{peer})
				c.mvpWriter = w
			case "rename":
				peer.Name = "other"
				c.syncClient(peer)
			case "duplicate":
				peer.GUID = "other-guid"
				c.syncClient(peer)
			case "tune_away":
				peer.RadioInfo.Radios = []types.Radio{{Frequency: 250000000}}
				c.syncClient(peer)
			case "modulation":
				peer.RadioInfo.Radios = []types.Radio{{Frequency: tr.Radio.Frequency, Modulation: types.ModulationFM}}
				c.syncClient(peer)
			case "unit":
				peer.RadioInfo.UnitID++
				c.syncClient(peer)
			case "seat":
				peer.Seat++
				c.syncClient(peer)
			case "empty_radios":
				peer.RadioInfo.Radios = nil
				c.syncClient(peer)
			case "snapshot_replaced":
				c.syncClients(nil)
			case "disconnect":
				c.removeClient(peer)
			}
			if c.ValidMVP(tr) {
				t.Fatal("old identity still valid")
			}
			c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
			if w.writes != 0 {
				t.Fatal("invalidated reply reached writer")
			}
		})
	}
}

func TestSRSMVPExpiryAndBusyChannel(t *testing.T) {
	for _, kind := range []string{"expired", "busy", "canceled", "other_pilot_busy"} {
		t.Run(kind, func(t *testing.T) {
			c, tr, w := mvpTestClient()
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			expires := time.Now().Add(40 * time.Millisecond)
			switch kind {
			case "expired":
				expires = time.Now().Add(-time.Second)
			case "busy":
				c.receivers[tr.Radio].deadline = time.Now().Add(time.Hour)
			case "canceled":
				cancel()
			case "other_pilot_busy":
				p := voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: tr.Radio.Frequency}}, 1, 1, 0, []byte("other"), []byte("other"))
				if _, ok := c.admitMVPPacket(&p, c.mvpEpoch); ok {
					t.Fatal("other pilot admitted")
				}
			}
			start := time.Now()
			c.TransmitMVP(ctx, tr, expires)
			if time.Since(start) > time.Second || w.writes != 0 {
				t.Fatalf("unbounded wait or voice write: %d", w.writes)
			}
		})
	}
}

func TestSRSMVPWholeUtteranceOverflow(t *testing.T) {
	for _, count := range []int{maxRxPackets, maxRxPackets + 1, maxRxPackets * 2} {
		r := &receiver{}
		for i := 1; i <= count; i++ {
			r.receive(&voice.Packet{OriginGUID: []byte("pilot"), PacketID: uint64(i)}, mvpRoute{})
		}
		r.deadline = time.Now().Add(-time.Second)
		transmission, ok := r.takeCompleted()
		if count == maxRxPackets {
			if !ok || len(transmission.packets) != count {
				t.Fatal("boundary utterance dropped")
			}
		} else if ok || len(transmission.packets) != 0 {
			t.Fatal("oversized utterance prefix escaped")
		}
		if r.origin != "" || r.overflow {
			t.Fatal("receiver did not reset")
		}
	}
}

func TestSRSMVPPacketAdmission(t *testing.T) {
	for _, kind := range []string{"valid", "wrong_frequency", "near_frequency", "wrong_modulation", "encrypted", "self", "reflected", "old_epoch", "unknown"} {
		t.Run(kind, func(t *testing.T) {
			c, tr, _ := mvpTestClient()
			p := voice.NewPacket([]byte{1}, []voice.Frequency{{Frequency: tr.Radio.Frequency}}, 1, 1, 0, []byte(tr.route.origin), []byte(tr.route.origin))
			epoch := c.mvpEpoch
			switch kind {
			case "wrong_frequency":
				p.Frequencies[0].Frequency = 250000000
			case "near_frequency":
				p.Frequencies[0].Frequency++
			case "wrong_modulation":
				p.Frequencies[0].Modulation = 1
			case "encrypted":
				p.Frequencies[0].Encryption = 1
			case "self":
				p.OriginGUID = []byte(c.clientInfo.GUID)
			case "reflected":
				p.RelayGUID = []byte(c.clientInfo.GUID)
			case "old_epoch":
				epoch--
			case "unknown":
				p.OriginGUID = []byte("unknown")
			}
			_, ok := c.admitMVPPacket(&p, epoch)
			if ok != (kind == "valid") {
				t.Fatalf("admitted=%v", ok)
			}
			if ok {
				if _, duplicate := c.admitMVPPacket(&p, epoch); duplicate {
					t.Fatal("duplicate packet admitted")
				}
			}
		})
	}
}

func TestSRSMVPTransportFailureNeverRetries(t *testing.T) {
	c, tr, w := mvpTestClient()
	w.fail = true
	if d := c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second)); d.Outcome != "write_failed" {
		t.Fatalf("%+v", d)
	}
	c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
	if w.writes != 1 {
		t.Fatalf("write attempts=%d", w.writes)
	}
}

func TestSRSMVPIdentityConcurrentAccess(t *testing.T) {
	c, tr, _ := mvpTestClient()
	peer := c.clients[tr.route.origin]
	var wg sync.WaitGroup
	wg.Go(func() {
		for range 100 {
			c.syncClients([]types.ClientInfo{peer})
			c.removeClient(peer)
		}
	})
	wg.Go(func() {
		for range 100 {
			c.ValidMVP(tr)
		}
	})
	wg.Wait()
}

func TestSRSMVPPartialUtteranceRejection(t *testing.T) {
	for _, failure := range []string{"malformed", "wall_duration", "identity_epoch"} {
		t.Run(failure, func(t *testing.T) {
			r := &receiver{}
			for i := 1; i <= 26; i++ {
				r.receive(&voice.Packet{OriginGUID: []byte("pilot"), PacketID: uint64(i)}, mvpRoute{epoch: 1})
			}
			switch failure {
			case "malformed":
				r.rejectCurrent()
			case "wall_duration":
				r.startedAt = time.Now().Add(-MaxTransmissionDuration - time.Second)
				r.receive(&voice.Packet{OriginGUID: []byte("pilot"), PacketID: 27}, mvpRoute{epoch: 1})
			case "identity_epoch":
				r.receive(&voice.Packet{OriginGUID: []byte("pilot"), PacketID: 27}, mvpRoute{epoch: 2})
			}
			r.deadline = time.Now().Add(-time.Second)
			if _, ok := r.takeCompleted(); ok {
				t.Fatal("partial utterance admitted")
			}
		})
	}
}

func TestSRSMVPOldConnectionMetadataIgnored(t *testing.T) {
	c, tr, w := mvpTestClient()
	old, current := &net.TCPConn{}, &net.TCPConn{}
	c.tcpConnection = current
	c.handleConnectionMessage(old, types.Message{Type: types.MessageSync})
	if !c.ValidMVP(tr) {
		t.Fatal("old connection changed current identity")
	}
	c.handleConnectionMessage(current, types.Message{Type: types.MessageExternalAWACSModeDisconnect})
	c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
	if c.ValidMVP(tr) || w.writes != 0 {
		t.Fatal("EAM disconnect retained a deliverable route")
	}
}

type notifyingWriter struct {
	*mvpWriterFake
	first chan struct{}
}

func (w *notifyingWriter) Write(b []byte) (int, error) {
	n, err := w.mvpWriterFake.Write(b)
	if w.writes == 1 {
		close(w.first)
	}
	return n, err
}

func TestSRSMVPIdentityChangeDuringSend(t *testing.T) {
	c, tr, w := mvpTestClient()
	tr.Audio = make(Audio, 16000*10)
	notify := &notifyingWriter{mvpWriterFake: w, first: make(chan struct{})}
	c.mvpWriter = notify
	done := make(chan Delivery, 1)
	go func() { done <- c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second)) }()
	<-notify.first
	c.clientsLock.Lock()
	c.mvpEpoch++
	before := w.writes
	c.clientsLock.Unlock()
	select {
	case result := <-done:
		if result.Outcome != "invalidated" || w.writes != before {
			t.Fatalf("writes continued after identity change: %+v", result)
		}
	case <-time.After(time.Second):
		t.Fatal("identity invalidation did not stop TX")
	}
}

func TestSRSMVPFinishStartedReplyAfterAgeLimit(t *testing.T) {
	c, tr, w := mvpTestClient()
	tr.Audio = make(Audio, frameSize*20)
	expires := time.Now().Add(300 * time.Millisecond)
	result := c.TransmitMVP(t.Context(), tr, expires)
	if result.Outcome != "sent" || w.writes != 20 || !result.LastSend.After(expires) {
		t.Fatalf("started response was clipped: %+v writes=%d", result, w.writes)
	}
}

func TestSRSMVPReceiveDecodePipeline(t *testing.T) {
	for _, scenario := range []string{"valid", "overflow", "malformed_opus"} {
		t.Run(scenario, func(t *testing.T) {
			c, tr, _ := mvpTestClient()
			encoded, err := c.encodeMVP(make(Audio, frameSize))
			if err != nil {
				t.Fatal(err)
			}
			count := 26
			if scenario == "overflow" {
				count = maxRxPackets + 1
			}
			in := make(chan voiceDatagram, count)
			for i := 1; i <= count; i++ {
				p := encoded[0]
				p.PacketID = uint64(i)
				p.OriginGUID, p.RelayGUID = []byte(tr.route.origin), []byte(tr.route.origin)
				if scenario == "malformed_opus" && i == 20 {
					p = voice.NewPacket([]byte{0x03}, p.Frequencies, p.UnitID, p.PacketID, 0, p.RelayGUID, p.OriginGUID)
				}
				in <- voiceDatagram{data: p.Encode(), epoch: c.mvpEpoch}
			}
			out := make(chan transmissionPackets)
			c.rxChan = make(chan Transmission, 1)
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			var wg sync.WaitGroup
			wg.Go(func() { c.receiveVoice(ctx, in, out) })
			wg.Go(func() { c.decodeVoice(ctx, out) })
			select {
			case got := <-c.rxChan:
				if scenario != "valid" || !c.ValidMVP(got) || got.ReceivedAt.IsZero() || len(got.Audio) != 26*int(frameSize) {
					t.Errorf("unexpected decoded utterance for %s", scenario)
				}
			case <-ctx.Done():
				if scenario == "valid" {
					t.Error("valid fake packet stream not decoded")
				}
			}
			cancel()
			wg.Wait()
		})
	}
}

type deadlineWriter struct {
	*mvpWriterFake
	deadlines          []time.Time
	failSet, failReset bool
}

func (w *deadlineWriter) SetWriteDeadline(deadline time.Time) error {
	w.deadlines = append(w.deadlines, deadline)
	if (deadline.IsZero() && w.failReset) || (!deadline.IsZero() && w.failSet) {
		return io.ErrClosedPipe
	}
	return nil
}

func TestSRSMVPWriteDeadlinesAndSendClocks(t *testing.T) {
	for _, scenario := range []string{"normal", "set_failure", "reset_failure"} {
		t.Run(scenario, func(t *testing.T) {
			c, tr, w := mvpTestClient()
			dw := &deadlineWriter{mvpWriterFake: w, failSet: scenario == "set_failure", failReset: scenario == "reset_failure"}
			c.mvpWriter = dw
			d := c.TransmitMVP(t.Context(), tr, time.Now().Add(time.Second))
			switch scenario {
			case "normal":
				if d.Outcome != "sent" || len(dw.deadlines) != 2*w.writes || !dw.deadlines[len(dw.deadlines)-1].IsZero() {
					t.Fatal("write deadline not restored for pings")
				}
			case "set_failure":
				if w.writes != 0 || !d.FirstSend.IsZero() {
					t.Fatal("write attempted without deadline")
				}
			case "reset_failure":
				if w.writes != 1 || d.FirstSend.IsZero() || d.LastSend.IsZero() || d.Outcome != "write_failed" {
					t.Fatal("successful packet write was missing from clocks")
				}
			}
		})
	}
}

func TestSRSMVPConfigBeforeConnect(t *testing.T) {
	for _, cfg := range []types.ClientConfiguration{
		{SRSMVP: true},
		{SRSMVP: true, PilotName: "Prometheus", Radios: []types.Radio{{Frequency: 133000000}, {Frequency: 250000000}}},
		{SRSMVP: true, PilotName: "Prometheus", Radios: []types.Radio{{Frequency: 133000000, ShouldRetransmit: true}}},
		{SRSMVP: true, PilotName: "Prometheus", Radios: []types.Radio{{Frequency: 133000000, Modulation: types.ModulationMIDS}}},
	} {
		if _, err := NewClient(cfg); err == nil {
			t.Fatal("invalid MVP config accepted")
		}
	}
}
