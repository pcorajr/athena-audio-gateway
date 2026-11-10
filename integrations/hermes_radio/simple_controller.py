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
from .cockpit import spoken_name
from .eye import KILL_REPORT, _QUESTION, domain, words

PROMPT = """You are "Athena", an experienced human AWACS/GCI controller on the radio with one pilot, callsign "Prometheus", flying in DCS World.

Every turn you get a live PICTURE (JSON) from the Athena Eye tracking system. It is your only source of truth: ownship (Prometheus' aircraft), contact groups already computed from Prometheus (bearing, range in nautical miles, altitude, aspect, closure, and "clock" = clock position relative to Prometheus' nose with ahead/behind/left/right), nearby airbases, Prometheus' own shots and their results, recent kills and losses, recent events and alive totals. data_age_s tells you how old the data is; if it is old or the picture is not connected, say so briefly and still give what you have. Never invent contacts, kills or numbers that are not in the picture.

Facts that matter:
- Optional memory is mission history, not a replacement for the live Eye: takeoff airfield, weapons used, observed kill/loss timeline and labelled disagreements. Never turn an unclear audit verdict into a confirmed fact.
- Every unit listed in contacts and totals is ALIVE. Something is dead or killed ONLY if it appears in recent_kills_and_losses, my_kills_this_mission or a shot result says kill. In my_kills_this_mission, confidence confirmed = a kill: say it plainly ("splash one T-55"), never "likely", even when inferred is true. Only confidence likely, or a recent_kills_and_losses row "no longer showing, likely destroyed", is "likely destroyed".
- "Mission status" / "status" / "how are we doing" = a short OVERVIEW, not only contacts: kills so far (count from my_kills_this_mission), what enemy remains (alive_totals_by_coalition, ground categories), nearest threat or bandit if any, then fuel and weapons in a few words. Two or three sentences.
- Mission kill totals come from my_kills_this_mission (cumulative for the whole mission, every row is one kill) or the memory section; recent_* lists are short windows, never totals.
- Names: say "bridge" for MOST/BRIDGE scenery codes, never read internal codes or numeric weapon ids aloud.
- my_shots: result pending = weapon still flying or not known yet, hit = hit but no kill reported, kill = target destroyed, likely kill = target stopped showing long after the shot, miss = no hit within the window. weapon is the weapon used (from the shot event or the loadout change). intended_target is a guess (DCS does not report the locked target); say "looks like" if you rely on it.
- ownship.loadout is what Prometheus has left: weapons by type with counts plus gun_rounds. If loadout is not available, say so with the reason in a few words.
- ownship.fuel is the fuel answer: total_lb in pounds (approximate when its source says so), else internal_pct percent; known false = "fuel unknown". Never report zero pounds unless total_lb is 0.
- Damage / "I'm hit": ownship.damage hit events and getLife/getLife0 drops are primary evidence. Read the damage call. Cockpit warning lights, engine RPM changes and fuel-loss trend are secondary clues only. No warnings NEVER means no damage; acknowledge the pilot even if telemetry is unavailable. A fuel loss trend alone is not proof of a leak.
- event_stream.state other than "live" means shot and kill reports may be missing; mention it only when asked about shots or kills.
- Bearings are magnetic when the picture says so, otherwise true; do not mention it unless asked.
- cockpit is live data from Prometheus' own aircraft (DCS Export.lua, about five times a second): config (gear, flaps, speedbrake, hook), flight (indicated airspeed, ground speed, altitude above ground, G, angle of attack), payload by station, rwr threats, locked_target and warning_lights. Prefer it over other fields when cockpit.fresh is true. If the cockpit is not available or not fresh and the question needs it, say "no cockpit data" in a few words.
- RWR ("what's painting me", "RWR", "spike", "am I locked"): give each cockpit.rwr threat with its type, what it is doing (searching, tracking, locked, missile launch) and its clock, locks and launches first, e.g. "SA-11 locked on you at your three o'clock." Empty threats with a fresh cockpit = "RWR is clean, nothing painting you." If threats.rwr_source is grpc_inferred (this jet exports no RWR, e.g. the F-4E), answer from threats.missiles_in_flight (one row per missile; threats.missiles_inbound only if that is absent) and threats.sam_in_range calls instead ("SAM SA-11 could be tracking you, ..."); nothing listed = "no RWR feed from this jet, no SAM in range and no missile inbound that I can see."
- "What am I locked on" / "what's my target": read cockpit.locked_target.call (and the unit type and side if present). locked_target null = "nothing locked".
- Fuel state: ownship.fuel (total_lb pounds, else internal_pct percent).
- Gear / configuration: cockpit.config (gear down, up or in transit; flaps percent; speedbrake).
- If the picture has rwr_alert_to_announce, START the answer with it, before anything else, e.g. "Prometheus, Athena, spike, SA-11 at your three o'clock." Then answer the question.

How to talk:
- Answer ONLY what was asked. Acknowledgements, intent calls, radio checks, readbacks and pilot reports ("acknowledge my last transmission", "copy", "egressing", "going to investigate", "eyes on target", "wheels up", "airborne", "weapons hot", "target destroyed") get ONE short sentence: acknowledge, plus at most the ONE target being worked (working_target.call) when it helps. Never a picture, never other groups.
- Give contacts only when asked (picture, threats, bogey dope, "any ...", mission status) or when a hostile aircraft or SAM is within twenty miles that you have not called in the last exchanges.
- Range tiers: ground targets only within TWENTY miles (ground_targets_within_20nm; if empty say "no ground targets within twenty miles"); an air picture or bogey dope is the three closest bandits WITHIN FIFTY miles (picture_top3 verbatim, both sentences of every call); if none inside fifty, give air_picture_empty_call (nearest bandit out to one hundred fifty miles). "Long range picture" = picture_long_range_top3. Never append extra-group counts.
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
     "grpc_inferred use threats.missiles_in_flight (else missiles_inbound) and threats.sam_in_range"),
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
PICTURE_ASK = re.compile(r"\b(picture|bogey dope|what do you see|what's out there|whats out there|threats? around)\b", re.I)
GROUND_ASK = re.compile(r"\b(ground|vehicles?|armor|infantry|trucks?|tanks?|sams?|triple a)\b", re.I)
LONG_ASK = re.compile(r"\b(long[- ]range|far|extended|one hundred fifty|150)\b", re.I)
TRAFFIC_ASK = re.compile(r"\b(traffic|blue only|only blue)\b", re.I)
ATC_ASK = re.compile(r"\b(take ?off|landing|land|approach|taxi\w*|runway)\b", re.I)
DAMAGE_ASK = re.compile(r"\b(i('?m| am| got| was| have been) hit|hit me|taking hits|damage report|my damage|"
                        r"i (took|have|sustained) damage|i('?m| am) damaged|am i damaged|"
                        r"damage to (me|my (aircraft|jet|plane|ownship)))\b", re.I)
CONFIRM_ASK = re.compile(r"\bconfirm(ed)?\b", re.I)
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
                         r"(give|send) me (a |the )?(brief|briefing|summary|debrief)|"
                         r"(generate|create|write|make|prepare|do) (a |the |my )?(brief|briefing|debrief|report|summary))\b", re.I)
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
# Map objects are never "ground units" for a generic "what remains"; air/sea comes from eye.domain().
NOT_GROUND = {"scenery", "static", "unknown"}
NON_WEAPON = re.compile(r"(ALQ|LITENING|AAQ|\bpod\b|TGP|fuel tank|\btank\b|smoke|HB-ALE|HB-ORD|pave-?spike)", re.I)
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
    "long_picture": "LONG RANGE PICTURE: picture_long_range_top3 verbatim (bandits within 150 miles).",
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
    "loadout": "LOADOUT: remaining weapons and gun rounds only; if the pilot also asks what they are flying, name the aircraft type first.",
    "launch": "MISSILE LAUNCH REPORT: read EVERY missile in threats.missiles_in_flight (spoken_name, clock, range; one row per missile id; missiles_inbound only if absent), not only the named type; "
              "none = no missile in the air that I can see.",
    "bearing_one": "BEARING REQUEST: give ONLY the nearest matching group's call line (hostile_groups are nearest "
                   "first), not every group.",
}
# Round 9: launch calls, aircraft questions, recent-release window
THREAT_NAME = (r"(?:missiles?|sams?|stingers?|manpads?|igla|strela|sa-?\s?\d+|s-?(?:75|125|200)|patriot|rapier|roland|hawk|"
               r"buk|kub|osa|tor|aaa)")
# Round 9 review: only a threat/incoming launch goes to the launch route; the pilot's own releases
# ("Maverick launched", "ready to launch", "cleared for launch") never do.
LAUNCH_ASK = re.compile(r"(\b%s[\s,.-]+launch(ed|es|ing)?\b|\blaunch(es)?,?\s+launch\b|"
                        r"\b(any|was that a?|see a|that a|another|new|second)\s+launch(es)?\b|"
                        r"\blaunch(ed|es)?\s+(at|toward|towards)\s+(me|us)\b|\blaunch(ed)?\s+(detected|warning|alert)\b|"
                        r"\b(shot|shooting|fired)\s+at\b|\bbeing\s+(shot|fired)\b|"
                        r"\b(another|second|new|other|incoming|inbound)\s+(missile|sam|stinger|manpads?)s?\b|"
                        r"\b(missile|stinger|manpads?)s?\s+(in the air|inbound|incoming|coming|up)\b|"
                        r"\bsee (the |that |a )?(missile|stinger)s?\b)" % THREAT_NAME, re.I)
OWN_LAUNCH = re.compile(r"(\b(i|we)(\s+(am|have|had|just)|'m|'ve)?\s+(just\s+)?(launch\w*|fired|shot)\b|"
                        r"\b(ready|cleared|clear|permission|requesting|about|going|preparing)\s+(to|for)\s+(\w+\s+)?launch)", re.I)
LAUNCH_FOLLOWUP = re.compile(r"\b(do you (still )?see (it|that|the missile)|see it|still (there|in the air|tracking))\b", re.I)
ANOTHER_ASK = re.compile(r"\b(another|second|other|again|same)\b", re.I)
NAMED_MISSILE = re.compile(r"\b(stinger|manpads?|igla|strela|sa-?\s?(\d+)|sam)\b", re.I)
AIRCRAFT_ASK = re.compile(r"\b(what am i (flying|in|on)|what (air ?plane|plane|jet|aircraft|bird)\b.{0,12}\b(am i|i am|i'm)|"
                          r"which (air ?plane|plane|jet|aircraft)|what (air ?plane|plane|jet|aircraft)|"
                          r"my (air ?plane|plane|jet|aircraft)|type of (air ?craft|plane)|am i flying|i('m| am) flying)\b", re.I)
RELEASE_WINDOW_S = 90
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
    if LAUNCH_ASK.search(t) and not OWN_LAUNCH.search(t):
        return "launch"
    if ATC_ASK.search(t):
        return "atc"
    if TRAFFIC_ASK.search(t) and not GROUND_ASK.search(t):
        return "traffic"
    age = (pic or {}).get("last_attack_age_s")
    explicit = target_damage or focus_of(t) or BEARING_ASK.search(t) or PICTURE_ASK.search(t) or GROUND_ASK.search(t)
    if CONFIRM_ASK.search(t) and not explicit and age is not None and 0 <= age < 300:
        return "kill_confirm"
    if PICTURE_ASK.search(t) and LONG_ASK.search(t):
        return "long_picture"
    if PICTURE_ASK.search(t):
        return "ground_picture" if GROUND_ASK.search(t) else "picture"
    if BEARING_ASK.search(t) and not ALL_ASK.search(t):
        return "bearing_one"
    if ACK.search(t) and not REQUEST.search(t):
        return "ack"
    return None


def _splash_target(alert):
    """'Splash one T-55 (likely).' -> 't-55'; never empty (an empty target would match everything)."""
    t = re.sub(r"^\s*splash\s+(one|two|three|\w+)\s+", "", (alert or "").lower()).strip(" .!")
    t = re.sub(r"\s*\(?likely\)?$", "", t).strip(" .,")
    return t or "\x00no-target\x00"


def spoken_target(name, category=None, dcs_type=False):
    """Radio-safe target name: scenery codes become words, unknowns become 'target'.
    dcs_type=True: the name is a raw DCS type string (shot rows) -> plain controller name (SA-2, Ural truck)."""
    name = (name or "").strip().lstrip("- ").strip()
    for pattern, word in SCENERY_NAMES:
        if pattern.search(name):
            return word
    if not name or name.lower() == "unknown" or (category == "scenery" and re.search(r"[()_]", name)):
        return "structure" if category in ("scenery", "static") else "target"
    return spoken_name(name, "target") if dcs_type else name


def spoken_aircraft(type_name):
    """'F-4E-45MC' -> 'F-4E', 'A-10C_2' -> 'A-10C' (the module variant is not radio talk)."""
    t = re.sub(r"-\d+MC$", "", str(type_name or "").split("_")[0]).strip()
    return t or None
def _article(craft):
    """a/an by sound: designations (F-4E, A-10C) are read letter by letter; words (Mirage, Hercules) by vowel."""
    if re.match(r"[A-Za-z]{1,2}[-/ ]?\d", craft) or re.match(r"[A-Z]/[A-Z]-?\d", craft):
        return "an" if craft[0].upper() in "AEFHILMNORSX" else "a"
    return "an" if craft[0].upper() in "AEIOU" else "a"
def loadout_calls(own, text=""):
    """Compact remaining-stores call that fits one transmission (pods and tanks are not weapons).
    Round 9: 'what am I flying and what's my loadout' answers both."""
    own = own or {}
    lo = own.get("loadout") or {}
    lead = ""
    craft = spoken_aircraft(own.get("type"))
    if craft and AIRCRAFT_ASK.search(text or ""):
        lead = "You're flying %s %s. " % (_article(craft), craft)
    if not lo.get("available"):
        return [lead.strip()] if lead else None
    def label(w):
        raw = w.get("spoken_name") or w.get("weapon")
        return family_name(raw, w.get("count") if isinstance(w.get("count"), int) else 2) or raw
    items = ["%s %s" % (label(w), words(w["count"])) for w in lo.get("weapons", [])
             if w.get("count") and label(w) and not NON_WEAPON.search(str(label(w)))]
    pods = []  # pods are carried stores, never weapons: listed after the weapons, never counted as fired
    for w in lo.get("weapons", []):
        name = str(label(w) or "")
        if re.search(r"\bpod\b", name, re.I) and not re.search(r"HB-|ALE|ORD", name) and name not in pods:
            pods.append(name)
    tail = (" Pods: %s." % _join_and(pods)) if pods else ""
    rounds = lo.get("gun_rounds")
    if rounds:
        items.append("gun %s rounds" % words(rounds))
    return [lead + ("You have " + ", ".join(items) + "." if items else "Winchester, no weapons left.") + tail]


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
                           hit.get("category"), dcs_type=True)
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


