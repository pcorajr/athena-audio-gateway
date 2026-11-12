"""Model-accuracy study for the radio model route (study only: never imported by the live adapter).

Replays recorded pilot asks + the picture Athena saw against the REAL radio model, through the unchanged
SimpleController._answer path (same prompt, same CodexLLM backend, effort, timeout and exchange deadline),
and scores each reply mechanically against the picture. Variants only rewrite the messages at the model
boundary, so the live code is never changed.

  python -m integrations.hermes_radio.tools.model_eval extract   OUT.json        # candidates from transcripts
  python -m integrations.hermes_radio.tools.model_eval run       SET RESULTS --variant baseline [--ids a,b] [--repeat n]
  python -m integrations.hermes_radio.tools.model_eval score     SET RESULTS [--variant v] [--check]
Budget: every model call (Codex attempt or API fallback) is counted in $MODEL_EVAL_BUDGET (default
~/.p1/study/budget.json); the runner refuses to go past MODEL_EVAL_MAX_CALLS (default 400).
"""
import argparse
import collections
import copy
import gzip
import json
import os
import re
import statistics
import sys
import time
import types

from .. import simple_controller as sc
from ..codex_llm import CodexLLM

DAYS = ("20261108", "20261109", "20261111")
TRANSCRIPTS = os.path.expanduser("~/.hermes/cache/athena-radio")
BUDGET = os.path.expanduser(os.environ.get("MODEL_EVAL_BUDGET", "~/.p1/study/budget.json"))
MAX_CALLS = int(os.environ.get("MODEL_EVAL_MAX_CALLS", "400"))


# ------------------------------------------------------------------ transcripts -> candidate items
def _exchanges(day):
    """Ordered exchanges of one transcript day: ask, picture record line, picture, reply, route, history."""
    path = os.path.join(TRANSCRIPTS, "transcript-%s.jsonl" % day)
    order, by = [], {}
    with open(path) as handle:
        for line_no, line in enumerate(handle, 1):
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "adapter_start":
                order.append(("start", None))
                continue
            if kind == "route" and rec.get("outcome") == "new_mission_controller_memory_cleared":
                order.append(("reset", rec.get("transmission_id")))
                continue
            tid = rec.get("transmission_id")
            if not tid or kind not in ("adapter_ingress", "tool", "reply_generated"):
                continue
            ex = by.get(tid)
            if ex is None:
                ex = by[tid] = {"tid": tid, "parts": []}
                order.append(("ex", tid))
            if kind == "adapter_ingress":
                ex["ask"], ex["time"] = rec.get("text"), rec.get("time")
            elif kind == "tool" and rec.get("tool") == "picture":
                ex["line"], ex["picture"] = line_no, (rec.get("result") or {}).get("picture")
                ex["recorded_class"] = (rec.get("result") or {}).get("call_class")
            elif kind == "reply_generated":
                ex["parts"].append(rec.get("text") or "")
                ex["route"], ex["model"] = rec.get("route"), rec.get("model")
                ex["llm_ms"] = (rec.get("timings_ms") or {}).get("llm")
                ex.setdefault("time", rec.get("time"))
    history, out = [], []
    for what, tid in order:
        if what == "start":
            history = []
            continue
        if what == "reset":  # logged inside the exchange that cleared it: drop what came before
            history = []
            continue
        ex = by[tid]
        if "line" not in ex or ex.get("ask") is None:
            continue
        ex["reply"] = " ".join(ex["parts"])
        ex["history"] = [list(h) for h in history[-8:]]
        out.append(ex)
        if (ex.get("route") or "").lower() != "fallback" and ex["reply"]:
            history.append((ex["ask"], ex["reply"]))
    return out


class _Log:
    def __init__(self):
        self.records = []

    def log(self, **values):
        values.pop("request", None)
        self.records.append(values)


class _Eye:
    """Serves one recorded picture; no alerts, no memory, nothing written."""
    def __init__(self, pic):
        self.pic = pic

    def picture(self):
        return copy.deepcopy(self.pic)


def _request(text):
    return types.SimpleNamespace(transcript=text, speaker="Prometheus", transmission_id="model-eval",
                                 frequency_hz=251000000, modulation="AM")


def route_today(ask, pic, history):
    """'fixed' | 'llm' | 'other' for this ask on today's controller (no model call: a stub answers)."""
    called = []

    def stub(messages):
        called.append(1)
        return "Prometheus, Athena, copy."
    ctl = sc.SimpleController(None, _Log(), model="stub", api_key="", eye=_Eye(pic), llm=stub)
    ctl.history.extend(tuple(h) for h in history)
    try:
        reply = ctl.exchange(_request(ask))
    except Exception as error:  # noqa: BLE001 - an old picture schema the fixed code cannot read
        return "error:%s" % type(error).__name__
    return "llm" if called else (reply.get("route") or "other")


def cmd_extract(args):
    items = []
    for day in DAYS:
        for ex in _exchanges(day):
            if (ex.get("route") or "").lower() != "llm" or not isinstance(ex.get("picture"), dict):
                continue
            items.append({"id": "%s:%d" % (day, ex["line"]), "time": ex.get("time"), "ask": ex["ask"],
                          "recorded_reply": ex["reply"], "recorded_model": ex.get("model"),
                          "recorded_llm_ms": ex.get("llm_ms"), "recorded_class": ex.get("recorded_class"),
                          "history": ex["history"], "today": route_today(ex["ask"], ex["picture"], ex["history"]),
                          "picture": ex["picture"]})
    with gzip.open(args.out, "wt") as handle:
        json.dump(items, handle)
    print("model-route exchanges: %d; still model today: %d" % (
        len(items), sum(1 for i in items if i["today"] == "llm")))
    print(collections.Counter(i["today"] for i in items))


