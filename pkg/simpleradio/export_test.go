package simpleradio

import (
	"context"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
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
