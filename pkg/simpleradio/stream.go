package simpleradio

import (
	"context"
	"sync"
	"sync/atomic"
	"time"
)

// MaxMVPReplySamples bounds the synthesized audio of one reply (all parts):
// 30 s at 16 kHz. Callers check it before speaking so they can say a short
// fallback line instead of going silent.
const MaxMVPReplySamples = 30 * 16000

// UnpromptedMVP mints one new token from an admitted reception, never from
// caller-supplied radio fields. Connection epoch, pilot identity and channel
// are revalidated here and on every packet. The adapter fences DCS sessions.
func (c *Client) UnpromptedMVP(last Transmission, id string) (Transmission, bool) {
	c.clientsLock.RLock()
	defer c.clientsLock.RUnlock()
	t := last
	t.route.unprompted = true
	if id == "" || !c.validMVPLocked(t) || c.mvpInterlockReason() != "" {
		return Transmission{}, false
	}
	t.TraceID, t.ReceivedAt, t.Audio = id, time.Now(), nil
	t.route.used = &atomic.Bool{}
	return t, true
}

// BeginMVPReply consumes the reception token ONCE. The returned private sender
// has a two-part lifetime and permanently closes on error. No new route token
// is minted and no transmitted part can be replayed via TransmitMVP.
func (c *Client) BeginMVPReply(t Transmission) func(context.Context, Audio, time.Time) Delivery {
	closed := !c.mvp || t.route.used == nil || t.route.used.Swap(true)
	var lock sync.Mutex
	parts, samples := 0, 0
	return func(ctx context.Context, audio Audio, expires time.Time) Delivery {
		lock.Lock()
		defer lock.Unlock()
		if closed || parts >= 2 {
			return Delivery{Outcome: "invalid_or_replayed"}
		}
		parts++
		samples += len(audio)
		// Bound actual synthesized duration as well as text. Never truncate audio.
		if samples > MaxMVPReplySamples {
			closed = true
			return Delivery{Outcome: "speech_duration_exceeded"}
		}
		c.txLock.Lock()
		defer c.txLock.Unlock()
		closed = true
		if c.mute || !c.liveTX {
			return Delivery{Outcome: "interlocked", Reason: c.mvpInterlockReason()}
		}
		if reason := c.MVPRejection(t); reason != "" {
			return Delivery{Outcome: "identity_changed", Reason: reason}
		}
		if ctx.Err() != nil {
			return Delivery{Outcome: "canceled"}
		}
		if !time.Now().Before(expires) {
			return Delivery{Outcome: "expired"}
		}
		packets, err := c.encodeMVP(audio, t.Radio)
		if err != nil {
			return Delivery{Outcome: "encode_failed"}
		}
		d := c.sendMVP(ctx, t, expires, packets)
		closed = d.Outcome != "sent"
		return d
	}
}
