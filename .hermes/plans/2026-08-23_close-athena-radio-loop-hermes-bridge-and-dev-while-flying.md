# Close the Athena Radio Loop: Personas, Gateway Health, and Dev-While-Flying

**Date:** 2026-08-23 (revised after pilot review)
**Status:** Plan — not executed
**Repos:** `pcorajr/athena-audio-gateway` (`athena/main`), `pcorajr/project-athena` (`dev`)
**Governing decisions:** ADR 0014 (SRS Audio Gateway Boundary), ADR 0012 (Athena/Hermes split)

---

## Revision note

This replaces the earlier version of this plan. Four changes from pilot feedback:

1. **Documentation is light.** Lessons learned and pitfalls only. No comprehensive
   findings document.
2. **The 2 s latency budget is dropped as a constraint.** It was invented, not
   measured. Replaced with: instrument, record a baseline, improve against it.
   Do not reject a design for being slow before there is a number.
3. **The gateway `TypeError` is promoted from a footnote to real work.** Research
   and diagnose it properly; the messaging layer is load-bearing for everything
   downstream.
4. **Personas are the routing model, and the set is open.** Athena is mission.
   Hermes is dev and system. More will follow. Build a registry, not an if/else.

---

## Goal

Get from "the gateway hears the pilot and says nothing" to "the pilot addresses a
persona by name in flight and that persona answers" — while keeping the messaging
layer underneath it healthy.

---

## What today established

Recorded here so the next session does not re-derive it.

**Audio chain is clean and correctly gain-staged.** Peak went from 0.014–0.028
(−31 dBFS, ~2 % of range) to 0.368–0.846 (−1 dBFS) across three iterations, a
**+29.6 dB** improvement, with the quiet floor still at exactly `0.00000`. Level
without added noise.

**SRS radio effects do not reach the gateway.** Digital silence between words in
every capture proves `ClientTransmissionPipelineProvider` runs at the listener's
playback, not before Opus encode. **The pilot keeps full radio immersion and the
gateway still receives clean audio.** No dry profile, no tradeoff — the previous
plan's Phase 2 is cancelled outright.

**Transcription is not the bottleneck.** `ggml-small.en` on CPU, ~1.0 s, verbatim
on every utterance in the final pass including *"Athena, give me a status update
on the battlefield"* and *"Hermes, let's adjust the spawn rate."* Last night the
same pipeline produced `"BOUGLY GOAT"` and `"ANGER 1-5"`. Two changes account for
it: the conversational prompt replacing the AWACS brevity prompt, and +30 dB of
signal. **The model-upgrade work (`medium`, `large-v3`, GPU) is retired** — it
was solving a problem that turned out to be gain staging.

One residual: the loudest capture hit −1.4 dBFS, close to clipping. The pilot is
backing the input off; re-measure and confirm peaks land nearer 0.6–0.7.

---

## Part A — Light documentation

Lessons and traps only. Target one short file, not a report.

### A1. Turn the diagnostic capture off

It has served its purpose and is writing voice audio to disk against ADR 0014.

- Remove `athena-debug-audio-dir` from `/etc/athena-gateway/config.yaml`
- Delete `/var/lib/athena-gateway/audio/*.wav`
- Restart; confirm the `DIAGNOSTIC MODE` warning is gone
- Keep the flag in the binary — it earned its place

### A2. `docs/PITFALLS.md` — one page

Five traps, each two or three lines. No narrative.

1. **`fixedSegmentLength` was 58; DCS-SRS writes 57.** Every real voice packet
   was rejected by the framing validation. Invisible to 1361 tests because our
   own encoder used the same wrong constant, so encode→decode round-tripped
   perfectly. *A symmetric bug is invisible to round-trip tests.*
2. **`ProtectSystem=strict` + `PrivateTmp=true` broke the diagnostic.** WAV
   capture wrote to `/tmp/athena-audio`, which does not exist inside the
   service's private namespace. Use a path under `StateDirectory`.