# ------------------------------------------------------------------ fact families (slim + facts variants)
# Owner direction: reuse the RETIRED controller's catalogue of narrow reads as the design for which slice of the
# picture a question needs (legacy/integrations/hermes_radio_legacy/tools.py: ownship, query_units, get_unit,
# count_units, group_summary, current_summary, mission_intel, recent_events, braa; facts.py: caveat-preserving
# sentences). Built here over the CURRENT Eye picture, which already carries ready BRAA+clock `call` lines (the old
# `braa` read), so no tool round trips: code picks the families, one model call answers.
FAMILY_RULES = (
    # (family, ask pattern) - simple keyword rules, several may match
    ("ownship", r"\b(fuel|gas|bingo|joker|what am i (flying|in)|aircraft am i|airplane am i|my (altitude|speed|heading|"
                r"position|jet|aircraft)|altitude|airspeed|how fast|heading|damage|i'?m hit|carrying|weapons?|loadout|"
                r"mavericks?|bombs?|rounds|ammo|stores|winchester)\b"),
    ("mission_intel", r"\b(air ?fields?|air ?bases?|airports?|home ?plate|rtb|land(ing)?|approach|nellis|nelli's|creech|"
                      r"divert|runway|tower|base)\b"),
    ("friendly_units", r"\b(friendl\w*|blue|wing ?m[ae]n|tanker|awacs|traffic|buddy|a-10s?|e-3)\b"),
    ("threats", r"\b(sams?|threats?|missiles?|tracking|locked|lock|spike\w*|rwr|paint\w*|shoot\w*|shot at|launch\w*|"
                r"sa-? ?\d+|radar|defen[cs]es?)\b"),
    ("hostile_air", r"\b(bandits?|picture|bogey|bogeys|hostile air|enemy air|helos?|helicopters?|fighters?|migs?|"
                    r"mig-? ?\d+|su-? ?\d+|bad guy|o'?clock)\b"),
    ("hostile_ground", r"\b(ground|targets?|armou?r|tanks?|trucks?|infantry|vehicles?|triple a|aaa|t-? ?\d+|bad guy|"
                       r"o'?clock|forces)\b"),
    ("count_units", r"\b(how many|count|left|remain\w*|still (alive|up|there)|alive|status)\b"),
    ("kills", r"\b(kills?|killed|kill|splash|destroyed|dead|died|tally|score|lose|lost|loss)\b"),
    ("recent_events", r"\b(last|latest|recent|what happened|did i hit|hits?|shoot\w*|shot|fired|lose|lost|"
                      r"how long|airborne|time)\b"),
    ("current_summary", r"\b(mission status|status|how are we doing|sitrep|overview)\b"),
)
# Which picture keys supply each family today (also the table in MODEL-ACCURACY.md).
FAMILY_KEYS = {
    "ownship": ("ownship", "cockpit"),
    "mission_intel": ("theatre", "airbases", "ownship"),
    "friendly_units": ("ownship", "air_traffic", "friendly_groups", "alive_totals_by_coalition", "friendly_kills_this_mission"),
    "threats": ("ownship", "threats", "cockpit", "rwr_alert_to_announce"),
    "hostile_air": ("ownship", "picture_top3", "air_picture_empty_call", "picture_long_range_top3", "hostile_air_groups",
                    "hostile_groups"),
    "hostile_ground": ("ownship", "ground_targets_within_20nm", "ground_empty_call", "hostile_groups"),
    "count_units": ("alive_totals_by_coalition", "hostile_groups_total", "hostile_groups", "kill_tallies"),
    "kills": ("my_kills_this_mission", "kill_tallies", "recent_kills_and_losses_last_10_min", "my_shots_last_15_min",
              "friendly_kills_this_mission", "splash_to_announce"),
    "recent_events": ("recent_events_last_10_min", "recent_kills_and_losses_last_10_min", "my_shots_last_15_min"),
    "current_summary": ("ownship", "my_kills_this_mission", "kill_tallies", "alive_totals_by_coalition", "picture_top3",
                        "air_picture_empty_call", "threats", "ground_targets_within_20nm", "ground_empty_call"),
}
# Always sent: connection/age header and the alerts the reply must lead with (old facts.py caveats).
BASE_KEYS = ("source", "connected", "available", "reason", "data_age_s", "bearings", "test_ownship",
             "splash_to_announce", "rwr_alert_to_announce", "working_target")
# No family matched (chit-chat, garbled, acks): a small general slice.
DEFAULT_FAMILIES = ("ownship", "hostile_air", "threats", "hostile_ground", "kills")
# get_unit: a named unit in the ask -> DCS type-name needles (simple table, deliberately incomplete).
NAMED = ((r"\bsa-? ?2\b", ("s_75", "snr_75", "s-75", "snr 75")), (r"\bsa-? ?3\b", ("s-125", "s_125", "snr s-125")),
         (r"\bsa-? ?6\b", ("kub",)), (r"\bsa-? ?8\b", ("osa",)), (r"\bsa-? ?10\b", ("s-300",)),
         (r"\bsa-? ?11\b", ("buk",)), (r"\bsa-? ?15\b", ("tor",)), (r"\bsa-? ?18\b", ("igla",)),
         (r"\btanker\b", ("kc-135", "kc135", "kc-130", "kc130", "il-78", "s-3b tanker")),
         (r"\b(awacs|e-3)\b", ("e-3", "e-2", "a-50")), (r"\ba-10s?\b", ("a-10",)), (r"\bmig-? ?(\d+)", None),
         (r"\bsu-? ?(\d+)", None), (r"\bt-? ?(55|62|72|80|90)\b", None), (r"\b(helicopters?|helos?|mi-8)\b", ("mi-8", "mi-24", "uh-1", "ka-50")),
         (r"\b(ural|truck)", ("ural", "zil", "kamaz", "gaz")), (r"\b(triple a|aaa)\b", ("zu-23", "zsu", "s-60", "shilka", "ks-19", "flak")))


