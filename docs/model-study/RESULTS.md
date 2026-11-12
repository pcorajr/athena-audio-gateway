# Athena model accuracy study (2026-11-11) - results

**Short answer for the owner.** Most of the wrong model answers on the 11-11 flight were caused by Athena's own
chat memory, not by missing data: before you spawned, Athena told you "tracking disconnected, you're not in an
aircraft", and that sentence stayed in the conversation it re-reads every turn. Later the model believed it and said
"fuel unknown" / "no connected picture" even though the live data had the answer. Replayed 5 times each, those
flight mistakes came back 2 out of 3 times (10 right of 30). With the old chat memory removed, the same questions
were answered right 10 of 12 times (all fuel/friendlies answers right; only "cleared to land" stayed wrong).
**Do these three things, in this order (all cheap, none adds delay):**
1. Clean the chat memory: forget it when you spawn or the connection comes up, and never keep an "unknown /
   not connected / not in an aircraft" answer in it.
2. Put seven short "answer from the data" rules at the top of the existing instructions (look again before saying
   unknown; where fuel, airfields and traffic are; no clearances, runways or winds; no internal names).
3. Add an automatic check after each model answer: if it says "unknown" while the data has the value, says
   "cleared to land", or speaks a number that is not in the data, Athena says "say again" instead.
Not worth it: higher reasoning effort (slower and not better); the "ready-made facts only" idea (its own gaps
caused new wrong answers). Numbers below come from 399 real model calls on 72 real and written radio questions.

## Results (same 72 questions each time unless noted; one run per variant)

| variant | accuracy | false "unknown" | inventions | median s | p95 s | calls used |
|---|---|---|---|---|---|---|
| (a) baseline: today's prompt + full picture | 64/72 = 89% | 5 | 1 | 1.76 | 3.62 | 97 (72 + 24 repeats + 1 backup) |
| (c) baseline + mechanical reply check | 63/72 = 88% right, wrong 8 -> 4, 5 "say again" | 2 | 0 | 1.76 | 3.62 | 0 (applied to (a)) |
| (b) slim picture (sections by question family) | 66/72 = 92% | 3 | 1 | 1.60 | 2.56 | 74 |
| (d) answer from ready-made fact lines only | 63/72 = 88% | 5 | 0 | 1.66 | 2.33 | 72 |
| (e) prompt diet (2,530 chars, grounding rules first) | 68/72 = 94% | 0 | 0 | 1.66 | 3.40 | 72 |
| (extra) baseline without chat history, 6 flight failures x2 | 10/12 (baseline same items 10/30) | 0 | 1 | 2.30 | 3.34 | 12 |
| (f) effort medium, first 34 items | 25/34 = 74% (baseline same 34: 26) | 4 | 2 | 2.16 | 5.19 | 35 |
| (f) backup gpt-5.4-mini, first 36 items | 33/36 = 92% (baseline same 36: 28) | 0 | 1 | 0.93 | 1.19 | 36 |

Calls: 399 of 400 (one extra smoke call). "Right" = every mechanical check passed (required facts present, no
contradiction, no false "unknown", no clearance/runway/wind/key name, every spoken number exists in the picture);
34 replies with no mechanical answer were read by hand (`manual-reads.json`). Latency = model time per call.

**Repeat test (baseline, 5 runs each).** 18:30:05 friendlies 1/5 right; 18:32:05 fuel 1/5; 18:32:22 fuel 1/5;
18:59:55 Nellis 4/5; 19:00:12 nearest base 3/5; 19:01:46 landing 0/5 (Luna also says "cleared to land" - the
prompt tells it to). Every variant on these six (first run): baseline 2/6, slim 3/6, facts 4/6, diet 6/6,
no-history 5/6 (both runs 10/12), mini 5/6, medium 2/6.

**How sure is this.** One run per variant and the model is not repeatable (above: the same question flips), so a
difference of 3 or fewer answers out of 72 is noise. The diet gain is entirely on the six flight failures (its
grounding lines name those fields): on the other 66 questions baseline and diet both got 62 right; slim 63, facts 59.
The chat-memory result is the strongest: on the three questions whose memory held the stale "disconnected / not in
an aircraft" line, baseline was right 3/15 times, without memory 6/6.

