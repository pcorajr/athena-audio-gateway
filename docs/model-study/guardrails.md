# Model guardrails — built, not deployed

## Outcome

Worktree `athena-audio-gateway-guardrails`, branch `feature/model-guardrails`, based on `ee138de` (r12.7).
No live adapter calls, DCS/SRS operations, restart, release, merge or push. Independent review and a flight remain gates.

The safeguards are built and exercised, but this sample **does not establish an accuracy improvement**.
The historical study's baseline is 64/72; a fresh ee138de run scored 63/72; the new path scored 65/72 with the same
mechanical grader and explicitly resolved manual items. Several grader regexes mistake true negative statements
for false unknowns. With those documented false alarms corrected, the fresh comparison is 65/72 versus 68/72.
A difference this small in a non-repeatable model is not strong evidence of an improvement.

| Path / scoring | Right | False unknown | Inventions* | Say again | Model median / p95 seconds |
|---|---:|---:|---:|---:|---:|
| Historical study baseline (provided) | 64/72 | 5 | 1 | 0 | 1.76 / 3.62 |
| Fresh ee138de, unchanged study grader | 63/72 | 3 | 1 | 1 | 1.681 / 2.705 |
| Guardrails, unchanged study grader | 65/72 | 3 | 0 | 0 | 1.619 / 2.995 |
| Fresh ee138de, audited grader false alarms | 65/72 | 1 | 1 | 1 | same |
| Guardrails, audited grader false alarms | 68/72 | 1 | 0 | 0 | same |

\* As in the study, inventions means unsupported tokens/numbers/clearances/keys, not every incorrect use of an
otherwise present number. The remaining four wrong answers are two friendly-aircraft counts, the ambiguous
"Really, because I'm looking at one right now" follow-up, and a missile-status false unknown. The checker is a
mechanical filter, not a proof that each fact belongs to the right unit, question or time. It is not a general
semantic/entity recognizer; arbitrary nicknames and paraphrases remain a limitation.

## Why the preflight history survived

The adapter restarted around 18:10 on November 11. At 18:10:55 it answered the test picture ask with
"Tracking disconnected; last known picture. I don't have your position; you're not showing in an aircraft."
Transcript tool/reply lines 1624–1625 show connected=false, ownship=null and that
fixed reply. The 18:26:02 log shows the event stream reconnecting; the first messages are simulation_fps, not
mission_start. Birth and player_enter_unit follow at 18:27:12. The first airborne picture (line 1630) is connected
and contains the F-4E, fuel and position.

`GrpcFeeder._check_session` resets the Eye only when an **already known** session ID changes. Its first None→ID
transition only invalidates optional Memory, not Eye.reset_at. Same-session stream recovery intentionally does
not reset the Eye. Birth/enter events also did not reset the controller. The controller watched only reset_at,
so none of these events cleared its preflight dialogue. Eight pairs is a rolling limit, not infinite history:
the first stale reply entered the flight, and subsequent missing-data replies could keep polluting the window.
Evidence is the read-only adapter log and transcript, plus the unchanged first-session conditional in eye.py.

## Changes

1. **Clean dialogue, not combat bookkeeping.** Existing mission resets still clear everything they did before.
   First session, a disconnected→connected transition, spawning and changing unit clear only chat history.
   Eye.chat_generation records reconnections and unit identity changes even between radio calls; no new service,
   persisted state or picture schema. The fake-Eye path also compares stable unit fields and connection/session.
   Missing-data/link/unknown/say-again/deferred replies are not stored, and their pilot question is dropped too.
   Factual FIXED answers still enter the same **eight complete-pair** history, including nearest-airfield answers.
2. **Grounding first.** The seven study grounding lines are verbatim at the top. Overlapping lower instructions
   were removed/combined, retaining mission/debrief, RWR, kill confidence, positional and close-bandit semantics.
   Actual ee138de prompt **10,124 → 9,788 characters**, 35 bullets. The study's older 10,183-character count predates
   the r12.7 prompt changes; it is not the length of ee138de.
3. **Mechanical reply check.** Numbers retain the study rounding/2% tolerance; small counts are no longer all
   exempt. Spoken/digit bearings, type abbreviations, plural/spoken designators and NATO suffixes are normalized.
   Type names and proper names are compared to picture text and its existing spoken-name aliases. False-unknown
   checks distinguish friendly airfields from friendly traffic, and absent Mavericks from an available loadout.
   Existing key/clearance guards are reused (no second model request). Rejection is `fallback` with its reason in
   reply/transcript metadata: existing matching fixed wording when available, otherwise "say again". Aircraft
   fallback reuses the aircraft-only lead of the existing loadout sentence. Existing clearance removal still
   strips forbidden sentences, rather than discarding an otherwise useful answer.

