# Wire Athena's Mission State Into Hermes: Instruction Leak, Live Store, and Tool Exposure

**Date:** 2026-08-24
**Status:** Approved for execution — implement without further review
**Repos:** `pcorajr/athena-audio-gateway` (`athena/main`), `pcorajr/project-athena` (`dev`)
**Governing decisions:** ADR 0014 (SRS Audio Gateway Boundary), ADR 0012 (Athena/Hermes split)

---

## Goal

The radio loop works end to end, but Athena answers `state=unavailable` because
Hermes has no Athena tools and there is no mission-state store behind them.
Close both gaps, and fix a defect introduced in the bridge adapter along the
way.

---

## Current context — verified by inspection

### What works

Live in-flight test on 133.0 AM, 16 transmissions:

- Recognition ~1.0 s, verbatim, dead consistent
- Persona routing: `athena` and `hermes` both resolved; 6 unaddressed calls
  correctly ignored
- `"Athena, spawn a tank at sector alpha"` → `intent=action_preview` →
  **suppressed on a live radio call**
- Hermes persona returns `state=ok`; nothing transmitted throughout

### The three defects

**1. Reply-shape instruction leaks across turns.**

A plain question to the `athena` conversation came back as:

```json
{"speech": "Too many to read out on the radio...", "intent": "factual", "state": "ok"}
```

The adapter sends `instructions` on a `store: true` conversation, so it
persists into the stored session and contaminates every later turn. Mine to
fix, and it worsens the longer a conversation runs.

**2. Hermes has zero Athena tools.**

The API server hands both personas the stock 26-tool default set: web, file,
terminal, code, browser, video, image, memory, skills, todo, sessions, cron,
delegation. No mission-state access at all.

Meanwhile `src/athena/hermes_tools.py` already implements **13 Athena tools**:

```
athena_count_units_tool          athena_get_current_snapshot_tool
athena_query_units_tool          athena_get_unit_tool
athena_get_health_tool           athena_get_recent_events_tool
athena_get_alerts_tool           athena_get_raw_evidence_tool
athena_group_summary_tool        athena_group_movement_tool
athena_mission_intel_tool        athena_mission_director_status_tool
athena_get_pending_cues_tool
```

They have never been exposed. `state=unavailable` was Hermes answering
honestly from general knowledge and correctly flagging that it had no mission
data — the contract worked, it just had nothing to report.

**3. There is no mission-state store.**

```
athena status --json
  → "code": "store_path_missing"
  → snapshot_count: 0
```

`ATHENA_MISSION_STATE_DB_PATH` is unset and no database exists anywhere.

### What already exists and must be reused, not rebuilt

- `scripts/hermes_tool_live_test.py` performs the **complete ingest**: live
  `StreamUnits` → `mission_state_snapshot_from_stream_units` → 
  `MissionStateSQLiteStore.persist_snapshot`, then exercises every tool.
- `python -m athena <cmd> --json` emits a clean, versioned envelope
  (`athena_tool_api.v1`) with `data`, `errors`, `evidence`, `limits`.
- DCS-gRPC is **reachable now** at `10.90.10.78:50051`.

**Do not write a new ingestor.** The path exists and is tested.

---

## Stage 1 — Fix the instruction leak

### 1.1 The defect

`APIServerClient.Exchange` sends `Instructions` with `Store: true`. Anything in
`instructions` becomes part of the persisted conversation.

### 1.2 The fix

Move the reply-shape contract from `instructions` into the per-turn `input`.
The input is the turn's content; instructions are session-level. Radio framing
is per-turn, so it belongs in the input.

- File: `pkg/athena/bridge/apiserver.go`
- `buildInput` gains the reply-shape block; `Instructions` is dropped from the
  request struct entirely rather than left set-but-empty, so the leak cannot
  regress silently.

### 1.3 Tests

- Assert the outgoing request carries **no** `instructions` field.
- Assert `input` contains both the transcript and the reply-shape contract.
- Regression: two sequential exchanges on the same conversation, second one
  asserting the instruction is present in its own input rather than assumed
  inherited.

---

## Stage 2 — Stand up the mission-state store

### 2.1 Choose the path

`ATHENA_MISSION_STATE_DB_PATH=/var/lib/athena/mission-state.db`

Under `/var/lib` rather than the repo: it is runtime state, not source, and it
must be readable by whatever Hermes runs as. Directory created with ownership
matching the account that will write it.

### 2.2 Seed from live DCS

Reuse `scripts/hermes_tool_live_test.py`'s ingest. Run it once against the live
server to prove the whole path, then confirm:

```
athena status --json          → snapshot_count > 0, source_status ok
athena ownship-status --json  → resolves Prometheus
```

**This is a live DCS read.** It is read-only — `StreamUnits` is a telemetry
subscription — and the project's safety gates permit read-only probes. No
mutation, no command execution.

### 2.3 Keep it fresh

A single snapshot goes stale immediately. Options, in order of preference:

1. **Periodic refresh** — a small systemd timer or the existing mission
   director loop, writing a snapshot every few seconds while DCS is up.
2. **On-demand** — the tool wrapper refreshes before answering.

Prefer (1): it keeps latency out of the radio path, and freshness metadata
already flows through the envelope so a stale answer is *labelled*, not
silently wrong. Start with a conservative interval; this is telemetry, not a
tight loop.

---

## Stage 3 — Expose Athena's tools to Hermes

### 3.1 Mechanism: a skill wrapping the CLI

Rejected alternatives and why:

- **MCP server** — the project AGENTS.md explicitly says *"Do not introduce MCP
  as the first integration path for the MVP."*
- **Native Hermes tool** — every core tool ships on every API call, and the
  Hermes contributor guide sets a high bar for that. Thirteen new tools would
  add materially to the 38k-token floor already measured.
- **Skill + CLI** — the guide's preferred rung: *"CLI command + skill"*. The
  CLI already emits a versioned JSON envelope with provenance and freshness.

A skill costs nothing until loaded, and only the `athena` persona needs it.

### 3.2 Skill design

`~/.hermes/skills/gaming/athena-mission-state/SKILL.md`

Must contain:

- **Trigger**: when the pilot asks about the battlefield, ownship, threats,
  units, or navigation.
- **Exact commands** for each capability, with `--json` mandatory.
- **Envelope shape** so the agent reads `data`/`errors`/`evidence` correctly
  rather than guessing at prose.
- **Freshness discipline**: if `errors` is non-empty or `source_status` is not
  ok, say so on the radio. A stale answer must be qualified, never presented as
  current. This maps directly onto the bridge's `state` field.
- **The anti-pattern**: do not count units by reading a table into the model —
  use `athena_count_units_tool` via `count-units`. This is named explicitly in
  the project guide.
- **Relative navigation**: the new `solve_navigation` is importable but has no
  CLI subcommand yet (see 3.4).

### 3.3 Scope it to the Athena persona

Radio answers should be scoped; the dev persona does not need mission tools and
the mission persona does not need browser automation.

Investigate `hermes skills config` for per-platform enablement. If per-
conversation scoping is not supported by the API server, document the
limitation rather than pretending it is enforced — an unenforced boundary
recorded as enforced is worse than none.

### 3.4 A CLI subcommand for relative navigation

`solve_navigation` landed in `metis` with 44 tests but is unreachable from the
CLI, so the skill cannot use it.

Add `python -m athena navigate --json` taking a destination and reading ownship
position/heading from the store. Same envelope as every other subcommand.

**Destination resolution order agreed earlier**: mission waypoint first, ICAO
second, free-form deferred.

---

## Files likely to change

| Path | Change |
|---|---|
| `athena-audio-gateway: pkg/athena/bridge/apiserver.go` | move reply-shape into input; drop `Instructions` |
| `athena-audio-gateway: pkg/athena/bridge/apiserver_test.go` | leak regression tests |
| `project-athena: src/athena/__main__.py` | `navigate` subcommand |
| `project-athena: src/athena/mission_state_cli.py` | navigation envelope builder |
| `project-athena: tests/unit/test_relative_navigation_cli.py` | **new** |
| `project-athena: scripts/` | ingest runner wrapper if the live test needs one |
| `~/.hermes/skills/gaming/athena-mission-state/SKILL.md` | **new** |
| `/etc/athena-gateway/` or systemd | `ATHENA_MISSION_STATE_DB_PATH`, refresh timer |
| `athena-audio-gateway: docs/PITFALLS.md` | the instruction-leak trap |

---

## Tests and validation

**Gateway** (baseline 1444, `-race`):
- No `instructions` on the wire; reply shape present in `input`
- Sequential-turn regression

**Athena** (baseline 1460 passed, 49 skipped):
- Navigation CLI: valid destination, unknown destination, no ownship in store,
  empty store — each returning the standard envelope with populated `errors`
  rather than raising

**Live, in order:**
1. `athena status --json` → `snapshot_count > 0`
2. `athena ownship-status --json` → resolves Prometheus
3. `athena navigate --json` → a real bearing and distance
4. Radio: *"Athena, what am I flying?"* → **`state=ok`**, correct airframe

Gates run **unpiped with the exit code captured** — a piped `tail` masked 21
lint failures earlier in this project:

```
make lint && make vet && make test && make skyeye && git diff --check
PYTHONPATH=src pytest tests/unit -q
```

Then verify against the **built binary**. Three real bugs in this project
passed unit tests and were caught only by running it.

---

## Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| Live DCS read during ingest | `StreamUnits` is read-only telemetry; permitted under the project's read-only gate. No mutation, no command execution |
| Stale snapshots answered as current | Freshness already flows through the envelope; the skill must map it onto the bridge `state` field and say so aloud |
| Tool exposure inflates the 38k token floor | Skill, not core tools — costs nothing until loaded |
| MCP creep | Explicitly rejected per project AGENTS.md |
| Per-persona tool scoping may not be enforceable | Investigate; if unsupported, **document the limitation rather than claiming it** |
| Store path permissions | `/var/lib/athena`, ownership set to the writing account, verified by a real read after a real write |
| Scope creep at the end of a long session | Three stages, nothing else. Navigation CLI is included only because the skill cannot use `solve_navigation` without it |

---

## Order

1. **Stage 1** — instruction leak. Small, self-contained, fixes a defect I
   introduced.
2. **Stage 2** — store path, live seed, verify `status` and `ownship-status`.
3. **Stage 3.4** — navigation CLI subcommand (the skill depends on it).
4. **Stage 3.1–3.3** — the skill, scoped to the Athena persona.
5. **Live radio verification** — *"Athena, what am I flying?"* must return
   `state=ok` with the real airframe.

Stage 2 depends on DCS being up. Stages 1 and 3.4 do not and can proceed
regardless.

---

## Open question, resolved by default

**Refresh cadence** is not yet chosen. Default to a conservative periodic
snapshot and record the actual interval once measured, rather than guessing a
number and enshrining it — the same discipline applied to the latency budget
earlier in this project.
