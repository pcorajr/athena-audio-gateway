# Athena radio v2

The production entry point is now an **LLM-first OpenAI API controller**, not
the legacy keyword classifier documented below. Gateway capture is independent
of command admission. Initial commands must address Athena; the narrow bare
followups `what type is it?`, `where did it go?`, and `where is it now?` continue
an established Prometheus conversation only while its mission/health scope is
still valid. Other speakers, ambiguous identity, wrong channel, and initial
unaddressed traffic cannot gain transmit permission by being logged.

## Current runtime (controller-owned deployment)

Provide `OPENAI_API_KEY` securely through the process environment, never a CLI
argument. `OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`; only HTTPS
is accepted. `ATHENA_RADIO_MODEL` defaults to the controller-selected
`gpt-5.4-mini`. Reasoning effort is **omitted** by default: measured tool requests
reject `reasoning_effort=low`. Optional `--reasoning` is explicit, never a fallback.
The worker/reviewer model is unrelated to this voice-runtime selection.

From the candidate checkout, using the already provisioned Athena interpreter:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/pcorajr/Ai-Workspace/projects/project-athena/src:$PWD
export ATHENA_RADIO_TRANSCRIPT_DIR="$HOME/.hermes/cache/athena-radio"
/home/pcorajr/Ai-Workspace/projects/project-athena/.venv/bin/python -B \
  -m integrations.hermes_radio --state-client-config "$ATHENA_STATE_CLIENT_CONFIG" \
  --model gpt-5.4-mini --controller-timeout 6 --request-timeout 2 \
  --cache-interval 2 --port 8650
```

`ATHENA_STATE_CLIENT_CONFIG` in that command must name the existing approved
client JSON, not a new server configuration. Its socket bounds/timeout remain
owned by the unchanged public client. No DCS access, state service restart,
credential discovery, profile changes, installation, or deployment is performed
by this source. Packet106's default/controller deploys after focused GREEN,
then commissions independent RV-1; tests are not deployment/voice acceptance.

The gateway needs the new binary. Keep its approved SRS/STT/TTS configuration,
and explicitly preserve `--athena-srs-mvp=true --mute=false --athena-live-tx=true
--srs-frequencies=133.0AM --athena-command-frequency=133.0AM
--athena-pilot-name=Prometheus --athena-hermes-endpoint=http://127.0.0.1:8650/radio/v1/exchange`.
Both processes must share the same private transcript root. No key belongs in
gateway flags. The existing legacy transcription flag does not disable v2's
private audit trail; MVP recognizer content is no longer copied to general logs.

## Capture, delivery, and streaming

Daily local-time `transcript-YYYYMMDD.jsonl` and `.txt` append under the root
above (directory 0700, files 0600 on Unix). Go and Python share an OS file lock;
TXT escapes physical line breaks. JSONL retains exact text. Reception is observed
before identity/admission; other speakers and busy arrivals still reach STT.
Malformed, short, overflowed, queued-over-capacity, blank, failed, or canceled
audio is recorded with a bounded reason, never called a successful transcription.
There is no guarantee against kernel/network packet loss or physical RF collisions.
Assembly is bounded to 64 concurrent origins; decode/STT queues are bounded and
overflows are explicit blank/error records, not silent omissions or delayed commands.
Each observed ID carries one terminal-outcome receipt through assembly, decode,
receive and intake. Graceful cancellation records unfinished observations once
as blank `inbound/capture_canceled`, including sends racing queue shutdown; it
does not drain an unlimited producer stream or admit delayed replies.

`received`, `inbound`, `adapter_ingress`, `route`, `tool`, `reply_generated`,
`reply_intended`, `delivery`, and timing records share transmission IDs. Generated
text is **not** proof it was spoken. `delivery` reports first/last successful local
UDP writes (or failure), never audibility. Logging failures emit the fixed stderr
code `athena_radio_transcript_write_failed`; the next record retries opening the
sink without replaying the possibly appended record. No raw provider error/auth
bodies, audio, or health-error strings enter these records.
`reply_generated` and `text_smoke_output` identify **Athena** as `speaker`;
`request_speaker` separately preserves the pilot's routing identity. Existing
append-only records are not rewritten.