## What was measured, and what each change would cost

- **Picture size.** Full picture median 26,603 characters (max 57,692); slim 3,891 (-85%); fact lines 308.
  Whole request median: baseline 36,800 chars, slim 14,900, facts 11,200, diet 29,200. Prompt 10,183 -> diet 2,530.
- **(a) Baseline.** Wrong answers: fuel unknown x2, "no friendly aircraft showing", "cleared to land", "can't confirm"
  the Maverick (it had killed a PT-76), "no SAM in range" while two early-warning radars were listed, two
  friendly-count answers that ignored the traffic list.
- **(b) Slim picture.** Code picks picture sections by keyword families (table below). Small gain (+2, within noise),
  p95 latency 3.6 -> 2.6 s. Cost: ~60 lines, one keyword-to-section table that must grow with every new picture
  field and question type; a wrong guess hides the answer (follow-ups like "Really? I'm looking at one" lost the
  airfield section).
- **(c) Reply check.** Across all 394 model replies of the study it caught 30 of 57 wrong answers and wrongly
  rejected 5 of 337 right ones (1.5%), turning them into "say again". On baseline: caught 4 of 8 (both fuel, the
  friendlies, the clearance), missed 4 (wrong counts, the missile, the EWRs), 1 false reject (a correct
  "no friendly airfield showing" for a question that also said "friendly"). Cost: ~80 lines + tests, no delay,
  no second call; upkeep = the forbidden-word list and the question-topic -> picture-field list.
- **(d) Facts only.** Code writes 3-10 sentences per question from the picture (reusing its `call` lines); the model
  may use nothing else. Fastest p95 (2.3 s), but it is a second copy of the picture logic and every gap became a wrong
  answer: AWACS not found (it lives in air_traffic, not friendly_groups), friendly ground forces not offered, mission
  status without fuel ("fuel unavailable"), follow-up questions with no keyword, fuel of older pictures (fixed in the
  tool after the run, not re-measured: 1 item). Highest build and maintenance cost.
- **(e) Prompt diet.** Same instructions cut to a quarter, grounding first. Best measured result and 0 false
  "unknown", but it drops many detailed rules (debrief, RWR, ATC wording) that this eval barely covers - replacing
  the prompt is not provable without a flight. Cheaper and safer: put its GROUNDING block at the top of today's prompt.
- **(f) Effort medium:** worse (74% vs 76% same items) and slower (p95 5.2 s, next to the 6 s Codex cut-off).
  **gpt-5.4-mini:** better on its 36 (33 vs 28) and twice as fast, but one run only and it still said
  "cleared to land"; worth a repeat test as the primary model, not a switch on this evidence.