3. **SRS `MicBoost` does not affect transmitted audio.** It exists only in
   `AudioPreview`; `AudioManager` — the real transmit path — has only
   `SpeakerBoost`. Only Windows input gain matters.
4. **Mic passthrough causes the feedback that makes people turn gain down.**
   Set Mic Output to the no-passthrough entry, *then* raise input gain.
5. **`--mute` was inert on upstream v1.10.0.** Fixed by cherry-pick. Audit
   `v1.10.0..upstream/main` before any live gate; the tag alone is not safe.

Also worth one line each: quiet-floor measurement is how you tell "SRS effects
reached us" from "they didn't", and logging *decode failures* (not just
successes) is what found the framing bug after hours of silent drops.

### A3. Point at it

- `ATHENA.md` — link `docs/PITFALLS.md`
- `deploy/README.md` — pilot audio setup in first-run validation: passthrough
  off, Windows input ~90 %, enhancements off, verify peaks
- Update the previous plan's phase status: Phase 1 complete, Phase 2 cancelled,
  model work retired — with reasons

### A4. Evidence to #187

Short comment: Phase 1 finding, the gain table, the three bugs. That issue is the
durable record.

---

## Part B — Diagnose the Hermes gateway

Promoted to real work. This layer carries the radio loop, the dev lane, and
everything after.

### What is known

```
ERROR agent.chat_completion_helpers: Streaming failed before delivery:
      Mapping() takes no arguments
  chat_completion_helpers.py:4543  _call_anthropic
  relay_llm.py:395                 stream
  chat_completion_helpers.py:4512  _open_anthropic_stream
  anthropic/resources/messages.py:1111   messages.stream(**final_kwargs)
  anthropic/_utils/_transform.py:88      maybe_transform
  anthropic/_utils/_transform.py:280     _transform_typeddict
  anthropic/_utils/_transform.py:179     if is_typeddict(...) and is_mapping(data)
  anthropic/_utils/_utils.py:160         return isinstance(obj, Mapping)
  typing.py:1328                    __instancecheck__
  typing.py:1606                    __subclasscheck__
  <frozen abc>:123                  __subclasscheck__
TypeError: Mapping() takes no arguments

WARNING agent.conversation_loop: API call failed (attempt 1/3)
  provider=minimax-oauth  base_url=https://api.minimax.io/anthropic
  model=MiniMax-M3
```

Environment, verified: `anthropic` 0.87.0, `pydantic` 2.13.4,
`typing_extensions` 4.15.0, Python 3.11.15 from the bundled
`.hermes-runtime` generation (not system Python). The SDK's `is_mapping` uses
`typing.Mapping` — a `_SpecialGenericAlias` — rather than
`collections.abc.Mapping`, so every check goes through
`__instancecheck__ → __subclasscheck__ → issubclass(cls, __origin__)`.

Frequency: **3 occurrences in 7 days.** Intermittent, not constant.

### What is NOT known — do not assert these

- **Whether the call recovered.** The log shows "attempt 1/3"; I have not
  confirmed what attempts 2 and 3 did.
- **What object triggers it.** `Mapping() takes no arguments` is the error
  Python raises when a class with no custom `__init__`/`__new__` is *called*
  with arguments — which is odd for an `issubclass` path. Something in the
  request payload is likely an unusual type. Tool schemas are the prime suspect
  (this gateway carries many), image content blocks second.
- **Whether it is Hermes' bug or the SDK's.** Could be either, or an
  interaction with the bundled Python's frozen `abc`.

### Approach

Do not guess. Reproduce it deterministically, then decide who owns it.

1. **Confirm the outcome.** Pull the full window around all 3 occurrences and
   establish whether the request eventually succeeded, fell through to another
   provider, or was lost. This decides urgency.
2. **Establish the trigger.** It only appears on the `minimax-oauth` →
   `api.minimax.io/anthropic` path, which is a *fallback*. Determine whether
   it reproduces on demand by forcing that provider with a tool-heavy request.