The existing URL negotiates `Accept: application/x-ndjson`. Each flushed line is
one validated v1 Response; `[DONE]` terminates the stream. The Go bridge starts
TTS on the first complete sentence while generation is still in progress. There
are at most two parts, 32 words/350 characters total, and an 8-second synthesized
audio cap. The route token is consumed once for the whole reply; each part checks
identity, channel, cancellation, expiry, mute and live-TX. No sentence is retried.
Text exceeding a bound is rejected, not clipped. A later failure cannot unsay an
already transmitted first sentence; every sentence must carry its own caveats.

## Tools and evidence limits

Only `ownship`, `query_units`, `get_unit`, `count_units`, `group_summary`,
`current_summary`, `mission_intel`, `recent_events`, and code-derived `braa` exist.
Arguments are allowlisted and bounded. Public typed framing/models, correlation,
health/freshness, projection evidence, scope and returned filters are validated.
Unsupported summary projections use the same bounded raw-client seam as ownship,
with exact projection validators; Athena source is unchanged.

The cache refreshes confirmed ownship plus nearest ten returned hostiles/friendlies
every two seconds. The source page is capped at min(100, configured max_rows);
truncated pages explicitly remain `returned_page_only`, never global nearest.
Category vocabulary is incomplete. Counts retain producer totals separately from
filtered-page counts. BRAA uses true geographic bearing, nautical miles, feet MSL;
missing position/altitude stays null. Heading reference and aspect are unavailable
in the pinned producer. Mission intel has null brief/objectives/airbases. Stale,
cached, paused, ended, inconsistent or unhealthy current state is unavailable.
Each turn retains the earliest monotonic expiry of the actual snapshot and tool
results supplied to its model, charging query duration and cache residence.
Every emitted sentence checks that deadline; a background refresh cannot renew
old wording. Expiry before the first part yields unable, and expiry after a part
stops further parts without replay. These checks do not prove semantic entailment.

Ten exchanges plus referenced IDs are kept in memory, with no raw factual tool
snapshots carried as conversation history. Mission/session/lineage changes and
observed outages clear continuation/history. Refresh after an unavailable turn
can restore live answers. Tools/speech remain untrusted data; no general Hermes
session or shell tool is involved. Model wording remains probabilistic: offline
tests prove transport/validation/interlocks, not live factual or audio quality.

## Text smoke (not radio/audio)

After controller deployment only, with the same `PYTHONPATH` and transcript root:

```sh
/home/pcorajr/Ai-Workspace/projects/project-athena/.venv/bin/python -B \
  -m integrations.hermes_radio.text_smoke \
  --question 'Athena, give me the picture.' --question 'what type is it?' \
  --question 'where did it go?' --question 'Athena, how many hostiles are recorded?' \
  --question 'Athena, summarize the groups.' --question 'Athena, current summary.' \
  --question 'Athena, what is my aircraft doing?' --question 'Athena, mission intel.' \
  --question 'Athena, recent events.' --question 'Athena, contacts within fifty miles?'
```

The smoke records `text_smoke_input/output` and reports first-sentence/total text
latency. It cannot transmit audio. Actual first-audio <=4s and audibility require
the controller's deployment and user voice test, not synthetic timing claims.

## Legacy helper reference (not the production entry point)

The remaining sections describe retained slice2 deterministic helpers and their
regression fixtures. The old `--hermes-*` adapter launch flags below are historical
and are **not** accepted by the v2 production CLI; use the current command above.

This is a separate stdlib-only Python process. It accepts the gateway's existing
text bridge, classifies a bounded read-only intent, queries Athena, and returns
short deterministic speech. No audio, DCS commands, database fallback, profile
installation, services, model/provider overrides, retries or conversation state.

## Run (controller-owned, after independent review)

Use the approved interpreter and the pinned/read-only Athena checkout:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/pcorajr/Ai-Workspace/projects/project-athena/src:$PWD
# Provision ATHENA_RADIO_HERMES_KEY securely in the process environment.
# ATHENA_RADIO_HERMES_ENDPOINT is the dedicated profile's full
# http://127.0.0.1:<operator-port>/v1/responses URL; no default is selected.
/home/pcorajr/.hermes/hermes-agent/venv/bin/python -B -m integrations.hermes_radio \
  --state-client-config /absolute/path/to/approved-client.json \
  --request-timeout <approved-header-and-body-seconds> \
  --hermes-timeout <approved-short-classification-seconds> \
  --port 8650
```

The endpoint is `POST http://127.0.0.1:8650/radio/v1/exchange`. `--port` changes
only the port; no non-loopback bind exists. `--speaker` defaults to `Prometheus`.
`--hermes-endpoint` may supply the same full URL instead of the environment.
No credentials are accepted on the command line or printed. SIGINT/SIGTERM
close the listener and drain the single bounded exchange and state worker.

