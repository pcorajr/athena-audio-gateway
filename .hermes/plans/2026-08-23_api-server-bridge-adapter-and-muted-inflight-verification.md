# Close the Radio Loop: API-Server Bridge Adapter, In-Game Verification, and the Next Capability

**Date:** 2026-08-23
**Status:** Plan — not executed
**Repos:** `pcorajr/athena-audio-gateway` (`athena/main`), `pcorajr/project-athena` (`dev`)
**Governing decisions:** ADR 0014 (Server-Side SRS Audio Gateway Boundary), ADR 0012 (Athena/Hermes split)

This is the final development push before in-game testing. Scope is deliberately
closed: Stage 1 builds the missing adapter, Stage 2 proves the loop end to end
while muted, Stage 3 is one chosen capability. Nothing else.

---

## Goal

The gateway currently hears the pilot, admits them, transcribes, resolves which
persona was addressed — and then stops, because nothing is listening. Close that
gap without weakening the safety boundary that everything else rests on.

---

## Current context — verified, not assumed

### What already works

| Stage | Evidence |
|---|---|
| Audio in | peak −1 dBFS, quiet floor `0.00000`, SRS effects proven RX-side |
| Admission | frequency + modulation + speaker, observed not trusted |
| Recognition | verbatim on every live utterance, ~1.0 s, `ggml-small.en` CPU |
| Persona routing | `athena`/`hermes` resolved, address stripped, fuzzy at distance 1 |
| API server | live on `127.0.0.1:8642`, bearer auth enforced |
| Session isolation | **proven**: each `conversation` recalled its own codeword; Athena denied knowing Hermes's |

Measured latency baseline (real, replacing the invented 2 s target):

| Case | Time | in / out tokens |
|---|---|---|
| Trivial, no tools | 1.62 s | 38,608 / 5 |
| One terminal tool call | 4.81 s | 77,321 / 74 |
| Radio-realistic question | 1.83 s | 38,616 / 9 |
| Session recall | 1.74–2.92 s | — |

### The blocking gap

Our bridge and the API server do not speak the same protocol:

```
bridge.HTTPClient sends:    {transmission_id, transcript, speaker, addressee, ...}
bridge.HTTPClient expects:  {deliverable, speech, intent_class, state, suppression_reason}

API server accepts:         {model, input, conversation, store}
API server returns:         {id, output:[{content:[{type:"output_text", text}]}], usage}
```

Setting `athena-hermes-endpoint=127.0.0.1:8642` today fails every exchange.
**Wiring is not a config change.**

### The existing contract is strict — and that is an asset

`bridge.Response.Validate()` already enforces:

- `intent_class` ∈ {factual, cue, action_preview, unrecognized}
- `state` ∈ {ok, stale, degraded, unavailable, ownship_ambiguous}
- deliverable ⇒ non-empty speech **and** no suppression reason
- non-deliverable ⇒ **no speech** and a mandatory suppression reason

That last pair is the safety property. A response that fails validation cannot
carry speech. The adapter's job is to produce responses that satisfy this
honestly, never to relax it.

---

## Stage 1 — Bridge adapter for the API server

### 1.1 The central decision: structured reply, conservative fallback

Two options were on the table. Take **both**, in priority order:

**Primary — instruct Hermes to answer in a parseable shape.** The request
carries a system-level instruction asking for a small JSON object:

```json
{"speech": "...", "intent": "factual|cue|action_preview|unrecognized",
 "state": "ok|stale|degraded|unavailable|ownship_ambiguous"}
```

**Fallback — conservative inference when parsing fails.** If the reply is not
valid JSON, or omits fields, or names an unknown enum value, the adapter does
**not** guess a permissive answer. It returns a suppressed response citing the
parse failure.

Why both: the primary path gives clean typed data in the normal case; the
fallback guarantees that a model which ignores the instruction, hallucinates an
enum, or emits prose can never produce a transmittable action.

### 1.2 Fail-closed rules — the ADR 0014 boundary in code

These are non-negotiable and each gets a dedicated test:

1. **Unparseable reply → suppressed.** Never synthesise arbitrary prose as
   speech. `IntentUnrecognized`, `StateDegraded`, reason `unparseable_reply`.
2. **Unknown enum value → suppressed.** A model inventing `intent: "command"`
   must not be coerced to the nearest known value.
3. **`action_preview` is never deliverable.** Even when the model marks it
   deliverable with well-formed speech, the adapter forces suppression. Live
   mutation is out of scope under ADR 0014 and #181; an action-shaped answer
   reaching the radio is the exact failure this plan exists to prevent.
4. **Empty speech with `deliverable: true` → suppressed.** Contradictory, so
   trust the safer reading.
5. **HTTP error, timeout, or cancellation → suppressed**, never retried. A late
   answer on a radio is worse than no answer, and the existing `Exchange`
   contract already states an error means nothing may be transmitted.
