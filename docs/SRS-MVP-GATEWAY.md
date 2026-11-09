# SRS PTT-only MVP gateway

This mode reuses the gateway's local Whisper/Piper and SRS/Opus path. PTT on the
configured command radio is the trigger. There is no wake word, persona registry,
development persona, GCI controller, Tacview dependency, or gRPC command path.
The text adapter owns factual interpretation and wording; the gateway never
rewrites an answer or sends audio to the adapter.

## Configuration

CLI flags and YAML keys use the same names. Environment overrides use the
existing `SKYEYE_` prefix and underscores. CLI values override YAML/environment.
This is a mode-only example, not a deployment authorization or a complete config:

```yaml
athena-srs-mvp: true
srs-frequencies: 133.0AM
athena-command-frequency: 133.0AM
athena-pilot-name: Prometheus
athena-hermes-endpoint: "http://127.0.0.1:<approved-port>/radio/v1/exchange"
athena-response-max-age: 10s
athena-hermes-timeout: 10s
athena-hermes-max-speech: 350
mute: true
athena-live-tx: false
recognizer: openai-whisper-local
enable-grpc: false
enable-transcription-logging: true
```

Replace the endpoint placeholder with the controller-approved adapter port.
Supply the existing SRS connection/coalition/credential and local Whisper model
settings through the approved deployment mechanism. This document contains no
credentials. Do not point this mode directly at a Hermes `/v1/responses` server.

| Flag / YAML key | MVP behavior |
|---|---|
| `athena-srs-mvp` | Default `false`. Explicitly selects native PTT-only mode; historical routing outside this mode is unchanged. |
| `athena-command-frequency` | Required, with explicit AM or FM. |
| `srs-frequencies` | Exactly one entry, equal to the command frequency **and modulation**. Use the scalar spelling above for one radio. Unknown/omitted modulation is rejected rather than inferred. |
| `athena-pilot-name` | Required. Resolve from current SRS metadata, never a hardcoded GUID. |
| `athena-hermes-endpoint` | Required full `http://<literal-loopback-IP>:<port>/radio/v1/exchange` URL. No DNS, userinfo, query, redirect, proxy or API-key-selected destination. |
| `athena-response-max-age` | Default `10s`; must be positive. Bounds assembled reception through first voice write, including STT, adapter, TTS and channel wait. |
| `athena-hermes-timeout` | `0` uses the existing `10s` bridge timeout. Negative is rejected. The exchange's remaining max-age budget can be shorter. No retries. |
| `athena-hermes-max-speech` | `0` uses `350` Unicode characters. May be reduced, never raised above 350. Oversized speech is rejected, not truncated. |
| `athena-live-tx` | Default `false`. Both `true` **and** `mute: false` are necessary for a voice write. It is an interlock, not human authorization. |
| `mute` | Always overrides live TX. Muted operation still exercises STT, adapter and TTS for an otherwise valid reply. |
| `enable-transcription-logging` | Configurable; existing default is `true`. Set `false` to omit gateway transcript text. Timing logs are content-free regardless. Adapter/provider retention is a separate policy. |
| `athena-debug-audio-dir` | Must be absent/empty. MVP startup rejects diagnostic WAV capture. |
| `athena-persona` | Must be absent/empty. No default Athena/Hermes persona registry is constructed. |
| `recognizer` | Must be `openai-whisper-local`. No cloud STT fallback. |
| `enable-grpc` | Must be false. |

Use positive decimal MHz with uppercase `AM` or `FM` for `srs-frequencies`,
for example `133.0AM`, `133.025AM` or `30.0FM`. Exponent notation (including
`133e0FM` and `133.0e0FM`), signs, hexadecimal notation, underscores and leading
whitespace are rejected before runtime parsing. Whitespace between the number
and modulation or after the modulation is allowed. This MVP-only restriction
prevents the legacy parser's permissive AM fallback from changing the route;
non-MVP parsing is unchanged.

Malformed config values fail startup rather than silently retaining defaults.
MVP config is validated before network connection, and the CLI validates it
before loading Whisper. Radio guard/retransmit is disabled. The gateway does not
write diagnostic audio and advertises recording consent as false; that is not a
guarantee about another SRS participant's recording behavior.

## Admission and delivery

- Exactly one unencrypted command frequency/modulation is admitted and encoded
  for TX, with exact frequency equality in MVP mode. Other radios cannot enter
  through the legacy all-radio encoder or legacy transmit entry points.
- The admitted display name must resolve uniquely across the current client
  snapshot, on the command route and coalition. Duplicate names, missing radios,
  tune-away, disconnect, seat/unit changes and reconnect invalidate old work.
  A display name is not cryptographic identity or proof of DCS ownship; the
  adapter independently checks ownship facts.
- Receiver packet/sample/duration overflow drops the **whole** utterance, never
  its prefix. Malformed Opus in MVP also drops the whole decoded utterance.
  The existing receiver requires more than one second of received audio; a
  short one-word key-up is not a supported reliability claim.