3. **Isolate the payload.** If it reproduces, bisect `final_kwargs` — tools
   only, messages only, system only — until the offending value is identified.
   Log its `type()` and `repr()`.
4. **Search upstream.** Check the `anthropic-sdk-python` issue tracker and
   Hermes' own issues for `Mapping() takes no arguments`. This smells like a
   known SDK/typing interaction; do not reinvent the diagnosis.
5. **Decide ownership.** SDK bug → pin or patch and report upstream. Hermes bug
   → fix in `chat_completion_helpers.py` with a regression test. Payload bug →
   fix at the source.

### Not in scope

Do not "fix" this by removing MiniMax from `fallback_providers`. That hides a
fault in the failover path — precisely the path that runs when things are
already going wrong.

---

## Part C — Persona routing

The pilot's model: **Athena is mission. Hermes is dev and system. More to come.**

That last clause is the design requirement. This is a registry, not a branch.

### The routing signal already exists

In today's live test, unprompted:

> *"**Athena**, give me a status update on the battlefield."*
> *"**Hermes**, let's adjust the spawn rate."*

The pilot already addresses personas by name, and Whisper transcribed both
correctly including under an accent. No wake word is needed — PTT is the
activation gate, and the address word is the routing key.

### Two independent axes — do not conflate

| Axis | Question | Mechanism | Exists? |
|---|---|---|---|
| **Admission** | Who may speak? | `athena-pilot-name` + frequency | Yes |
| **Routing** | Who is being addressed? | Leading address word in transcript | No |

### Design

**Persona registry**, config-driven:

```yaml
personas:
  athena:
    aliases: [athena]
    endpoint: <mission lane>
  hermes:
    aliases: [hermes]
    endpoint: <dev/system lane>
```

Adding a persona is a config entry. No code change, no redeploy of logic.

**Address extraction** is a small deterministic function: take the leading token
or two, normalise, match against the registry, strip the address from the
transcript before forwarding. Fixture-first, trivially testable, no LLM.

**Fuzzy matching for accented speech.** Upstream SkyEye already depends on
`go-edlib` precisely for callsign near-misses. Reuse it rather than exact
matching — *"Athena"* and *"Hermes"* both transcribed cleanly today, but one
noisy transmission should not silently drop a command.

**Unaddressed transmissions:** fail quiet, log the reason. Do not guess a
persona. Consider a configurable default later, once there is evidence about how
often it happens.