GUN_SHELL = re.compile(r"(-(APT|HEI?|API|AP|TP)\b|-(RED|BLUE)$|^M\d{2}[A-Z]?\d*-|^F\d{3}_|_\d+_\d+_)", re.I)  # M53-APT-RED, F100_M39_20_HEI
KILL_GROUP_WORDS = {"vehicles": ("truck", "trucks"), "armor": ("armor", "armor"), "SAM": ("SAM unit", "SAM units"),
                    "AAA": ("triple-A", "triple-A"), "aircraft": ("aircraft", "aircraft"),
                    "infantry": ("infantry", "infantry"), "other": ("other target", "other targets")}
def _kill_group(kill):
    cat = CATEGORY_ALIASES.get(kill.get("category"), kill.get("category"))
    if cat == "ground vehicle":
        return "vehicles"
    if cat in ("armor", "SAM", "AAA", "infantry"):
        return cat
    if cat and domain(cat) == "air":
        return "aircraft"
    return "other"
WEAPON_FAMILIES = ((re.compile(r"^AGM[- _]?65", re.I), "Maverick", "Mavericks"),
                   (re.compile(r"^AIM[- _]?9", re.I), "Sidewinder", "Sidewinders"),
                   (re.compile(r"^AIM[- _]?7", re.I), "Sparrow", "Sparrows"),
                   (re.compile(r"^AIM[- _]?54", re.I), "Phoenix", "Phoenixes"))
