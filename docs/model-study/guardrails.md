# Model guardrails — review rework, branch only

## Status

This rework supersedes the generic checker and the conclusions in the earlier guardrails report.
It is built and tested on `feature/model-guardrails`, compared with `ee138de`; it is **not deployed**.
No adapter requests, DCS operations, service restarts, releases, merges or pushes were made.
Independent re-review and an actual flight remain acceptance gates. **No model-accuracy improvement is demonstrated.**

## What the live code does

- **No generic name or number validation remains.** Capitalized words, aircraft aliases, locations, bearings,
  ranges, weapon counts and arbitrary numeric tokens are not checked against a bag of picture values.
  The old bag-of-numbers helper survives only in the offline study scorer, not the controller.
- **Missing-data contradictions:** `reply_guardrails.py` examines model sentences, with separate negative clauses,
  against the stated subject: ownship fuel, available loadout, ownship/position/type, connection,
  nearest friendly field or friendly air traffic. Fuel presence is checked for the stated units when supplied.
  A different absent radar/cockpit feed cannot excuse a false fuel denial. An unqualified “I don't know” uses
  the question's subject. Named tanker/AWACS absence is not contradicted by unrelated friendly traffic.
  Contradicted sentences are replaced with the existing fixed subject wording when available, otherwise dropped.
  Other sentences survive. If nothing remains, the route is `fallback`, saying “say again”, with reasons logged.
- **Explicit false all-clears:** supported no-SAM/in-range, no-tracking, no-missile, no-bandit/hostile/air-contact,
  no-ground-target and general “all clear”/“you're clear”/“no threats” forms are tested against their corresponding
  collections. Air contacts use the stated numeric tier, default 50 nm (long range 150); ground targets use
  `ground_targets_within_20nm`. SAM range negatives use `sam_in_range`; an early-warning radar alone is not a SAM.
  Radar tracking uses `sam_radar.tracking_you` and DCS-radar tracking confidence, not mere range inference.
  Inbound missiles use `missiles_inbound`, not the pilot's own weapon track. Corrections use complete Eye `call`
  strings; a tracking row without geometry has the factual fallback “Radar tracking you.” These replacements
  are not replaced by “say again” alone while a matching threat call is available.
- **Kill counts only:** explicit numeric counts before “kills” and explicit no-kill claims are compared with
  the pilot's kill list, not unrelated picture numbers. “New” uses the controller's delivered read-back ledger;
  mission totals use the complete pilot list. Recent `by_me` killed rows provide an absence-check fallback when
  the cumulative list is empty. Existing fixed kill sentences supply the correction, retaining likely/confirmed
  wording. No guessed dead units or inferred counts are invented by this checker.
- **Controller-owned announcements:** on model routes (including model failure or a request too old to call the
  model), the controller puts a close-bandit call first, then pending SAM/launch and splash calls, before prose.
  Existing `_alert_rows` count-merging is reused. Exact model echoes of inserted sentences are removed.
  Whole calls reserve room first; a deferred model answer includes “Ask again for the rest.” or the existing
  subject-specific deferral. Unsqueezable announcements stay pending, never half-spoken or marked as delivered.
  Fixed-route assembly is unchanged. Marks and kill acknowledgements commit only for wording actually included;
  a failed/expired/unsent delivery re-arms them on the next exchange.
- **Dialogue and repeats:** Eye/controller retain the last non-null pilot identity. Same unit → null → same
  unit does not clear history; cockpit id -1 ↔ real gRPC id for the same name/type is a feed change, not a spawn.
  A real new unit id, name or type still clears; the remembered real id survives fallback. First session and
  disconnected→connected transitions clear dialogue, not kill/launch state. Existing mission reset is unchanged.
  History keeps up to eight complete question/answer pairs, using final spoken sentences after correction and
  budgeting. Stale unknown/unavailable/not-connected/no-position/not-in-an-aircraft/don't-know/say-again/deferral
  sentences are excluded without discarding a factual sentence next to them. Valid current “No kills showing”
  and “No traffic” remain. This is an availability filter, **not proof that every retained model claim is true**.
  Last-delivered speech is separate: “say again”, “repeat”, “say that again” and “repeat last” use a fixed route
  to repeat it; a say-again fallback or delivery older than two minutes re-answers its original question.
  Failed delivery is not the repeat target. As before, absent gateway outcome records mean assumed delivery.
