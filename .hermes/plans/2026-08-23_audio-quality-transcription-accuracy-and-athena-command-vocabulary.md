# Audio Quality, Transcription Accuracy, and Natural-Language Athena Commands

**Date:** 2026-08-23
**Status:** Plan — not executed
**Repos:** `pcorajr/athena-audio-gateway` (branch `athena/main`), `pcorajr/project-athena`
**Governing decision:** ADR 0014 — Server-Side SRS Audio Gateway Boundary

---

## Goal

Take the pipeline from "it transcribed one clean sentence" to "I can talk to
Athena in plain English and be understood reliably."

Three outcomes:

1. **Audio arrives clean.** No SRS radio effects, no background noise
   injection, no NATO tone, no over-processing between the pilot's microphone
   and Whisper.
2. **Transcription is rock solid** for natural speech — not just aviation
   brevity. Dictionary priming is a crutch; the real wins are model size,
   decode parameters, and removing the AWACS bias currently baked in.
3. **Athena has its own command vocabulary.** Not AWACS brevity. Questions
   about mission state, ownship, threats, and free-form conversation with the
   LLM.

---

## Current state — measured, not assumed

From the 2026-08-23 01:10 live test on 133.000 AM, A-10C II, pilot `Prometheus`:

| Utterance | Transcript | Verdict |
|---|---|---|
| "Athena, radio check" | `"Athena Radio Check."` | correct |
| "Say bullseye" | `"SAY BULLS EYE"` | split |
| "Nearest threat" | `"NEAREST THREAT"` | correct |
| "Bogey dope" | **`"BOUGLY GOAT"`** | wrong |
| "Angels one five" | `"ANGER 1-5"` | wrong |
| "Say again your last" | `"SAY AGAIN YOUR LAST"` | correct |
| (keyed, little speech) | `[MUSIC]` | no speech detected |

Latency: 1.09–4.26 s recognition for 6.4–30 s of audio, `ggml-small.en` on CPU.

### What is already known about the stack

- `pkg/recognizer/whisper.go:49` **already** calls `SetInitialPrompt(...)`.
  My earlier claim that the prompt was unwired was wrong.
- That prompt (`pkg/recognizer/prompt.go`) is pure AWACS GCI:
  `ANYFACE`, `BOGEY`, `PICTURE`, `DECLARE`, `SNAPLOCK`, `SPIKED`, `BULLSEYE`,
  `BRAA`. It is **actively biasing the model toward brevity** and away from
  the natural speech we want.
- Only two Whisper parameters are set: initial prompt and language. Everything
  else is whisper.cpp default — no beam search, no temperature control, no
  `no_speech_threshold` tuning, no `suppress_non_speech_tokens`.
- Model is `ggml-small.en` (466 MB) on CPU. The 4090 is idle.
- Audio chain into Whisper is clean: 16 kHz mono F32, Opus-decoded, no
  resampling, no gain applied on the receive path (`F32AdjustVolume` is
  transmit-side only).

### What SRS does to the pilot's voice before it reaches us

Read from DCS-SRS source at `/tmp/srs-architecture-review-20260822`:

- `Common/Audio/Providers/ClientAudioProvider.cs` instantiates **both**
  `SpeexPreprocessorProvider` (denoise + AGC on the microphone) and
  `ClientTransmissionPipelineProvider` (radio effects).
- `ClientTransmissionPipelineProvider` applies, when enabled: radio effects
  chain, **background noise injection**, **NATO tone**, encryption effects,
  and a wet/dry mix controlled by `RadioEffectsRatio`.
- Defaults from `Common/Settings/ProfileSettingsStore.cs`:
  - `RadioEffectsRatio = 1.0` — **fully wet**, i.e. maximum effect
  - `NATOTone = true`, `NATOToneVolume = 1.2`
  - `RadioEncryptionEffects = true`
  - `RadioEffectsClipping = false`
- `transmission.NoAudioEffects` is checked first and bypasses the whole chain.

**This is the single largest untested lever.** A NATO tone mixed at 1.2 volume
plus injected background noise is exactly the kind of signal that turns
"bogey dope" into "bougly goat".

