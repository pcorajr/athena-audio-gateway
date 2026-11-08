"""Simple conversational radio controller: one rich live picture + one LLM call per transmission.

Design (user direction 2026-11-07): a capable model reads Athena's live data and talks like a
human GCI controller. Minimal rules: the picture is the only source of facts; everything else is
the model's judgement. With an Athena Eye attached (ATHENA_EYE_HOST), the picture comes from the
in-memory DCS-gRPC tracker in eye.py; otherwise from the Athena state service (legacy path).
"""
from collections import Counter, deque
import http.client
import json
import re
import threading
import time
import unicodedata

from .contract import MAX_SPEECH_CHARS, response, speaker_matches
from .eye import KILL_REPORT, _QUESTION, words

PROMPT = """You are "Athena", an experienced human AWACS/GCI controller on the radio with one pilot, callsign "Prometheus", flying in DCS World.

Every turn you get a live PICTURE (JSON) from the Athena Eye tracking system. It is your only source of truth: ownship (Prometheus' aircraft), contact groups already computed from Prometheus (bearing, range in nautical miles, altitude, aspect, closure, and "clock" = clock position relative to Prometheus' nose with ahead/behind/left/right), nearby airbases, Prometheus' own shots and their results, recent kills and losses, recent events and alive totals. data_age_s tells you how old the data is; if it is old or the picture is not connected, say so briefly and still give what you have. Never invent contacts, kills or numbers that are not in the picture.

Facts that matter:
- Optional memory is mission history, not a replacement for the live Eye: takeoff airfield, weapons used, observed kill/loss timeline and labelled disagreements. Never turn an unclear audit verdict into a confirmed fact.
- Every unit listed in contacts and totals is ALIVE. Something is dead or killed ONLY if it appears in recent_kills_and_losses, my_kills_this_mission or a shot result says kill. In my_kills_this_mission, confidence confirmed = a kill: say it plainly ("splash one T-55"), never "likely", even when inferred is true. Only confidence likely, or a recent_kills_and_losses row "no longer showing, likely destroyed", is "likely destroyed".
- Mission kill totals come from my_kills_this_mission (cumulative for the whole mission, every row is one kill) or the memory section; recent_* lists are short windows, never totals.
- Names: say "bridge" for MOST/BRIDGE scenery codes, never read internal codes or numeric weapon ids aloud.
- my_shots: result pending = weapon still flying or not known yet, hit = hit but no kill reported, kill = target destroyed, likely kill = target stopped showing long after the shot, miss = no hit within the window. weapon is the weapon used (from the shot event or the loadout change). intended_target is a guess (DCS does not report the locked target); say "looks like" if you rely on it.
- ownship.loadout is what Prometheus has left: weapons by type with counts plus gun_rounds. If loadout is not available, say so with the reason in a few words.
- ownship.fuel is the fuel answer: total_lb in pounds (approximate when its source says so), else internal_pct percent; known false = "fuel unknown". Never report zero pounds unless total_lb is 0.
- Damage / "I'm hit": ownship.damage hit events and getLife/getLife0 drops are primary evidence. Read the damage call. Cockpit warning lights, engine RPM changes and fuel-loss trend are secondary clues only. No warnings NEVER means no damage; acknowledge the pilot even if telemetry is unavailable. A fuel loss trend alone is not proof of a leak.
- event_stream.state other than "live" means shot and kill reports may be missing; mention it only when asked about shots or kills.
- Bearings are magnetic when the picture says so, otherwise true; do not mention it unless asked.
- cockpit is live data from Prometheus' own aircraft (DCS Export.lua, about five times a second): config (gear, flaps, speedbrake, hook), flight (indicated airspeed, ground speed, altitude above ground, G, angle of attack), payload by station, rwr threats, locked_target and warning_lights. Prefer it over other fields when cockpit.fresh is true. If the cockpit is not available or not fresh and the question needs it, say "no cockpit data" in a few words.
- RWR ("what's painting me", "RWR", "spike", "am I locked"): give each cockpit.rwr threat with its type, what it is doing (searching, tracking, locked, missile launch) and its clock, locks and launches first, e.g. "SA-11 locked on you at your three o'clock." Empty threats with a fresh cockpit = "RWR is clean, nothing painting you." If threats.rwr_source is grpc_inferred (this jet exports no RWR, e.g. the F-4E), answer from threats.missiles_inbound and threats.sam_in_range calls instead ("SAM SA-11 could be tracking you, ..."); nothing listed = "no RWR feed from this jet, no SAM in range and no missile inbound that I can see."
- "What am I locked on" / "what's my target": read cockpit.locked_target.call (and the unit type and side if present). locked_target null = "nothing locked".
- Fuel state: ownship.fuel (total_lb pounds, else internal_pct percent).
- Gear / configuration: cockpit.config (gear down, up or in transit; flaps percent; speedbrake).
- If the picture has rwr_alert_to_announce, START the answer with it, before anything else, e.g. "Prometheus, Athena, spike, SA-11 at your three o'clock." Then answer the question.

How to talk:
- Answer ONLY what was asked. Acknowledgements, intent calls, radio checks, readbacks and pilot reports ("acknowledge my last transmission", "copy", "egressing", "going to investigate", "eyes on target", "wheels up", "airborne", "weapons hot", "target destroyed") get ONE short sentence: acknowledge, plus at most the ONE target being worked (working_target.call) when it helps. Never a picture, never other groups.
- Give contacts only when asked (picture, threats, bogey dope, "any ...", mission status) or when a hostile aircraft or SAM is within twenty miles that you have not called in the last exchanges.
- An air picture or bogey dope is the three closest hostile air groups WITHIN FIFTY nautical miles only: read picture_top3 verbatim, both sentences of EVERY call. If empty say "no bandits within fifty miles". Never append extra-group counts. Ground pictures use the nearest requested ground groups instead.
- Traffic means AIRCRAFT, including helicopters, unless explicitly asked for ground/vehicles. Blue/friendly traffic comes from air_traffic, never friendly ground groups. For ATC use the selected airbase's traffic field, including aircraft on runways/taxiways within two miles and arriving/departing aircraft within five miles. The field itself is not traffic.
- Never say "overhead" unless that contact has overhead_allowed true (within three nautical miles). Nineteen miles is NOT overhead.
- A bearing or position request for a kind of target ("bearing to enemy armor", "where is the SAM") = the NEAREST matching group only, unless the pilot says "all" or "every".
- Calm, natural, clear and short: usually one or two sentences. Summarise groups instead of listing everything.
- Start with "Prometheus, Athena," (or just "Prometheus," for a quick follow-up).
- Positional calls: every group and airbase in the picture has a ready "call" line in the required order, BRAA first then clock (bearing, range, altitude or ground target, then the clock). Use that call line VERBATIM; never give a range or a clock position without the bearing. Format for reference:
  Air: "Bandit Su-27 two-ship, bearing zero three eight, twelve miles, angels fifteen. At your ten o'clock, high."
  Ground: "Enemy armor, bearing zero three eight, three miles, ground target. Target is at your eleven o'clock."
- Weapons: "what do I have left" = the loadout counts by weapon plus gun rounds. "What did I use on that target" / "what killed that" = the weapon from my_shots or my_kills_this_mission.
- If the picture has splash_to_announce, start the answer with it once, e.g. "Prometheus, Athena, splash one T-72." (keep "likely" only when it says so).
- "bandit" only for enemy aircraft. Enemy ground units are "enemy armor", "SAM site", "triple A", "enemy trucks" and so on. Friendly aircraft are "friendly".
- Air threats first (nearest and hot first), then SAMs, then ground.
- No markdown, no lists, no parentheses, no abbreviations with periods. Write numbers so a text-to-speech voice reads them well.
- Radio checks and chit-chat: answer naturally ("Prometheus, Athena, loud and clear.").
- Follow-up questions refer to the previous exchanges.
- Development notes: the pilot is also the developer of Athena. ONLY when a "DEV NOTE" system hint is present (explicit phrasing such as "feature request", "note for development", "bug report", "Athena should be able to ...", "add a feature"), reply briefly "Prometheus, Athena, copy, noted for development: <one-line restatement>." Without that hint it is a normal radio call: kill reports ("target destroyed", "splash", "armor dead") and chat ("switching missions") are acknowledged normally, never noted for development.
- Basic ATC is fine: for takeoff, landing or approach requests at an airfield, check the picture for aircraft near that field (within about five miles and below about five thousand feet) and say "no traffic" or call the traffic with the same BRAA + clock format, then approve ("cleared for takeoff", "cleared to land"). Don't invent runway numbers, winds or other data that is not in the picture.
"""

