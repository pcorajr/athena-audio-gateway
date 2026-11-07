package simpleradio

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/dharmab/skyeye/pkg/simpleradio/voice"
	"github.com/rs/zerolog/log"
)

// receiver buffers incoming transmissions on a single radio frequency.
type receiver struct {
	// radio is the frequency/modulation this receiver is tuned to. It is
	// carried through to the decoded transmission so downstream admission can
	// treat the arrival channel as an observed fact.
	radio types.Radio
	// lock protects the receiver's state.
	lock sync.RWMutex
	// buffer of received voice packets.
	buffer []voice.Packet
	// origin is the GUID of a client we are currently listening to. We can only listen to one client at a time, and whoever started broadcasting first wins.
	origin types.GUID
	// deadline is extended every time another voice packet is received. When we pass the deadline, the transmission is considered over.
	deadline  time.Time
	startedAt time.Time
	// packetNumber is the number of the last received voice packet. We only record a packet if its packet number is larger than the last received packet's, and skip any that were dropped or delivered out of order.
	// If we were more ambitious we would reassemble the packets and use Opus's forward error correction to recover from lost packets... too bad!
	packetNumber uint64
	// overflow poisons the whole utterance, never just its suffix.
	overflow bool
	route    mvpRoute
}

// Receive returns a channel that receives transmissions over the radio. Each transmission is F32LE PCM audio data.
func (c *Client) Receive() <-chan Transmission {
	return c.rxChan
}

// receive checks if the given packet is part of a new transmission or matches a transmission in progress.
// If either case is true, the packet is buffered into the receiver.
func (r *receiver) receive(packet *voice.Packet, route mvpRoute) {
	r.lock.Lock()
	defer r.lock.Unlock()
	// Accept the packet if it is either:
	// - the first packet of a new transmission
	isNewTransmission := r.origin == "" && r.packetNumber == 0
	// - a newer packet from the same origin
	isNewerPacket := packet.PacketID > r.packetNumber
	isSameOrigin := r.origin == types.GUID(packet.OriginGUID)
	shouldAcceptPacket := isNewTransmission || (isNewerPacket && isSameOrigin)
	if !shouldAcceptPacket {
		return
	}

	if isNewTransmission {
		log.Info().Str("origin", string(packet.OriginGUID)).Msg("receiving transmission")
		r.route = route
		r.startedAt = time.Now()
	}

	if route.epoch != r.route.epoch || len(r.buffer) >= maxRxPackets || time.Since(r.startedAt) > MaxTransmissionDuration {
		r.overflow = true
		r.buffer = nil
	}
	if !r.overflow {
		r.buffer = append(r.buffer, *packet)
	}
	r.origin = types.GUID(packet.OriginGUID)
	r.deadline = time.Now().Add(maxRxGap)
	r.packetNumber = packet.PacketID
}

// hasTransmission checks if the receiver has a complete transmission buffered.
func (r *receiver) hasTransmission() bool {
	r.lock.RLock()
	defer r.lock.RUnlock()
	hasPackets := len(r.buffer) > 0 || r.overflow
	isComplete := time.Now().After(r.deadline)
	return hasPackets && isComplete
}

// isReceivingTransmission checks if the receiver is currently buffering an in-progress transmission.
func (r *receiver) isReceivingTransmission() bool {
	r.lock.RLock()
	defer r.lock.RUnlock()
	return r.deadline.After(time.Now())
}

// reset clears the receiver's buffer.
func (r *receiver) reset() {
	r.lock.Lock()
	defer r.lock.Unlock()
	r.buffer = make([]voice.Packet, 0)
	r.origin = ""
	r.deadline = time.Time{}
	r.startedAt = time.Time{}
	r.packetNumber = 0
	r.overflow = false
	r.route = mvpRoute{}
}

func (r *receiver) rejectCurrent() {
	r.lock.Lock()
	defer r.lock.Unlock()
	if r.origin != "" {
		r.overflow, r.buffer = true, nil
	}
}

// maxRxGap is a duration after which the receiver will assume the end of a transmission if no packets are received.
const maxRxGap = 300 * time.Millisecond

// MaxTransmissionDuration is the longest acceptable received transmission
// length. Longer transmissions are dropped in their entirety.
const MaxTransmissionDuration = 30 * time.Second

// maxRxPackets is the longest acceptable received transmission length in packets.
const maxRxPackets = int(MaxTransmissionDuration / frameLength)