- **Existing guards remain:** clearance-family sentence removal, spoken-key rejection, 280-character speech cap,
  and Test-pilot marking. The seven grounding rules lead the prompt; the contradictory “your discretion”
  instruction is gone. Measured prompt length: **9,686 characters**, below the 10,124-character baseline.
- **Eval provenance:** cached rechecks require SHA-256 equality of the complete ordered messages, including
  prompt, picture, user request and history. Equal-length but different messages fail.

## What it does not guarantee

This is a narrow contradiction filter, not a semantic fact-checker. It does not validate arbitrary names,
locations, clock positions, vectors, weapon codes, non-kill counts, mission summaries or pilot-report attribution.
It does not recognize every paraphrase. “You are clear of the SA-2” and directional ahead/behind negatives are
left alone because these rules cannot establish their scoped truth. A no-data statement about a particular
weapon track is not refuted by an unrelated inbound missile. A correct airborne count cannot be inferred from
alive category totals alone. Named-role/type absence and fine-grained equipment status are not generally checked.
A kill tied in prose to a specific secondary explosion can still be attribution-ambiguous; correction supplies
recorded kills, not proof of that explosion's cause. No-position geometry claims and invented dead-unit assertions
outside the listed negatives/count forms are not mechanically validated. Speech can still be incomplete or wrong.
The existing raw-key guard does not reliably catch spoken internal weapon/pod codes.

The guarantee is that **recognized contradictions are revised sentence by sentence**, with authoritative
controller-owned calls protected from unrelated bad prose; not that every dangerous statement is recognized.

## Verification

Both runners used the project virtualenv, `umask 077`, private `TMPDIR=~/.p1gr`, and project-src/worktree PYTHONPATH.

- pytest: **621 passed, 6 skipped, 2,059 subtests**.
- unittest: **624 tests, OK, 6 skipped**. Existing ResourceWarnings remain; no failing tests.
- The recorded 11-11 line-1890 SAM and line-1834 splash survive “Stay sharp.” and unrelated clearance prose.
  Real w03 tests reject false no-SAM/no-threats/clear replies and “Splash nine kills”, preserving good sentences.
  Fuel-present/absent paraphrases, scoped negatives, null/pseudo identity flaps, real respawns, final history,
  repeat-last, expired/failed delivery, cache hashes, whole-call budgeting and re-arming are covered.
  ClearanceGuardTests/AtcTests again assert threat survival with an actual threat in their fixtures.
- **319 real exchanges replayed with a stub against ee138de:** all **149 fixed replies unchanged**.
  **23 model replies** gain controller-owned Eye announcements, with unchanged `MODEL.` content and no route change.
  Every inserted whole call was checked against the picture. The exhaustive table is in `guardrails-review-audit.md`.
- **2 preflight + 37 flight exchanges:** all **25 fixed replies unchanged**; only model exchange 20261111:1630
  gains its pending SAM sentence. Maximum replay lengths: 276 (all) / 246 (flight).
- The initially differing fixed splash at 20261108:303 was a **replay-key collision**, not live suppression.
  The harness used splash wording as identity; a previous model-route delivery consumed that same synthetic key.
  Transcript snapshots omit private keys, so the replay now uses transmission id + field + index when keys are
  absent, preserving supplied real keys. Eye uses target kind/id/time, not splash wording. A regression creates
  two real Eye kill records with identical wording and distinct keys, then proves both are spoken in succession.
  Stripped historical keys cannot establish event-by-event live deduplication; this snapshot replay is not such proof.
  Also, the historical transcripts predate ee138de: 89 baseline fixed outputs differ from original recorded speech,
  including newly fixed routes. The acceptance comparison here is candidate versus **ee138de**, not historical audio.

## All stored model replies and checker cost

The stateless sweep uses cleaning and clearance removal before the narrow checker; it deliberately tests all saved
model outputs even when today's controller would take a fixed route. Rejections are not new model calls.
`guardrails-review-audit.md` lists **every** removed/replaced sentence, replacement and manual judgment.

| Corpus | Replies | Narrowly changed replies | Incremental after existing key guard | Identified false rejections |
|---|---:|---:|---:|---:|
| Recorded 11-08 / 11-09 / 11-11 | 284 | 8 | 6 | 0 |
| Study results, all variants/repetitions | 394 | 25 | 25 | 0 |