**Open question that must be answered before anything else:** are these
effects applied on the *transmitting* client before Opus encode, or on the
*receiving* client at playback? If TX-side, we inherit them and must turn
them off in the pilot's SRS profile. If RX-side, our decoded audio is already
clean and the accuracy problem is entirely model-side. Phase 1 answers this.

---

## Proposed approach

Sequenced so that each phase produces evidence the next phase depends on.
Do not reorder: tuning the recogniser before confirming the input signal is
clean would tune against a corrupted baseline.

- **Phase 1** — establish ground truth about the audio signal itself.
- **Phase 2** — remove SRS-side degradation, if Phase 1 shows any.
- **Phase 3** — fix the recogniser: model, parameters, and the wrong prompt.
- **Phase 4** — build a repeatable accuracy harness so changes are measured,
  not guessed at.
- **Phase 5** — design the Athena command vocabulary and natural-language lane.

Phases 1–4 are prerequisites for 5. There is no point designing a command
vocabulary that a 70 %-accurate recogniser cannot hear.

---

## Phase 1 — Ground truth on the audio signal

**Goal:** know exactly what the audio reaching Whisper sounds like, and
whether SRS has degraded it.

### 1.1 Capture decoded PCM to a WAV file (debug-only)

Add a debug-gated capture that writes the exact `[]float32` handed to the
recogniser out as a 16 kHz mono WAV.

- File: `internal/application/recognize.go` — new `--athena-debug-audio-dir`
  flag, default empty (disabled).
- Write via `pkg/pcm` conversion + `encoding/binary` WAV header; no new
  dependency.
- Filename: `<transmissionID>.wav`.

**Safety boundary — this deliberately violates the ADR 0014 no-audio-retention
rule, so it must be fenced:**

- Off unless an explicit CLI flag names a directory.
- Startup log line must state loudly that audio is being written to disk.
- Directory must not be under `/opt` or `/etc`; recommend `/tmp`.
- Documented in `deploy/README.md` as a diagnostic mode only, never for
  normal operation.
- Not enabled in the systemd unit.
- ADR note: this is a debug affordance, not a change to the retention
  contract. Consider whether ADR 0014 needs an explicit "diagnostic capture"
  carve-out or whether a doc note suffices.

### 1.2 Listen to the captured audio

Play back three captures and judge by ear:

- Is there a continuous tone? → NATO tone is reaching us.
- Is there hiss/static under the voice? → background noise effect.
- Does it sound bandpass-filtered/radio-ish? → radio effects chain.
- Is it clipped or distorted? → gain staging problem.

### 1.3 Resolve the TX-vs-RX question definitively

Cross-check the ear test against the source:

- Trace `ClientAudioProvider` call sites in DCS-SRS to determine whether the
  pipeline runs before Opus encode (TX) or at playback (RX).
- Corroborate: if audio is clean by ear, effects are RX-side and Phase 2
  collapses to nothing.

**Deliverable:** a one-paragraph written finding — "SRS applies X to
transmitted audio, we receive it as Y" — recorded in the issue.

---

## Phase 2 — Remove SRS-side degradation (conditional on Phase 1)

Only if Phase 1 shows effects reaching us.

### 2.1 Pilot-side SRS profile changes

In the pilot's SRS client profile for the Athena command radio:

| Setting | Change to | Why |
|---|---|---|
| `RadioEffectsRatio` | `0.0` | fully dry, no radio colouring |
| `NATOTone` | `false` | removes a constant tone under speech |
| `RadioEncryptionEffects` | `false` | not used on the command channel |
| `RadioEffectsClipping` | `false` | already default, confirm |

These are per-profile in SRS. **A dedicated SRS profile for the Athena
channel is the clean approach** — the pilot keeps full radio immersion on
UHF/VHF flight comms and gets a dry channel only for talking to Athena.

### 2.2 Investigate `NoAudioEffects`

`transmission.NoAudioEffects` bypasses the entire effects chain. Determine
whether a receiving client can signal this, or whether it is TX-only. If a
client-side flag exists, that is a cleaner fix than profile settings because
it cannot be forgotten.

### 2.3 Microphone-side checks (pilot machine)