6. **Response `transmission_id` is set by the adapter**, from the request. The
   model must not be able to influence correlation.
7. **Speech length is bounded.** A model returning three paragraphs would tie up
   the radio; over the cap → suppressed with `speech_too_long`, not truncated
   mid-sentence.

### 1.3 Persona → conversation mapping

`Persona.Endpoint` already exists but is unused. Extend `Persona` with a
`Conversation` field defaulting to the persona name, and pass it as the
`conversation` parameter. Isolation is already proven empirically; the adapter
just has to preserve it.

Guard: if `addressee` is empty (single-persona deployment, nil registry), fall
back to a configured default conversation rather than sending none — an absent
`conversation` means a fresh session per transmission, silently destroying
continuity.

### 1.4 New file: `pkg/athena/bridge/apiserver.go`

```go
// APIServerClient adapts the Hermes OpenAI-compatible API server to the
// bridge contract.
type APIServerClient struct {
    endpoint    string        // http://127.0.0.1:8642
    apiKey      string        // API_SERVER_KEY
    model       string        // "hermes-agent"
    timeout     time.Duration
    maxSpeech   int
    defaultConv string
    http        *http.Client
}

func NewAPIServerClient(cfg APIServerConfig) (*APIServerClient, error)
func (c *APIServerClient) Exchange(ctx context.Context, req Request) (Response, error)
```

Internal helpers, each independently testable:

- `buildInput(req Request) string` — transcript plus the reply-shape instruction
  and the minimum context (speaker, frequency, addressee).
- `parseReply(transmissionID, raw string) (Response, error)` — the entire
  fail-closed decision table. **Pure function, no I/O**, so every rule above is
  a table-driven unit test with no network.
- `extractOutputText(body []byte) (string, error)` — walk
  `output[].content[].text`, tolerating multiple message items.

Keep `HTTPClient` untouched. Two implementations of one interface, selected by
config, is cleaner than one client with a mode flag — and it leaves a working
path if the API server is ever unavailable.

### 1.5 Secret handling

`API_SERVER_KEY` is a credential. It must come from the environment, never a CLI
flag (flags appear in `ps` output and shell history) and never a config file
committed to a repo. Read `ATHENA_HERMES_API_KEY` — falling back to
`API_SERVER_KEY` — and fail startup with a clear message if the endpoint is
configured but no key is present.

Log the endpoint. **Never** log the key, the full request body, or the raw
reply at info level. The transcript is already treated as sensitive elsewhere in
this codebase; keep that consistent.

---

## Stage 2 — Wire and verify in-game, still muted

### 2.1 Configuration

```yaml
athena-hermes-endpoint: http://127.0.0.1:8642
athena-hermes-timeout: 15s      # generous; the budget is being measured, not enforced
mute: true                      # unchanged — #181 still gates transmission
```

`ATHENA_HERMES_API_KEY` in the service environment file, mode `0600`, owned by
the service account.

### 2.2 Stage timing instrumentation

Record and log per transmission, correlated by `transmission_id`:

```
ptt_release → transcript_ready
transcript  → persona_resolved
bridge_exchange (the Hermes round trip)
total
```

Purpose is a **baseline to improve against**, not a threshold to enforce. No
design gets rejected for exceeding a number that was never measured.

### 2.3 Pre-flight checks — before the pilot is in the cockpit

Run these on the ground so a flight is not wasted on a config error:

1. Gateway starts with the endpoint set; journal shows persona routing enabled.
2. Synthetic exchange against the live API server returns a valid response.
3. Wrong/absent API key produces a clean startup failure, not a runtime one.
4. `mute: true` confirmed in the running config.

### 2.4 The in-game test

Pilot flies, keys 133.0 AM, and says a small set of deliberately varied calls:

| Call | Exercises |
|---|---|
| "Athena, what am I flying?" | factual, ownship |
| "Athena, what is near me?" | factual, mission state |
| "Hermes, what branch am I on?" | the dev persona, separate session |
| "What is near me?" (no address) | **must be ignored** |
| "Athena, spawn a tank at sector alpha" | **must be action_preview → suppressed** |

Watch the journal for the full chain per `transmission_id`. Nothing can
transmit; `mute: true` was proven inert-safe when it was fixed earlier.

The last two rows matter most. They are the safety properties, exercised live
rather than only in unit tests — and this project has already produced two bugs
(`--mute` inert on the pinned tag, the lane selection keyed on the wrong field)
that unit tests passed and a real run caught.

---

## Stage 3 — Choose one

Both are worthwhile; do exactly one before testing.

### Option A — Relative navigation (`project-athena`)

*"Based on my current heading I want to land at XYZ — which way do I turn and
for how long?"*

Computation, not lookup, and it belongs in Athena. Bearing math inside a
language model is the "do not let an LLM count units" anti-pattern named in the
project's own AGENTS.md. Athena calculates; the LLM phrases.