Two recorded “No position call” removals were already spoken-key violations, not new safety catches. The 6 other
recorded corrections include all **five required 11-11 failures**: no connected picture, fuel unknown twice,
no friendly airfield twice; plus the 11-09:535 false new-kill denial. The study's secondary-explosion denial
(11-09:409) is a valid no-new-kill correction under its rubric, but its causal qualifier is ambiguous as noted above;
zero identified false rejects is a manual sample judgment, not a universal claim. Stateless “new” checks use recent
kill ages and cannot reconstruct all historic delivery bookkeeping; their replacement is a mission tally.
Existing clearance removals (5 recorded, 11 study) are not counted as new-check successes.

“F-4E Phantom”, “Ural three seven fives”, the correct eighteen-aircraft total and “Running in, copy” all pass.
Two overbroad development matches (a missing tanker with unrelated traffic, and a missing own-missile track with
an inbound missile present) were narrowed and covered by regression tests before final verification.

Final-code checker-only timing over three passes per saved reply (not network latency):
- 284 recorded: median **0.026 ms**, p95 **0.093 ms**, maximum **0.624 ms**.
- 394 study: median **0.036 ms**, p95 **0.087 ms**, maximum **0.189 ms**.
All 678 outputs were also injected through the normal controller with stub model text: maximum final speech
**280 chars recorded / 278 study**, never over 280. Some requests now route fixed; this cap exercise is distinct
from the direct checker sweep.

## Fresh real-model results — 56 of at most 80 calls

The reviewer's exact 25-item Appendix C selection was generated once on each path, interleaved old/new.
Each item reached the normal model route in a dry run; no fixed route was bypassed. Both paths also ran the
poisoned-history trio after the actual line-1624 preflight reply and a first-session change without a mission reset:
“anybody blue up here with me”, “how's the gas holding up”, “is there somewhere friendly I can put down”.
Preflight replies were fixed and cost no calls. A locked budget counted HTTP attempts before sending.
All **56** attempts succeeded without extra fallback attempts; no live adapter or DCS endpoint was contacted.

| Sample | Audited right | Wrong/incomplete | Narrow rejections | Median / p95 model seconds |
|---|---:|---:|---:|---:|
| ee138de fresh 25 | 21 | 4 | 0 | 1.781 / 3.333 |
| Candidate fresh 25 | 20 | 5 | 1 | 1.782 / 4.949 |
| ee138de poisoned-history trio | 3 | 0 | 0 | 2.632 / 4.685 |
| Candidate poisoned-history trio | 3 | 0 | 0 | 1.788 / 2.041 |

Original scorer outputs remain separate: old 19 right / 2 wrong / 4 manual; candidate 20 / 1 / 4.
The stricter audit rejects incomplete friendly counts, omitted EWR calls and incomplete mission-status answers;
old also invented gun hits on 11-09:147, while new correctly reported pending shots. Candidate additionally
invented “eighteen already handled” and spoke raw HB pod codes. The candidate's one mechanical correction
removed the false no-new-kill sentence at 11-09:409 while keeping the factual next sentence.
These unfiltered mistakes are outside the narrow guarantees and are not hidden by the historical scorer.

The candidate sent no stale disconnected preflight assistant history after the first-session transition;
the old path did. Both still answered the trio correctly. **No accuracy improvement is shown**: this single
paired sample is noisy, and its audited candidate score is slightly lower. No flight-quality claim follows.
After the final mechanical-only corrections, all **28 candidate generations** were cached-rechecked with exact
ordered-message hashes; final spoken outputs were unchanged. These are rechecks, not extra fresh calls.
Maximum speech across the fresh trials and both stub replays was 276 characters.

## Evidence and remaining limits

- `guardrails-review-audit.md`: exhaustive sentence revisions, all 23 model-route replay differences, all fresh
  replies/latencies and original/audited judgments.
- `guardrails-review-evidence.json.gz`: raw fresh replies and metadata, ordered-message hashes, rechecks, both
  old/new replay sets, sweep changes, summary and baseline-versus-recorded mismatches.
- `guardrails-review-verification.json`: separate 678-output cap, checker-cost and static-scan results.
- Private scripts/logs: Athena `~/.p1gr/`; local continuation evidence `athena-p1/rework2/`.
  Earlier `guardrails-*` study artifacts describe the superseded generic-check implementation and are not
  evidence that this narrower implementation validates arbitrary names/numbers.

Not proven: actual SRS delivery/timing, real speech recognition, event-level identity/reconnection behavior in
missing historical logs, full live deduplication from stripped snapshots, or statistically meaningful accuracy.
No deployment is authorized by this report; independent re-review and a real sortie are still needed.