def families(ask):
    found = [name for name, pattern in FAMILY_RULES if re.search(pattern, ask or "", re.I)]
    return found


def named_needles(ask):
    out = []
    for pattern, needles in NAMED:
        m = re.search(pattern, ask or "", re.I)
        if not m:
            continue
        if needles is None:  # mig-21 / su-17 / t-55: the type number itself
            prefix = re.match(r"[a-z]+", m.group(0).lower()).group(0)
            out.append(("%s-%s" % (prefix, m.group(1)),))
        else:
            out.append(needles)
    return out


def slim_picture(pic, ask):
    """Only the sections the question's families need (old narrow reads, current picture keys)."""
    fams = families(ask) or list(DEFAULT_FAMILIES)
    keys = set(BASE_KEYS)
    for name in fams:
        keys.update(FAMILY_KEYS[name])
    out = {k: v for k, v in pic.items() if k in keys}
    own = out.get("ownship")
    if isinstance(own, dict) and "ownship" not in fams:  # position only: drop loadout / damage detail
        out["ownship"] = {k: v for k, v in own.items() if k not in ("loadout", "damage")}
    if "cockpit" in out and "ownship" not in fams and "threats" not in fams:
        out.pop("cockpit")
    return out


def _calls(rows, n=3):
    return [r["call"] for r in (rows or [])[:n] if isinstance(r, dict) and r.get("call")]


def _matches(row, needles):
    blob = " ".join(list((row.get("types") or {}).keys()) + [str(row.get("sam") or ""), str(row.get("type") or ""),
                                                             str(row.get("target") or "")]).lower()
    return any(n in blob for n in needles)


def _caveats(pic, fams):
    """Old facts.py idea: keep the qualifiers that apply to THIS answer, and only those."""
    lines = []
    if not pic.get("connected", pic.get("available", True)):
        lines.append("Tracking is not connected; this is the last known picture.")
    age = (pic.get("data_age_s") or {}).get("units")
    if isinstance(age, (int, float)) and age > 60:
        lines.append("Contact data is %d seconds old." % age)
    streams = ((pic.get("data_health") or {}).get("streams")) or {}
    relevant = {"units-air": ("hostile_air", "friendly_units"), "units-helo": ("hostile_air", "friendly_units"),
                "units-ground": ("hostile_ground", "count_units"), "events": ("kills", "recent_events")}
    for stream, fam_list in relevant.items():
        state = str(streams.get(stream) or "up")
        if not state.startswith("up") and any(f in fams for f in fam_list):
            lines.append("Caveat: the %s feed is down, so %s may be missing." % (
                stream, {"units-air": "aircraft", "units-helo": "helicopters", "units-ground": "ground units",
                         "events": "recent kills"}[stream]))
    if "kills" in fams and (pic.get("data_health") or {}).get("combat_events") not in (None, "ok"):
        lines.append("Caveat: kill reporting is degraded; kills may be unconfirmed.")
    return lines