SENTENCE_END = re.compile(r"(?<!\d)[.!?](?=\s|$)")
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp", "Zs"})
_JUNK = re.compile(r"^[\W_]*$")
PART_CHARS = 420
FALLBACK = "Prometheus, Athena, say again, I had a problem on my end."

# Question focus (round 3): a one-line hint telling the model which picture section answers the question.
FOCUS_RULES = (
    ("lock", re.compile(r"\b(what am i locked|what('?s| is| do i have) (my )?(locked|lock|target)|my (lock|target)\b|"
                        r"locked target|what have i got locked|what('?s| is) on my radar)", re.I),
     "cockpit.locked_target (call, unit, side); null = nothing locked"),
    ("rwr", re.compile(r"\b(rwr|paint\w*|spik\w*|radar warning|threat warning|nails|mud spike|"
                       r"(locking|locked on|lock on|tracking|targeting|looking at) me\b|am i (being )?locked\b(?! on)|"
                       r"anyone locking|launch|missile inbound|threats? on me)", re.I),
     "cockpit.rwr.threats (type, signal, clock); empty with fresh cockpit = RWR clean; when threats.rwr_source is "
     "grpc_inferred use threats.missiles_inbound and threats.sam_in_range"),
    ("fuel", re.compile(r"\b(fuel|bingo|joker|gas)\b", re.I),
     "ownship.fuel (total_lb pounds, else internal_pct percent; known false = fuel unknown)"),
    ("gear", re.compile(r"\b(gear|wheels|flaps|speed ?brakes?|hook|configured|configuration)\b", re.I),
     "cockpit.config"),
    ("flight", re.compile(r"\b(airspeed|indicated|ground ?speed|how fast|altitude|agl|above ground|g load|"
                          r"how many g|angle of attack|aoa|mach|vertical speed)\b", re.I),
     "cockpit.flight"),
    ("loadout", re.compile(r"\b(what do i have left|have left|weapons? (left|remaining|status)|stores|winchester|"
                           r"ammo|rounds left|loadout)\b", re.I),
     "ownship.loadout (or cockpit.payload)"),
)


def focus_of(text):
    """Which cockpit/picture sections the question is about (list of names), in rule order."""
    return [name for name, pattern, _ in FOCUS_RULES if pattern.search(text or "")]


def focus_hint(text):
    hints = [hint for name, pattern, hint in FOCUS_RULES if pattern.search(text or "")]
    return ("FOCUS: answer from " + "; ".join(hints)) if hints else None


# Call classes (round 4 verbosity + dev-note fixes): deterministic hints, the model follows them.
DEV_NOTE = re.compile(r"\b(feature request|note for (the )?develop\w*|development note|dev note|bug report|"
                      r"athena should (be able to|have|know|say|tell)|add (a |the |an )?feature|log (a |this as a )?bug)\b",
                      re.I)
ACK = re.compile(r"\b(copy|copies|roger|wilco|egress\w*|going to investigate|investigating|eyes on|wheels up|"
                 r"airborne|tally|visual|in hot|off target|rtb|returning to base|heading home|thanks|thank you|"
                 r"understood|good copy|switching (missions?|frequenc\w+|radios?)|stand ?by|checking in|rolling in|"
                 r"pushing|commencing|engaging|in the weeds|on my way|proceeding|acknowledged?)\b", re.I)
REQUEST = re.compile(r"(\?|\b(picture|bogey dope|bearing|vector|where|what|which|how|say|give|any|status|threats?|"
                     r"declare|request|fuel|left|range|braa|bra|check|are there|is there|tell me|need)\b)", re.I)