- Windows mic level: target ~75 %, avoid clipping.
- Disable Windows "audio enhancements" on the Pimax Crystal Super input.
- SRS `MicBoost` default is `0.514`; verify it is not clipping the input.
- Confirm SRS input device is explicitly the Pimax mic, not "default".

**Deliverable:** before/after WAV captures of the same spoken phrase.

---

## Phase 3 — Fix the recogniser

### 3.1 Replace the AWACS prompt with an Athena prompt

`pkg/recognizer/prompt.go` currently primes for GCI brevity. This is wrong for
our use case and is measurably hurting natural speech.

Design a new prompt that:

- Names **Athena** as the callsign, not `ANYFACE`.
- Primes for **natural conversational English first**, aviation terms second.
- Includes the aviation terms we actually care about: *bullseye*, *bogey*,
  *angels*, *heading*, *contact*, *threat*, *SAM*, *bandit*, *friendly*,
  plus DCS-specific nouns.
- Handles the number problem explicitly: *niner*, *tree*, *fife*, and
  "angels one five" = 15,000 ft.
- Is **configurable**, not hardcoded — a `--athena-prompt-file` flag so the
  vocabulary can be tuned without a rebuild.

Keep the upstream GCI prompt intact for non-Athena mode.

### 3.2 Move to a larger model on the GPU

`ggml-small.en` is the accuracy floor for aviation audio. Options:

| Model | Size | Notes |
|---|---|---|
| `ggml-small.en` | 466 MB | current baseline |
| `ggml-medium.en` | 1.5 GB | materially better on accents/jargon |
| `ggml-large-v3` | 3 GB | best, multilingual, slowest on CPU |

The 4090 has 24 GB idle. The upstream Vulkan build offloads Whisper to GPU.
`medium.en` on GPU should beat `small.en` on CPU on **both** accuracy and
latency.

Steps: build the Vulkan variant, download `medium.en`, benchmark all
combinations against the Phase 4 harness. Decide on evidence, not assumption.

### 3.3 Tune whisper.cpp decode parameters

Currently only prompt and language are set. Worth testing:

- **Beam search** — whisper.cpp defaults to greedy. Beam size 5 typically
  gives a solid accuracy gain for modest cost.
- `SetTemperature` / temperature fallback — controls retry behaviour on
  low-confidence output.
- `no_speech_threshold` — directly governs the `[MUSIC]` failure mode.
- `suppress_non_speech_tokens` — should stop `[MUSIC]`, `[BLANK_AUDIO]` and
  similar markers appearing as transcripts at all.
- `SetMaxContext` — prevents prompt bleed across transmissions.

Each is a one-line change; the value is in measuring them, which is why
Phase 4 comes next.

### 3.4 Handle non-speech output explicitly

`[MUSIC]` is currently treated as a valid transcript and forwarded. It should
be detected and dropped as "no speech", never sent to Hermes.

- File: `internal/application/recognize.go` or the recogniser.
- Filter known non-speech markers, and treat empty/whitespace as silence.

---

## Phase 4 — Accuracy harness

Without this, every subsequent change is guesswork.

### 4.1 Fixture corpus

Record 20–30 real transmissions covering:

- plain conversational English ("Athena, what's the nearest threat?")
- aviation numbers ("angels one five", "heading two seven zero")
- brevity terms
- short utterances (< 2 s)
- long utterances (> 15 s)
- degraded audio (background engine noise, high-G breathing)

Store as WAV + expected-text pairs under `pkg/recognizer/testdata/`.
**Check whether these count as personal voice data before committing them to
a public fork** — if so, keep them local and gitignored.

### 4.2 Scoring

Add a benchmark that runs the corpus through a given model/prompt/parameter
combination and reports **word error rate**, plus per-category breakdown
(numbers vs. brevity vs. plain speech).

Upstream already has `make benchmark-whisper` — extend rather than duplicate.

### 4.3 Comparison matrix

Run: `{small.en, medium.en} × {CPU, GPU} × {old prompt, new prompt} ×
{greedy, beam}`. Record WER and latency. Choose on evidence.

---

## Phase 5 — Athena command vocabulary and the LLM lane