def fact_lines(pic, ask):
    """Ready-made sentences for the question's families (the picture already carries most as `call` strings)."""
    fams = families(ask) or list(DEFAULT_FAMILIES)
    lines = _caveats(pic, fams)
    own = pic.get("ownship")
    if not own:
        lines.append("No ownship: Prometheus is not showing in an aircraft, so nothing can be given as a range or "
                     "bearing from him.")
    own = own or {}
    for alert in (pic.get("rwr_alert_to_announce") or []) + (pic.get("splash_to_announce") or []):
        lines.append("Say first: %s" % alert)
    fuel = own.get("fuel") or {}
    red = (pic.get("alive_totals_by_coalition") or {}).get("red" if own.get("coalition", "blue") == "blue" else "blue")
    blue = (pic.get("alive_totals_by_coalition") or {}).get(own.get("coalition") or "blue")
    kills = [k for k in (pic.get("my_kills_this_mission") or []) if isinstance(k, dict)]
    th = pic.get("threats") or {}
    for name in fams:
        if name == "ownship" and own:
            lines.append("Prometheus is flying a %s, altitude %s feet, %s knots, heading %s." % (
                own.get("type"), own.get("alt_ft"), own.get("speed_kt"), own.get("heading_mag") or own.get("heading_true")))
            if fuel.get("spoken"):
                lines.append("Fuel: %s." % fuel["spoken"])
            elif fuel.get("total_lb") is not None:  # fixed after the run: older pictures have no 'spoken'
                lines.append("Fuel: %s pounds, %s percent internal." % (fuel["total_lb"], fuel.get("internal_pct")))
            elif fuel.get("internal_pct") is not None:
                lines.append("Fuel: %s percent internal (pounds unknown for this airframe)." % fuel["internal_pct"])
            else:
                lines.append("Fuel: unknown (no fuel value from the jet).")
            lo = own.get("loadout") or {}
            if lo.get("available"):
                weapons = ", ".join("%s %s" % (w.get("count"), w.get("spoken_name") or w.get("weapon"))
                                    for w in lo.get("weapons") or []) or "no weapons"
                lines.append("Weapons left: %s; gun rounds %s." % (weapons, lo.get("gun_rounds")))
            elif lo:
                lines.append("Loadout unavailable: %s." % str(lo.get("reason"))[:80])
            life = (own.get("damage") or {}).get("life_percent")
            if life is not None and re.search(r"damage|hit", ask or "", re.I):
                lines.append("Aircraft life: %s percent; hit events: %d." % (life, len((own.get("damage") or {}).get("events") or [])))
        if name == "mission_intel":
            ab = pic.get("airbases") or {}
            wanted = [b for b in ab.get("nearest_friendly") or [] if b.get("name") and re.search(
                re.escape(b["name"].split()[0].lower()[:5]), (ask or "").lower())]
            lines += ["Friendly airfield: " + c for c in _calls(wanted + list(ab.get("nearest_friendly") or []))] or [
                "No friendly airfield showing."]
            for base in (wanted + list(ab.get("nearest_friendly") or []))[:1]:
                traffic = base.get("traffic") or {}
                if traffic.get("count") is not None:
                    lines.append("Traffic at %s: %s aircraft." % (base.get("name"), traffic.get("count")))
            if ab.get("nearest") and (ab["nearest"][0].get("owner") != (own.get("coalition") or "blue")):
                lines.append("Nearest airfield of any owner: %s (%s)." % (ab["nearest"][0].get("call"), ab["nearest"][0].get("owner")))
        if name == "friendly_units":
            traffic = (pic.get("air_traffic") or {}).get(own.get("coalition") or "blue") or []
            if own:
                lines.append("Friendly aircraft in the air picture: %d groups, %d aircraft." % (
                    len(traffic), sum(int(g.get("count") or 1) for g in traffic if isinstance(g, dict))))
                lines += _calls(traffic)
            if blue:
                lines.append("Friendly alive totals: " + ", ".join("%s %s" % (v, k) for k, v in blue.items()) + ".")
        if name == "threats":
            lines += _calls(th.get("sam_in_range"), 4) or ["No SAM in range."]
            flight = th.get("missiles_in_flight") if isinstance(th.get("missiles_in_flight"), list) else th.get("missiles_inbound")
            lines += _calls(flight) or ["No missile in the air that I can see."]
            if th.get("rwr_source") == "grpc_inferred":
                lines.append("No RWR feed from this jet.")
            if th.get("missile_scan") not in (None, "on"):
                lines.append("Missile tracking is limited: %s." % th.get("missile_scan"))
        if name == "hostile_air":
            top = pic.get("picture_top3")
            lines += list(top or []) or ([pic["air_picture_empty_call"]] if pic.get("air_picture_empty_call") else [])
        if name == "hostile_ground":
            lines += _calls(pic.get("ground_targets_within_20nm")) or [pic.get("ground_empty_call") or
                                                                      "No ground targets within twenty miles."]
            if re.search(r"o'?clock", ask or "", re.I):
                clock = re.search(r"(\w+) o'?clock", ask, re.I).group(1).lower()
                for g in (pic.get("hostile_groups") or []) + (pic.get("friendly_groups") or []):
                    if g.get("call") and clock in g["call"].lower() and g["call"] not in lines:
                        lines.append(g["call"])
        if name == "count_units" and red:
            lines.append("Enemy alive totals: " + ", ".join("%s %s" % (v, k) for k, v in red.items()) + ".")
        if name in ("kills", "current_summary"):
            if kills:
                lines.append("My kills this mission: %d (%s)." % (len(kills), ", ".join(
                    "%s %s by %s" % (k.get("target"), k.get("confidence") or "", k.get("weapon")) for k in kills[-6:])))
            elif "my_kills_this_mission" in pic:
                lines.append("My kills this mission: none.")
        if name in ("kills", "recent_events"):
            for row in (pic.get("recent_kills_and_losses_last_10_min") or [])[:4]:
                victim = row.get("victim") or {}
                if victim.get("category") in ("weapon", "unknown"):
                    continue
                lines.append("%d s ago: %s %s %s%s%s." % (row.get("age_s") or 0, victim.get("coalition"), victim.get("type"),
                             row.get("event"), " by me with %s" % row.get("weapon") if row.get("by_me") else "",
                             " (friendly loss)" if row.get("friendly_loss") else ""))
        if name == "recent_events":
            shots = pic.get("my_shots_last_15_min") or {}
            for s in (shots.get("shots") or [])[:2]:
                hit = (s.get("hit") or {}).get("type")
                lines.append("My shot: %s %d s ago, result %s%s." % (s.get("weapon"), s.get("age_s") or 0, s.get("result"),
                                                                     " on %s" % hit if hit else ""))
            lines += list((pic.get("recent_events_last_10_min") or [])[:3])
            if re.search(r"\b(time|how long)\b", ask or "", re.I):
                lines.append("The picture has no mission clock or takeoff time.")
        if name == "current_summary":
            if red:
                lines.append("Enemy alive totals: " + ", ".join("%s %s" % (v, k) for k, v in red.items()) + ".")
            lines += (list(pic.get("picture_top3") or []) or [pic.get("air_picture_empty_call")])[:1]
            lines += _calls(th.get("sam_in_range"), 1)
    for needles in named_needles(ask):  # get_unit: the named unit's own rows; not showing is not destroyed
        rows = [g for g in (pic.get("hostile_groups") or []) + (pic.get("friendly_groups") or []) if _matches(g, needles)]
        rows += [s for s in th.get("sam_in_range") or [] if _matches(s, needles)]
        dead = [k for k in kills + list(pic.get("friendly_kills_this_mission") or []) if _matches(k, needles)]
        for r in rows[:3]:
            lines.append("Alive and showing: " + r["call"])
        for k in dead[:3]:
            lines.append("Killed this mission: %s (%s)." % (k.get("target"), k.get("confidence")))
        if not rows and not dead:
            lines.append("No %s showing in the picture (not showing does not prove it was destroyed)." % needles[0].upper())
    if own and own.get("type") and not any("is flying a" in l for l in lines):
        lines.append("Prometheus' aircraft: %s." % own.get("type"))
    seen, out = set(), []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def qclasses(ask):
    """Families that the reply check uses to decide whether an 'unknown' claim contradicts present data."""
    return families(ask)