The state JSON is loaded by unchanged `athena.state_service_client.load_client_config`.
It must explicitly supply `socket_path`, `max_request_bytes`, `max_response_bytes`,
`query_timeout_ms`, all four `query_limits` and all `recent_events_limits` fields.
The upstream loader restricts production sockets to `/run/athena/`; this adapter
does not loosen that rule. Deadlines are operator configuration, not inferred
from the desired audible-response latency. Nothing is installed or launched on
behalf of the controller.

Runtime dependency verified against Athena commit
`fdbfe79f8e0f859a4122197e9eaa380cbb101776`, slice2 gateway base
`1aef9b4c3a8e9ac8434bfed2d980ffca85aad169`. Revalidate against a changed dependency.
The adapter itself adds no third-party dependency; Athena's existing runtime
imports must already be provisioned in the interpreter.

## Wire and safety behavior

`contract.json` documents the unchanged native Request/Response fields;
`contract.py` is the executable validator. Requests and responses are each
bounded to 64 KiB; transcript UTF-8 is bounded to 32000 bytes; spoken output to
350 Unicode characters. Unknown/duplicate keys, malformed UTF-8/JSON, nonfinite
numbers, trailing JSON, non-UTC or reversed timestamps, wrong speaker, and
nonempty addressee are rejected. UTC nanoseconds emitted by Go remain ordered.
Only factual replies may be deliverable. Malformed HTTP is an empty 4xx;
concurrent exchanges are closed immediately, with a best-effort empty 503.
Because rejection does not read/queue the request, a client sending its body
after the early close may instead observe a broken pipe or connection reset.
Neither outcome is speech, queued work or permission to retry. HTTP chunking, compression,
ambiguous lengths and `Expect` are not accepted. Each connection closes after
one request. Header/body acquisition has a cumulative deadline.

Keyword matches run first (case-insensitive, with optional Athena/polite wording):

| Phrases | Deterministic response |
|---|---|
| ownship / onship / status / how am I / my aircraft / where am I | Reported ownship heading (reference unavailable) and speed |
| picture | Known hostile air count and nearest known group's closest aircraft BRAA |
| bogey dope / nearest threat / nearest bandit | Nearest known hostile aircraft BRAA |
| threats / SAM | Nearest known hostile air-defence unit, or no SAM data |
| mission status / situation | Recorded friendly and hostile air/ground type counts |
| home plate / nearest airbase | Unable: the pinned public producer has no airfield data |
| recent events / what happened / any events / sitrep | Latest recorded events |
| help / say again | Fixed capability list, without state access |

Specific mission status consumes its phrase before the broad status match.
Multiple keyword hits, action wording, and unsupported words around a keyword
return the capability list. Arbitrary `watch the ...` is not an ownship alias.
Only no-hit text makes one Hermes `/v1/responses` call, with `model=athena-radio`,
`stream=false`, `store=false`, `tools=[]`, `tool_choice=none`, no conversation,
no prior response and no session headers. Only an exact one-field intent JSON
is admitted. Model text never supplies queries, paths, facts or final speech.
The HTTP client uses literal loopback, no proxy, no redirect or retry, a bounded
body and a cumulative deadline. A classification failure or unrecognized answer
returns exactly: `Athena understands: ownship, picture, bogey dope, threats, mission status, home plate, recent events.`
This local capability fact is `intent_class=factual`, `state=ok`, deliverable even
when state is disconnected; `ok` does not assert that battlefield state is healthy.
Bridge fields and validation are unchanged. Model facts and tool calls are never admitted.

IMPORTANT: the Hermes server selects enabled tools from its profile config;
request `tools=[]` alone is not a sandbox. Controller preflight must verify the
dedicated `athena-radio` server actually has ALL toolsets and memory writes
disabled and the approved model/provider route. An alias is not route proof.
`store=false` does not guarantee the provider/server retains no transcripts.
No such live preflight is performed by this implementation or its tests.

Facts use fresh opaque UUIDs and a single worker thread. `recent_events` uses the
unchanged public `StateServiceClient`, limit `min(3, query max_rows, event max_rows)`.
The pinned client rejects successful `ownship` and `mission_intel`, so the approved
adapter-only `OwnshipReader` uses public `StateQueryRequest`, `encode_client_frame`,
`decode_server_frame`, `StateQueryResponse` and `STATE_API_VERSION`. It issues only
`ownship` or `mission_intel`, each with `{}`. It retains configured directional byte caps, partial I/O,
one cumulative connect/write/read/validation deadline, strict NDJSON/correlation,
and closure on all paths. It additionally validates exact ownship/position and
evidence keys, empty limits, public leaf models plus canonical safe text/finite
numbers, and generation/mission/lineage/source/health-evidence consistency.
No private upstream client method or validator substitution is used.