- There is one serial exchange through STT → bridge → TTS → final write. Audio
  publication is nonblocking in MVP; busy stages do not accumulate a reply
  backlog. Replayed packet IDs and consumed response route tokens are rejected.
- Before each stage, and again at the socket writer, identity/connection state
  is checked. Reconnection replaces the live map, invalidates queued work and
  never replays a partially sent reply. Old TCP metadata and UDP generations
  cannot repopulate/deliver an old route.
- Clear-channel waiting is cancellable and bounded by remaining response age.
  Traffic from an unadmitted speaker on the command channel still keeps it busy.
  If expiry occurs before first send, transmit nothing. Once a short reply has
  started, finish it rather than cutting off its qualification; process
  cancellation, identity change, or transport failure aborts it without replay.
- The frozen native response must match the request ID, fit the speech limit,
  and be `deliverable: true` with `intent_class: factual`. Other intents are not
  spoken. Stale/degraded/unavailable/ambiguous factual wording is preserved
  verbatim, not promoted to clean facts. Errors produce silence.
- JSON is bounded to 64 KiB and rejects unknown/duplicate keys, missing required
  fields, null/type substitutions, malformed Unicode and trailing data. Speech
  control characters are rejected. Transcript UTF-8 is bounded to 32000 bytes.

Local Whisper/Piper APIs may not interrupt an inference already running when a
context expires. The gateway discards its late result and keeps the single
exchange occupied until the backend returns; it does not start competing work.
No live inference, accent quality, SRS interoperability or audible latency is
established by the offline tests.

## Timing evidence

Each accepted pipeline exchange emits one `srs_mvp_timing_ms` JSON log event:

`transmission_id`, `received`, `stt_done`, `reply_received`, `tts_done`,
`first_send`, `last_send`, `outcome`.

Times are integer millisecond offsets from assembled reception (`received=0`),
using the process monotonic clock. Unreached stages are `-1`; a transport/contract
error without a validated bridge reply leaves `reply_received=-1`. Send clocks
are successful local packet-write completion times. No transcript, answer,
GUID, pilot name or tactical facts are included in this timing event.

These clocks do **not** measure physical PTT release or receiver audibility.
Endpointing precedes `received`; decoding and STT follow it. A successful socket
write is not proof that a pilot heard an answer. The legacy idle/exit-after timer
is not started in the isolated MVP run loop; the controller owns process
start/stop and uses normal process cancellation/signals.

## Offline checks

Use Make, not direct Go build/test commands:

```sh
make test-athena-mvp
git diff --check
```

The focused target uses the existing `BUILD_VARS`/`BUILD_FLAGS`, `-race -count=1`,
`TestSRSMVP...` and existing mute regressions. It does not run `generate`, build
Whisper, load models, start the application, or install prerequisites. The
adapter round-trip test is visibly skipped unless its explicit root is supplied.

If this worktree lacks Whisper headers/libraries, an already provisioned build
can be supplied **read-only** with the existing Makefile variable:

```sh
make test-athena-mvp WHISPER_CPP_PATH=/absolute/path/to/existing/whisper.cpp
```

If those prerequisites are unavailable, report the full-target failure and the
excluded `internal/application` and `cmd/skyeye` packages. The independent safety
packages can still be exercised explicitly, without claiming the exclusions pass:

```sh
make test-athena-mvp SRSMVP_PACKAGES='./pkg/simpleradio ./pkg/simpleradio/voice ./pkg/athena/admission ./pkg/athena/bridge ./pkg/athena/mvp'
```

After **both candidates are reviewed**, the controller runs composition:

```sh
SRSMVP_TEXT_ADAPTER_ROOT=/absolute/path/to/accepted-SRS-H \
SRSMVP_ATHENA_SRC=/absolute/path/to/accepted-Athena/src \
SRSMVP_PYTHON=/absolute/path/to/approved/python \
make test-athena-mvp-integration
```

The integration target runs only `TestSRSMVPTextRoundTrip`. It starts the
specified `legacy.integrations.hermes_radio_legacy.fixture_server --port 0 --scenario <name>`
with `-B`, explicit PYTHONPATH and a restricted environment, reads its content-free
loopback readiness URL, and requires clean SIGTERM teardown. The fixture is
owned by SRS-H and injects fake model/state dependencies into its real adapter.
No production config, credential, model, socket, or SRS server is selected.

Scenarios: current_state, recent_events, unavailable, stale, ambiguous,
unrecognized. Tests combine the real gateway bridge, serial pipeline and
identity-bound transport with synthetic STT/TTS and an in-memory packet writer.
They check normal, wrong-correlation, late, muted, live-TX-off and duplicate
paths. An absent/unaccepted fixture is pending composition evidence, not a pass.

Offline success is not permission to deploy or transmit. Independent RV-1,
controller acceptance, muted key-up and the separate live-transmit gates remain.