DIET_PROMPT = """You are "Athena", a human AWACS/GCI controller on the radio with one pilot, callsign "Prometheus", in DCS World.
GROUNDING (most important):
- The PICTURE (JSON) is your only source of facts. Say only names, numbers, bearings and ranges that are in it.
- Before saying "unknown", "unavailable", "not connected" or "nothing showing", look again: if the field has a value, give the value. ownship.fuel.spoken is the fuel; airbases.nearest_friendly is the friendly airfield list; air_traffic is the friendly air traffic; connected true means the picture is live.
- Never invent clearances ("cleared to land/for takeoff"), runways, winds, altimeter, times or units. You are not a tower: for landing give the airfield call and its traffic, then "your discretion".
- Never read JSON key names, codes or ids aloud.
- Every unit in contacts and totals is alive. Dead only from my_kills_this_mission, recent_kills_and_losses or a shot result kill. confidence confirmed = say it plainly; likely = "likely".
- If ownship is null you have no position for Prometheus: no range or bearing answers; totals and kills are still fine.
- data_health problems matter only for the stream named; never let them hide data that is present.
HOW TO ANSWER:
- Start "Prometheus, Athena,". Answer only what was asked, one or two short sentences, no lists, no markdown, numbers written for speech.
- Positions: read the ready "call" line of the group or airbase verbatim, one sentence per call; never a range or clock without the bearing; nearest matching group only unless asked for all.
- Air picture = picture_top3 verbatim, else air_picture_empty_call. Ground = ground_targets_within_20nm calls, else ground_empty_call.
- Kill totals = my_kills_this_mission (kill_tallies.me). Weapons left = ownship.loadout counts + gun_rounds. Aircraft type = ownship.type.
- SAMs/RWR: cockpit.rwr threats when fresh; with threats.rwr_source grpc_inferred read threats.sam_in_range and threats.missiles_in_flight calls verbatim ("could be" for range-only rows); none = no SAM in range and no missile inbound that I can see.
- Traffic means aircraft. "bandit" only for enemy aircraft; enemy ground units are enemy armor, SAM site, triple A, enemy trucks; friendly aircraft are "friendly".
- If splash_to_announce or rwr_alert_to_announce is present, say it first.
- Acknowledgements, reports and chit-chat: one short natural sentence, no picture.
- Development notes only when a DEV NOTE hint is present: "Prometheus, Athena, copy, noted for development: <restatement>."
"""

FACTS_NOTE = ("This turn the PICTURE is replaced by FACTS: ready-made lines computed from the live picture. Answer "
              "using ONLY these lines; read them as written; add no other names or numbers. If they do not answer the "
              "question, say what they do show or ask the pilot to say again.\nFACTS:\n")


def transform(variant, messages, ask, pic, stats):
    """Rewrite the chat messages for a variant (at the model boundary; the live path built them)."""
    if variant in ("baseline", "medium", "mini", "nohist"):
        return messages
    out = []
    for m in messages:
        content = m.get("content") or ""
        if m["role"] == "system" and content.startswith("PICTURE "):
            if variant == "slim":
                slim = json.dumps(slim_picture(pic, ask), ensure_ascii=True, default=str, separators=(",", ":"))
                stats["picture_chars"], stats["sent_chars"] = len(content) - 8, len(slim)
                content = "PICTURE " + slim
            elif variant == "facts":
                facts = "\n".join("- " + line for line in fact_lines(pic, ask))
                stats["picture_chars"], stats["sent_chars"] = len(content) - 8, len(facts)
                content = FACTS_NOTE + facts
        elif m["role"] == "system" and content == sc.PROMPT and variant == "diet":
            content = DIET_PROMPT
        out.append(dict(m, content=content))
    return out


# ------------------------------------------------------------------ runner (the real model)
def _api_key():
    """OPENAI_API_KEY read by name from ~/.hermes/.env (as the launcher does); never printed."""
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as handle:
            for line in handle:
                if line.startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _budget(add=0):
    data = {"calls": 0}
    if os.path.exists(BUDGET):
        with open(BUDGET) as handle:
            data = json.load(handle)
    if add:
        data["calls"] += add
        os.makedirs(os.path.dirname(BUDGET), exist_ok=True)
        with open(BUDGET, "w") as handle:
            json.dump(data, handle)
    return data["calls"]


class Boundary:
    """Wraps the live backend: applies the variant to the messages, counts and times every call."""
    def __init__(self, inner, variant):
        self.inner, self.variant = inner, variant
        self.label = getattr(inner, "label", "openai-api")
        self.model = getattr(inner, "model", None)
        self.item = None
        self.calls = []

    def answer_by(self, messages, deadline):
        ask, pic = self.item["ask"], self.item["picture"]
        stats = {}
        messages = transform(self.variant, messages, ask, pic, stats)
        stats["prompt_chars"] = sum(len(m.get("content") or "") for m in messages)
        t0 = time.monotonic()
        if hasattr(self.inner, "answer_by"):
            text, meta = self.inner.answer_by(messages, deadline)
        else:
            text, meta = self.inner(messages, deadline=deadline), {"backend": "openai-api", "model": self.model}
        stats["model_s"] = round(time.monotonic() - t0, 3)
        n = 2 if meta.get("fallback_reason") and hasattr(self.inner, "answer_by") else 1
        _budget(n)
        stats["calls"] = n
        self.calls.append(dict(stats, meta=meta))
        return text, meta


