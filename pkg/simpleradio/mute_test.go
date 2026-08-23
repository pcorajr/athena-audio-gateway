package simpleradio

import (
	"context"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
)

// muteTestClient builds a client with no UDP connection.
//
// The nil connection is the assertion mechanism: writePackets dereferences
// c.udpConnection, so if the mute check were ever bypassed the test would panic
// instead of failing quietly. This proves the transmit path is unreachable
// rather than merely unused.
func muteTestClient(mute bool) *Client {
	radio := types.Radio{Frequency: 30_000_000, Modulation: types.ModulationFM}
	return &Client{
		mute:          mute,
		udpConnection: nil,
		receivers:     map[types.Radio]*receiver{radio: {radio: radio}},
	}
}

func onePacket() []voice.Packet {
	return []voice.Packet{{AudioBytes: []byte{0x01, 0x02, 0x03}}}
}

// With --mute set, no packet may reach the socket.
func TestMuteMakesTransmitPathUnreachable(t *testing.T) {
	t.Parallel()

	client := muteTestClient(true)
	packetChan := make(chan []voice.Packet, 1)
	packetChan <- onePacket()

	ctx, cancel := context.WithTimeout(t.Context(), 250*time.Millisecond)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer func() {
			if r := recover(); r != nil {
				t.Errorf("muted client attempted to write to the socket: %v", r)
			}
			close(done)
		}()
		client.transmitPackets(ctx, packetChan)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("transmitPackets did not stop on context cancellation")
	}
}

// The inverse: without mute the same client does reach the write path, which is
// what makes the muted case above meaningful rather than vacuous.
func TestUnmutedClientReachesTransmitPath(t *testing.T) {
	t.Parallel()

	client := muteTestClient(false)
	packetChan := make(chan []voice.Packet, 1)
	packetChan <- onePacket()

	ctx, cancel := context.WithTimeout(t.Context(), 250*time.Millisecond)
	defer cancel()

	reached := make(chan bool, 1)
	go func() {
		defer func() {
			// A panic here means writePackets dereferenced the nil connection,
			// i.e. the transmit path was reached.
			reached <- recover() != nil
		}()
		client.transmitPackets(ctx, packetChan)
	}()

	select {
	case didReach := <-reached:
		if !didReach {
			t.Error("expected an unmuted client to reach the socket write")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("transmitPackets did not reach the write path or stop")
	}
}