The other new reads use the unchanged public client: `query_units` with the
configured `max_rows`, opposite red/blue coalition and `state=present`; or two
`count_units` reads with `group_by=type`, one per friendly/hostile coalition.
They additionally validate exact projection evidence, positions, unique unit IDs,
returned filters, and truncation warnings. Unknown/neutral ownship coalition
never implies an enemy. Compositions require the same mission/session, lineage,
generation, source, publication time, observation and health cut. A changing cut
returns unavailable, without retry. Query count is bounded: two for picture,
bogey dope or threats; three for mission status; one for home plate. Each query
retains its configured cumulative timeout; there is no new timeout or row budget.

### Tactical evidence limits

- Public `GenerationUnit` has no category. `tactical.py` therefore uses explicit,
  conservative exact DCS type vocabularies for air, ground and air defence. This
  is incomplete, especially for mods: unlisted types are never classified from
  unit/group names, altitude or speed. They are omitted with a spoken qualifier.
  Type-based counts include helicopters as air, not just fixed-wing aircraft.
- Picture counts only returned known hostile aircraft. A truncated page or
  unknown types makes the count an explicit lower bound (`at least`), never the
  page size masquerading as the whole force. `count_units` rows aggregate entire
  type buckets, not unit rows; limited buckets also produce lower bounds.
  Mission status says `Recorded type-based counts` because that operation has
  no presence-state filter; it is not an active-airborne strength assertion.
- Queries are ID-ordered, not distance-ordered. Nearest selection uses great-circle
  horizontal distance, then typed group identity and unit ID for stable ties.
  Picture describes the nearest group's **closest aircraft**, not an invented
  centroid or formation size. Missing group identity is explicit. Limited pages,
  unknown types or missing positions say `Nearest returned`, not globally nearest.
- Geographic tactical bearings are initial TRUE bearings, always labeled `true`; range is nautical
  miles; aircraft altitude is thousands of feet MSL (producer `UnitData.alt` is
  documented MSL metres). No magnetic correction, aspect, inferred coordinates,
  intent, weapons range or site threat ranking is invented. Coincident/antipodal
  points have no unique bearing and say so. Missing positions/altitude stay missing.
  Ownship heading is separate: the pinned producer maps `orientation.heading`, not
  true-north yaw. Its reference is not reliably established, so the reported number
  is qualified with `heading reference unavailable`, never labeled true or magnetic.
- Air defence is only the listed types, including known radar, launcher and AAA
  components. It is not every ground unit, nor a claim a launcher is operational.
- `mission_intel` currently returns identity plus null `airbases`, with
  `partial_data`. Its exact typed projection is validated; an arbitrary object,
  changed schema or alleged airbase cannot become a friendly airfield. Home plate
  deliberately says unable until a reviewed public contract supplies that evidence.

All factual templates also check the supplied observation/generation timestamps
against the producer's integer-millisecond age rule, without consulting a live
clock, changing metadata or inventing a freshness budget. Relevant health
records must retain the public fixed dependency declarations; `healthy` requires
a success timestamp and no error metadata, as produced by `HealthRegistry`.
Legitimate nonhealthy records retain their qualifications. Error-severity issues
in a successful response's warnings array are rejected before warning wording.

Null ownship, mismatched player name, or confidence other than `high` yields
`Ownship not confirmed.` It never selects a unit from the SRS display name.
Invalid/unavailable state yields `Athena not connected.` Successful facts keep
stale/cached/paused/ended/source-health/missing-field/truncation qualifications.
Unknown warnings or event-row warnings suppress rather than discard a caveat.
An overlong answer suppresses whole; no text is clipped. No raw event payloads
or health error messages are spoken. Unknown-to-template public event kinds use
a known timestamp and `event recorded`; truly unknown vocabulary rejected by
the unchanged upstream validator yields unavailable, not fabricated events.
Valid empty pages say `No recorded events yet.` even when qualified, but never
turn a failed query into empty success.

### Deliberate limits

- Gateway still owns route/frequency/GUID/epoch freshness, private-channel
  admission, late-response suppression, mute, TTS and the transmit interlock.
  Loopback is not authentication against other local users.