PICTURE_ASK = re.compile(r"\b(picture|bogey dope|what do you see|what's out there|whats out there|threats? around|mission status)\b", re.I)
GROUND_ASK = re.compile(r"\b(ground|vehicles?|armor|infantry|trucks?|tanks?|sams?|triple a)\b", re.I)
TRAFFIC_ASK = re.compile(r"\b(traffic|blue only|only blue)\b", re.I)
ATC_ASK = re.compile(r"\b(take ?off|landing|land|approach|taxi\w*|runway)\b", re.I)
DAMAGE_ASK = re.compile(r"\b(i('?m| am| got| was| have been) hit|hit me|taking hits|damage report|my damage|"
                        r"i (took|have|sustained) damage|i('?m| am) damaged|am i damaged|"
                        r"damage to (me|my (aircraft|jet|plane|ownship)))\b", re.I)
CONFIRM_ASK = re.compile(r"\bconfirm\b", re.I)
KILL_ASK = re.compile(r"\b(kills?|killed|splash|destroyed|taken out|dead|eliminated|neutrali[sz]ed)\b", re.I)
# Round 6 (radio talk): deterministic acknowledgements for attack-flow intent calls.
INTENT_ACKS = (
    (re.compile(r"\b(time to impact|seconds? (to|till|until|til) impact|impact in)\b", re.I), "copy."),
    (re.compile(r"\babort\w*\b", re.I), "copy, abort."),
    (re.compile(r"\bweapons? (hot|free)\b", re.I), "copy, weapons hot."),
    (re.compile(r"\bweapons? (cold|safe)\b", re.I), "copy, weapons cold."),
    (re.compile(r"\b(running in|run in|in hot|rolling in|commencing (the |my )?(attack|run))\b", re.I), "copy, running in."),
    (re.compile(r"\b(pickle\w*|bombs? away|rifle|fox (one|two|three)|magnum|missiles? away|weapons? away|"
                r"rockets? away)\b", re.I), "copy, weapon away."),
    (re.compile(r"\b(off target|coming off)\b", re.I), "copy, off target."),
)
CONFIRM_TAG = re.compile(r"[,.]?\s*\bconfirm\b[.?!]*\s*$", re.I)
GENUINE_QUESTION = re.compile(r"\b(what|how|when|where|which|who|did|does|is|are|any)\b", re.I)
IMPACT_ASK = re.compile(r"\b(bomb impact|weapon impact|impact|did (it|that) hit|any hits?)\b", re.I)
DEBRIEF_ASK = re.compile(r"\b(debrief\w*|mission (end(ed|ing)?|complete|over|summary|report|briefing)|"
                         r"end(ing)? (of )?(the |this |my )?mission|provide (a |the |me a )?(brief|briefing|summary)|"
                         r"(give|send) me (a |the )?(brief|briefing|summary|debrief))\b", re.I)
TOTAL_ASK = re.compile(r"\b(total|tally|so far|all (my |the )?kills|this mission|overall|how many kills)\b", re.I)
REMAIN_ASK = re.compile(r"\b(remain\w*|left|still (alive|there|up)|what('?s| is) left)\b", re.I)
WINGMAN_ASK = re.compile(r"\b(wing ?m[ae]n|wingie|dash two|number two|my two|friendl\w*)\b", re.I)
CATEGORY_ASK = (
    (re.compile(r"\b(air ?defen[cs]es?|sams?|triple a|aaa|flak|shilka|strela|manpads?)\b", re.I), ("AAA", "SAM")),
    (re.compile(r"\b(armou?r|tanks?)\b", re.I), ("armor",)),
    (re.compile(r"\b(artillery|arty|mlrs|howitzers?)\b", re.I), ("artillery",)),
    (re.compile(r"\b(infantry|troops?|soldiers?)\b", re.I), ("infantry",)),
    (re.compile(r"\b(trucks?|vehicles?|transports?)\b", re.I), ("ground vehicle",)),
    (re.compile(r"\b(bridges?|buildings?|structures?)\b", re.I), ("scenery", "static")),
)
CATEGORY_ALIASES = {"vehicles": "ground vehicle", "vehicle": "ground vehicle", "air defense": "SAM",
                    "aaa": "AAA", "sam": "SAM", "tank": "armor", "armour": "armor"}
CATEGORY_WORDS = {"armor": "enemy armor", "AAA": "triple A", "SAM": "SAM", "infantry": "enemy infantry",
                  "ground vehicle": "enemy vehicles", "scenery": "structures", "static": "structures",
                  "artillery": "enemy artillery", "train": "trains", "ship": "ships"}
# Categories that are never "ground units" for a generic "what remains" (air or map objects).
NOT_GROUND = {"fighter", "attack", "bomber", "helicopter", "transport", "tanker", "awacs", "aircraft", "air", "plane",
              "uav", "drone", "scenery", "static", "ship"}
NON_WEAPON = re.compile(r"(ALQ|LITENING|AAQ|\bpod\b|TGP|fuel tank|\btank\b|smoke)", re.I)
SCENERY_NAMES = ((re.compile(r"\bMOST\b|BRIDGE", re.I), "bridge"), (re.compile(r"HANGAR", re.I), "hangar"),
                 (re.compile(r"BUNKER", re.I), "bunker"), (re.compile(r"TOWER", re.I), "tower"))
BEARING_ASK = re.compile(r"\b(bearing|vector|heading to|where('s| is| are)?|braa|bra|steer)\b", re.I)
ALL_ASK = re.compile(r"\b(all|every|each|everything|list)\b", re.I)
CLASS_HINTS = {
    "dev_note": "DEV NOTE: explicit development note. Reply \"Prometheus, Athena, copy, noted for development: "
                "<one-line restatement>.\"",
    "kill_report": "PILOT KILL REPORT (a normal tactical call, NOT a development note): one short sentence. If "
                   "splash_to_announce is present say it, else \"copy, splash\" with the target type if known.",
    "ack": "ACK OR INTENT CALL: one short sentence. Acknowledge; add at most working_target.call when it helps. "
           "No picture, no other groups.",
    "picture": "AIR PICTURE: picture_top3 verbatim, the three nearest hostile air groups within fifty miles. "
               "Every bearing, range, angels, clock and high/low/level must survive. No extra-group counts.",
    "ground_picture": "GROUND PICTURE: nearest requested ground groups, at most three, call lines verbatim.",
    "traffic": "TRAFFIC: aircraft only, including helos, from air_traffic. Blue means blue coalition. "
               "Do not substitute ground units. Read the nearest aircraft call lines verbatim.",
    "atc": "ATC: check the requested airbase's traffic.count and traffic.aircraft, not capped friendly_groups. "
           "Include on-field aircraft and arrivals/departures. Never say no traffic when count is nonzero.",
    "damage": "DAMAGE: acknowledge the pilot. Use ownship.damage events and life percentage first, "
              "cockpit warnings/RPM/fuel-loss trends second. No warnings is not evidence of no damage.",
    "kill_confirm": "KILL CONFIRMATION REQUEST, not a pilot kill report: answer from my_kills_this_mission "
                    "and my_shots_last_15_min. No air picture. Do not upgrade likely kills merely on a question.",
    "intent": "INTENT CALL: short acknowledgement only.",
    "impact": "IMPACT / RESULT CHECK: report the latest weapon's result only.",
    "loadout": "LOADOUT: remaining weapons and gun rounds only.",
    "bearing_one": "BEARING REQUEST: give ONLY the nearest matching group's call line (hostile_groups are nearest "
                   "first), not every group.",
}
SPEECH_CAP = 360  # whole answer, characters (~25 s of speech); trimmed at a sentence boundary
ACK_CAP = 220