def make_controller(variant, item):
    key = _api_key()
    ctl = sc.SimpleController(None, _Log(), model="gpt-5.4-mini", api_key=key, eye=_Eye(item["picture"]))
    if variant == "mini":
        inner = ctl.ask
        inner.__func__  # bound method; called with deadline
    else:
        effort = "medium" if variant == "medium" else "low"
        inner = CodexLLM(ctl.ask, model="gpt-6-luna", effort=effort, timeout=6.0, fallback_label="openai-api:gpt-5.4-mini")
        inner.credentials.get()  # what warm() does at adapter start, done synchronously here
    boundary = Boundary(inner, variant)
    ctl._llm = boundary
    return ctl, boundary


def cmd_build(args):
    """Eval data file: the recorded pictures + real asks/histories the set needs (from `extract` output)."""
    from .model_eval_set import ITEMS
    with gzip.open(args.candidates, "rt") as handle:
        cands = {c["id"]: c for c in json.load(handle)}
    data = {"pictures": {}, "real": {}, "routes": {}}
    for item in ITEMS:
        pic_id = item.get("pic", item["id"])
        data["pictures"][pic_id] = cands[pic_id]["picture"]
        if "pic" not in item:
            c = cands[item["id"]]
            data["real"][item["id"]] = {k: c[k] for k in ("ask", "history", "time", "recorded_reply", "recorded_model",
                                                          "recorded_llm_ms")}
        ask = item.get("ask") or cands[item["id"]]["ask"]
        history = item.get("history", cands[pic_id]["history"] if "pic" not in item else [])
        data["routes"][item["id"]] = route_today(ask, data["pictures"][pic_id], history)
    with gzip.open(args.out, "wt") as handle:
        json.dump(data, handle)
    print(collections.Counter(data["routes"].values()))
    for key, route in data["routes"].items():
        if route != "llm":
            print("NOT MODEL TODAY:", key, route)


def load_set(path):
    """Items (model_eval_set.ITEMS) joined with their recorded pictures, asks and histories."""
    from .model_eval_set import ITEMS
    with gzip.open(path, "rt") as handle:
        data = json.load(handle)
    items = []
    for spec in ITEMS:
        item = copy.deepcopy(spec)
        real = data["real"].get(item["id"])
        if real:
            item.update(ask=real["ask"], history=real["history"], recorded_reply=real["recorded_reply"],
                        recorded_model=real["recorded_model"], pic=item["id"], src="real")
        else:
            item.setdefault("history", [])
            item["src"] = "written"
        item["route_today"] = data["routes"].get(item["id"])
        item["picture"] = data["pictures"][item["pic"]]
        items.append(item)
    return items, data


def cmd_run(args):
    items, _ = load_set(args.set)
    if args.ids:
        wanted = args.ids.split(",")
        items = [i for i in items if i["id"] in wanted]
    elif args.first:
        items = items[:args.first]
    cache = {}
    with open(args.results, "a") as out:
        for rep in range(args.repeat):
            for item in items:
                used = _budget()
                if used + 2 > MAX_CALLS:
                    print("BUDGET STOP at %d calls" % used)
                    return
                key = item["pic"]
                if key not in cache:
                    cache[key] = item["picture"]
                ctl, boundary = make_controller(args.variant, item)
                if args.variant != "nohist":  # nohist: same as baseline without the chat history
                    ctl.history.extend(tuple(h) for h in item.get("history") or [])
                boundary.item = item
                t0 = time.monotonic()
                reply = ctl.exchange(_request(item["ask"]))
                total = round(time.monotonic() - t0, 3)
                call = boundary.calls[-1] if boundary.calls else {}
                row = {"id": item["id"], "variant": args.variant, "rep": rep + args.rep_start, "ask": item["ask"],
                       "reply": reply.get("speech"), "route": reply.get("route"), "model": reply.get("model"),
                       "total_s": total, "model_s": call.get("model_s"), "calls": call.get("calls", 0),
                       "meta": call.get("meta"), "prompt_chars": call.get("prompt_chars"),
                       "picture_chars": call.get("picture_chars"), "sent_chars": call.get("sent_chars"),
                       "at": time.strftime("%H:%M:%S")}
                out.write(json.dumps(row) + "\n")
                out.flush()
                print("%-22s %-8s %5.2fs %-6s %s" % (item["id"], args.variant, call.get("model_s") or 0,
                                                    reply.get("route"), (reply.get("speech") or "")[:150]), flush=True)
                time.sleep(args.pause)
    print("budget used: %d" % _budget())


# ------------------------------------------------------------------ scorer (mechanical) + reply check
# Shared pure number parsing; historical scoring tolerances below are unchanged.
words_to_numbers = sc.words_to_numbers
picture_numbers = sc.picture_numbers


INVENT = re.compile(r"\b(cleared (to land|for (take ?off|landing|the approach))|runway \w+|winds?\b|altimeter|qnh|"
                    r"tower frequency|squawk)", re.I)
FALSE_UNKNOWN = re.compile(r"\b(unknown|unavailable|not available|no (connected )?picture|not connected|disconnected|"
                           r"(can't|cannot|unable to) (confirm|see|tell)|no data|no \w+( \w+)? (showing|available|data)|"
                           r"don't have|do not have|nothing showing|not showing)\b", re.I)