- This is bounded keyword classification with one tool-less paraphrase fallback,
  not general conversational parsing. It can only trigger fixed read-only queries,
  never an action. There is no open-ended question answering.
- Socket framing matches upstream's persistent connection model: one bounded
  newline frame, rejecting trailing data in the read buffer and immediately
  queued bytes. It cannot predict bytes a peer sends after the reader closes.
- Recent-event success has no separate normalization-health field in this pinned
  public envelope. The adapter preserves available event-stream/query health and
  warnings; it does not invent a claim that normalization is current or complete.
- No live SRS/DCS/model/audio, accent-quality, audible-latency or combined Go
  composition result is established by these Python tests.

## Offline verification

From the feature checkout, with bytecode disabled to keep Athena read-only:

```sh
export PYTHONDONTWRITEBYTECODE=1
PYTHONPATH=/home/pcorajr/Ai-Workspace/projects/project-athena/src:$PWD \
  /home/pcorajr/Ai-Workspace/projects/project-athena/.venv/bin/python \
  -B -m unittest discover -s integrations/hermes_radio/tests -p 'test_*.py' -v
git diff --check
```

Tests use synthetic data, ephemeral loopback HTTP, and actual test-owned AF_UNIX
sockets under `$TMPDIR`. Only the test socket-root constant is relocated while
loading synthetic client documents; config, codec, semantic validation and I/O
are real. The permanent canonical-ownship regression loads Athena's synthetic
`tests/fixtures/state_service/e12_domain_queries.json` read-only. Timing probes
wrap real validation to delay it, never bypass it. No test reads credentials.
Context regressions also use the real `GenerationPublisher`, `HealthRegistry`
and `DomainQueryDispatcher` with in-memory synthetic inputs and an empty event
read port, including cached/nonhealthy and submillisecond-age controls. The busy
test forces headers -> real early rejection/close -> body, narrowly accepts
503 or that rejected request's broken pipe/reset, and requires first-request
success, no rejected model/state invocation or later work, and normal recovery.

## Fake-only gateway fixture interface

```sh
PYTHONPATH=<accepted-Athena-src>:<SRS-H-root> <approved-python> -B \
  -m integrations.hermes_radio.fixture_server --port 0 --scenario current_state
```

Scenarios and synthetic bridge transcript choices:

| Scenario | Transcript | Expected reply |
|---|---|---|
| current_state | status | factual/ok, F-16 heading and speed |
| recent_events | recent events | factual/ok, latest recorded event |
| unavailable | status | factual/unavailable, Athena not connected |
| stale | status | factual/stale with spoken caveat |
| ambiguous | status | factual/ownship_ambiguous, Ownship not confirmed |
| unrecognized | unclear check | factual/ok, deliverable fixed capability list |

The real keyword path remains enabled: `unrecognized` must use no keyword, or
it will correctly take the keyword route instead of the fake model fallback.
After the real HTTP handler is serving, stdout contains exactly one flushed
content-free line: `{"ready":true,"url":"http://127.0.0.1:<port>/radio/v1/exchange"}`.
This launcher never consults production config, environment credentials, live
sockets or models. Injected synthetic state frames pass through the actual
public/adapter validators and renderer, not a canned HTTP answer. Startup failure
is nonzero with a fixed diagnostic on stderr. SIGTERM/SIGINT cleanly stop only
this test process. CLI tests exercise all scenarios, readiness, HTTP and teardown.
The later SRS-G combined integration target remains controller-owned.

Packet104 intentionally changes the unrecognized fixture's old Go composition
expectation: it must now accept factual/deliverable capabilities, not silence.
Ownship speech qualifies its heading with `heading reference unavailable`. No Go test or bridge field is changed
here; the controller owns updating/revalidating downstream composition expectations.

## Whisper prompt (controller-owned activation)

`whisper-prompt.txt` contains short radio examples and the required hotwords.
The gateway already implements `--athena-prompt-file`; pass the reviewed file's
absolute path as that CLI override. This replaces the recognizer's initial
prompt and is loaded at gateway construction, not hot-reloaded. The controller
must restart the gateway if activating/changing the file. No Go build or binary
change is needed. Preserve `mute=false`, `live-tx=true`, and exactly 133.0 AM.
Adapter deployment/restart and every live text smoke remain controller-owned
after independent RV-1. Offline prompt checks do not establish transcription or
user-voice quality, live latency, or permission to restart any service.
