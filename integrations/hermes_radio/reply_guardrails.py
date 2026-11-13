"""Small contradiction checks, not a general factual validator.

Only model-authored sentences enter here. Eye announcements never do.
"""
import re


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+", text or "") if s.strip()]


def check_sentence(sentence, pic, ask, controller=None):
    from . import simple_controller as sc
    own = pic.get("ownship") or {}
    fuel = own.get("fuel") or {}
    threats = pic.get("threats") or {}
    text = sentence.lower().replace("’", "'")
    fixes, reasons = [], []

    def reject(reason, calls=()):
        reasons.append(reason)
        fixes.extend(calls or [])

    def calls(rows):
        return [r.get("call") if isinstance(r, dict) else r for r in rows or []
                if (r.get("call") if isinstance(r, dict) else r)]

    # Each comma/semicolon clause owns its negative assertion. A different missing
    # feed cannot excuse a false fuel claim ("Fuel unknown, radar unavailable").
    missing = r"unknown|unavailable|not available|not connected|disconnected|no connected picture|no picture|no position|not in an aircraft|don't know|do not know|don't have|do not have|not showing|(?:can't|cannot|unable to) (?:confirm|see|tell|call)|\bno\b.{0,65}(?:data|reading|showing|available)"
    for clause in re.split(r"[,;]|\b(?:but|while)\b|\band\b(?=\s+(?:radar|cockpit)\b)", text):
        if not re.search(missing, clause):
            continue
        if pic.get("connected", pic.get("available", False)) and re.search(r"not connected|disconnected|no (connected )?picture", clause):
            reject("connection present")
        if own and not re.search(r"from the|their|its|target|enemy|bandit|friendly", clause) and re.search(r"\bposition\b|not in an aircraft", clause):
            reject("pilot present", sc.loadout_calls({"type": own.get("type")}, "what aircraft am I flying?") if "aircraft" in clause else [])
        subject = clause
        # An unqualified "I don't know" refers to the question, but named missing
        # radar/cockpit/flight-status data never borrows the question's subject.
        if re.fullmatch(r"\W*(?:prometheus|athena)?\W*(?:i )?(?:don't know|do not know|unknown|unavailable)\W*", clause):
            subject = ask.lower()
        if re.search(r"\b(fuel|gas|pounds|percent)\b", subject):
            present = fuel.get("known") is not False and (
                fuel.get("total_lb") is not None if "pounds" in clause else
                fuel.get("internal_pct") is not None if "percent" in clause else
                fuel.get("total_lb") is not None or fuel.get("internal_pct") is not None)
            if present:
                reject("fuel present", sc.fuel_calls(fuel))
        if re.search(r"air ?field|air ?base", subject):
            fields = (pic.get("airbases") or {}).get("nearest_friendly") or []
            if fields:
                reject("friendly field present", calls(fields[:1]))
        elif (re.search(r"friendly (?:aircraft|air traffic)|friendlies|friendly traffic|\btraffic\b", subject)
              and not re.search(r"tanker|awacs|enemy|hostile|ground", subject)):
            rows = (pic.get("air_traffic") or {}).get(own.get("coalition") or "blue") or []
            if rows:
                reject("friendly traffic present", calls(rows[:3]))
        if re.search(r"weapons?|loadout|ammo|stores", subject) and not re.search(r"maverick|missile tracking", subject):
            if (own.get("loadout") or {}).get("available"):
                reject("loadout present", sc.loadout_calls(own))
        if re.search(r"what (?:aircraft|am i flying)|aircraft (?:type|am i)", subject) and own.get("type"):
            reject("aircraft present", sc.loadout_calls({"type": own["type"]}, "what aircraft am I flying?"))

    # Explicit all-clears are checked only against their own collection/tier.
    # Radio-check 'loud and clear' and geographic 'ahead' are not global all-clears.
    for clause in re.split(r"[,;]|\b(?:but|while)\b|\band\b(?=\s+(?:radar|cockpit)\b)", text):
        negative = bool(re.search(r"\b(no|nothing|none)\b|\b(?:all clear|you're clear|you are clear)\b|^\s*clear[.!]?\s*$", clause))
        if not negative or re.search(r"\b(ahead|behind)\b", clause):
            continue
        if re.search(r"(?:you're|you are) clear of\b", clause):
            continue  # named/geographic clearance is not a global all-clear
        general = bool(re.search(r"\bno threats\b|all clear|you're clear|you are clear|^\s*clear[.!]?\s*$", clause))
        tracking = bool(re.search(r"(?:nothing|no .*) (?:is )?tracking|no .*radar", clause))
        sam = threats.get("sam_in_range") or []
        if not general:
            sam = [r for r in sam if not str(r.get("call", "")).startswith("Early warning radar")]
        range_claim = re.search(r"within (.+?) (?:miles|nm)", clause)
        if range_claim:
            numbers = sc.words_to_numbers(range_claim[1])
            if len(numbers) == 1:
                sam = [r for r in sam if r.get("range_nm", 1e9) <= numbers[0]]
        if tracking or general:
            rows = (threats.get("sam_radar") or {}).get("tracking_you") or []
            matching = [r for r in sam if "tracking you" in str(r.get("confidence", "")) and "DCS radar" in str(r.get("confidence", ""))]
            if rows or matching:
                reject("radar tracking present", calls(matching or rows) or ["Radar tracking you."])
        if not tracking and (general or re.search(r"\bno sams?\b|no sam threats", clause)):
            if sam:
                reject("SAMs in range", calls(sam))
        if general or (re.search(r"\bno missiles?\b|nothing inbound", clause) and not re.search(r"\btrack(?:ing)?\b|data|feed", clause)):
            rows = threats.get("missiles_inbound") or []
            if rows:
                reject("missiles inbound", calls(rows))
        if general or re.search(r"no (?:bandits?|hostiles?|enemy aircraft|air contacts)", clause):
            tier = 150 if re.search(r"150|one hundred (?:and )?fifty|long range", clause) else 50
            if range_claim and len(sc.words_to_numbers(range_claim[1])) == 1:
                tier = sc.words_to_numbers(range_claim[1])[0]
            rows = [r for r in pic.get("hostile_groups", []) if sc.domain(r.get("category")) == "air" and r.get("range_nm", 1e9) <= tier]
            fallback = (pic.get("picture_long_range_top3" if tier == 150 else "picture_top3") or []) if tier in (50, 150) else []
            if rows or fallback:
                reject("hostile aircraft present", calls(rows) or calls(fallback))
        if re.search(r"no (?:enemy )?ground targets", clause) and pic.get("ground_targets_within_20nm"):
            reject("ground targets present", calls(pic["ground_targets_within_20nm"]))

    # Kill counts are the only numbers checked. Never compare against unrelated
    # picture numbers. New means not yet read back; totals use the whole pilot list.
    if not re.search(r"wingman|friendly kills|enemy kills", text):
        is_new = bool(re.search(r"\bnew\b", text + " " + ask.lower())) and not sc.TOTAL_ASK.search(ask)
        kills = pic.get("my_kills_this_mission") or []
        if not kills:
            # Older snapshots can carry a recent pilot kill before the cumulative
            # list. Do not use friendly losses or another pilot's kills.
            kills = [dict(r, target=(r.get("victim") or {}).get("type"),
                          category=(r.get("victim") or {}).get("category"),
                          confidence="likely" if r.get("inferred") else "confirmed")
                     for r in pic.get("recent_kills_and_losses_last_10_min") or []
                     if r.get("by_me") and r.get("event") == "killed"]
        kill_pic = dict(pic, my_kills_this_mission=kills)
        selected = kills
        if controller is not None and is_new:
            rows = controller._kill_rows(kill_pic, False, controller._guard_pic_time)
            selected, _ = controller._split_new(rows, "me")
        elif is_new:
            # A stateless recorded-output check cannot reconstruct prior delivery.
            # Live checks always receive the controller's read-back ledger.
            selected = [] if "since your last check" in text else [k for k in kills if k.get("age_s", 0) <= 600]
        negative = re.search(r"\bno (?:new |confirmed )*kills?\b", text)
        number = r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
        counts = re.findall(r"\b(" + number + r"(?:[ -]+" + number + r")*) (?:new |confirmed |total )*kills?\b", text)
        bad_count = any(sc.words_to_numbers(n) != [len(selected)] for n in counts)
        if (negative and selected) or (counts and "my_kills_this_mission" in pic and bad_count):
            if controller is not None:
                replacement = controller.kill_confirm_calls("confirm kills" if is_new else "mission total kills", kill_pic,
                                                            controller._guard_pic_time, commit=False)
            else:
                replacement = sc.requested_calls("kill_confirm", "mission total kills", dict(kill_pic, ownship=own or {"type": "unknown"}))
            reject("kill count/absence contradicts pilot kills", replacement)
    return list(dict.fromkeys(fixes)), reasons


def revise(reply, pic, ask, controller=None):
    from . import simple_controller as sc
    body = re.sub(r"^Prometheus,?\s*(Athena,?\s*)?", "", reply or "", flags=re.I)
    kept, changes = [], []
    for sentence in sentences(body):
        fixed, reasons = check_sentence(sentence, pic, ask, controller)
        if reasons:
            changes.append({"sentence": sentence, "replacement": fixed, "reasons": reasons})
            kept.extend(fixed)
        else:
            kept.append(sentence)
    return (sc.ADDRESS + " ".join(kept) if kept else ""), changes