KEYNAME = re.compile(r"(no position call|[a-z]+_[a-z_]+|\bownship\b|\bpicture_top3\b|\bjson\b)", re.I)
NUM_EXEMPT = {1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 0.0}


def _norm(text):
    return re.sub(r"[\u2018\u2019]", "'", (text or "")).lower()


def ungrounded_numbers(reply, pic):
    body = re.sub(r"\b(prometheus|athena)\b", "", reply or "", flags=re.I)
    body = re.sub(r"\b[A-Za-z]+-?\d+[A-Za-z]*\b", "", body)  # type names: SA-2, T-55, F-4E, MQ-9
    pnums = picture_numbers(pic)
    bad = []
    for n in words_to_numbers(body):
        if float(n) in NUM_EXEMPT or float(n) in pnums:
            continue
        if any(abs(float(n) - p) <= max(1.0, 0.02 * p) for p in pnums if p > 50):
            continue
        bad.append(n)
    return bad


def score_reply(item, reply):
    """(verdict, reasons): verdict right | wrong | sayagain | manual. Mechanical checks first."""
    truth = item.get("truth") or {}
    text = _norm(reply)
    reasons, flags = [], set()
    if not reply or reply.strip().endswith("say again.") and len(reply) < 45:
        return "sayagain", ["say again"], flags
    for group in truth.get("must", []):
        alts = group if isinstance(group, list) else [group]
        if not any(re.search(a, text, re.I) for a in alts):
            reasons.append("missing: %s" % "|".join(alts))
            flags.add("missing")
    for pattern in truth.get("must_not", []):
        if re.search(pattern, text, re.I):
            reasons.append("contradiction: /%s/" % pattern)
            flags.add("wrong_fact")
    if truth.get("data_present", True) and FALSE_UNKNOWN.search(text) and not any(
            re.search(a, text, re.I) for a in truth.get("unknown_ok", [])):
        reasons.append("false unknown: '%s'" % FALSE_UNKNOWN.search(text).group(0))
        flags.add("false_unknown")
    if INVENT.search(text) and not any(re.search(a, text, re.I) for a in truth.get("invent_ok", [])):
        reasons.append("invention: '%s'" % INVENT.search(text).group(0))
        flags.add("invention")
    if KEYNAME.search(reply or "") and not re.search(r"[a-z]+_[a-z_]+", truth.get("keyname_ok", "^$")):
        reasons.append("key name: '%s'" % KEYNAME.search(reply).group(0))
        flags.add("invention")
    bad = ungrounded_numbers(reply, item["picture"])
    bad = [n for n in bad if not any(str(int(n)) in a for a in truth.get("numbers_ok", []))]
    if bad:
        reasons.append("numbers not in picture: %s" % bad)
        flags.add("invention")
    if reasons:
        return "wrong", reasons, flags
    if truth.get("manual"):
        return "manual", ["manual read: " + truth["manual"]], flags
    return "right", [], flags


def reply_check(reply, pic, ask):
    """Variant (c): a generic mechanical gate after generation. Returns a rejection reason or None.
    Uses only the picture (no per-item truth): grounded numbers, no false 'unknown', no forbidden words."""
    text = _norm(reply)
    if INVENT.search(text):
        return "forbidden word: " + INVENT.search(text).group(0)
    if KEYNAME.search(reply or ""):
        return "key name"
    bad = ungrounded_numbers(reply, pic)
    if bad:
        return "number not in picture: %s" % bad
    unknown = FALSE_UNKNOWN.search(text)
    spoken = re.sub(r"\b(prometheus|athena|test pilot)\b", "", text)
    if unknown and not words_to_numbers(re.sub(r"\b[a-z]+-?\d+[a-z]*\b", "", spoken)):  # a partial answer with a value passes
        own = pic.get("ownship") or {}
        fuel = own.get("fuel") or {}
        topics = (  # (ask pattern, the picture has the answer)
            (r"\b(fuel|gas|bingo|joker)\b", fuel.get("total_lb") is not None or fuel.get("internal_pct") is not None),
            (r"\b(air ?fields?|air ?bases?|home ?plate|rtb|land|nelli'?s|creech|divert|vectors?)\b",
             bool((pic.get("airbases") or {}).get("nearest_friendly"))),
            (r"\b(friendl\w*|traffic|wing ?m[ae]n|a-10s?|awacs)\b",
             bool((pic.get("air_traffic") or {}).get(own.get("coalition") or "blue"))),
            (r"\b(carrying|weapons?|loadout|mavericks?|rounds|ammo|stores)\b", bool((own.get("loadout") or {}).get("available"))),
            (r"\b(what am i flying|aircraft am i|airplane am i)\b", bool(own.get("type"))),
        )
        connected = pic.get("connected", pic.get("available", True))
        if connected and re.search(r"not connected|disconnected|no (connected )?picture", text):
            return "claims not connected while connected"
        for pattern, present in topics:
            if present and re.search(pattern, ask or "", re.I):
                return "claims unknown while the picture has it (%s)" % pattern.split("|")[0].strip("\\b(")
    return None


