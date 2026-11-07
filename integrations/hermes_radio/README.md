# Hermes radio text adapter

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
| ownship / onship / status / how am I / my aircraft / where am I | Ownship heading true and speed |
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
- Bearings are initial TRUE bearings, always labeled `true`; range is nautical
  miles; aircraft altitude is thousands of feet MSL (producer `UnitData.alt` is
  documented MSL metres). No magnetic correction, aspect, inferred coordinates,
  intent, weapons range or site threat ranking is invented. Coincident/antipodal
  points have no unique bearing and say so. Missing positions/altitude stay missing.
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
Ownship speech also labels heading `true`. No Go test or bridge field is changed
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
