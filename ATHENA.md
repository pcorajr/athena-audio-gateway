# Athena Audio Gateway: fork notes

This is a fork of [`dharmab/skyeye`](https://github.com/dharmab/skyeye) (MIT), adapted into the **Athena Audio Gateway**, the radio half of Project Athena.

**Documentation home: the `project-athena` repository** ([architecture](https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/README.md), [runbooks](https://github.com/pcorajr/project-athena/tree/dev/docs/runbooks)). This file keeps only what is specific to this fork: what changed from upstream, how upstream is tracked, how to build, and licensing. Also see `docs/PITFALLS.md` for traps this project hit.

Governing decisions: ADR 0014 (server-side SRS audio gateway boundary) and the ADR 0016+ series, in [project-athena](https://github.com/pcorajr/project-athena/tree/dev/docs/architecture/adr).

## What this fork is for

Upstream SkyEye is a GCI/AWACS bot: it listens on SRS, interprets brevity, and answers as an air controller. This fork keeps SkyEye's **audio interface** and replaces its **brain**.

```text
SRS -> admit pilot -> Whisper STT -> HTTP to adapter -> Piper TTS -> SRS (same channel)
```

- **Kept:** SRS client, Opus, speech-to-text (local Whisper), speech synthesis (Piper), transmission.
- **Replaced:** the GCI lane (`parse -> control -> compose`). In Athena mode ("SRS MVP", `athena-srs-mvp: true`) the gateway posts the transcript to the local adapter at `127.0.0.1:8650` and speaks the returned text verbatim.
- **Not started at all** in MVP mode: brevity parser, radar scope, GCI controller, personas, gRPC, remote recognizers (the configuration refuses them).

The adapter is Python, in `integrations/hermes_radio/` (Athena Eye, SimpleController, cockpit feed, memory link, Codex client). It receives text and returns text; it never gets audio. The gateway owns admission, expiry, the mute and live-TX interlocks, TTS and transmit.

### Current behaviour (live path)

- Joins SRS as `GCI Athena [BOT]` (coalition blue) on **133.0 AM, 251.0 AM and 33.0 FM**, and replies on the channel the pilot called on.
- Admits only the pilot: exact frequency and modulation, SRS client name in the list `Prometheus,Player,Client` (any capitals), then a sticky SRS id so name flips do not reset the identity.
- Calls shorter than 1 s or longer than 30 s are dropped. Answers are strictly in arrival order.
- A reply is at most two parts and 900 characters. A bridge failure means silence.
- Every call and reply is logged to the private transcript `~/.hermes/cache/athena-radio/transcript-YYYYMMDD.{jsonl,txt}` shared with the adapter.

## Athena additions

| Package | Role |
|---|---|
| `pkg/athena/admission` | Command-channel gate: frequency + modulation + speaker name + duration |
| `pkg/athena/bridge` | Text-only gateway-adapter contract and loopback HTTP/NDJSON client (`mvp.go`, `stream.go`) |
| `pkg/athena/mvp` | The SRS MVP pipeline: capture, one STT worker, ordered replier, streaming TTS |
| `pkg/athena/transcript` | Shared private transcript writer |
| `pkg/simpleradio/mvp.go` | Multi-radio reception, pilot identity, one-shot route token, transmit |
| `pkg/recognizer` | Whisper with a base prompt file and a live dynamic vocabulary file |
| `internal/application/srs_mvp.go` | Starts the MVP runner and bypasses the upstream lanes |

Some Athena settings (all in `~/.config/athena-gateway/mvp.yaml` or flags): `athena-srs-mvp`, `athena-pilot-name`, `athena-command-frequency`, `srs-frequencies`, `athena-hermes-endpoint` (loopback only), `athena-hermes-timeout`, `athena-response-max-age`, `athena-live-tx`, `mute`, `voice: feminine`. Flags win over the config file. The launcher ([start-stop](https://github.com/pcorajr/project-athena/blob/dev/docs/runbooks/start-stop.md)) sets the live values.

## Legacy, not on the live path

Older Athena lanes and upstream features still in the code: `athenaCommandLane` with personas, the Hermes API-server bridge (`bridge.NewAPIServerClient`), the generic HTTP bridge client, upstream GCI (`pkg/brevity`, `pkg/parser`, `pkg/radar`, `pkg/composer`, `pkg/encyclopedia`), auto-picture and threat monitoring, telemetry/ACMI, remote recognizers. They are deliberately retained so upstream SkyEye commits keep cherry-picking cleanly (see Upstream tracking policy); they are linked but not executed in MVP mode. Python legacy code lives under `legacy/` ([legacy.md](https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/legacy.md)).

## Upstream tracking policy

**Pinned to a tag, audited before every live gate. Not auto-tracked.**

Automatic tracking would let upstream change safety-relevant behaviour without review, which is unacceptable for software that can key a radio. But a bare pin is not safe either.

### Why the audit is mandatory

We pinned `v1.10.0`. That tag shipped with **`--mute` silently ignored**: `cmd/skyeye/main.go` set `conf.Configuration.Mute` from the flag, but `internal/application/app.go` never passed it into `srs.ClientConfiguration`. A run believed to be receive-only would have transmitted. Upstream fixed it five days after the tag.

### Procedure before any live gate

```sh
git fetch --tags upstream
git log --oneline v1.10.0..upstream/main
```

For each commit, decide: does it touch `pkg/simpleradio`, `pkg/recognizer`, `pkg/synthesizer`, `internal/application`, or the build/lint gates? If yes, cherry-pick with `-x` to preserve authorship and the upstream SHA. If it only touches GCI concerns (`pkg/encyclopedia`, `pkg/brevity`, `pkg/parser`, `pkg/radar`, `pkg/composer`) or docs, skip it and note why.

### Audit log

**2026-08-22: audited `v1.10.0..upstream/main` (14 commits).** Cherry-picked (10):

| Local | Upstream | Why |
|---|---|---|
| `d15490d` | `ab0d024` | **`--mute` was ignored.** Safety-critical. |
| `946de42` | `9467cf7` | **Framing validation in `Decode`.** A corrupt packet whose length header pointed in-bounds could decode with a garbage `OriginGUID`, the exact field our speaker check screens on. |
| `974b0ad` | `159c599` | `RadioFrequency.String()` transposed format verb |
| `869d2ec` | `9cf4c20` | Bounds the inbound voice buffer and channel |
| `27b93b4` | `099a480` | `golang.org/x/crypto` 0.51.0 to 0.52.0 |
| `0e21116` | `71c4a8a` | `wg.Go()` simplification in the SRS client |
| `042d118` | `a7dffbf` | golangci-lint v2.12.2 |
| `bac352d` | `fb5d987` | Tests run with `-race` |
| `7593145` | `bc6ee52` | `-race` hardcoded in the test target |
| `f198231` | `5eaab3b` | Baseline test coverage for `pkg/simpleradio` |

Skipped (4), GCI or docs only, not on the Athena command path: `60f465f` (F-14BU), `38a2c77` (encyclopedia aircraft), `bd2c727` (README), `4c2c16d` (doc spelling).

Two cherry-picks conflicted in `pkg/simpleradio/client.go`, both because `Transmission` now carries the receiving radio. Resolved by taking upstream's change with our `transmissionPackets` channel type. After the audit: `make lint` 0 issues, `make vet` clean, `make test` 1336 tests passing **with `-race`**, no data races. (Figures as of that audit; later Athena commits added tests.)

## Building and testing

Upstream's `CLAUDE.md` applies: **never use bare `go build` / `go test`.** The build needs `CGO_ENABLED=1`, `-tags nolibopusfile`, C include paths, and a prebuilt `libwhisper.a`.

```sh
umask 077                                  # tests check transcript privacy
export TMPDIR="$HOME/.hermes/cache/scratch" # any private, existing directory
make whisper   # once; builds third_party/whisper.cpp
make test
make test-athena-mvp   # focused offline SRS-MVP checks
make lint vet fix format
```

The live binary (`~/.local/opt/athena-gateway-mvp/bin/skyeye-radio-v11-3d5a990`) was built by hand with the same flags the Makefile uses (`BUILD_VARS`/`BUILD_FLAGS`), from the repo root (`G`):

```sh
CGO_ENABLED=1 GOARCH=amd64 GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local \
  C_INCLUDE_PATH="$G/third_party/whisper.cpp/ggml/include:$G/third_party/whisper.cpp/include" \
  LIBRARY_PATH="$G/third_party/whisper.cpp/build_go/src:$G/third_party/whisper.cpp/build_go/ggml/src" \
  go build -tags nolibopusfile -o "$OUT" ./cmd/skyeye/
```

(Source: the launcher helper `~/.hermes/cache/athena-srs-mvp-20261106/run/anycoal.sh`, outside git. Plain `make` builds `skyeye` with the same tags.)

Arch/CachyOS dependencies: `go cmake base-devel opus libsoxr`. Python adapter tests: see `integrations/hermes_radio/README.md`.

## Licensing

MIT, inherited from upstream, attribution retained. **No GPL DCS-SRS source and no AGPL OverlordBot source may enter this tree.** The SRS relationship is protocol-level only. Audited 2026-08-22: no GPL/AGPL text in any tracked file, no AGPL modules in `go.mod`.

## Safety posture

Live transmit is **on** in the Athena launcher (`--mute=false --athena-live-tx=true`), but only for replies to the admitted pilot, on the channel he called on, with the one-shot route token, expiry check and interlock re-checked per reply part. The gateway started by hand with the config file alone is muted and has live-TX off. Never start the gateway by hand against a live SRS server; use the launcher. Live transmit was originally gated by Project Athena issue #181 and needs the owner's explicit approval to change.

## Deployment

Runs on **athena** (`10.90.10.112`, Linux) alongside the adapter and Mission Memory, connecting over the LAN to the SRS server on **ATHENA-DCS** (`10.90.10.78`, Windows) at port 5002. Reads DCS only through the adapter. See [deployment](https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/deployment.md) and `deploy/README.md`.

Do not host the gateway on a machine that also runs DCS: a flight client is busy rendering, and upstream does not support local CPU speech recognition alongside DCS.