Only meaningful once Phases 1–4 give a reliable transcript.

### 5.1 The core design question

Two models, and this is the decision to make deliberately:

**A. Intent classification.** A fixed set of Athena commands, matched by
Hermes, dispatched to bounded Athena tools. Predictable, testable, limited.

**B. Free-form conversation.** The transcript goes to the LLM with Athena's
tools available. Natural, flexible, less predictable.

**Recommendation: B with a safety floor.** The stated goal is talking to the
LLM naturally, not memorising a command list. But:

- Read-only factual queries → answered freely.
- Anything action-shaped → typed preview only, never executed (ADR 0014).
- Unrecognised → fail quiet, never guess.

This is already what the bridge contract supports: `intent_class` distinguishes
`factual` / `cue` / `action_preview` / `unrecognized`.

### 5.2 Athena's own vocabulary — not AWACS

Draft the actual phrasings to support, e.g.:

- *"Athena, what's my fuel state?"*
- *"Athena, where's the nearest SAM?"*
- *"Athena, what am I flying?"*
- *"Athena, how far to the target?"*
- *"Athena, what's the mission status?"*
- *"Athena, say again"*

These map to existing Athena tools: mission-state snapshot, unit queries,
ownship resolution, threat proximity alerts.

**This needs a working session with you** — the vocabulary should reflect what
you actually want to ask mid-flight, not what I imagine.

### 5.3 Wire the Hermes bridge

Set `athena-hermes-endpoint`. The Athena command lane replaces observe-only.
Keep `mute: true` — Hermes answers land in the log, not on the radio, until
issue #181's transmit gate.

### 5.4 Wake-word or always-listening?

Every transmission on the command channel currently goes to Whisper. Options:

- **Always listen** (current) — simple, no wake word, but transcribes
  everything including chatter.
- **Address detection** — only forward to Hermes if the transcript starts with
  "Athena". Cheap, natural, and the pilot already does it.

Recommend address detection at the *Hermes bridge* boundary, not the audio
boundary: still transcribe everything (cheap, already happening), but only
forward addressed transmissions. Keeps the door open for cue delivery.

---

## Files likely to change

| Path | Change |
|---|---|
| `pkg/recognizer/prompt.go` | Athena prompt alongside the GCI one |
| `pkg/recognizer/whisper.go` | decode parameters, non-speech filtering |
| `pkg/recognizer/whisper_test.go` | parameter and prompt coverage |
| `internal/application/recognize.go` | debug WAV capture, `[MUSIC]` filtering |
| `internal/conf/configuration.go` | prompt file, debug audio dir, model path |
| `cmd/skyeye/main.go` | new flags |
| `deploy/athena-gateway.yaml` | model choice, prompt file, documented defaults |
| `deploy/README.md` | SRS profile setup, diagnostic mode, model guidance |
| `Makefile` | Vulkan build target if GPU path is adopted |
| `pkg/recognizer/testdata/` | fixture corpus (check data-sensitivity first) |
| `ATHENA.md` | audio-quality and recogniser-tuning notes |

---

## Tests and validation

- `make lint && make vet && make test` — must stay at 0 issues, all green,
  `-race` enabled. Current baseline: 1336 tests.
- New unit tests: prompt construction, non-speech filtering, WAV writer,
  decode-parameter plumbing.
- `make benchmark-whisper` extended with WER scoring.
- Live validation: read a fixed script on 133.000 AM, compare WER before and
  after each change.
- `git diff --check` clean; every gate run unpiped with the real exit code
  captured.

---

## Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| **Debug WAV capture writes voice to disk**, contrary to ADR 0014 | Flag-gated off by default, loud startup warning, `/tmp` only, documented as diagnostic, never in the unit file |
| Fixture corpus contains the pilot's real voice | Decide on public-fork suitability before committing; gitignore if in doubt |
| Vulkan build is flagged experimental upstream | Keep the CPU build as fallback; decide on benchmark evidence |
| `medium.en` may raise latency past usable | Measure; radio interaction tolerates ~2 s, not ~8 s |
| Turning off radio effects hurts immersion | Use a dedicated SRS profile for the Athena channel only |
| Free-form LLM lane widens the safety surface | Action language stays preview-only; ADR 0014 boundary is unchanged |
| Prompt tuning overfits to my test script | Corpus must include phrasings I did not write |

