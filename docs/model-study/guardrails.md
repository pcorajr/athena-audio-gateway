# Guardrails — controller-owned alerts, not sentence fact-checking

## Status and scope decision

Branch `feature/model-guardrails`, compared with `ee138de`; **not deployed**.
The re-review exposed false corrections of fuel flow, another aircraft's position and
range-qualified negatives. The owner chose deletion over more semantic regexes:
**A1 missing-data checks, A2 all-clear checks and A3 kill-count checks are removed**.
`integrations/hermes_radio/reply_guardrails.py`, its live call sites and its tests are gone.
The independent offline study scorer retains its own historical checks for measurement only.
Earlier `guardrails-review-*` artifacts describe the superseded checks, not current guarantees.

The five November 11 missing-data failures are no longer mechanically corrected. Neither
are “negative threats”, incorrect kill totals or other false all-clears. Clearing stale
conversation history and putting grounding rules first may help the model, but **no
accuracy improvement is demonstrated**. Pending Eye calls remain protected independently.

## Current behavior and deliberate limits

| Live behavior | What it catches/does | What it deliberately leaves alone |
|---|---|---|
| Pending announcements | On the model path, including model failure, speaks close-bandit then pending SAM/launch/splash calls before model prose. Whole calls reserve space; unspoken calls remain pending. | Does not repeatedly inject all in-range threats; fixed-route assembly is unchanged. |
| Alert-specific echo removal | Removes a model sentence naming an actually inserted alert: same threat designation plus radar/tracking/launch/sees-you wording, or same splash target plus splash/destroyed/kill wording. Normalizes articles, punctuation, hyphens and spoken numbers. Eye row type supplies known family aliases such as S-75 / SA-2. | Different threats/kills survive, including different threats sharing geometry. Unknown identity and unsupported paraphrases are not guessed. No generic name/number validation. Whole-sentence suppression can also remove a weapon detail or tally attached to that same alert. |
| Conversation lifecycle | Clears on first session, reconnect, real spawn/name/type change; keeps stable real identity across null and pseudo-ID feed flaps. | Does not clear combat bookkeeping on chat-only resets. |
| Sentence history filter | Excludes unavailable/unknown, don't-know, disconnected, no-position, say-again and deferral sentences. Drops an immediately dependent pronoun sentence; preserves a bogey's unknown identity. | Does not judge truth: “No threats” and other negatives may remain. Useful neighboring sentences survive. |
| Repeat-last | Fixed playback for say again, repeat, say that again, repeat last, say again your last, say your last again, repeat your last. Failed delivery is not selected; expired (>2 minutes) or fallback speech re-asks the original question. | “Say again fuel/bearing” is a new question. Literal playback can repeat stale information; repeating stores it again and renews its delivery time. Missing gateway outcomes mean assumed sent. |
| Existing guards | Clearance-family removal, spoken internal-key rejection, 280-character cap, Test-pilot marker. | No factual validation of the model's fuel, loadout, airfield, traffic, positions, scopes, names or kill counts. |
| Prompt | Seven grounding rules first; no “your discretion”. Pending to-announce fields are explicitly controller-owned: model must not mention, paraphrase or contradict them. 9,596 characters (limit 9,686). | Prompt compliance is not assured; fresh model outputs still attempted echoes. |

Only inserted calls participate in echo removal. Alert keys are marked only for spoken
calls; failed/expired delivery re-arms them. Transcript snapshots omit private event keys,
so offline replays use exchange + field + index, never call text as identity. Distinct live
Eye kill keys with identical splash wording are separately tested. Snapshot replay cannot
prove historical event-level deduplication or real SRS delivery.

## Verification of this scope cut

Private Athena processes, project virtualenv, `umask 077`, `TMPDIR=~/.p1/rework3`.
No requests to the live adapter, DCS operations, service changes, push or merge.