// minRxDuration is the minimum duration of a transmission to be considered for speech recognition. This reduces
// thrashing due to transmissions too short to contain any useful content.
const minRxDuration = 1 * time.Second // 1s is whisper.cpp's minimum duration, it errors for any samples shorter than this.

// transmissionPackets is a complete buffered transmission together with the
// radio it arrived on.
type transmissionPackets struct {
	radio      types.Radio
	packets    []voice.Packet
	route      mvpRoute
	receivedAt time.Time
	traceID    string
	origin     types.GUID
	reason     string
}

// takeCompleted atomically detaches a complete utterance and resets assembly.
func (r *receiver) takeCompleted() (transmissionPackets, bool) {
	r.lock.Lock()
	defer r.lock.Unlock()
	if r.origin == "" || time.Now().Before(r.deadline) {
		return transmissionPackets{}, false
	}
	valid := !r.overflow && time.Duration(len(r.buffer))*frameLength > minRxDuration
	t := transmissionPackets{radio: r.radio, packets: r.buffer, route: r.route, receivedAt: time.Now()}
	r.buffer, r.origin, r.packetNumber = nil, "", 0
	r.deadline, r.startedAt, r.route, r.overflow = time.Time{}, time.Time{}, mvpRoute{}, false
	return t, valid
}

// receiveVoice listens for incoming UDP voice packets, decodes them into VoicePacket structs, and routes them to the out channel for audio decoding.
func (c *Client) receiveVoice(ctx context.Context, in <-chan voiceDatagram, out chan<- transmissionPackets) {
	if c.mvp {
		c.receiveMVPVoice(ctx, in, out)
		return
	}
	// t is a ticker which triggers the check for the end of a transmission.
	t := time.NewTicker(frameLength)
	defer t.Stop()
	for {
		select {
		case b := <-in:
			packet, err := voice.Decode(b.data)
			if err != nil {
				log.Debug().Err(err).Msg("failed to decode voice packet")
				if c.mvp {
					// A malformed packet during an utterance must not leave a
					// plausible prefix for speech recognition.
					for _, receiver := range c.receivers {
						receiver.rejectCurrent()
					}
				}
				continue
			}

			logger := log.With().Str("GUID", string(packet.OriginGUID)).Logger()
			var route mvpRoute
			if c.mvp {
				var admitted bool
				route, admitted = c.admitMVPPacket(packet, b.epoch)
				if !admitted {
					continue
				}
			}

			// Log every decoded voice packet before any filtering. Without
			// this, a packet that matches no receiver is dropped silently and
			// there is no way to tell "the server never sent us audio" apart
			// from "audio arrived on a frequency we are not tuned to" -- which
			// are very different faults with very different fixes.
			freqs := make([]string, 0, len(packet.Frequencies))
			for _, f := range packet.Frequencies {
				freqs = append(freqs, fmt.Sprintf("%.3fMHz/mod%d", f.Frequency/1_000_000, f.Modulation))
			}
			logger.Debug().
				Strs("packetFrequencies", freqs).
				Int("receivers", len(c.receivers)).
				Msg("decoded inbound voice packet")

			if c.secureCoalitionRadios.Load() {
				c.clientsLock.RLock()
				client, ok := c.clients[types.GUID(packet.OriginGUID)]
				c.clientsLock.RUnlock()
				if !ok {
					logger.Warn().Msg("ignoring voice packet from unknown client")
					continue
				}
				if client.Coalition != c.clientInfo.Coalition {
					logger.Trace().Msg("ignoring voice packet from different coalition")
					continue
				}
			}

			for radio, receiver := range c.receivers {
				for _, frequency := range packet.Frequencies {
					testRadio := types.Radio{
						Frequency:   frequency.Frequency,
						Modulation:  types.Modulation(frequency.Modulation),
						IsEncrypted: frequency.Encryption != 0,
					}
					if (c.mvp && exactMVPRadio(testRadio, radio)) || (!c.mvp && testRadio.IsSameFrequency(radio)) {
						receiver.receive(packet, route)
					}
				}
			}
		case <-t.C:
			// Check if everyone has stopped talking.
			if len(in) == 0 {
				for _, receiver := range c.receivers {
					if transmission, ok := receiver.takeCompleted(); ok {
						if c.mvp {
							// No backlog while another utterance is being decoded.
							select {
							case out <- transmission:
							default:
							}
						} else {
							select {
							case out <- transmission:
							case <-ctx.Done():
								return
							}
						}
					}
				}
			}
		case <-ctx.Done():
			log.Info().Msg("stopping SRS audio receiver due to context cancellation")
			return
		}
	}
}
