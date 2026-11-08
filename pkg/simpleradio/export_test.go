package simpleradio

import (
	"context"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

// NewSRSMVPFixtureClient is available only in the test binary. It constructs the
// real transport with synthetic live metadata and an in-memory packet writer;
// it cannot connect to SRS. The external integration test can therefore compose
// the real serial runner and real interlocks without a package import cycle.
func NewSRSMVPFixtureClient(mute, liveTX bool) (*Client, Transmission, func() int) {
	client, transmission, writer := mvpTestClient()
	client.mute, client.liveTX = mute, liveTX
	transmission.Audio = make(Audio, 32000)
	return client, transmission, func() int { return writer.writes }
}

// ObserveSRSMVPFixture supplies the same bounded-lifetime receipt as assembly,
// letting external Runner tests force a producer past its cancellation drain.
func ObserveSRSMVPFixture(ctx context.Context, writer *transcript.Writer) (func(Transmission) Transmission, func()) {
	ledger := &captureLedger{ctx: ctx, writer: writer, pending: make(map[*captureReceipt]transcript.Record)}
	return func(t Transmission) Transmission {
		t.receipt = ledger.begin(transcript.Record{ID: t.TraceID, Speaker: t.ClientName, FrequencyHz: uint64(t.Radio.Frequency), Modulation: "AM"})
		return t
	}, ledger.cancel
}

// CaptureSRSMVPFixture exercises real datagram admission, assembly and Opus
// decode. Only the peer and socket writer are synthetic; no route is injected
// into the delivered Transmission. The stale prefix models a Read spanning an
// identity update. The caller owns cancellation and must join the workers.
func CaptureSRSMVPFixture(ctx context.Context, writer *transcript.Writer, prefix string) (*Client, <-chan struct{}, error) {
	c, tr, _ := mvpTestClient()
	c.transcript = writer
	c.rxChan = make(chan Transmission, 16)
	packets, err := c.encodeMVP(make(Audio, 32000), tr.Radio)
	if err != nil {
		return nil, nil, err
	}
	in := make(chan voiceDatagram, len(packets)+1)
	assembled := make(chan transmissionPackets, 16)
	if prefix != "" {
		p := voice.NewPacket(packets[0].AudioBytes, packets[0].Frequencies, 123, 1, 0, []byte(tr.route.origin), []byte(tr.route.origin))
		epoch := uint64(0)
		if prefix == "self_relay" {
			epoch, p.RelayGUID = 1, []byte(c.clientInfo.GUID)
		}
		in <- voiceDatagram{data: p.Encode(), epoch: epoch}
	}
	for i, packet := range packets {
		p := voice.NewPacket(packet.AudioBytes, packet.Frequencies, 123, uint64(i+2), 0, []byte(tr.route.origin), []byte(tr.route.origin))
		in <- voiceDatagram{data: p.Encode(), epoch: 1}
	}
	done := make(chan struct{})
	assemblyDone := make(chan struct{})
	go func() { defer close(assemblyDone); defer close(assembled); c.receiveVoice(ctx, in, assembled) }()
	go func() { defer close(done); c.decodeVoice(ctx, assembled); <-assemblyDone }()
	return c, done, nil
}
