"""Eval set for tools/model_eval.py: real model-route asks (2026-11-08/09/11) + written asks on the same pictures.

Each item: id, pic (transcript day:line of the recorded picture), ask, optional history (real items take the
logged history), truth. truth keys (all regexes, matched on the lower-cased reply):
  must        list of groups; each group is a regex (or list of alternatives) that must appear
  must_not    regexes that contradict the picture (wrong fact)
  data_present  False when the honest answer IS "unknown / not showing" (default True: such a claim is wrong)
  unknown_ok  regexes for a partial 'unavailable' that is right (e.g. 'pounds unavailable for this airframe')
  manual      the mechanical checks pass but a person must read it (note says what is right)
Ground truth was written from the recorded picture of each item (values quoted in the comments).
"""
A, B, C, D = "20261111:1689", "20261111:1866", "20261111:1593", "20261109:409"
E, F, G = "20261108:1101", "20261111:1484", "20261111:1378"
FUEL_UNKNOWN = {"data_present": False, "must": [r"unknown|not available|unavailable|don't have|no fuel"],
                "must_not": [r"pounds|percent"]}
# The six 2026-11-11 flight failures (repeated 5x under baseline).
FLIGHT_FAILURES = ["20261111:1663", "20261111:1689", "20261111:1697", "20261111:1858", "20261111:1866",
                   "20261111:1890"]
