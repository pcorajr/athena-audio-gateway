# Athena Audio Gateway — fork notes

This is a fork of [`dharmab/skyeye`](https://github.com/dharmab/skyeye) (MIT),
adapted into the **Athena Audio Gateway** for Project Athena.

Governing decision: **ADR 0014 — Server-Side SRS Audio Gateway Boundary**
(`docs/architecture/adr/0014-srs-audio-gateway-boundary.md` in `pcorajr/project-athena`).

## What this fork is for

Upstream SkyEye is a GCI/AWACS bot: it listens on SRS, interprets brevity, and
answers as an air controller. This fork keeps SkyEye's **audio interface** and
replaces its **brain**.

```text
recognize -> admit -> bridge -> synthesize
```

- **Kept:** SRS client, Opus, speech-to-text, speech synthesis, transmission.
- **Replaced:** the GCI lane (`parse -> control -> compose`) becomes a text
  exchange with Hermes. Hermes decides what to say; the Gateway speaks it
  verbatim.
- **Not started at all** when the Athena lane is active: brevity parser, radar
  scope, GCI controller.

Hermes is a consumer of language. It receives a transcript and returns text. It
never receives PCM, never opens an audio device, and never speaks directly.

## Athena additions

| Package | Role |
|---|---|
| `pkg/athena/admission` | Command-channel gate: frequency + modulation + speaker |
| `pkg/athena/bridge` | Text-only Gateway↔Hermes contract and HTTP client |
| `internal/application/athena.go` | The command lane that replaces GCI |

Configuration (all optional; absent means upstream SkyEye behaviour):

| Key | Meaning |
|---|---|
| `AthenaCommandFrequencyHz` | The single command frequency |
| `AthenaCommandModulation` | Defaults to FM |
| `AthenaPilotName` | The only SRS client permitted to issue commands |
| `AthenaHermesEndpoint` | Hermes text bridge URL; requires the gate |
| `AthenaHermesTimeout` | Bounded, never retried |

## Upstream tracking policy

**Pinned to a tag, audited before every live gate. Not auto-tracked.**

Automatic tracking would let upstream change safety-relevant behaviour without
review, which is unacceptable for software that can key a radio. But a bare pin
is not safe either — see below.

### Why the audit is mandatory

We pinned `v1.10.0`. That tag shipped with **`--mute` silently ignored**:
`cmd/skyeye/main.go` set `conf.Configuration.Mute` from the flag, but
`internal/application/app.go` never passed it into `srs.ClientConfiguration`.
A run believed to be receive-only would have transmitted.

Upstream fixed it five days after the tag. A pin alone would have carried that
bug into a live validation.

### Procedure before any live gate

```sh
git fetch --tags upstream
git log --oneline v1.10.0..upstream/main
```

For each commit, decide: does it touch `pkg/simpleradio`, `pkg/recognizer`,
`pkg/synthesizer`, `internal/application`, or the build/lint gates? If yes,
cherry-pick with `-x` to preserve authorship and the upstream SHA. If it only
touches GCI concerns (`pkg/encyclopedia`, `pkg/brevity`, `pkg/parser`,
`pkg/radar`, `pkg/composer`) or docs, skip it and note why.

### Audit log

**2026-08-22 — audited `v1.10.0..upstream/main` (14 commits).**

Cherry-picked (10):

| Local | Upstream | Why |
|---|---|---|
| `d15490d` | `ab0d024` | **`--mute` was ignored.** Safety-critical. |
| `946de42` | `9467cf7` | **Framing validation in `Decode`.** A corrupt packet whose length header pointed in-bounds could decode with a garbage `OriginGUID` — the exact field our speaker check screens on. |
| `974b0ad` | `159c599` | `RadioFrequency.String()` transposed format verb |
| `869d2ec` | `9cf4c20` | Bounds the inbound voice buffer and channel |
| `27b93b4` | `099a480` | `golang.org/x/crypto` 0.51.0 → 0.52.0 |
| `0e21116` | `71c4a8a` | `wg.Go()` simplification in the SRS client |
| `042d118` | `a7dffbf` | golangci-lint v2.12.2 |
| `bac352d` | `fb5d987` | Tests run with `-race` |
| `7593145` | `bc6ee52` | `-race` hardcoded in the test target |
| `f198231` | `5eaab3b` | Baseline test coverage for `pkg/simpleradio` |

Skipped (4) — GCI or docs only, not on the Athena command path:
`60f465f` (F-14BU), `38a2c77` (encyclopedia aircraft), `bd2c727` (README),
`4c2c16d` (doc spelling).

Two cherry-picks conflicted in `pkg/simpleradio/client.go`, both because
`Transmission` now carries the receiving radio. Resolved by taking upstream's
change with our `transmissionPackets` channel type.

After the audit: `make lint` 0 issues, `make vet` clean, `make test` 1336 tests
passing **with `-race`**, no data races.

## Building

Upstream's `CLAUDE.md` applies: **never use bare `go build` / `go test`.** The
build needs `CGO_ENABLED=1`, `-tags nolibopusfile`, C include paths, and a
prebuilt `libwhisper.a`.

```sh
make whisper   # once; builds third_party/whisper.cpp
make test
make lint vet fix format
```

Arch/CachyOS dependencies: `go cmake base-devel opus libsoxr`.

## Licensing

MIT, inherited from upstream, attribution retained. **No GPL DCS-SRS source and
no AGPL OverlordBot source may enter this tree** — the SRS relationship is
protocol-level only. Audited 2026-08-22: no GPL/AGPL text in any tracked file,
no AGPL modules in `go.mod`.

## Safety posture

Receive-only. No transmit path is enabled. Live `SrsService.Transmit` is gated
by Project Athena issue #181 and requires separate explicit approval.