def call_class(text, pic=None):
    """dev_note | kill_report | ack | picture | bearing_one | None for one pilot transmission."""
    t = (text or "").strip()
    if DEV_NOTE.search(t):
        return "dev_note"
    if KILL_REPORT.search(t) and not _QUESTION.search(t):
        return "kill_report"
    target_damage = re.search(r"\bdamag\w*\b", t, re.I) and (
        re.search(r"\b(targets?|enemy|hostiles?|bandits?)\b", t, re.I) or GROUND_ASK.search(t))
    if DAMAGE_ASK.search(t) and not target_damage:
        return "damage"
    if intent_ack(t) and (not _QUESTION.search(t) or (CONFIRM_TAG.search(t) and not GENUINE_QUESTION.search(t))):
        return "intent"  # round 6: "running in ..., confirm" is an acknowledgement, not a kill check
    if IMPACT_ASK.search(t):
        return "impact"
    if CONFIRM_ASK.search(t) and KILL_ASK.search(t):
        return "kill_confirm"
    if "loadout" in focus_of(t) and not KILL_ASK.search(t):
        return "loadout"
    if ATC_ASK.search(t):
        return "atc"
    if TRAFFIC_ASK.search(t) and not GROUND_ASK.search(t):
        return "traffic"
    age = (pic or {}).get("last_attack_age_s")
    explicit = target_damage or focus_of(t) or BEARING_ASK.search(t) or PICTURE_ASK.search(t) or GROUND_ASK.search(t)
    if CONFIRM_ASK.search(t) and not explicit and age is not None and 0 <= age < 300:
        return "kill_confirm"
    if PICTURE_ASK.search(t):
        return "ground_picture" if GROUND_ASK.search(t) else "picture"
    if BEARING_ASK.search(t) and not ALL_ASK.search(t):
        return "bearing_one"
    if ACK.search(t) and not REQUEST.search(t):
        return "ack"
    return None


def spoken_target(name, category=None):
    """Radio-safe target name: scenery codes become words, unknowns become 'target'."""
    name = (name or "").strip().lstrip("- ").strip()
    for pattern, word in SCENERY_NAMES:
        if pattern.search(name):
            return word
    if not name or name.lower() == "unknown" or (category == "scenery" and re.search(r"[()_]", name)):
        return "structure" if category in ("scenery", "static") else "target"
    return name


def loadout_calls(own):
    """Compact remaining-stores call that fits one transmission (pods and tanks are not weapons)."""
    lo = (own or {}).get("loadout") or {}
    if not lo.get("available"):
        return None
    items = ["%s %s" % (w.get("weapon"), words(w["count"])) for w in lo.get("weapons", [])
             if w.get("count") and w.get("weapon") and not NON_WEAPON.search(w["weapon"])]
    rounds = lo.get("gun_rounds")
    if rounds:
        items.append("gun %s rounds" % words(rounds))
    return ["You have " + ", ".join(items) + "."] if items else ["Winchester, no weapons left."]


def intent_ack(text):
    for pattern, ack in INTENT_ACKS:
        if pattern.search(text or ""):
            return ack
    return None


def impact_calls(pic):
    shots = ((pic.get("my_shots_last_15_min") or {}).get("shots")) or []
    recent = [s for s in shots if s.get("age_s") is not None and s["age_s"] <= 120]
    if not recent:
        return ["Copy, no recent weapon release showing."]
    shot = min(recent, key=lambda s: s["age_s"])
    hit = shot.get("hit") or {}
    gone = shot.get("no_longer_showing") or []
    target = spoken_target(gone[0] if gone else hit.get("type") or (shot.get("intended_target") or {}).get("type"),
                           hit.get("category"))
    result = shot.get("result") or "pending"
    if result == "kill":
        return ["Good hit, splash one %s." % target]
    if result == "hit":
        return ["Hit observed on %s, no kill yet." % target]
    if result == "miss":
        return ["No hit observed."]
    if result.startswith("likely"):
        return ["Likely kill on %s, target no longer showing." % target]
    if shot.get("in_flight") or shot.get("weapon_in_flight"):
        return ["Copy, weapon still in flight, no result yet."]
    return ["Copy, no result yet."]


def requested_calls(klass, text, pic):
    """Render fixed-format air calls directly: a model/speech cap must not drop their second sentence."""
    if not pic.get("ownship"):
        return None
    if klass == "picture" and "picture_top3" in pic:
        return pic["picture_top3"] or [pic["air_picture_empty_call"]]
    if klass == "traffic" and "air_traffic" in pic:
        coal = ("blue" if re.search(r"\bblue\b", text, re.I) else
                "red" if re.search(r"\bred\b", text, re.I) else
                pic["ownship"].get("coalition") if re.search(r"\bfriendl\w*\b", text, re.I) else None)
        groups = (pic["air_traffic"].get(coal, []) if coal else
                  [g for rows in pic["air_traffic"].values() for g in rows])
        groups = sorted(groups, key=lambda g: g["range_nm"])
        return [g["call"] for g in groups[:3]] or ["No %saircraft showing within %s miles." % (
            (coal + " ") if coal else "", words(pic.get("contacts_within_nm", 80)))]
    if klass == "intent":
        return [intent_ack(text) or "copy."]
    if klass == "impact":
        return impact_calls(pic)
    if klass == "loadout":
        return loadout_calls(pic["ownship"])
    if klass == "damage":
        damage = pic["ownship"].get("damage") or {}
        recent = [r for r in damage.get("events", []) if r["age_s"] < 300]
        if recent:
            return [recent[-1]["call"]]
        return None  # the model can describe secondary clues, but cannot deny the pilot's report
    if klass == "kill_confirm" and "my_kills_this_mission" in pic:
        kills = pic["my_kills_this_mission"]
        if not kills:
            return ["No confirmed kills yet."]
        lines = []
        for confidence in ("confirmed", "likely"):
            counts = Counter(k["target"] for k in kills if k.get("confidence") == confidence)
            if counts:
                lines.append("%s %s." % ("Confirmed:" if confidence == "confirmed" else "Likely:",
                                          ", ".join("%s %s" % (words(n), t) for t, n in counts.items())))
        return lines
    return None


