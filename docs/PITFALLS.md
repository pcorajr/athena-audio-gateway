# Pitfalls

Traps this project actually hit. Each cost real debugging time; none are
obvious from the code.

## 1. `fixedSegmentLength` was 58; DCS-SRS writes 57

Every inbound voice packet was rejected by the framing validation with an
off-by-one. Invisible to 1361 tests because our own encoder used the same wrong
constant, so encode→decode round-tripped perfectly.

**Lesson: a symmetric bug is invisible to round-trip tests.** Validate against
the other implementation's source, not your own encoder.

## 2. `ProtectSystem=strict` + `PrivateTmp=true` broke the diagnostic

The WAV capture wrote to `/tmp/athena-audio`, which does not exist inside the
service's private namespace. Our own hardening defeated our own diagnostic.

**Use a path under `StateDirectory`** (`/var/lib/athena-gateway/...`).

## 3. SRS `MicBoost` does not affect transmitted audio

It exists only in `AudioPreview`; `AudioManager` — the real transmit path — has
only `SpeakerBoost`. Adjusting the SRS mic slider changes what you hear when
previewing, not what is sent.

**Only Windows input gain matters.** We spent an hour on the wrong slider.

## 4. Mic passthrough causes the feedback that makes people lower the gain

Hearing yourself in your own headset is SRS's "Mic Output" passthrough. The
natural reaction is to turn the mic down, which starves recognition.

**Set Mic Output to the no-passthrough entry first, then raise input gain.**
Doing it in that order recovered +29.6 dB.

## 5. `--mute` was inert on upstream v1.10.0

The flag was read but never passed to the SRS client. The tag we pinned had the
bug; it was fixed on `main` five days later.

**Audit `v1.10.0..upstream/main` before any live gate.** A release tag is not
automatically safe.

## 6. A webhook returns `202 accepted`, not an answer

Hermes webhook routes spawn the agent as a background task and return before it
starts (`gateway/platforms/webhook.py`). They cannot return speech.

**Use the API server** (`/v1/responses`), which awaits the agent and returns the
text in the response body. `202` proves transport acceptance, nothing more.

## 7. Credentials do not belong in CLI flags

A flag value is visible in `ps` output and lands in shell history.

**Environment only**, via a mode-0600 systemd `EnvironmentFile`.

## 8. `t.Errorf` from a test HTTP handler is a data race

A handler runs on the server's goroutine and, in timeout tests, outlives the
test function. `-race` reports it against whichever test happens to be running,
which makes it look unrelated to the real cause.

## Two techniques that paid for themselves

**Quiet-floor measurement** distinguishes "SRS effects reached us" from "they
did not". A digital-silence floor of exactly `0.00000` between words proved the
radio effects are applied at the listener's playback, not before transmit —
which meant full immersion and clean audio, with no tradeoff.

**Log decode failures, not just successes.** Hours of "no audio" looked
identical to "audio arriving and being silently dropped". One log line on the
failure path found the framing bug immediately.