The original study's five false rejections all pass now (four "no friendly airfield" cases and one absent-Maverick
case). Against its same 394 labelled replies, the combined guards flag **37/57 wrong, 2/337 labelled right**,
versus 30/57 and 5/337. Both residual nominal false rejects are explained in guardrails-summary.json: one says no
live fuel picture despite connected=true and fuel present (a study-grader miss); the other says "your discretion",
which r12.7's pre-existing clearance guard forbids. We did not weaken that existing safety rule. These are flag
counts, not all whole-reply drops: the clearance guard can preserve the other sentence.

There is no extra model call or network wait. Local checking is not literally free: measured over 275 checks,
**5.687 ms median / 11.192 ms p95** (12.069 ms max). Model-time latency includes the existing Codex/API fallback;
55 of the 72 questions reach the model on r12.7, and 17 are fixed. Primary and backup settings are unchanged.

## Real flight and repeats

`guardrails-flight.md` contains every old/new reply for the two preflight exchanges and all **37 flight exchanges**,
including the explicit first-session/spawn transition and the 18:35:00/18:35:20 airfield/name follow-up. Both
controllers answer that follow-up "Creech"; the new controller never sends the disconnected preflight reply to
the model after the transition. The old replay retains it. The old aircraft-type answer was "no cockpit data";
the final new replay gives F-4E-45MC. Neither final 37-exchange replay says "say again".

The six originally failed flight asks are **already fixed routes in ee138de**. Both old and new replays give the
same friendly traffic, 3,640-pound fuel twice, Nellis at 29 miles, Creech at seven miles and landing field/traffic
without a clearance. This is not a gain attributable to these guardrails. Repeating the three history-affected
asks five times each gives **15/15 right, zero model calls**, because forcing those questions to the model would
violate the live path we were asked to measure.

The standalone 72-item set imports each item's recorded historical dialogue. The new path filters that dialogue
using the same stale-reply policy. It does not fabricate successful previous replies. Consequently an ambiguous
follow-up whose original airfield answer was missing can lose its referent in that independent-item benchmark.
The sequential flight replay uses the actual preceding new fixed airfield reply and resolves the follow-ups.

## Measurement provenance and checks

**295/300 allowed calls used: 290 generations plus five existing API fallback attempts.** A locked budget is
incremented before each HTTP attempt, including failures. Credentials are warmed once per private controller;
no credential values are printed. Every sampled trial is retained, not just the best-scoring answers.

The final 72 samples are run `suite-verified`. During that run a grammatical "Their" was mistaken for a name;
this and plural spoken-type parsing were repaired with RED→GREEN tests. `suite-rechecked` sends those saved real
model outputs through the final controller, asserting identical prompt length and assistant history. It is
explicitly a **post-check replay, not 72 new calls**. The prompt and model inputs were unchanged by these repairs.
A fresh sequential flight (`history-release`, 15 attempts including its two preflight exchanges and one API
fallback) then exercised the final path. The last optimization only makes normalization lazy; all 394 study gate
verdicts were checked unchanged. No more model calls should be made from this budget.

- Stub replay: all **319 exchanges identical**, including all **149 fixed replies**; no route changes.
- Separate flight stub replay: 2 preflight + **37 flight** exchanges identical (25 fixed including preflight).
  The three later 21:37 smoke calls are outside the prior review's 319/37 snapshot and are deliberately excluded.
- Python: **614 passed, 6 skipped, 2,057 subtests** (pytest); **617 tests OK, 6 skipped** (unittest).
- All original five false-reject fixtures and new history, name/number, unknown, fallback, follow-up and cap tests
  are in `tests/test_model_guardrails.py`. Old tests with invented model stub facts now expect rejection or supply
  the facts they assert; no fixed-route expectations were loosened. The first-session reset test now expects
  chat-only invalidation; partial stream reconnects still preserve chat when another stream stays up.
- `guardrails-results.jsonl.gz`: all real trial replies, histories, backend metadata, raw model text and latencies.
- `guardrails-verdicts.jsonl`, `guardrails-manual.json`: unchanged-grader and audited verdicts, with reasons.
- `guardrails-summary.json`: machine-counted totals, timings, guard checks and call budget.
- `guardrails-stub-replay.json.gz`: full old/new offline comparisons.
- Private execution/test logs remain under `~/.p1/guardrails/` on Athena.

Not proven: speech recognition and delivery in a real sortie, accuracy under changing live data/load, universal
name/semantic recognition, or an actual model-accuracy gain. Independent review is still needed before any release.