def cap_speech(answer, limit=SPEECH_CAP):
    """Trim an over-long answer at the last sentence end within `limit` (never mid-sentence)."""
    answer = (answer or "").strip()
    if len(answer) <= limit:
        return answer
    cut = 0
    for m in SENTENCE_END.finditer(answer):
        if m.end() > limit:
            break
        cut = m.end()
    if cut:
        return answer[:cut].strip()
    cut = answer.rfind(" ", 0, limit)
    return answer[:cut if cut > 0 else limit].rstrip(" ,;:") + "."


def _num(value):
    return None if value is None else round(value)


def clean(text):
    text = "".join(" " if unicodedata.category(c) in _UNSAFE else c for c in (text or "").replace("*", ""))
    return " ".join(text.split())


def split_parts(answer, limit=PART_CHARS):
    """First sentence alone (fast first audio), then the rest packed into parts of at most `limit` chars."""
    sentences, start = [], 0
    for m in SENTENCE_END.finditer(answer):
        sentences.append(answer[start:m.end()].strip())
        start = m.end()
    if answer[start:].strip():
        sentences.append(answer[start:].strip())
    parts = []
    for sentence in sentences:
        pieces = []
        while len(sentence) > limit:  # pathological run-on sentence: break on a word boundary
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        pieces.append(sentence)
        for piece in pieces:
            if len(parts) >= 2 and len(parts[-1]) + 1 + len(piece) <= limit:
                parts[-1] = parts[-1] + " " + piece
            elif piece:
                parts.append(piece)
    return parts