**Session separation follows persona.** Each persona gets its own Hermes session
and profile, so mission chatter never lands in the dev conversation and vice
versa. `athena-radio-gateway` already exists in the design for exactly this
(issue #180).

### Bridge contract change

`bridge.Request` gains an `addressee` field. The `Response` is unchanged —
whichever persona answered, the Gateway synthesizes text verbatim.

---

## Part D — Close the loop

### D1. Hermes ingress — measure before choosing

The bridge contract and HTTP client exist; nothing is listening.

Two candidates:

- **Webhook route** (`gateway/platforms/webhook.py`) — no new infrastructure,
  supported extension point, HMAC-signed. **Open question: can a route return a
  structured body synchronously, or only acknowledge?** If only acknowledge,
  this is dead on arrival.
- **API server** (`/v1/chat/completions`, `/v1/responses`) — already
  OpenAI-compatible, synchronous, bearer-auth, enabled via
  `API_SERVER_ENABLED=true` on port 8642. Supports named conversations, which
  maps cleanly onto per-persona sessions.

**The API server now looks like the stronger candidate** — it is synchronous by
construction and its `conversation` parameter gives per-persona session
continuity without inventing anything. Verify before committing.

### D2. Baseline, don't budget

Instrument the stages and **record what they actually cost**:

```
PTT release → transcript ready
transcript → persona resolved
Hermes round trip (incl. Athena tools)
synthesis
```

Write the numbers down. Improve against them. **Do not design against an
invented target** — the earlier 2 s figure was not derived from anything.

Where a fast path is worth having, the evidence will show it.

### D3. Athena relative navigation

*"Based on my current heading I want to go to XYZ airport — which way do I turn
and for how long?"*

This is **computation, not lookup**, and it belongs in Athena. Bearing math in a
language model is exactly the "do not let an LLM count units" anti-pattern in the
project AGENTS.md. Athena calculates; the LLM phrases.

Inputs: ownship position and heading (Athena has both), destination by name.
Outputs: bearing, relative bearing (±180°), turn direction (shortest arc),
distance, ETA, with the usual provenance and freshness envelope.

Fixture-first, no live DCS required. Needs a child issue under MVP-08.

**This is the highest-value Athena capability available** — the difference
between reciting facts and being useful in the cockpit.

### D4. Wire it

Set the endpoint, keep `mute: true`, verify answers land in the journal with a
correlated `transmission_id`. Unmute only under issue #181.

*"Spawn a tank near sector A"* remains a typed non-executing preview. Live
mutation crosses the read-only boundary the architecture rests on and needs its
own ADR.

---

## Files likely to change

| Path | Change |
|---|---|
| `docs/PITFALLS.md` | new — five traps, one page |
| `ATHENA.md` | link pitfalls |
| `deploy/README.md` | pilot audio setup |
| `/etc/athena-gateway/config.yaml` | drop debug audio dir |
| `pkg/athena/persona/` | new — registry, address extraction, fuzzy match |
| `pkg/athena/bridge/bridge.go` | `addressee` on Request |
| `internal/application/athena.go` | persona routing, stage timing |
| `hermes-agent: agent/chat_completion_helpers.py` | pending gateway diagnosis |
| `project-athena: src/metis/` | relative navigation |

---

## Validation

- Gateway: `make lint && make vet && make test` — 0 issues, green with `-race`.
  Baseline 1361.
- Athena: `pytest tests/unit -q` — baseline 1416 passed, 49 skipped.
- Persona routing: fixtures for each alias, near-miss spellings, unaddressed,
  address-mid-sentence, and case variation.
- Navigation: wrap-around, reciprocal headings, destination directly behind.
- Gateway `TypeError`: a failing reproduction **before** any fix.
- Every gate run unpiped with the real exit code — a piped `tail` masked 21 lint
  failures earlier in this project.

---

## Risks

| Risk | Mitigation |
|---|---|
| Gateway `TypeError` is intermittent and hard to reproduce | Confirm outcome first; if calls recover, it is lower urgency than it looks. Do not mask it by dropping the fallback provider |
| Webhook cannot answer synchronously | Test first; API server is the likely answer |
| Persona misrouting sends mission chatter to the dev lane | Fail quiet on no match; never guess |
| Accent degrades address recognition | Fuzzy match via `go-edlib`; both words transcribed cleanly today but one sample is not proof |
| Clipping at −1.4 dBFS | Pilot reducing gain; re-measure |
| Scope creep into live mutation | Preview only. Execution needs its own ADR |

---

## Order

1. **A1–A4** — light docs, capture turned off. Short.
2. **B** — gateway diagnosis. Research first, reproduce second, fix third.
3. **C** — persona registry and address extraction. Fixture-first, independent
   of B.
4. **D1–D2** — ingress decision and baseline measurement.
5. **D3** — relative navigation.
6. **D4** — wire it.

B and C are independent and can interleave. D depends on B being understood — do
not build the radio loop on a messaging layer with a known unexplained fault.

---

## Open questions

1. **Personas beyond Athena and Hermes** — what is the third likely to be? It
   shapes whether the registry needs per-persona toolsets and profiles from the
   start, or can add them later.
2. **Destination naming for navigation** — ICAO, mission waypoint number, or
   free-form name? Determines the resolution layer.
3. **Unaddressed transmissions** — fail quiet, or default to a persona?
   Recommend fail quiet until there is evidence.