ITEMS = [
    # ---------------------------------------------------------------- real asks (recorded picture + history)
    # no ownship; blue alive: fighter 12, attack 1, AWACS 1, helicopter 4; air_traffic empty (needs ownship)
    {"id": "20261111:1378", "truth": {"data_present": False, "must_not": [r"not connected|no connected"],
     "manual": "right = a count from alive totals (about 18 incl. helos) or 'no position for a traffic list'"}},
    {"id": "20261111:1384", "truth": {"data_present": False, "must": [r"unknown|not (currently )?(showing|in an aircraft)|no (ownship|position|fuel)|don't have"],
     "must_not": [r"pounds|percent"]}},  # no ownship
    # test F/A-18; sam_in_range = EWR 1L13 31 mi, RLS 19J6 31/67 mi (could be watching); no SAM
    {"id": "20261111:1405", "truth": {"must": [r"early warning|ewr|1l13|19j6"], "must_not": [r"\bsa-?\s?\d"]}},
    # nearest_friendly EMPTY (only neutral fields): 'no friendly airfield' is the right answer
    {"id": "20261111:1415", "truth": {"data_present": False, "must": [r"no friendly|none showing|not showing"],
     "must_not": [r"^(?!.*neutral).*(hi ?14|h i fourteen)"]}},  # naming HI14 is fine only as neutral
    {"id": "20261111:1418", "truth": FUEL_UNKNOWN},  # fuel.known False
    {"id": "20261111:1421", "truth": {"data_present": False, "must": [r"unavailable|not available|unknown|no loadout|can't|cannot|don't have|eval"],
     "must_not": [r"\b(agm|aim|gbu|maverick|sidewinder)"]}},  # loadout unavailable (Eval off)
    # test, on the ground at Ramat David (nearest_friendly: Ramat David < 1 mi)
    {"id": "20261111:1448", "truth": {"must": [r"ramat david"]}},
    {"id": "20261111:1457", "truth": FUEL_UNKNOWN},
    {"id": "20261111:1460", "truth": {"must": [r"thirty[- ]five|\b35\b"]}},  # red armor alive 35
    # blue traffic 7 groups / 12 aircraft (4 on the ground at angels zero); alive blue air 13
    {"id": "20261111:1484", "truth": {"must": [r"seven|eight|twelve|thirteen|eleven"], "must_not": [r"fifteen|no friendl"],
     "manual": "right = 12 in traffic, 13 alive or 7-8 airborne; fifteen is wrong"}},
    {"id": "20261111:1546", "truth": {"must": [r"early warning|ewr|19j6|1l13"]}},  # EWRs 39/46 mi
    {"id": "20261111:1577", "truth": {"must": [r"no sams?|nothing ahead|none ahead|no .{0,10}threats? ahead|clear ahead|behind|seven o'clock"],
     "must_not": [r"\bsa-?\s?\d"]}},  # only EWRs at seven o'clock (behind)
    {"id": "20261111:1593", "truth": {"must": [r"two thousand five hundred eighty|2,?580"]}},
    # EWRs could be watching (8/9 mi); nothing tracking
    {"id": "20261111:1606", "truth": {"must": [r"early warning|ewr|could be watching|nothing (is )?tracking|no radar|not (being )?tracked|not tracking|no sams? (are |is )?tracking"],
     "must_not": [r"(sa-?\s?\d+|sam) (is )?tracking you"]}},
    # FLIGHT FAILURE 18:30:05: connected, A-10C 5 mi nine o'clock, 6 friendly aircraft
    {"id": "20261111:1663", "truth": {"must": [r"a-10|friendly|friendlies"], "must_not": [r"no friendl|can't confirm|cannot confirm"]}},
    # FLIGHT FAILURES 18:32:05 / 18:32:22: fuel 3,640 pounds
    {"id": "20261111:1689", "truth": {"must": [r"three thousand six hundred forty|3,?640"]}},
    {"id": "20261111:1697", "truth": {"must": [r"three thousand six hundred forty|3,?640"]}},
    {"id": "20261111:1705", "truth": {"manual": "garbled; any short polite reply or say again, no invented facts"}},
    # nearest_friendly Creech 209 / 27 mi; nearest overall Groom Lake (neutral)
    {"id": "20261111:1721", "truth": {"must": [r"creech|groom"]}},
    {"id": "20261111:1729", "truth": {"must": [r"creech"]}},  # follow-up: name of that airfield
    # 3 confirmed kills (one T-55 + two T 55 scenery); fuel 3,640; 1 AGM-65A left
    {"id": "20261111:1801", "truth": {"must": [r"three"], "must_not": [r"(two|four|five) (confirmed )?kills"]}},
    # FLIGHT FAILURE 18:59:55: Nellis 134 / 29 mi in nearest_friendly
    {"id": "20261111:1858", "truth": {"must": [r"nellis", r"one three four", r"twenty[- ]nine"], "must_not": [r"no friendly"]}},
    # FLIGHT FAILURE 19:00:12: Creech 337 / 7 mi
    {"id": "20261111:1866", "truth": {"must": [r"creech", r"three three seven", r"seven miles"], "must_not": [r"no friendly"]}},
    {"id": "20261111:1874", "truth": {"must": [r"creech"]}},  # Creech 348 / 7 mi
    # FLIGHT FAILURE 19:01:46 (backup model): no clearance may be invented; Creech 069 / 5 mi
    {"id": "20261111:1890", "truth": {"must": [r"creech"]}},
    {"id": "20261111:1923", "truth": {}},  # chit-chat
    # F-100D: 40 percent, pounds unknown for this airframe
    {"id": "20261109:71", "truth": {"must": [r"forty percent|40 ?%|forty per ?cent"], "must_not": [r"(thousand|hundred)[^.]*pounds"],
     "unknown_ok": [r"pounds", r"airframe"]}},
    # gun hits on the T-55 8 s ago, T-55 still alive, no kill
    {"id": "20261109:147", "truth": {"must": [r"hit|yes"], "must_not": [r"splash|destroyed|(confirmed|a) kill(?!.{0,20}not)"],
     "manual": "right = hits on the T-55, no kill, still alive"}},
    # kills: SNR 75V + Ural-4320T (Ural killed 18 s ago by me)
    {"id": "20261109:409", "truth": {"unknown_ok": [r"secondary"], "must": [r"ural"], "must_not": [r"(t-55|t-90|btr)[^.]{0,20}(killed|destroyed|died)"]}},
    {"id": "20261109:484", "truth": {"must": [r"one (agm|maverick)|agm-?\s?65|maverick", r"six hundred thirty[- ]nine|639"],
     "must_not": [r"(two|three|four) (agm|maverick)"]}},  # 1 AGM-65A, 639 rounds
    # AGM-65H fired 29 s ago; PT-76 killed by it 0 s ago
    {"id": "20261108:996", "truth": {"must": [r"pt|kill|splash|hit|impact"],
     "manual": "right = the Maverick hit/killed a PT-76 (it is no longer flying); 'pending' is stale"}},
    {"id": "20261108:708", "truth": {"unknown_ok": [r"rwr"], "must": [r"rwr|no sam|nothing|clean"],
     "must_not": [r"(locked|tracking) (on )?you", r"no rwr threats"]}},  # no RWR feed: 'no RWR threats' implies a clean RWR
    {"id": "20261108:1101", "truth": {"must": [r"seven thousand seven hundred seventy|7,?770|seventy percent"]}},
    # ---------------------------------------------------------------- written asks on recorded pictures
    # A = 11-11 flight 18:32 (F-4E, connected, A-10C two-ship 6 mi ten o'clock, 4 IL-76, T-55 17 mi ten o'clock,
    #     SA-2 radar sees you 18 mi seven o'clock (SAM site 10 alive), Creech 3 mi, fuel 3,640, no kills, no shots)
    {"id": "w01", "pic": A, "ask": "Athena, how many friendlies are airborne?",
     "truth": {"must": [r"six|seven|five"], "must_not": [r"no friendl|not connected|can't confirm"]}},
    {"id": "w02", "pic": A, "ask": "Athena, what's at my ten o'clock?",
     "truth": {"must": [r"a-10|armor|t-55"], "must_not": [r"bandit"]}},
    {"id": "w03", "pic": A, "ask": "Athena, is the SA-2 still alive?",
     "truth": {"must": [r"yes|alive|still|sees you|active|up"], "must_not": [r"destroyed|dead|killed|\bdown\b"]}},
    {"id": "w04", "pic": A, "ask": "Athena, how far is the tanker?",
     "truth": {"data_present": False, "must": [r"no tanker|not showing|don't (see|have)|no .{0,20}tanker|negative|no kc"],
               "must_not": [r"tanker[^.]{0,40}(miles|bearing)"]}},
    {"id": "w05", "pic": A, "ask": "Athena, did anybody shoot at me?",
     "truth": {"must": [r"no |nothing|negative|not that i can see|none"], "must_not": [r"missile (launch|inbound)[^.]{0,20}(at|toward) you"]}},
    {"id": "w06", "pic": A, "ask": "Athena, say my altitude.",
     "truth": {"must": [r"eleven thousand|11,?\d{3}|eleven point|angels eleven"],
               "manual": "ownship.alt_ft 11,360 (cockpit altitude may differ slightly)"}},
    {"id": "w07", "pic": A, "ask": "Athena, how many SAM units does red have?", "truth": {"must": [r"\bten\b|\b10\b"]}},
    {"id": "w08", "pic": A, "ask": "Athena, any bandits out there?",
     "truth": {"must": [r"no bandits|no hostile air|nothing|negative|none"], "must_not": [r"bandit [a-z]"]}},
    {"id": "w10", "pic": A, "ask": "Athena, uh, the closest bad guy, where's he at?",
     "truth": {"must": [r"armor|t-55", r"zero four five|seventeen"]}},
    {"id": "w11", "pic": A, "ask": "Athena, is Creech close?", "truth": {"must": [r"creech", r"three miles|two point eight"]}},
    {"id": "w12", "pic": A, "ask": "Athena, what time is it?",
     "truth": {"data_present": False, "must": [r"don't have|no (mission )?(time|clock)|unable|not available|unknown|say again|can't"]}},
    {"id": "w13", "pic": A, "ask": "Athena, ranger ranger check.", "truth": {"manual": "garbled radio check: loud and clear or say again"}},
    {"id": "w14", "pic": A, "ask": "Athena, how long have I been airborne?",
     "truth": {"data_present": False, "manual": "no takeoff event; 'entered aircraft 293 s ago' (about five minutes) or unknown is right"}},
    # B = 11-11 flight 19:00 (4 confirmed kills: T-55 + 3 T 55 scenery, last by AGM-65A 68 s ago; no Mavericks left;
    #     SAM site 10 alive incl. SA-2 25 mi; A-10C 031/13 mi and 356/20 mi; red alive: vehicles 11, SAM 10, armor 6, AAA 6)
    {"id": "w15", "pic": B, "ask": "Athena, how many kills do I have?",
     "truth": {"must": [r"four"], "must_not": [r"(three|five) (confirmed )?kills"]}},
    {"id": "w16", "pic": B, "ask": "Athena, is the SA-2 still alive?",
     "truth": {"must": [r"yes|alive|still|active|up"], "must_not": [r"destroyed|dead|killed|\bdown\b"]}},
    {"id": "w17", "pic": B, "ask": "Athena, any Mavericks left?",
     "truth": {"unknown_ok": [r"maverick"], "must": [r"\bno\b|none|zero|winchester|negative"], "must_not": [r"(one|two|three|four) (agm|maverick)"]}},
    {"id": "w18", "pic": B, "ask": "Athena, what did I kill last?", "truth": {"must": [r"t[- ]?55"]}},
    {"id": "w19", "pic": B, "ask": "Athena, where are the A-10s?",
     "truth": {"must": [r"a-10", r"zero three one|thirteen|three five six|twenty"]}},
    {"id": "w20", "pic": B, "ask": "Athena, how many enemy trucks are still alive?", "truth": {"must": [r"eleven"]}},
    {"id": "w21", "pic": B, "ask": "Athena, what's my fuel?", "truth": {"must": [r"three thousand six hundred forty|3,?640"]}},
    {"id": "w22", "pic": B, "ask": "Athena, status of the triple A?",
     "truth": {"must": [r"triple a|aaa", r"three|six"], "must_not": [r"destroyed|dead|killed"]}},
    # C = 11-11 test (MQ-9, Mi-8MT two-ship 8 mi seven o'clock low, MiG-21Bis nine-ship 40 mi, triple A 3 mi
    #     seven o'clock, EWR 8 mi seven o'clock, fuel 2,580 lb, no weapons, friendly ground 2 mi)
    {"id": "w23", "pic": C, "ask": "Athena, what's at my seven o'clock?",
     "truth": {"must": [r"mi-8|helicopter|bandit|triple a|early warning|ewr"]}},
    {"id": "w24", "pic": C, "ask": "Athena, are those helicopters a threat?",
     "history": [["Athena, what's at my seven o'clock?",
                  "Prometheus, Athena, Test pilot. Bandit Mi-8MT two-ship, zero one one, eight miles, angels three, seven o'clock low."]],
     "truth": {"unknown_ok": [r"intent|threat"], "must": [r"mi-8|helicopter|helo|two-ship|eight miles"],
               "manual": "Mi-8MT two-ship 8 mi angels 3 (transport helicopters); no invented weapons"}},
    {"id": "w25", "pic": C, "ask": "Athena, how many MiG-21s are up?", "truth": {"must": [r"\bnine\b|\b9\b"]}},
    {"id": "w26", "pic": C, "ask": "Athena, what am I carrying?",
     "truth": {"must": [r"no weapons|nothing|none|empty|no stores|no ordnance|zero"]}},
    {"id": "w27", "pic": C, "ask": "Athena, any friendly ground forces near me?",
     "truth": {"must": [r"friendly", r"two miles|two two eight|one three one"]}},
    {"id": "w28", "pic": C, "ask": "Athena, say fuel.", "truth": {"must": [r"two thousand five hundred eighty|2,?580"]}},
    # D = 11-09 (F-4E, kills SNR 75V + Ural-4320T by AGM-65A, 3 AGM-65A left, SA-2 could be tracking 8 mi four o'clock)
    {"id": "w29", "pic": D, "ask": "Athena, how many Mavericks do I have?",
     "truth": {"must": [r"three"], "must_not": [r"(one|two|four) (agm|maverick)"]}},
    {"id": "w30", "pic": D, "ask": "Athena, what killed the Ural?", "truth": {"must": [r"agm|maverick"]}},
    {"id": "w31", "pic": D, "ask": "Athena, is the SA-2 tracking me?",
     "truth": {"must": [r"could be"], "must_not": [r"(sa-?\s?2|sam) (is )?tracking you"]}},
    {"id": "w32", "pic": D, "ask": "Athena, what's the score?", "truth": {"must": [r"\btwo\b"]}},
    # E = 11-08 (A-10C, MiG-15bis one-ship 295/37 mi angels 23 three o'clock high, 9 kills, 276 rounds, E-3A 167/42 mi)
    {"id": "w33", "pic": E, "ask": "Athena, where's the MiG?", "truth": {"must": [r"two nine five", r"thirty[- ]seven"]}},
    {"id": "w34", "pic": E, "ask": "Athena, how many gun rounds do I have?", "truth": {"must": [r"two hundred seventy[- ]six|276"]}},
    {"id": "w35", "pic": E, "ask": "Athena, how many kills this mission?", "truth": {"must": [r"\bnine\b|\b9\b"]}},
    {"id": "w36", "pic": E, "ask": "Athena, is the AWACS up?",
     "truth": {"must": [r"e-3|awacs", r"yes|one six seven|forty[- ]two|airborne|up"]}},
    # F = 11-11 test (F-4E, fuel known false, no bandits within 50, MiG-25PD 95 mi, F/A-18C likely destroyed 46 s ago)
    {"id": "w37", "pic": F, "ask": "Athena, fuel state?", "truth": FUEL_UNKNOWN},
    {"id": "w38", "pic": F, "ask": "Athena, did we lose anyone?",
     "truth": {"unknown_ok": [r"likely"], "must": [r"f/a-18|hornet|f-18|fa-18|eighteen", r"likely|no longer showing"], "must_not": [r"confirmed (killed|destroyed|lost|loss)"]}},
    {"id": "w39", "pic": F, "ask": "Athena, any bandits within fifty miles?",
     "truth": {"must": [r"\bno\b|nothing|negative|none"]}},
    # G = 11-11 no ownship (red alive: fighter 13, attack 27, helicopter 2)
    {"id": "w40", "pic": G, "ask": "Athena, how many enemy fighters are up?", "truth": {"must": [r"thirteen|\b13\b"]}},
]