class SimpleController:
    # RadioServer: hold concurrent transmissions for this controller (answered in turn) instead of 503.
    queues = True

    def __init__(self, tools, transcript, *, model, api_key, base_url="https://api.openai.com/v1",
                 timeout=12.0, radius_nm=200.0, eye=None, llm=None, speaker="Prometheus,Player,Client"):
        self.tools, self.transcript, self.eye = tools, transcript, eye
        self.model, self._key = model, api_key
        self._host = base_url.split("//", 1)[1].split("/", 1)[0]
        self._path = "/" + base_url.split("//", 1)[1].split("/", 1)[1].rstrip("/") + "/chat/completions"
        self.timeout, self.radius_nm = timeout, radius_nm
        self.history = deque(maxlen=8)
        self.speaker = speaker
        self._llm = llm or self.ask
        self._turn = threading.Lock()  # one exchange at a time; later arrivals wait their turn
        self._waiting = 0
        self._waiting_lock = threading.Lock()
        self.empty_transcripts = 0  # whisper returned nothing usable (suppressed, counted)
        self._confirmed_kills = []
        self._pending_ack = None  # round 6: (abs_time, target) already read back to the pilot
        self.transmissions = 0
        # Duck-typed for server code that may consult a provider attribute.
        self.provider = self

    # ---------------------------------------------------------------- picture
    def picture(self):
        if self.eye is not None:
            pic = self.eye.picture()
            # memory6: optional light enrichment; the link owns a <=120 ms whole-exchange deadline.
            memory = getattr(self.eye, "memory", None)
            if memory is not None:
                section = memory.summary()
                if section is not None:
                    pic["memory"] = section
            return pic
        return self._state_service_picture()

    def _state_service_picture(self):
        """Legacy path: Athena state service (kept for fallback when no eye is configured)."""
        from athena.state_service_client import ClientQueryError
        from . import tools as tools_mod
        from .tools import _plain, category, geometry

        if not getattr(tools_mod, "_eye_lenient", False):
            strict = tools_mod.usable

            def lenient(frame):
                try:
                    return strict(frame)
                except ClientQueryError as error:
                    if str(error) != "evidence_expired":
                        raise
            tools_mod.usable, tools_mod._eye_lenient = lenient, True
        own = _plain(self.tools.read("ownship", {}).data)["ownship"]
        if own is None:
            return {"available": False, "reason": "Prometheus is not in an aircraft slot (no ownship)"}
        mine = own.get("coalition")
        enemy = "red" if mine == "blue" else "blue"
        cap = min(100, self.tools.config.query_limits.max_rows)
        pic = {"available": True, "ownship": {k: own.get(k) for k in own if k not in ("provenance_key",)}}
        if own.get("position") and own["position"].get("alt_m") is not None:
            pic["ownship"]["altitude_ft_msl"] = _num(own["position"]["alt_m"] / 0.3048)
        for side, coalition in (("hostile", enemy), ("friendly", mine)):
            params = {"coalition": coalition, "state": "present", "limit": cap}
            if own.get("position"):
                params["near"] = {"anchor": "ownship", "radius_m": self.radius_nm * 1852}
            try:
                data = _plain(self.tools.read("query_units", params).data)
            except Exception as error:  # noqa: BLE001 - report, don't hide
                pic[side] = {"error": type(error).__name__ + ": " + str(error)}
                continue
            rows = []
            for row in data.get("rows", []):
                if row.get("unit_id") == own.get("unit_id"):
                    continue
                braa = geometry(own, row)
                rows.append({"type": row.get("unit_type"), "category": category(row.get("unit_type")),
                             "group": row.get("group_name"), "bearing_true": braa["bearing_true_deg"],
                             "range_nm": braa["range_nm"], "alt_ft": braa["altitude_ft_msl"]})
            rows.sort(key=lambda r: (r["range_nm"] is None, r["range_nm"] or 0))
            pic[side] = rows[:25]
        try:
            events = _plain(self.tools.read("recent_events", {"limit": 12}).data)
            pic["recent_events"] = [{k: e.get(k) for k in ("event_type", "observed_at_utc", "text")}
                                    for e in events.get("events", [])]
        except Exception as error:  # noqa: BLE001
            pic["recent_events"] = {"error": str(error)}
        return pic

    def _picture_with_retry(self):
        last = None
        for _ in range(2):
            try:
                return self.picture(), None
            except Exception as error:  # noqa: BLE001 - retry once, then tell the pilot
                last = error
                time.sleep(0.05)
        return {"available": False, "reason": "Athena tracking not reachable (%s)" % type(last).__name__}, last

    @staticmethod
    def picture_summary(pic):
        own = pic.get("ownship") or {}
        hostile = pic.get("hostile_groups", pic.get("hostile")) or []
        friendly = pic.get("friendly_groups", pic.get("friendly")) or []
        out = {"connected": pic.get("connected", pic.get("available")), "ownship": own.get("type"),
               "hostile_groups": len(hostile) if isinstance(hostile, list) else None,
               "friendly_groups": len(friendly) if isinstance(friendly, list) else None,
               "units_age_s": (pic.get("data_age_s") or {}).get("units")}
        if isinstance(pic.get("airbases"), dict):
            out["airbases"] = pic["airbases"].get("count")
        if isinstance(pic.get("my_shots_last_15_min"), dict):
            out["shots"] = pic["my_shots_last_15_min"].get("by_result")
        if isinstance(pic.get("recent_kills_and_losses_last_10_min"), list):
            out["kills_losses"] = len(pic["recent_kills_and_losses_last_10_min"])
            out["kills_inferred"] = sum(1 for r in pic["recent_kills_and_losses_last_10_min"] if r.get("inferred"))
        if isinstance(pic.get("my_kills_this_mission"), list):
            out["my_kills"] = len(pic["my_kills_this_mission"])
        if pic.get("splash_to_announce"):
            out["splash"] = pic["splash_to_announce"]
        es = pic.get("event_stream")
        if isinstance(es, dict):
            out["events"] = es.get("state")
            out["events_per_min"] = es.get("events_per_min")
            out["last_event_age_s"] = es.get("last_event_age_s")
            if es.get("resubscribes"):
                out["resubscribes"] = es["resubscribes"]
        loadout = own.get("loadout") if isinstance(own, dict) else None
        if isinstance(loadout, dict):
            out["loadout"] = ({w.get("weapon"): w.get("count") for w in loadout.get("weapons", [])}
                              if loadout.get("available") else "n/a: " + str(loadout.get("reason"))[:60])
            if loadout.get("available"):
                out["gun_rounds"] = loadout.get("gun_rounds")
        cp = pic.get("cockpit")
        if isinstance(cp, dict):
            if not cp.get("available"):
                out["cockpit"] = "n/a"
            else:
                rwr = cp.get("rwr") or {}
                out["cockpit"] = {"fresh": cp.get("fresh"), "age_s": cp.get("age_s"), "rwr": rwr.get("count"),
                                  "rwr_highest": rwr.get("highest"), "gear": (cp.get("config") or {}).get("gear"),
                                  "fuel_lb": (cp.get("fuel") or {}).get("total_lb"),
                                  "lock": (cp.get("locked_target") or {}).get("type")}
        if pic.get("rwr_alert_to_announce"):
            out["rwr_alert"] = pic["rwr_alert_to_announce"]
        if isinstance(own, dict) and own.get("fuel_fraction") is not None:
            out["fuel"] = own["fuel_fraction"]
        if isinstance(own, dict) and isinstance(own.get("fuel"), dict):
            out["fuel_lb"] = own["fuel"].get("total_lb")
        if isinstance(pic.get("threats"), dict):
            th = pic["threats"]
            out["rwr_source"] = th.get("rwr_source")
            if th.get("sam_in_range") or th.get("missiles_inbound"):
                out["threats"] = {"sam": len(th.get("sam_in_range") or []),
                                  "missiles": len(th.get("missiles_inbound") or [])}
        if isinstance(es, dict) and es.get("kinds_total"):
            out["event_kinds"] = es["kinds_total"]
        if isinstance(hostile, list) and hostile and isinstance(hostile[0], dict) and "clock" in hostile[0]:
            h = hostile[0]
            out["nearest_hostile"] = "%s %s %snm %s" % (h.get("count"), h.get("category"), h.get("range_nm"),
                                                        h.get("clock"))
        if pic.get("reason"):
            out["reason"] = pic["reason"]
        return out

    # ---------------------------------------------------------------- kill confirmation (round 6)
    def _kill_rows(self, pic, wingman, pic_time):
        """Kill rows (key, target, category, weapon, confidence, abs_time) for the pilot or the friendly flight.

        key is the Eye's stable per-kill seq when published, else None (matched by owner, target, weapon
        and picture-time reconstructed event time). pic_time is taken right after the picture, never after
        the LLM call, so model latency cannot shift identities. Categories are normalised (eye6 publishes
        "vehicles", older pictures "ground vehicle").
        """
        owner = "wingman" if wingman else "me"

        def row(k, target, category, weapon, confidence, age):
            key = k.get("seq", k.get("kill_id", k.get("id")))
            category = CATEGORY_ALIASES.get(category, category)
            return ((owner, "id", key) if key is not None else None, spoken_target(target, category), category,
                    weapon, confidence or "confirmed", pic_time - (age or 0))

        mine = pic.get("my_kills_this_mission") or []
        if not wingman:
            return [row(k, k.get("target"), k.get("category"), k.get("weapon"), k.get("confidence"), k.get("age_s"))
                    for k in mine]
        if "friendly_kills_this_mission" in pic:  # eye6 cumulative contract: all friendly rows incl. mine
            my_seqs = {k.get("seq") for k in mine if k.get("seq") is not None}
            return [row(k, k.get("target"), k.get("category"), k.get("weapon"), k.get("confidence"), k.get("age_s"))
                    for k in pic.get("friendly_kills_this_mission") or []
                    if k.get("seq") not in my_seqs and k.get("shooter") != "me"]
        coalition = ((pic.get("ownship") or {}).get("coalition"))
        rows = []
        for r in pic.get("recent_kills_and_losses_last_10_min") or []:
            killer, victim = r.get("killer") or {}, r.get("victim") or {}
            if r.get("by_me") or r.get("friendly_loss") or not killer or (
                    coalition and killer.get("coalition") != coalition):
                continue
            rows.append(row(r, victim.get("type"), victim.get("category"), r.get("weapon"),
                            "likely" if r.get("inferred") else "confirmed", r.get("age_s")))
        return rows

    def _match(self, row, unused):
        for c in unused:
            if row[0] is not None or c["key"] is not None:
                if row[0] is not None and c["key"] == row[0]:
                    return c
                continue
            if c["target"] == row[1] and c["weapon"] == row[3] and abs(c["t"] - row[5]) <= 2.0:
                return c
        return None

    def _split_new(self, rows, owner):
        """(new, upgraded): one-to-one matching against kills already read back for this owner.

        upgraded = rows read back earlier as likely that are now confirmed (same identity, reported once,
        never counted as a second kill)."""
        unused = [c for c in self._confirmed_kills if c["owner"] == owner]
        new, upgraded = [], []
        for row in rows:
            c = self._match(row, unused)
            if c is None:
                new.append(row)
                continue
            unused.remove(c)
            if c["confidence"] != "confirmed" and row[4] == "confirmed":
                upgraded.append((row, c))
        return new, upgraded

    def _remember(self, owner, rows, upgraded):
        for row in rows:
            self._confirmed_kills.append({"owner": owner, "key": row[0], "target": row[1], "weapon": row[3],
                                          "t": row[5], "confidence": row[4]})
        for row, c in upgraded:
            c["confidence"] = row[4]
        del self._confirmed_kills[:-1000]

    @staticmethod
    def _count_line(rows, label):
        counts = Counter(r[1] for r in rows)
        return "%s %s." % (label, ", ".join("%s %s" % (words(n), t) for t, n in counts.items()))

    def kill_confirm_calls(self, text, pic, pic_time=None, commit=True):
        """commit=False leaves the acknowledgement in self._pending_ack; the caller commits it only once the
        answer is actually transmitted (an overflowed answer must not mark kills as read back)."""
        wingman = bool(WINGMAN_ASK.search(text))
        owner = "wingman" if wingman else "me"
        prefix = "wingman " if wingman else ""
        cats = next((c for pattern, c in CATEGORY_ASK if pattern.search(text)), None)
        rows = self._kill_rows(pic, wingman, time.time() if pic_time is None else pic_time)
        if cats:
            rows = [r for r in rows if r[2] in cats]
        new, upgraded = self._split_new(rows, owner)
        lines = []
        if TOTAL_ASK.search(text):
            for confidence, label in (("confirmed", "Mission total:"), ("likely", "Likely:")):
                sel = [r for r in rows if r[4] == confidence]
                if sel:
                    lines.append(self._count_line(sel, label))
            if not lines:
                lines.append("No %skills yet." % prefix)
        else:
            confirmed = [r for r in new if r[4] == "confirmed"]
            likely = [r for r in new if r[4] != "confirmed"]
            if len(confirmed) == 1 and not likely:
                lines.append(("Confirmed, wingman splash one %s." if wingman else "Confirmed, splash one %s.")
                             % confirmed[0][1])
            else:
                if confirmed:
                    lines.append(self._count_line(confirmed, "Wingman confirmed:" if wingman else "Confirmed:"))
                if likely:
                    lines.append(self._count_line(likely, "Likely:"))
            if upgraded:
                lines.append(self._count_line([r for r, _ in upgraded], "Now confirmed, earlier likely:"))
            if not new and not upgraded:
                lines.append("No new %skills since your last check." % prefix if rows
                             else "No %skills showing." % prefix)
        lines = [" ".join(line.split()) for line in lines]
        # remember only what this answer actually reported (category-filtered rows), upgrades in place
        if commit:
            self._remember(owner, new, upgraded)
        else:
            self._pending_ack = (owner, new, upgraded)
        if REMAIN_ASK.search(text):
            lines.append(self.remaining_line(pic, cats))
        return lines

    @staticmethod
    def remaining_line(pic, cats):
        own = (pic.get("ownship") or {}).get("coalition")
        totals = pic.get("alive_totals_by_coalition") or {}
        enemy = totals.get("red" if own == "blue" else "blue" if own == "red" else "red") or {}
        if cats:
            n = sum(enemy.get(c, 0) for c in cats)
            label = CATEGORY_WORDS.get(cats[0], "enemy " + str(cats[0]))
            return ("%s %s remain." % (words(n).capitalize(), label)) if n else "No %s remaining." % label
        ground = [(k, v) for k, v in enemy.items() if v and k not in NOT_GROUND]
        if not ground:
            return "No enemy ground units remaining."
        parts = ["%s %s" % (words(v), CATEGORY_WORDS.get(k, "enemy " + str(k)))
                 for k, v in sorted(ground, key=lambda kv: -kv[1])]
        return "Remaining: %s." % ", ".join(parts)

    # ---------------------------------------------------------------- llm
    def ask(self, messages):
        body = json.dumps({"model": self.model, "messages": messages, "max_completion_tokens": 400,
                           "store": False}).encode()
        conn = http.client.HTTPSConnection(self._host, timeout=self.timeout)
        try:
            conn.request("POST", self._path, body=body, headers={
                "Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
            reply = conn.getresponse()
            raw = reply.read()
        finally:
            conn.close()
        if reply.status != 200:
            raise RuntimeError("openai_http_%d: %s" % (reply.status, raw[:300].decode("utf-8", "replace")))
        return json.loads(raw)["choices"][0]["message"]["content"].strip()

    # ---------------------------------------------------------------- radio
    def _log(self, request, kind, **values):
        self.transcript.log(request=request, kind=kind, provider="openai-api", model=self.model, **values)

    def stream(self, request):
        """Answer EVERY transmission from the pilot on the command frequency (no 'Athena' prefix needed)."""
        text = request.transcript.strip()
        self._log(request, "adapter_ingress", text=text, outcome="received")
        if not speaker_matches(request.speaker, self.speaker):
            self._log(request, "route", route="ignored", outcome="not_pilot")
            yield response(request, reason="unaddressed_or_noncommand")
            return
        self.transmissions += 1
        if _JUNK.match(text):
            self.empty_transcripts += 1
            self._log(request, "route", route="ignored", outcome="empty_transcript",
                      counts={"empty_transcripts": self.empty_transcripts, "transmissions": self.transmissions})
            yield response(request, reason="unaddressed_or_noncommand")
            return
        with self._waiting_lock:
            self._waiting += 1  # in flight: answering or waiting
            queued = self._waiting > 1
        if queued:
            self._log(request, "route", route="LLM", outcome="queued_behind_previous")
        try:
            with self._turn:
                yield from self._answer(request, text)
        finally:
            with self._waiting_lock:
                self._waiting -= 1

    def _answer(self, request, text):
        start = time.monotonic()
        # memory6: owner's immediate acknowledgement; report I/O runs off-turn.
        memory = getattr(self.eye, "memory", None)
        if DEBRIEF_ASK.search(text) and not re.search(r"\bstatus\b", text, re.I):
            generation = memory.generation if memory is not None else None
            queued = memory.schedule_debrief() if memory is not None else False
            answer = "debrief written" if queued else "Debrief unavailable."
            self._log(request, "reply_generated", text=answer, outcome="intended", part=1)
            if memory is not None:
                memory.publish("radio_call", {"request": text, "reply": answer, "report_status": "queued" if queued else "unavailable"},
                               generation=generation)
            yield response(request, speech=answer, state="ok")
            return
        klass = call_class(text)
        report = None
        if klass == "kill_report" and self.eye is not None and hasattr(self.eye, "pilot_report"):
            try:
                report = self.eye.pilot_report(text)  # before the picture: the confirmed kill is in it
            except Exception:  # noqa: BLE001 - never block the answer on bookkeeping
                report = None
        pic, error = self._picture_with_retry()
        pic_time = time.time()  # round 6: kill identities use picture time, never post-LLM time
        klass = call_class(text, pic)
        splash_keys = pic.pop("_splash_keys", None) if isinstance(pic, dict) else None
        rwr_keys = pic.pop("_rwr_keys", None) if isinstance(pic, dict) else None
        result = {"summary": self.picture_summary(pic), "picture": pic,
                  "error": (type(error).__name__ + ": " + str(error)[:200]) if error else ""}
        if klass:
            result["call_class"] = klass
        if report:
            result["pilot_report"] = report
        self._log(request, "tool", tool="picture", args={}, outcome="error" if error else "ok",
                  result=result,
                  counts={"empty_transcripts": self.empty_transcripts, "transmissions": self.transmissions},
                  timings_ms={"tool": round((time.monotonic() - start) * 1000)})
        messages = [{"role": "system", "content": PROMPT}]
        for q, a in self.history:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "system", "content": "PICTURE " + json.dumps(
            pic, ensure_ascii=True, default=str, separators=(",", ":"))})
        hint = focus_hint(text)
        if hint:
            messages.append({"role": "system", "content": hint})
        if klass:
            messages.append({"role": "system", "content": CLASS_HINTS[klass]})
        messages.append({"role": "user", "content": text})
        announced = (splash_keys or []) + (rwr_keys or [])
        t_llm = time.monotonic()
        answer = None
        for attempt in (1, 2):
            try:
                answer = self._llm(messages)
                break
            except Exception as failure:  # noqa: BLE001 - one retry, then a spoken fallback
                self._log(request, "controller_error", outcome=type(failure).__name__ + ": " + str(failure)[:300],
                          attempt=attempt, timings_ms={"total": round((time.monotonic() - start) * 1000)})
        self._pending_ack = None
        calls = (self.kill_confirm_calls(text, pic, pic_time, commit=False) if klass == "kill_confirm" and pic.get("ownship")
                 and "my_kills_this_mission" in pic else requested_calls(klass, text, pic))
        if calls is not None:
            age = (pic.get("data_age_s") or {}).get("units")
            stale = ["Tracking disconnected; last known picture."] if not pic.get("connected", True) else []
            if age is not None and age > 60:
                stale.append("Contact data is %s seconds old." % words(age))
            prefix = "Prometheus, Athena, "
            answer = prefix + " ".join(stale + calls)
            alerts, announced = [], []
            if len(answer) > MAX_SPEECH_CHARS:
                # Never clip a requested call into a misleading partial group report.
                self._log(request, "controller_error", outcome="fixed_response_overflow",
                          counts={"speech_chars": len(answer)})
                answer = prefix + "requested calls are too long for one transmission. Ask for one group at a time."
                self._pending_ack = None  # nothing was read back: keep the previous acknowledgement state
            else:
                if self._pending_ack:
                    self._remember(*self._pending_ack)
                    self._pending_ack = None
                remaining = MAX_SPEECH_CHARS - len(answer)
                for field, keys in (("rwr_alert_to_announce", rwr_keys), ("splash_to_announce", splash_keys)):
                    for i, alert in enumerate(pic.get(field) or []):
                        if len(alert) + 1 > remaining:
                            continue  # keep this alert pending for a later transmission
                        remaining -= len(alert) + 1
                        alerts.append(alert)
                        if keys and i < len(keys):
                            announced.append(keys[i])
                answer = prefix + " ".join(stale + alerts + calls)
        else:
            answer = cap_speech(clean(answer) or FALLBACK, ACK_CAP if klass in ("ack", "kill_report") else SPEECH_CAP)
        if announced and self.eye is not None and hasattr(self.eye, "mark_announced"):
            self.eye.mark_announced(announced)
        llm_ms = round((time.monotonic() - t_llm) * 1000)
        self.history.append((text, answer))
        if memory is not None:  # memory6: generated radio reply, not a claim of SRS delivery
            memory.publish("radio_call", {"request": text, "reply": answer, "delivery": "generated"},
                           generation=pic.get("_memory_generation") if isinstance(pic, dict) else None)
        # Fixed replies are budgeted above: first sentence, then all remaining calls
        # in the second frame. The gateway allows two frames, not 420-char chunks.
        parts = split_parts(answer, MAX_SPEECH_CHARS if calls is not None else PART_CHARS)
        for index, part in enumerate(parts, 1):
            self._log(request, "reply_generated", text=part, outcome="intended", part=index,
                      timings_ms={"llm": llm_ms, "total": round((time.monotonic() - start) * 1000)})
            yield response(request, speech=part, state="ok")

    def exchange(self, request):
        parts = list(self.stream(request))
        if not parts[0]["deliverable"]:
            return parts[0]
        return response(request, speech=" ".join(p["speech"] for p in parts), state="ok")
