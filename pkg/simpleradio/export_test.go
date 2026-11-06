package simpleradio

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