- **History removed entirely** fixes the stale-memory failures but would break real follow-ups ("What's the name of
  the airfield?"). The recommended targeted cleaning (reset on spawn / connection up, never keep "unknown" replies)
  was NOT measured - it needs a replay test from the 11-11 transcript when it is built.

## Ranked recommendation (most accuracy for least complexity and delay)

1. **Clean the chat history** (SimpleController: clear `history` when ownship appears or `connected` turns true;
   do not append a reply that claims missing data). Evidence: 3/15 -> 6/6 on poisoned questions. A few lines, 0 s.
2. **Grounding block first in the existing PROMPT** (the 7 GROUNDING lines of `DIET_PROMPT`), and delete the
   prompt's own "approve (cleared for takeoff, cleared to land)" instruction, which is where the invented clearance
   comes from (Luna said it 5/5). Evidence: diet 0 false unknowns, 6/6 flight failures. Text only, 0 s.
3. **Mechanical reply check -> "say again"** as the safety net for what 1 and 2 miss. Evidence: 30/57 wrong caught,
   1.5% false rejects. ~80 lines, 0 s, no second model call.

Not provable without a flight: real speech-recognition text and history patterns; the targeted history cleaning
itself; how many "say again" the check produces per sortie; whether the shortened/reordered prompt keeps the
debrief, RWR and ATC behaviour; Luna latency while the subscription is busy; whether the data_health "down 2306s"
wording contributes (not isolated here - the slim and facts variants only pass a stream caveat to the families that
need it).

## What we can reuse from the old code

The retired controller (`legacy/integrations/hermes_radio_legacy/`) gave the model nine narrow read tools and turned
results into fixed, caveat-preserving sentences. **Carries over (as ideas, now used by variants b and d):** the
catalogue of narrow reads as the list of question families; per-answer caveats limited to the data that answer uses
(`facts.py _caveats`: stale, cached, unhealthy component) - here a down helicopter stream is mentioned only for air
questions; "not found does not prove destroyed" (get_unit); "nearest returned is not nearest known"; deterministic
sentences (today the Eye already writes them as `call` lines); the old GUARD rules "if insufficient evidence say
unable", "historical dialogue is not current evidence" (exactly the 11-11 failure) and "at most two sentences".
**Cannot carry over (code):** every read is wired to the retired State Service socket client (`StateServiceClient`,
frames, schema validation, generation/scope checks) that no longer exists; the tool-calling loop (up to four model
rounds per reply) breaks the one-call rule and would add seconds; the fixed DCS type vocabulary and true-bearing
geometry are replaced by the Eye's categories and magnetic `call` lines; `prompt_builder.py` targets an old local
model (qwen3:8b) with an FL/HDG contact list.

| old read (retired) | what it answered | picture keys that supply it today | family in this study |
|---|---|---|---|
| ownship | type, heading, speed, identity | `ownship` (type, alt_ft, speed_kt, heading_mag, fuel, loadout, damage), `cockpit` | ownship |
| query_units | bounded page of units by side/category/radius | `picture_top3`, `air_picture_empty_call`, `picture_long_range_top3` (air); `ground_targets_within_20nm`, `ground_empty_call` (ground); `air_traffic` (friendly air) | hostile_air, hostile_ground, friendly_units |
| get_unit | refresh one named unit; not found is not destroyed | `hostile_groups` / `friendly_groups` rows by type, `threats.sam_in_range`, `my_kills_this_mission` | named-unit lookup |
| count_units | totals by coalition/type | `alive_totals_by_coalition`, `hostile_groups_total`, `kill_tallies` | count_units |
| group_summary | groups with caveats | `hostile_groups` / `friendly_groups` (count, types, call) | inside the unit families |
| current_summary | totals + sections | `my_kills_this_mission`, `alive_totals_by_coalition`, `picture_top3`, `threats`, `ownship.fuel` | current_summary |
| mission_intel | mission identity (airbases were null) | `theatre`, `airbases.nearest_friendly` / `nearest` (+ traffic), `memory.takeoff_airfield` | mission_intel |
| recent_events | latest events | `recent_events_last_10_min`, `recent_kills_and_losses_last_10_min`, `my_shots_last_15_min` | recent_events, kills |
| braa | bearing/range/altitude to a unit | every row's ready `call` line (bearing, range, altitude, clock) | all families |
| facts.py caveats | stale / cached / unhealthy qualifiers | `connected`, `data_age_s`, `data_health.streams` (only the stream the family needs), `data_health.combat_events` | caveat lines |

## Files (branch `study/model-accuracy`, worktree `~/Ai-Workspace/worktrees/athena-audio-gateway-model-study`)

- `integrations/hermes_radio/tools/model_eval.py` - extract / build / run / score / table; the runner drives the
  unchanged `SimpleController._answer` with the real `CodexLLM` (gpt-6-luna, effort low, 6 s, 10 s exchange deadline,
  gpt-5.4-mini fallback); variants only rewrite messages at the model boundary; hard 400-call budget file.
- `integrations/hermes_radio/tools/model_eval_set.py` - 33 real model-route asks + 39 written asks with ground truth.
- `docs/model-study/eval-data.json.gz` (recorded pictures + histories), `results.jsonl` (every call, reply, latency),
  `manual-reads.json`, `README.md`; `integrations/hermes_radio/tests/test_model_eval.py` (offline, 5 tests).
- Of 282 model-route exchanges on 11-08/09/11, 191 still reach the model today (88 now fixed, 3 debrief); 33 were
  picked (all real questions with checkable answers + the six failures + some chit-chat), the rest are acks.
