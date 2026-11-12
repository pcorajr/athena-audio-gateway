# Athena radio adapter (`integrations/hermes_radio`)

This is the Python half of the Athena voice stack. The Go gateway (this repo's `cmd/skyeye`) turns pilot speech into text and posts it here; this process decides what Athena says and returns text for the gateway to speak.

> Documentation home: **project-athena** ([architecture](https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/README.md); also `../../../project-athena` on the Athena host). This README covers only the code in this folder. Name note: the folder is called `hermes_radio` for historical reasons. The live adapter does **not** use a Hermes session.

## What it does now

One process, `python -m integrations.hermes_radio`, listening on `127.0.0.1:8650` (loopback only). Inside it:

| Module | Job |
|---|---|
| `__main__.py` | Starts everything in "Eye mode": transcript, Eye + its DCS-gRPC feeder, cockpit listener, SimpleController, optional Codex backend, HTTP server. |
| `server.py` | `POST /radio/v1/exchange`. Validates the request, hands it to the controller, streams the reply as NDJSON (one sentence part per line, then `[DONE]`). |
| `contract.py`, `contract.json` | The wire contract and its validator (request keys, 64 KiB bodies, 900-character speech, `factual` replies only). |
| **`simple_controller.py`** | **The radio brain.** One picture and at most one model call (two attempts) per pilot transmission. Deterministic replies for kill confirms, remaining counts, loadout, acknowledgements, impact, damage events and debrief. Range tiers 20 / 50 / 150 nm. Adds the once-per-group close-bandit call. Keeps the last eight exchanges. |
| **`eye.py`** | **Athena Eye.** In-memory DCS tracker fed by DCS-gRPC (units, events, stores via Eval, airbases). Builds the "picture": groups with ready-made BRA-then-clock call lines (one sentence each so three bandits fit one transmission, e.g. `F-5E, three five six, thirty three, angels sixteen, twelve o'clock low.`), kills, shots, threats, loadout. Also writes the dynamic Whisper vocabulary file. `python -m integrations.hermes_radio.eye --host 10.90.10.113` is a read-only live check. |
| **`cockpit.py`** | UDP 7778 listener for the client kit's `AthenaExport.lua` (RWR, lock, fuel, gear, payload). |
| **`codex_llm.py`** | Model backend: `gpt-6-luna`, effort low, on the ChatGPT/Codex subscription (token from the Hermes default profile). Any failure falls back to `gpt-5.4-mini` on the OpenAI API (`SimpleController.ask`). One deadline per exchange (`EXCHANGE_DEADLINE_S` 10 s from arrival): Codex gets at most 6 s of wall-clock time, the API model the rest, then Athena says "Prometheus, Athena, say again." No retry loop; the Hermes token is refreshed in the background, never inside a call. |
| **`memory_link.py`** | Non-blocking publisher from the Eye to Mission Memory (Unix socket); gives the controller a memory summary (120 ms limit) and requests debriefs. Memory down means these return "unavailable" and the radio carries on. |
| `debrief.py` | CLI: `python -m integrations.hermes_radio.debrief --mission last [--read]`. |
| `transcript.py` | Shared private transcript writer (`transcript-YYYYMMDD.{jsonl,txt}`), file-locked with the Go side. |

**Answers every pilot transmission.** The pilot does not need to say "Athena" first. Only the configured pilot (`--speaker`, default `Prometheus,Player,Client`) is answered; empty or junk transcripts are dropped.

## Legacy, not on the live path
The old State Service adapter (`Controller`, `RadioTools`, `OpenAIProvider`, `Adapter`, `facts`, `tactical`, the keyword intent classifier, ...), its fixtures and tests were moved to [`legacy/integrations/hermes_radio_legacy/`](../../legacy/integrations/hermes_radio_legacy/README.md) (REL-5, `git mv`, history kept). The live adapter imports nothing from there and no longer accepts `--state-client-config`, `--reasoning`, `--controller-timeout` or `--cache-interval` (passing one is now an argparse error, and running without `--eye-host` exits 1); the legacy entry point is `python -m legacy.integrations.hermes_radio_legacy`. Reference only: see [legacy.md](https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/legacy.md). Do not extend it.
`transcript_fixture.py` stays here (the Go transcript test uses it).
## Running it

Normally you do not run it by hand. The launcher `~/.hermes/cache/athena-srs-mvp-20261106/run/eye-up-r8.sh` starts Memory, this adapter and the gateway from a frozen release snapshot. See [start-stop](https://github.com/pcorajr/project-athena/blob/dev/docs/runbooks/start-stop.md).

Key options (flag, or environment variable):

| Option | Env | Default |
|---|---|---|
| `--port` | | 8650 |
| `--eye-host`, `--eye-port` | `ATHENA_EYE_HOST`, `ATHENA_EYE_PORT` | overridden by `ATHENA_DCS_SOURCE` (`client` = 10.90.10.113:50051, `server` = 10.90.10.78:50051) |
| `--cockpit-port`, `--cockpit-host` | `ATHENA_COCKPIT_PORT`, `ATHENA_COCKPIT_HOST` | 7778 (0 = off), 0.0.0.0 |
| `--test-ownship` | `ATHENA_TEST_OWNSHIP` | unset. **Test only:** exact DCS unit name treated as the pilot when no real player unit is in the mission (a real player always wins). The picture says `TEST stand-in pilot: <unit>`; Memory records carry `test_ownship`. Never set for a real flight. |
| cockpit sender allowlist | `ATHENA_COCKPIT_ALLOWED` (comma list, `*` = anyone) | the pilot's client PC (`ATHENA_DCS_CLIENT_HOST`, else 10.90.10.113) in client AND server mode (AthenaExport.lua always runs on the client PC), plus the Eye's DCS host; other senders are dropped, counted and named (`cockpit.rejected_packets_other_source`, `cockpit.rejected_senders`, and `cockpit.reason` while no packet was accepted) |
| `--llm-backend` | `ATHENA_RADIO_BACKEND` | `openai`; the launcher sets `codex` |
| `--codex-model`, `--codex-effort`, `--codex-timeout` | `ATHENA_RADIO_CODEX_*` | `gpt-6-luna`, `low`, 6 s (wall clock) |
| `--model` (API fallback) | `ATHENA_RADIO_MODEL` | `gpt-5.4-mini` |
| `--speaker` | `ATHENA_RADIO_SPEAKER` | `Prometheus,Player,Client` |
| `--transcript-dir` | `ATHENA_RADIO_TRANSCRIPT_DIR` | `~/.hermes/cache/athena-radio` |
| `--whisper-prompt-out` | `ATHENA_WHISPER_DYNAMIC_PROMPT` | dynamic Whisper vocabulary file |
| memory | `ATHENA_MEMORY_ENABLED`, `ATHENA_MEMORY_ROOT`, `ATHENA_MEMORY_SOCKET` | enabled by the launcher; socket `~/.hermes/cache/athena-memory/memory.sock` |

`OPENAI_API_KEY` goes in the process environment only, never on a command line.

## Whisper prompt

The gateway builds Whisper's initial prompt from two parts:

1. A **base prompt file** (`--athena-prompt-file`). The launcher passes `whisper-prompt.txt` from this folder. Its text is an older phrase list from the keyword era ("Athena, ownship. Athena, picture. Athena, bogey dope. ... home plate ..."). It is loaded once at gateway start.
2. A **dynamic prompt** (`--athena-dynamic-prompt-file`), rewritten by the Eye every 30 s: your callsign, the ten nearest airfields, and brevity words. The gateway re-reads it when it changes (max 600 characters; ignored when older than 30 minutes).

The dynamic prompt carries the current vocabulary. The base file is a candidate for refresh (backlog).

## Wire and safety behaviour

- Loopback only; one request per connection; 64 KiB body; header and body deadline 2 s.
- Request keys are exactly `transmission_id, transcript, speaker, frequency_hz, modulation, received_at, transcribed_at`; `addressee` must be empty; speaker must be in the pilot list.
- Responses carry `deliverable`, `speech` (at most 900 characters), `intent_class` (`factual` when deliverable), `state`. At most two deliverable parts per reply (wire limit). SimpleController itself sends ONE part within `MAX_REPLY_CHARS` (280 characters, about 21 s of Piper speech at the measured 13.4 characters a second, under the gateway's 30 s cap; the spoken debrief within `DEBRIEF_CHARS` 150), and fixed replies (kills, tally, loadout, launches, pictures, intent acks, fuel, nearest or named airfield, landing/approach/takeoff requests = the field and its traffic, friendly traffic) never call the model; Athena never clears or approves anything, and a model reply saying "cleared to land" or "permission granted" has those words removed. Named fields resolve against every airfield of the mission (the Eye's `_airfields`, kept out of the model's picture); transcript reply records carry `route` = `fixed` | `llm` | `debrief` | `fallback`, `model` (who wrote it) and `timings_ms`. A deliverable frame also carries the optional `route` and `model` labels; the gateway copies them into its `reply_intended`, `delivery` and `exchange_outcome` records (frames without them stay valid and are labelled `LLM`).
- Any contract error gives an empty 400; the gateway then says "say again" (it does so on any bridge failure or a reply over 30 s of audio). Failures mean a fixed fallback line, never an invented fact.
- The gateway owns admission, expiry, mute/live-TX interlocks, TTS and transmit. This process never touches audio.

## Transcript records

Both sides append to `~/.hermes/cache/athena-radio/transcript-YYYYMMDD.{jsonl,txt}` (directory 0700, files 0600). Kinds include `received`, `inbound`, `adapter_ingress`, `route`, `tool` (full picture and `call_class`), `reply_generated`, `reply_intended`, `delivery`, `exchange_outcome`, `controller_error`, timing. Generated text is not proof it was spoken; `delivery` reports local packet writes, not audibility. Reply records carry `route` = `fixed` | `llm` | `debrief` | `fallback` and `model` (`call_class` is in the `tool` record). Splash, threat and close-bandit calls, kill read-backs and launch calls ('same missile') count as said only once delivered: at the next pilot call the controller reads the gateway's `exchange_outcome` for the previous reply and, if it was not `sent`, makes them pending again (`route` `rearm`). A new mission or DCS session (`Eye.reset_at` changed; a stream reconnect does not) clears the controller's chat history and kill/missile bookkeeping (`route` `reset`).

## Tests

Use `umask 077` and a private `TMPDIR`, or tests fail for the wrong reason.

```sh
umask 077
export TMPDIR="$HOME/.hermes/cache/scratch" PYTHONDONTWRITEBYTECODE=1
cd ~/Ai-Workspace/projects/athena-audio-gateway
PYTHONPATH=$HOME/Ai-Workspace/projects/project-athena/src:$PWD \
  $HOME/Ai-Workspace/projects/project-athena/.venv/bin/python -B \
  -m unittest discover -s integrations/hermes_radio/tests -p 'test_*.py'
```

Tests use synthetic data, ephemeral loopback HTTP and test-owned Unix sockets. They read no credentials and need no DCS. They prove transport, validation and logic, not live voice quality, latency or recognition.

The default run covers live code only. Legacy tests run on demand from the repo root; `athena` and the full `metis` live only in project-athena `legacy/src`, so the path must be `PYTHONPATH=$HOME/Ai-Workspace/projects/project-athena/legacy/src:$HOME/Ai-Workspace/projects/project-athena/src:$PWD`: `python -B -m pytest legacy` (or `unittest discover -s legacy/integrations/hermes_radio_legacy/tests -p 'test_*.py'`).


## Offline text smoke (no audio)

`python -m legacy.integrations.hermes_radio_legacy.text_smoke --question 'picture' --question 'loadout'` sends text questions to an **already running** adapter (`--url`, default `http://127.0.0.1:8650/radio/v1/exchange`) as the pilot and records the replies and latency in the transcript. It never touches audio or SRS. Note that it causes real model calls and real transcript entries, so run it only when the stack is up on purpose.