---

## Decisions — answered 2026-08-23

All five questions are now closed. These are decisions, not proposals.

### 1. Immersion is a requirement, not a tradeoff

The pilot wants **more** immersion, not less. Accuracy must come from the data
Athena returns and from the recogniser — **not** from degrading the radio
experience.

This reframes Phase 2 entirely. The TX-vs-RX question from Phase 1 is now the
deciding factor:

- **If effects are RX-side** (applied at the listener's playback), then the
  pilot hears full radio character while the gateway receives clean audio.
  **Nothing to change — we get both.** This is the hoped-for outcome.
- **If effects are TX-side** (baked in before Opus encode), we have a genuine
  conflict. Do **not** strip effects globally. Options, in order of preference:
  1. `NoAudioEffects` per-transmission flag, if a receiver can request it.
  2. Compensate on our side — the recogniser absorbs the degradation via
     model size and decode tuning rather than the pilot losing immersion.
  3. Only as a last resort, a dedicated dry profile — and even then, framed as
     a fallback the pilot can decline.

**Do not sacrifice immersion for word error rate without asking again.**

### 1b. Accent — this changes the model recommendation

The pilot is a **native Spanish speaker with a Hispanic accent**. This is a
first-class requirement, not an edge case, and it invalidates part of Phase 3.2.

The `.en` models (`small.en`, `medium.en`) are English-**only**, trained
predominantly on native-speaker English. The multilingual models
(`large-v3`, `medium`) are trained across ~100 languages including a great
deal of accented English, and **frequently outperform the `.en` variants on
non-native speech** despite the `.en` models scoring better on native-speaker
benchmarks.

Revised model matrix for Phase 4.3 — the `.en`-only assumption is dropped:

| Model | Size | Rationale |
|---|---|---|
| `ggml-small.en` | 466 MB | current baseline, for comparison only |
| `ggml-medium.en` | 1.5 GB | English-only, likely weaker on accent |
| `ggml-medium` | 1.5 GB | **multilingual, likely better on accent** |
| `ggml-large-v3` | 3 GB | **best accent handling, needs GPU for latency** |

`large-v3` on the idle 4090 is now the leading candidate rather than a
stretch goal. The fixture corpus (Phase 4.1) **must** be recorded in the
pilot's own voice — scoring against a native-speaker corpus would select the
wrong model.

Also set language explicitly to `en` even on multilingual models, so the model
transcribes accented English rather than attempting Spanish.

### 2. Command vocabulary — navigation and spatial reasoning

The pilot's actual intent, in their words:

- *"what is near me"*
- *"based on my current heading I want to go to XYZ airport to land — which
  way should I turn and for how long so I'm headed that way"*
- *"where is the target relative to my heading"*
- eventually: *"hey Athena, spawn a tank near sector A"*

This is materially different from the AWACS brevity the prompt currently
primes for. Three distinct capability classes:

**Class A — proximity and situational queries.** "What's near me." Athena
already has this: mission-state snapshot, unit queries, threat proximity.
Available now.

**Class B — relative navigation and vectoring.** "Which way do I turn, and for
how long." This is **new computation**, not a lookup. Requires:
- ownship position and current heading (Athena has this)
- destination resolution — airport/waypoint/target by name
- relative bearing = target bearing − current heading, normalised to ±180°
- turn direction (left/right, shortest arc) and magnitude in degrees
- optionally time-to-turn at current rate, and distance/ETA at current speed

None of this exists yet. It is a **new Athena deterministic query surface** —
and it belongs in Athena, not the LLM. Computing bearings in a language model
is exactly the "do not let an LLM count units" anti-pattern in the project
AGENTS.md. The LLM should *phrase* the answer; Athena must *calculate* it.

Requires a new card in `project-athena`, likely under the MVP-08 epic:
**"Relative navigation query surface"** — bearing, turn direction, distance,
ETA, all deterministic and fixture-tested.

**Class C — mission mutation.** "Spawn a tank near sector A."

This is **live DCS mutation** and is explicitly out of scope under ADR 0014
and the project MVP safety gates. It is not a small extension — it crosses the
read-only boundary the entire architecture is built around.

Path forward, and it is deliberately slow:
1. Under the current design this yields a **typed non-executing preview** —
   Athena confirms what *would* happen and does nothing. That is already
   supported via `intent_class: action_preview`.
2. Actual execution needs its own ADR, its own safety-gated card, a confirmation
   protocol, and an audit trail. It is a separate project, not a feature.

**Do not implement spawn execution as part of this workstream.** Implement the
preview lane, prove it never dispatches, and treat live mutation as a future
decision the pilot makes with full sight of the risk.

### 3. Voice fixtures — local only

Recorded in the pilot's voice, stored locally, **gitignored**. Never committed
to the public fork.

Add `pkg/recognizer/testdata/*.wav` to `.gitignore`. The harness reads from
disk; nothing needs to be in version control. Document the corpus contents in
a committed manifest (phrases and expected text) without the audio.

### 4. Latency budget — 2 seconds

From PTT release to Athena's answer. Confirmed.

Budget allocation to design against:

| Stage | Target |
|---|---|
| Recognition | ≤ 1.0 s |
| Hermes round-trip incl. Athena tools | ≤ 0.6 s |
| Speech synthesis | ≤ 0.4 s |

Recognition is currently 1.09 s for 6.4 s of audio on `small.en`/CPU — already
at budget before Hermes or TTS. **GPU offload is therefore required, not
optional**, especially if `large-v3` is selected for accent handling.

If the budget cannot be met, reduce model size before exceeding 2 s.

### 5. No wake word — the microphone is already gated

The pilot's observation is correct and it removes the feature: **the mic is
not live.** SRS only transmits while PTT is held. Keying the radio *is* the
activation gesture, and it is a deliberate physical action.

A wake word would add a second activation on top of one that already exists —
worse ergonomics, another recognition failure mode, and a word that must
survive an accented pronunciation.

**Decision: no wake word. PTT is the activation signal.**

The command channel is also already single-purpose: one dedicated frequency,
one authorised speaker. Everything arriving on it was deliberately sent to
Athena. Address detection is unnecessary and is dropped from Phase 5.4.

`Athena` remains the callsign for the recogniser prompt and for the pilot's
natural phrasing — but nothing is *gated* on hearing it.

---

## Superseded guidance

These earlier recommendations in this plan are now overridden:

- **Phase 2 "dedicated dry SRS profile"** — demoted to last-resort fallback.
  Immersion is a requirement. See Decision 1.
- **Phase 3.2 `.en` model preference** — `.en` models may be the wrong family
  for accented speech. Test multilingual variants. See Decision 1b.
- **Phase 5.4 address detection** — dropped. PTT is the gate. See Decision 5.
- **Phase 4.1 generic corpus** — must be the pilot's own voice. See Decision 1b.

---

## Original open questions (now answered)

1. **Immersion vs. accuracy** — happy with a dedicated dry SRS profile for the
   Athena channel, keeping full radio effects on flight comms?
2. **Command vocabulary** — what do you actually want to ask mid-flight? This
   drives Phase 5 and I should not invent it.
3. **Voice fixtures** — are recordings of your voice acceptable in the public
   fork, or keep them local?
4. **Latency budget** — what is the longest acceptable delay between releasing
   PTT and hearing Athena? That decides model and beam-search choices.
5. **Wake word** — is "Athena, ..." as the address form acceptable, or do you
   want everything on the channel treated as directed at her?

---

## Recommended order for tomorrow

1. Phase 1 in full — capture, listen, resolve TX-vs-RX. **Everything else
   depends on knowing whether the input is clean.**
2. Phase 3.1 and 3.4 — Athena prompt and `[MUSIC]` filtering. Small, high
   value, independent of the audio finding.
3. Phase 4.1 and 4.2 — corpus and WER scoring, so Phase 3.2/3.3 can be decided
   on measurement.
4. Phase 2 only if Phase 1 shows contamination.
5. Phase 5.2 as a conversation with you, not solo design work.

Phases 1–4 are within a day. Phase 5 needs your input first.