def summarize(rows, items, use_check=False):
    by_id = {i["id"]: i for i in items}
    out = collections.Counter()
    lat, per = [], []
    for row in rows:
        item = by_id[row["id"]]
        reply = row["reply"] or ""
        rejected = None
        if use_check and row.get("route") == "llm":
            rejected = reply_check(reply, item["picture"], item["ask"])
        verdict, reasons, flags = score_reply(item, sc.FALLBACK if rejected else reply)
        out[verdict] += 1
        for f in flags:
            out[f] += 1
        if row.get("model_s"):
            lat.append(row["model_s"])
        out["calls"] += row.get("calls") or 0
        if row.get("meta", {}) and row["meta"].get("fallback_reason"):
            out["fallbacks"] += 1
        per.append((row, verdict, reasons, rejected))
    lat.sort()
    stats = {"n": len(rows), "right": out["right"], "manual": out["manual"], "wrong": out["wrong"],
             "sayagain": out["sayagain"], "false_unknown": out["false_unknown"], "invention": out["invention"],
             "wrong_fact": out["wrong_fact"], "missing": out["missing"], "calls": out["calls"],
             "fallbacks": out["fallbacks"],
             "median_s": round(statistics.median(lat), 2) if lat else None,
             "p95_s": round(lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))], 2) if lat else None}
    return stats, per


def cmd_score(args):
    items, _ = load_set(args.set)
    rows = [json.loads(l) for l in open(args.results)]
    if args.variant:
        rows = [r for r in rows if r["variant"] == args.variant]
    manual = {}
    if args.manual and os.path.exists(args.manual):
        manual = json.load(open(args.manual))
    stats, per = summarize(rows, items, args.check)
    for row, verdict, reasons, rejected in per:
        key = "%s|%s" % (row["id"], row["reply"])
        if verdict == "manual" and key in manual:
            verdict = manual[key]
        print("%-6s %-22s %s%s\n       ask: %s\n       reply: %s%s" % (
            verdict.upper(), row["id"], "REJECTED(%s) " % rejected if rejected else "", "; ".join(reasons),
            row["ask"], row["reply"], ""))
    print(json.dumps(stats))


def cmd_table(args):
    """One line per variant (first repetition of each item only), the reply-check rows, and the repeats."""
    items, _ = load_set(args.set)
    rows = [json.loads(l) for l in open(args.results)]
    manual = json.load(open(args.manual)) if args.manual and os.path.exists(args.manual) else {}
    print("| variant | items | right | right % | wrong | false unknown | invention | wrong fact | say again | manual | median s | p95 s | calls |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    order = ["baseline", "check", "slim", "facts", "diet", "nohist", "medium", "mini"]
    seen = sorted({r["variant"] for r in rows}, key=lambda v: order.index(v) if v in order else 99)
    for variant in seen[:1] + ["check"] + seen[1:]:
        base = "baseline" if variant == "check" else variant
        first = [r for r in rows if r["variant"] == base and r.get("rep", 0) == 0]
        stats, per = summarize(first, items, use_check=variant == "check")
        right = stats["right"]
        for row, verdict, _, rejected in per:  # resolve manual reads
            key = "%s|%s" % (row["id"], sc.FALLBACK if rejected else row["reply"])
            if verdict == "manual" and manual.get(key) == "right":
                right += 1
            elif verdict == "manual" and manual.get(key) == "wrong":
                stats["wrong"] += 1
            elif verdict == "manual":
                print("UNREAD MANUAL:", key)
        unresolved = stats["manual"]
        ids = {r["id"] for r in first}
        same = ""
        if len(ids) < len(items):  # a subset: baseline on the same items, for a fair comparison
            bstats, bper = summarize([r for r in rows if r["variant"] == "baseline" and r.get("rep", 0) == 0
                                      and r["id"] in ids], items)
            bright = bstats["right"] + sum(1 for row, v, _, _ in bper if v == "manual"
                                           and manual.get("%s|%s" % (row["id"], row["reply"])) == "right")
            same = " (baseline on the same %d: %d right, %d wrong, median %ss)" % (len(ids), bright, bstats["wrong"] + sum(
                1 for row, v, _, _ in bper if v == "manual" and manual.get("%s|%s" % (row["id"], row["reply"])) == "wrong"),
                bstats["median_s"])
        print("| %s%s | %d | %d | %.0f%% | %d | %d | %d | %d | %d | %d | %s | %s | %d |" % (
            variant, same, stats["n"], right, 100.0 * right / max(1, stats["n"]), stats["wrong"], stats["false_unknown"],
            stats["invention"], stats["wrong_fact"], stats["sayagain"], unresolved, stats["median_s"], stats["p95_s"],
            0 if variant == "check" else sum(r.get("calls") or 0 for r in rows if r["variant"] == base)))
    from .model_eval_set import FLIGHT_FAILURES
    print()
    for fid in FLIGHT_FAILURES:
        reps = [r for r in rows if r["variant"] == "baseline" and r["id"] == fid]
        verdicts = [summarize([r], items)[1][0][1] for r in reps]
        print("repeat %s: %d/%d right  %s" % (fid, verdicts.count("right"), len(verdicts), verdicts))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract")
    p.add_argument("out")
    p = sub.add_parser("build")
    p.add_argument("candidates")
    p.add_argument("out")
    p = sub.add_parser("run")
    p.add_argument("set")
    p.add_argument("results")
    p.add_argument("--variant", default="baseline", choices=("baseline", "slim", "facts", "diet", "medium", "mini", "nohist"))
    p.add_argument("--ids")
    p.add_argument("--first", type=int)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--rep-start", type=int, default=0)
    p.add_argument("--pause", type=float, default=1.0)
    p = sub.add_parser("table")
    p.add_argument("set")
    p.add_argument("results")
    p.add_argument("--manual")
    p = sub.add_parser("score")
    p.add_argument("set")
    p.add_argument("results")
    p.add_argument("--variant")
    p.add_argument("--check", action="store_true")
    p.add_argument("--manual")
    args = parser.parse_args(argv)
    {"extract": cmd_extract, "build": cmd_build, "run": cmd_run, "score": cmd_score, "table": cmd_table}[args.cmd](args)


if __name__ == "__main__":
    main()