def family_name(name, n=1):
    """Common missile families by their service name (AGM-65A -> Maverick); other weapons keep their designation."""
    for pattern, one, many in WEAPON_FAMILIES:
        if pattern.match(str(name or "").strip()):
            return one if n == 1 else many
    return None
def _spoken_weapon(name, n):
    return "%s %s" % (words(n), family_name(name, n) or name)
def _join_and(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
def debrief_summary(pic):
    """ONE short radio summary (round 9): kills grouped by broad category, real weapons released, gun kills.
    Gun rounds in the picture are rounds REMAINING/used counters, never 'fired' weapons."""
    from .eye import weapon_kind
    def is_gun(name):
        return bool(name) and (str(name).lower().startswith("gun") or weapon_kind(name) == "gun"
                               or bool(GUN_SHELL.search(str(name))))
    kills = [k for k in (pic.get("my_kills_this_mission") or []) if k.get("confidence", "confirmed") == "confirmed"]
    parts = []
    if kills:
        groups = Counter(_kill_group(k) for k in kills)
        order = list(KILL_GROUP_WORDS)
        ranked = sorted(groups.items(), key=lambda kv: (-kv[1], order.index(kv[0])))
        shown, rest = ranked[:4], sum(n for _, n in ranked[4:])
        phrases = ["%s %s" % (words(n), KILL_GROUP_WORDS[g][0 if n == 1 else 1]) for g, n in shown]
        if rest:
            phrases.append("%s more" % words(rest))
        parts.append("%s kill%s: %s." % (words(len(kills)).capitalize(), "" if len(kills) == 1 else "s", _join_and(phrases)))
    else:
        parts.append("No confirmed kills.")
    used = ((pic.get("my_weapons_used_this_mission") or {}).get("by_weapon")) or {}
    real = []
    for w, n in sorted(used.items(), key=lambda kv: -(kv[1] if isinstance(kv[1], (int, float)) else 0)):
        if isinstance(n, (int, float)) and n > 0 and not is_gun(w) and not NON_WEAPON.search(str(w)):
            real.append(_spoken_weapon(w, int(n)))
    if real:
        parts.append(_join_and(real[:3])[:1].upper() + _join_and(real[:3])[1:] + " fired.")
    gun_kills = sum(1 for k in kills if is_gun(k.get("weapon")))
    if gun_kills:
        parts.append("Guns: %s kill%s." % (words(gun_kills), "" if gun_kills == 1 else "s"))
    return cap_speech(" ".join(parts), PART_CHARS - 80)


def requested_calls(klass, text, pic):
    """Render fixed-format air calls directly: a model/speech cap must not drop their second sentence."""
    if not pic.get("ownship"):
        return None
    if klass == "long_picture" and "picture_long_range_top3" in pic:
        return pic["picture_long_range_top3"] or ["No bandits within one hundred fifty miles."]
    if klass == "ground_picture" and "ground_targets_within_20nm" in pic and not re.search(
            r"\b(friendl\w*|blue|own|our)\b", text, re.I):
        rows = pic["ground_targets_within_20nm"]
        cats = next((c for pattern, c in CATEGORY_ASK if pattern.search(text)), None)
        if cats:
            def matches(r):
                found = [r.get("category")] + list(r.get("member_categories") or [])
                return any(CATEGORY_ALIASES.get(c, c) in cats for c in found)
            rows = [r for r in rows if matches(r)]
        rows = sorted(rows, key=lambda r: r.get("range_nm", 1e9) if isinstance(r, dict) else 1e9)
        calls = [r["call"] if isinstance(r, dict) else r for r in rows[:3]]
        if calls:
            if len(rows) > 3:
                calls.append("Plus %s more." % words(len(rows) - 3))
            return calls
        return ["No %s within twenty miles." % CATEGORY_WORDS.get(cats[0], "enemy " + str(cats[0]))] if cats else [
            pic["ground_empty_call"]]
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
        return loadout_calls(pic["ownship"], text)
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


def _word_cut(sentence, cap):
    """One long sentence: cut at the last word boundary within cap and end with a period (never mid-word)."""
    sentence = (sentence or "").strip()
    if len(sentence) <= cap:
        return sentence
    cut = sentence[:cap - 1]
    if not sentence[cap - 1:cap - 0].isspace() and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:-.") + "."
def fit_reply(answer, part_limit=PART_CHARS, limit=MAX_SPEECH_CHARS, word_trim=True):
    """Safety net (round 9): the reply must fit the gateway cap and two frames. Trim whole sentences from the
    end (the callsign/close-bandit sentence stays first). Returns (text, trimmed); text is None when
    word_trim=False and not even the first sentence fits."""
    answer = (answer or "").strip()
    def fits(t):
        return len(t) <= limit and len(split_parts(t, part_limit)) <= 2
    if fits(answer):
        return answer, False
    sentences, start = [], 0
    for m in SENTENCE_END.finditer(answer):
        sentences.append(answer[start:m.end()].strip())
        start = m.end()
    if answer[start:].strip():
        sentences.append(answer[start:].strip())
    while len(sentences) > 1:
        sentences.pop()
        text = " ".join(sentences)
        if fits(text):
            return text, True
    first = sentences[0] if sentences else answer
    if not word_trim:
        return None, True
    cap = min(limit, part_limit * 2 - 20)
    text = _word_cut(first, cap)
    while not fits(text) and cap > 40:
        cap -= 20
        text = _word_cut(first, cap)
    return text, True
def _named_missile(text):
    m = NAMED_MISSILE.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if m.group(2):
        return "SA-%s" % m.group(2)
    return {"stinger": "Stinger", "sam": None, "manpad": "MANPADS", "manpads": "MANPADS"}.get(word, word.capitalize())
def _missile_name_set(missiles):
    return [_missile_name(m.get("type")) for m in missiles]
def _missile_name(kind):
    kind = str(kind or "").strip()
    m = re.match(r"SA-?(\d+)", kind, re.I)
    return "SA-%s" % m.group(1) if m else (kind or "missile")
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
        self._reported_missiles = {}  # round 9: missile key -> (range_nm, time) already called to the pilot
        self._last_launch_reply = 0.0
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
        # Legacy state-service picture lives in legacy/integrations/hermes_radio_legacy/simple_legacy.py
        return {"available": False, "reason": "no Athena Eye configured"}

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
            flight = th.get("missiles_in_flight") if isinstance(th.get("missiles_in_flight"), list) else th.get("missiles_inbound")
            if th.get("sam_in_range") or flight:
                out["threats"] = {"sam": len(th.get("sam_in_range") or []),
                                  "missiles": len(flight or [])}
        if isinstance(es, dict) and es.get("kinds_total"):
            out["event_kinds"] = es["kinds_total"]
        if isinstance(hostile, list) and hostile and isinstance(hostile[0], dict) and "clock" in hostile[0]:
            h = hostile[0]
            out["nearest_hostile"] = "%s %s %snm %s" % (h.get("count"), h.get("category"), h.get("range_nm"),
                                                        h.get("clock"))
        if pic.get("reason"):
            out["reason"] = pic["reason"]
        return out

    @staticmethod
    def _with_close_bandits(answer, close_bandits, announced, part_limit=PART_CHARS):
        """Prepend each not-yet-called bandit inside 20 nm (current geometry) after the callsign prefix; only
        calls actually present in the final text are acknowledged, the rest stay pending."""
        prefix = "Prometheus, Athena, "
        added = 0
        for row in close_bandits or []:
            call = row["call"]
            if call in answer:
                announced.append(row["key"])
                continue
            # one new call per reply, and the whole reply must fit the first-sentence + one-part transport budget
            # (two frames); anything not added stays pending for the next reply
            if added:
                continue
            candidate = (prefix + call + " " + answer[len(prefix):]) if answer.startswith(prefix) else (
                call + " " + answer)
            # the same framing the reply is sent with: at most two frames and the whole-answer cap
            if len(candidate) > MAX_SPEECH_CHARS or len(split_parts(candidate, part_limit)) > 2:
                continue
            added += 1
            answer = candidate
            announced.append(row["key"])
        return answer

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
        self._confirm_covers_own_splashes = not wingman and not cats  # own, unfiltered: every pending splash is read
        self._confirm_is_own = not wingman  # splash_to_announce holds only the pilot's kills
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
            if not new and not upgraded and not wingman and self.recent_release(pic, cats):
                lines.append("Not confirmed yet, checking.")
            elif not new and not upgraded:
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
    def recent_release(pic, cats=None):
        """True when the pilot released a weapon (or scored a hit) within ~90 s on a target not yet confirmed
        dead: the kill event may simply lag the shot."""
        shots = (pic.get("my_shots_last_15_min") or {}).get("shots") or []
        if shots:
            for s in shots:
                age = s.get("age_s")
                result = str(s.get("result") or "pending")
                if age is None or not 0 <= age <= RELEASE_WINDOW_S or result in ("kill", "miss") or result.startswith("likely"):
                    continue
                if cats:
                    cat = CATEGORY_ALIASES.get((s.get("hit") or {}).get("category") or (s.get("intended_target") or {}).get("category"),
                                               (s.get("hit") or {}).get("category") or (s.get("intended_target") or {}).get("category"))
                    if cat not in cats:
                        continue
                return True
            return False
        age = pic.get("last_attack_age_s")
        return age is not None and 0 <= age <= RELEASE_WINDOW_S and not cats
    def launch_calls(self, text, pic):
        """Every missile in flight near the pilot (not only the named type); 'same missile' / 'new launch' by id.
        Reads the Eye's one-row-per-missile threats.missiles_in_flight; missiles_inbound only when it is absent."""
        threats = pic.get("threats")
        if not isinstance(threats, dict):
            return None
        source = "missiles_in_flight" if isinstance(threats.get("missiles_in_flight"), list) else "missiles_inbound"
        if source not in threats:
            return None
        now = time.time()
        self._reported_missiles = {k: v for k, v in self._reported_missiles.items() if now - v[1] <= 180}
        rows, ids = [], set()
        for m in threats.get(source) or []:
            if not isinstance(m, dict):
                continue
            ident = m.get("id", m.get("missile_id", m.get("weapon_id")))
            if ident is not None:
                if ident in ids:
                    continue  # one missile listed twice (shot-event row + live row)
                ids.add(ident)
            rows.append(m)
        missiles = sorted(rows, key=lambda m: m.get("range_nm") if isinstance(m.get("range_nm"), (int, float)) else 1e9)
        self._last_launch_reply = now
        if not missiles:
            return ["No missile in the air that I can see."]
        def key(m):
            ident = m.get("id", m.get("missile_id", m.get("weapon_id")))
            return ("id", str(ident)) if ident is not None else (m.get("type"), m.get("launcher"))
        def name_of(m):
            return _missile_name(m.get("spoken_name") or m.get("type"))
        def where(m):
            bits = []
            if m.get("clock") is not None:
                bits.append("your %s o'clock" % words(m["clock"]))
            if isinstance(m.get("range_nm"), (int, float)):
                n = max(1, round(m["range_nm"]))
                bits.append("%s mile%s" % (words(n), "" if n == 1 else "s"))
            return ", ".join(bits)
        def seen(m):
            old = self._reported_missiles.get(key(m))
            r = m.get("range_nm")
            return old is not None and (key(m)[0] == "id" or not isinstance(r, (int, float)) or old[0] is None
                                        or r <= old[0] + 2)
        shown = missiles[:3]
        asked_another = bool(ANOTHER_ASK.search(text or ""))
        had_before = bool(self._reported_missiles)
        was_seen = [seen(m) for m in shown]
        same = len(missiles) == 1 and asked_another and was_seen[0]
        for m in shown:
            self._reported_missiles[key(m)] = (m.get("range_nm") if isinstance(m.get("range_nm"), (int, float)) else None, now)
        names = [name_of(m) for m in shown]
        if same:
            return ["Same missile, %s in the air, %s." % (names[0], where(shown[0]))]
        named = _named_missile(text)
        lead = ""
        if named and not any(named.lower() in n.lower() for n in names + [name_of(m) for m in missiles]):
            lead = "Negative %s; " % named
        fresh = [i for i, ok in enumerate(was_seen) if not ok] if (asked_another and had_before) else []
        order = fresh + [i for i in range(len(shown)) if i not in fresh]
        lines = []
        for pos, i in enumerate(order):
            span = where(shown[i])
            tail = (", " + span) if span else ""
            if pos == 0 and fresh:
                lines.append("%sNew launch, %s in the air%s." % (lead, names[i], tail))
            elif pos == 0:
                lines.append("%s%s in the air%s." % (lead, names[i], tail))
            else:
                lines.append("Also %s%s." % (names[i], tail))
        if len(missiles) > 3:
            lines.append("Plus %s more." % words(len(missiles) - 3))
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
        ground = [(k, v) for k, v in enemy.items() if v and k not in NOT_GROUND and domain(k) == "ground"]
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
        last = getattr(self._llm, "last", None)  # CodexLLM: which backend answered the latest call
        if last is not None and "llm" not in values:
            values["llm"] = dict(last)
        provider = "openai-codex" if getattr(self._llm, "label", "").startswith("openai-codex") else "openai-api"
        model = getattr(self._llm, "model", self.model) if provider == "openai-codex" else self.model
        self.transcript.log(request=request, kind=kind, provider=provider, model=model, **values)

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
            try:
                dpic = self.picture()
                summary = debrief_summary(dpic)
            except Exception:  # noqa: BLE001 - the report is the deliverable; the spoken part is best effort
                dpic, summary = {}, ""
            answer = ("Prometheus, Athena, debrief written. " + summary).strip() if queued else (
                ("Prometheus, Athena, debrief unavailable. " + summary).strip())
            said = []
            answer = self._with_close_bandits(answer, (dpic or {}).get("_close_bandits"), said)
            answer, debrief_trimmed = fit_reply(answer, PART_CHARS, PART_CHARS)  # one frame
            if debrief_trimmed:
                self._log(request, "tool", tool="reply_budget", args={}, outcome="trimmed", result={"trimmed": True},
                          trimmed=True)
            if said and self.eye is not None and hasattr(self.eye, "mark_announced"):
                self.eye.mark_announced(said)
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
        if klass is None and time.time() - self._last_launch_reply <= 90 and LAUNCH_FOLLOWUP.search(text):
            klass = "launch"  # "do you see it?" right after a launch call
        splash_keys = pic.pop("_splash_keys", None) if isinstance(pic, dict) else None
        rwr_keys = pic.pop("_rwr_keys", None) if isinstance(pic, dict) else None
        close_bandits = pic.pop("_close_bandits", None) if isinstance(pic, dict) else None
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
        self._confirm_covers_own_splashes = False
        self._confirm_is_own = False
        trimmed = False
        if klass == "launch":
            calls = self.launch_calls(text, pic) if pic.get("ownship") else None
        else:
            calls = (self.kill_confirm_calls(text, pic, pic_time, commit=False) if klass == "kill_confirm" and pic.get("ownship")
                     and "my_kills_this_mission" in pic else requested_calls(klass, text, pic))
        if calls is not None:
            age = (pic.get("data_age_s") or {}).get("units")
            stale = ["Tracking disconnected; last known picture."] if not pic.get("connected", True) else []
            if age is not None and age > 60:
                stale.append("Contact data is %s seconds old." % words(age))
            prefix = "Prometheus, Athena, "
            if klass != "kill_confirm" and len(calls) > 1 and len(prefix + " ".join(stale + calls)) > MAX_SPEECH_CHARS:
                kept = list(calls)  # round 9: drop whole contacts from the end, never clip one mid-call
                while len(kept) > 1 and len(prefix + " ".join(stale + kept)) > MAX_SPEECH_CHARS:
                    kept.pop()
                calls, trimmed = kept, True
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
                        if field == "splash_to_announce" and klass == "kill_confirm" and (
                                getattr(self, "_confirm_covers_own_splashes", False)
                                or (getattr(self, "_confirm_is_own", False)
                                    and _splash_target(alert) in " ".join(calls).lower())):
                            # the confirmation itself reads the kill; mark the splash as said, don't repeat it
                            if keys and i < len(keys):
                                announced.append(keys[i])
                            continue
                        alert = alert.strip()
                        if alert and alert[-1] not in ".!?":
                            alert += "."
                        if alert in alerts or alert in " ".join(calls):
                            if keys and i < len(keys):
                                announced.append(keys[i])  # already said in this reply
                            continue
                        if len(alert) + 1 > remaining:
                            continue  # duplicate, or keep this alert pending for a later transmission
                        remaining -= len(alert) + 1
                        alerts.append(alert)
                        if keys and i < len(keys):
                            announced.append(keys[i])
                answer = prefix + " ".join(stale + alerts + calls)
        else:
            answer = cap_speech(clean(answer) or FALLBACK, ACK_CAP if klass in ("ack", "kill_report") else SPEECH_CAP)
        answer = self._with_close_bandits(answer, close_bandits, announced,
                                          MAX_SPEECH_CHARS if calls is not None else PART_CHARS)
        answer, net_trimmed = fit_reply(answer, MAX_SPEECH_CHARS if calls is not None else PART_CHARS)
        trimmed = trimmed or net_trimmed
        if trimmed:
            self._log(request, "tool", tool="reply_budget", args={}, outcome="trimmed", result={"trimmed": True},
                      trimmed=True)
        if announced and self.eye is not None and hasattr(self.eye, "mark_announced"):
            flat = []
            for key in announced:
                flat.extend(key if isinstance(key, list) else [key])
            self.eye.mark_announced(flat)
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
                      **({"trimmed": True} if trimmed else {}),
                      timings_ms={"llm": llm_ms, "total": round((time.monotonic() - start) * 1000)})
            yield response(request, speech=part, state="ok")

    def exchange(self, request):
        parts = list(self.stream(request))
        if not parts[0]["deliverable"]:
            return parts[0]
        return response(request, speech=" ".join(p["speech"] for p in parts), state="ok")