- pytest: **612 passed, 6 skipped, 2,075 subtests**; unittest: **615 tests, OK, 6 skipped**.
  Count reduction from 621/624 is deliberate removal of sentence-check tests, offset by
  new echo/repeat/no-fact-check regressions. Existing ResourceWarnings remain.
- Both original threat-survival suites still use actual threats; whole-call/cap assertions remain.
- **319 recorded exchanges:** all **149 fixed** outputs/routes identical to ee138de;
  **23 model replies** gain only pending announcements, retaining `MODEL.`. Maximum 276 chars.
- **2 preflight + 37 flight exchanges:** all **25 fixed** identical; only model exchange
  `20261111:1630` gains its pending SA-2 call. Maximum 246 chars.
- Cap-only replay of **284 recorded + 394 study saved replies:** maxima **280 / 278** chars.
  This is not a factual-accuracy sweep and makes no claim to correct the five old failures.
- All ten reviewer's pending-call raw generations are deterministic regression fixtures.
  The four exact duplicate cases (three SA-2 paraphrases and spoken-number T-55 splash)
  are eliminated. Counting the three additional same-target kill reiterations gives
  **7/10 before → 0/10 after**. Controller's exact alert survives once; different named alerts survive.
- **10 fresh model calls**, five varied asks on recorded 11-11:1890 SAM and five on
  11-11:1834 pending kill, through the repository eval tool. Locked HTTP-attempt budget
  records **10/30 used**, including fallbacks (none needed). Final duplicate threat/kill
  speech **0/10**; no factual replacements (those checks no longer exist). Manual inspection
  of raw/final text found no unrelated-alert removal. Maximum final length **255** chars.
  Model latency median **2.501 s**, p95/max **3.126 s**. A final mechanical narrowing was
  exact-message-hash cached-rechecked: all ten final outputs unchanged, no extra model calls.

Evidence: private `~/.p1/rework3/{verification.json,real-new.jsonl,budget.json,pytest.txt,unittest.txt}`;
regression fixtures `tests/fixtures/guardrails-review.json` and `guardrails-real-extra.jsonl`.
No statistically meaningful model-quality result, real audio/delivery verification or flight
acceptance is claimed. Independent review and a live sortie remain necessary.

## Every changed model-route stub reply

Every old reply was `Prometheus, Athena, MODEL.`; every new reply retains `MODEL.` after
the following whole pending calls. Raw historical weapon labels are preserved, not validated.

| Exchange | Added calls |
|---|---|
| 20261108:123 | Splash one Ural-375 |
| 20261108:169 | Splash one Ural-375; four Paratrooper AKS-74 |
| 20261108:405 | Splash one T-55; one unknown |
| 20261108:453 | Splash one T-55; two unknown |
| 20261108:536 | Splash one unknown; one VAZ NEW |
| 20261108:560 | Splash one T-55 |
| 20261108:755 | Two 53-UOR-281U launch calls |
| 20261108:865 | Two 53-UOR-281U launch calls; Ural-375 ZU-23 splash |
| 20261108:886 | Four 53-UOR-281U launch calls |
| 20261108:894 | One 53-UOR-281U launch call |
| 20261108:903 | One 53-UOR-281U launch call |
| 20261108:914 | One 53-UOR-281U launch call |
| 20261108:973 | Three 53-UOR-281U launch calls |
| 20261108:996 | Two 53-UOR-281U launch calls; PT splash |
| 20261108:1244 | Splash one F-5E-3 |
| 20261108:1253 | Splash two F-5E-3 |
| 20261109:272 | SA-2 could be tracking, eleven o'clock, 23 miles |
| 20261109:328 | SA2V755 launch, two o'clock, seven miles |
| 20261109:361 | SA2V755 launch, ten o'clock, seven miles |
| 20261109:377 | SA2V755 launch, nine o'clock, nine miles |
| 20261109:401 | Splash one Ural-4320T |
| 20261109:527 | Splash two ZIL-131 KUNG |
| 20261111:1630 | SA-2 could be tracking, eleven o'clock, 23 miles |