- Inputs: ownship position + heading (available), destination by **mission
  waypoint first**, ICAO second, free-form deferred (agreed earlier).
- Outputs: bearing, relative bearing ±180°, turn direction by shortest arc,
  distance, ETA, with the standard provenance/freshness envelope.
- Fixture-first; wrap-around, reciprocal heading, and destination-directly-behind
  cases are where this kind of code fails.
- Needs a child issue under MVP-08.

**Highest capability value.** Turns Athena from a fact-reciter into something
useful in the cockpit.

### Option B — Trim the 38k-token floor

Every request pays ~38,600 input tokens before the pilot says anything — system
prompt, memory, and every tool schema. A radio-scoped Hermes profile with a
narrow toolset (mission queries only, no browser/image/media) would cut that
substantially.

**Highest latency value**, and it directly attacks the largest measured lever.

### Recommendation

**Option A.** Latency is currently tolerable and unmeasured in the cockpit —
Option B optimises a number we have not yet felt. Option A adds a capability the
pilot explicitly asked for. If the in-game test shows latency actually hurts,
Option B becomes evidence-backed rather than speculative.

---

## Files likely to change

| Path | Change |
|---|---|
| `pkg/athena/bridge/apiserver.go` | **new** — adapter, parse, fail-closed table |
| `pkg/athena/bridge/apiserver_test.go` | **new** — safety rules first |
| `pkg/athena/bridge/bridge.go` | no contract change expected |
| `pkg/athena/persona/persona.go` | `Conversation` field |
| `internal/application/app.go` | client selection, key resolution, startup validation |
| `internal/application/athena.go` | pass conversation; stage timing |
| `internal/conf/configuration.go` | endpoint mode, timeout, max speech |
| `cmd/skyeye/main.go` | flags; **no key flag** |
| `deploy/athena-gateway.yaml`, `deploy/README.md` | endpoint + env-file key |
| `docs/PITFALLS.md` | 202-vs-synchronous; secrets in flags |
| `project-athena: src/metis/`, `tests/unit/` | Stage 3A only |

---

## Tests and validation

**Order matters: write the fail-closed tests before the adapter.** They are the
specification.

Gateway (baseline 1380, `-race`):
- `parseReply` table: valid JSON; missing fields; unknown intent; unknown state;
  `action_preview` marked deliverable; empty speech + deliverable; over-long
  speech; prose instead of JSON; JSON wrapped in prose; empty body.
- Every suppressed path asserts **no speech survives** and a reason is present.
- Transport: non-200; timeout; cancelled context; malformed envelope.
- Conversation mapping incl. the empty-addressee default.
- Round-trip: adapter output satisfies `Response.Validate()` in every case.

Athena (baseline 1416 passed, 49 skipped) — Stage 3A only.

Gates, each run **unpiped with the exit code captured** — a piped `tail` masked
21 lint failures earlier in this project:

```
make lint && make vet && make test && make skyeye && git diff --check
```

Then verify against the **built binary**, not just tests. Two real bugs in this
project passed unit tests and were caught only by running it.

---

## Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| **An action-shaped answer becomes transmittable** | `action_preview` forced non-deliverable regardless of what the model claims; dedicated test; exercised live in Stage 2 |
| Model ignores the reply-shape instruction | Conservative fallback; unparseable → suppressed, never prose-as-speech |
| Model invents an enum value | Rejected, not coerced to nearest |
| API key leaks via `ps`/history | Environment only, never a flag; never logged |
| Missing `conversation` silently breaks continuity | Default conversation; explicit test |
| Latency worse than it feels in the cockpit | Instrument and baseline; do not enforce an invented budget |
| Gateway `Mapping()` fault surfaces mid-flight | Known: 1/30 days, not reproduced. Suppression path already handles a failed exchange safely |
| Scope creep in "last development work" | Stage 3 is **one** option, not both |

---

## Open questions

1. **Reply-shape instruction placement** — per-request `instructions`, or a
   persistent instruction in the persona's Hermes profile? Per-request is
   self-contained and survives profile drift; a profile-level instruction keeps
   the prompt smaller. Leaning per-request for Stage 1.
2. **Max speech length** — a first cut of ~350 characters (roughly 20 seconds
   spoken) seems right for radio. Confirm against how Athena's answers actually
   read once heard.
3. **`store: true` growth** — persistent conversations accumulate. Not a Stage 1
   problem, but worth a retention decision before extended flying.

---

## Order

1. Stage 1 tests (the fail-closed table) — the specification.
2. Stage 1 adapter + persona conversation mapping.
3. Stage 1 config, flags, secret handling, startup validation.
4. Full gates + built-binary verification, incl. deliberate misconfiguration.
5. Stage 2 pre-flight on the ground.
6. Stage 3 (recommend Option A).
7. **In-game test, muted.**

Steps 1–4 are the bulk. Step 5 is minutes. Step 7 is the payoff — and the first
time the pilot sees the whole chain run on a live radio call.
