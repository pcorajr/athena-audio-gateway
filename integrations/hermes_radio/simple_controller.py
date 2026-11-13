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
from .codex_llm import read_chunks
from .eye import KILL_REPORT, NO_POSITION, _QUESTION, domain, words, pilot_identity

PROMPT = """GROUNDING (most important):
- The PICTURE (JSON) is your only source of facts. Say only names, numbers, bearings and ranges that are in it.
- Before saying "unknown", "unavailable", "not connected" or "nothing showing", look again: if the field has a value, give the value. ownship.fuel.spoken is the fuel; airbases.nearest_friendly is the friendly airfield list; air_traffic is the friendly air traffic; connected true means the picture is live.
- Never invent clearances ("cleared to land/for takeoff"), runways, winds, altimeter, times or units. You are not a tower: for landing give the airfield call and its traffic.
- Never read JSON key names, codes or ids aloud.
- Every unit in contacts and totals is alive. Dead only from my_kills_this_mission, recent_kills_and_losses or a shot result kill. confidence confirmed = say it plainly; likely = "likely".
- If ownship is null you have no position for Prometheus: no range or bearing answers; totals and kills are still fine.
- data_health problems matter only for the stream named; never let them hide data that is present.

You are "Athena", an experienced human AWACS/GCI controller on the radio with one pilot, callsign "Prometheus", flying in DCS World.

Every turn you get a live PICTURE (JSON) from the Athena Eye. data_age_s tells you how old the data is; if old or disconnected, say so briefly and give what you have.

Facts that matter:
- Optional memory is mission history, not a replacement for the live Eye: takeoff airfield, weapons used, observed kill/loss timeline and labelled disagreements. Never turn an unclear audit verdict into a confirmed fact.
- A confirmed kill stays confirmed even when inferred is true. Only confidence likely, or a recent_kills_and_losses row "no longer showing, likely destroyed", is "likely destroyed".
- "Mission status" / "status" / "how are we doing" = a short OVERVIEW, not only contacts: kills so far (count from my_kills_this_mission), what enemy remains (alive_totals_by_coalition, ground categories), nearest threat or bandit if any, then fuel and weapons in a few words. Two or three sentences. Mission kill totals come from my_kills_this_mission (cumulative for the whole mission, every row is one kill) or the memory section; recent_* lists are short windows, never totals.
- Names: say "bridge" for MOST/BRIDGE scenery codes.
- my_shots: result pending = weapon still flying or not known yet, hit = hit but no kill reported, kill = target destroyed, likely kill = target stopped showing long after the shot, miss = no hit within the window. weapon is the weapon used (from the shot event or the loadout change). intended_target is a guess (DCS does not report the locked target); say "looks like" if you rely on it. Weapons: "what do I have left" = the loadout counts by weapon plus gun rounds. "What did I use on that target" / "what killed that" = the weapon from my_shots or my_kills_this_mission.
- ownship.loadout is what Prometheus has left: weapons by type with counts plus gun_rounds. If loadout is not available, say so with the reason in a few words. Fuel state: ownship.fuel is the fuel answer: total_lb in pounds (approximate when its source says so), else internal_pct percent; known false = "fuel unknown". Never report zero pounds unless total_lb is 0.
- Damage / "I'm hit": ownship.damage hit events and getLife/getLife0 drops are primary evidence. Read the damage call. Cockpit warning lights, engine RPM changes and fuel-loss trend are secondary clues only. No warnings NEVER means no damage; acknowledge the pilot even if telemetry is unavailable. A fuel loss trend alone is not proof of a leak.
- data_health.combat_events not "ok" = kills may be unconfirmed; mention it only when asked about kills.
- Bearings are magnetic when the picture says so, otherwise true; do not mention it unless asked.
- cockpit is live aircraft data: config, flight, payload, rwr, locked_target and warning_lights. Prefer it when cockpit.fresh is true; if unavailable or stale and needed for the question, say "no cockpit data". Gear / configuration: cockpit.config (gear down, up or in transit; flaps percent; speedbrake).
- RWR ("what's painting me", "RWR", "spike", "am I locked"): give each cockpit.rwr threat with its type, what it is doing (searching, tracking, locked, missile launch) and its clock, locks and launches first, e.g. "SA-11 locked on you at your three o'clock." Empty threats with a fresh cockpit = "RWR is clean, nothing painting you." If threats.rwr_source is grpc_inferred (this jet exports no RWR, e.g. the F-4E), answer from threats.missiles_in_flight (one row per missile; threats.missiles_inbound only if that is absent) and threats.sam_in_range calls verbatim ("tracking" only when its confidence is DCS radar, else "could be"); nothing listed = "no RWR feed from this jet, no SAM in range and no missile inbound that I can see. missile_scan not on: say missile tracking is limited and why."
- "What am I locked on" / "what's my target": read cockpit.locked_target.call (and the unit type and side if present). locked_target null = "nothing locked".
- Pending rwr_alert_to_announce calls are context: the controller speaks them before your text. Do not repeat or contradict them.

How to talk:
- Answer ONLY what was asked. Acknowledgements, intent calls, radio checks, readbacks and pilot reports ("acknowledge my last transmission", "copy", "egressing", "going to investigate", "eyes on target", "wheels up", "airborne", "weapons hot", "target destroyed") get ONE short sentence: acknowledge, plus at most the ONE target being worked (working_target.call) when it helps. Never a picture, never other groups. Radio checks and chit-chat: answer naturally ("Prometheus, Athena, loud and clear.").
- Give contacts only when asked (picture, threats, bogey dope, "any ...", mission status) or when a hostile aircraft or SAM is within twenty miles that you have not called in the last exchanges. Air threats first (nearest and hot first), then SAMs, then ground.
- Range tiers: ground targets only within TWENTY miles (ground_targets_within_20nm; if empty say "no ground targets within twenty miles"); an air picture or bogey dope is the three closest bandits WITHIN FIFTY miles (read the picture_top3 calls verbatim, every call whole, nothing added between them); if none inside fifty, give air_picture_empty_call (nearest bandit out to one hundred fifty miles). "Long range picture" = picture_long_range_top3. Never append extra-group counts. Never cut a call mid-way: if the answer must be shorter, leave out whole calls from the end.
- Traffic means AIRCRAFT, including helicopters, unless explicitly asked for ground/vehicles. Blue/friendly traffic comes from air_traffic, never friendly ground groups. For ATC use the selected airbase's traffic field, including aircraft on runways/taxiways within two miles and arriving/departing aircraft within five miles. The field itself is not traffic.
- Never say "overhead" unless that contact has overhead_allowed true (within three nautical miles). Nineteen miles is NOT overhead.
- A bearing or position request for a kind of target ("bearing to enemy armor", "where is the SAM") = the NEAREST matching group only, unless the pilot says "all" or "every".
- Calm, natural, clear and short: usually one or two sentences. Summarise groups instead of listing everything.
- Start with "Prometheus, Athena," (or just "Prometheus," for a quick follow-up).
- Positional calls: every group and airbase in the picture has a ready "call" line in the required order, BRA first then clock (bearing, range, altitude or ground target, then the clock and high, low or level). Use that call line VERBATIM, as one sentence; never give a range or a clock position without the bearing. Hostile air calls in an air picture start with the aircraft type (plus two-ship, three-ship when more than one) and give the range without "miles" (under ten miles they keep "miles"). In traffic lists and close-bandit warnings a hostile call starts with "Bandit", a friendly one with "Friendly". Format for reference:
  Air picture: "Su-27 two-ship, zero three eight, twelve, angels fifteen, ten o'clock high."
  Close bandit or traffic: "Bandit MiG-29S, two seven zero, eight miles, angels twelve, nine o'clock level."
  Ground: "Enemy armor, bearing zero three eight, three miles, ground target, eleven o'clock."
- Pending splash_to_announce calls are context: the controller speaks them before your text. Do not repeat or contradict them.
- "bandit" only for enemy aircraft. Enemy ground units are "enemy armor", "SAM site", "triple A", "enemy trucks" and so on. Friendly aircraft are "friendly".
- No markdown, no lists, no parentheses, no abbreviations with periods. Write numbers so a text-to-speech voice reads them well.
- Follow-up questions refer to the previous exchanges.
- Development notes: the pilot is also the developer of Athena. ONLY when a "DEV NOTE" system hint is present (explicit phrasing such as "feature request", "note for development", "bug report", "Athena should be able to ...", "add a feature"), reply briefly "Prometheus, Athena, copy, noted for development: <one-line restatement>." Without that hint it is a normal radio call: kill reports ("target destroyed", "splash", "armor dead") and chat ("switching missions") are acknowledged normally, never noted for development.
- Basic ATC: Athena never clears or approves anything: no clearance, runway, wind or altimeter. Use the selected airfield traffic (within five miles and below five thousand feet): "no traffic" or BRAA + clock. A friendly airfield comes ONLY from airbases.nearest_friendly: its call line, else "no friendly airfield showing".
"""

SENTENCE_END = re.compile(r"(?<!\d)[.!?](?=\s|$)")
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp", "Zs"})
_JUNK = re.compile(r"^[\W_]*$")
# ONE reply budget (phase 0). Athena's Piper voice speaks 13.4 characters a second (median, 466 live parts;
# slowest long reply 10.6) and the gateway refuses more than 30 s of audio ("say again, too long"):
# 280 characters is ~21 s typical, <= 26.5 s at the slowest rate seen. Three compact air calls fit.
# Every reply path (model, fixed, debrief, alerts, close bandits) fits this, sent as ONE frame.
MAX_REPLY_CHARS = 280
DEBRIEF_CHARS = 150  # the spoken debrief; the details are in the written report
assert MAX_REPLY_CHARS <= MAX_SPEECH_CHARS
# ONE deadline per exchange (phase 1), from the moment the request reaches the adapter. The gateway gives the
# whole bridge call 20 s. Measured 11-07..09: Codex p50 1.7 s, p90 3.2 s, p99 7.7 s, max 8.2 s; API mini p99
# 1.5 s, max 1.9 s. Codex gets at most 6 s of wall-clock time, the API fallback the rest, and at 10 s Athena
# says FALLBACK: never silence, never a guess, and 10 s of margin to the gateway's own "say again".
EXCHANGE_DEADLINE_S = 10.0
MIN_MODEL_S = 2.0  # less than this left (the request waited behind another one): no model call, say again
FALLBACK = "Prometheus, Athena, say again."
# Missing-data/link statements become false after connecting or spawning (11-11 preflight).
STALE_CHAT = re.compile(r"unknown|unavailable|not available|not connected|disconnected|"
                        r"no (?:connected )?picture|no cockpit|don['’]t (?:have|know)|do not (?:have|know)|"
                        r"no position|not (?:showing )?in an aircraft|last known picture|say again|ask again for|"
                        r"no (?:fuel|loadout|position|friendly airfield|friendly aircraft).{0,40}(?:data|reading|showing)", re.I)
REPEAT_ASK = re.compile(r"^\W*(?:athena[ ,]+)?(?:say again|repeat|say that again|repeat last)[.!? ]*$", re.I)


def factual_history(answer):
    """Keep useful sentences, not missing-data caveats; legitimate negatives remain."""
    from .reply_guardrails import sentences
    body = re.sub(r"^Prometheus,?\s*(Athena,?\s*)?", "", answer or "", flags=re.I)
    kept = [s for s in sentences(body) if not STALE_CHAT.search(s)]
    return ADDRESS + " ".join(kept) if kept else ""


# Question focus (round 3): a one-line hint telling the model which picture section answers the question.
FOCUS_RULES = (
    ("lock", re.compile(r"\b(what am i locked|what('?s| is| do i have) (my )?(locked|lock|target)|my (lock|target)\b|"
                        r"locked target|what have i got locked|what('?s| is) on my radar)", re.I),
     "cockpit.locked_target (call, unit, side); null = nothing locked"),
    ("rwr", re.compile(r"\b(rwr|paint\w*|spik\w*|radar warning|threat warning|nails|mud spike|"
                       r"(locking|locked on|lock on|tracking|targeting|looking at) me\b|am i (being )?locked\b(?! on)|"
                       r"anyone locking|launch|missile inbound|threats? on me)", re.I),
     "cockpit.rwr.threats (type, signal, clock); empty with fresh cockpit = RWR clean; when threats.rwr_source is "
     "grpc_inferred use threats.missiles_in_flight and threats.sam_in_range (DCS radar: tracking; range only: could be)"),
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
                 r"airborne|visual|in hot|off target|rtb|returning to base|heading home|thanks|thank you|"
                 r"understood|good copy|switching (missions?|frequenc\w+|radios?)|stand ?by|checking in|rolling in|"
                 r"pushing|commencing|engaging|in the weeds|on my way|proceeding|acknowledged?)\b", re.I)
# "Athena, tally." / "tally, kills" / "confirm tally" / "I ask for a tally" = the mission kill total (live 11-09);
# "tally two trucks" (visual contact) and "two, tally" / "visual, tally" (brevity: target in sight) are not.
TALLY_ASK = re.compile(r"(\b(kill )?tally,?\s+kills?\b|\b(confirm|give me|request|ask for|what('?s| is))\s+(a |the |my )?"
                       r"(kill )?tally\b|\bkill tally\b|^\W*(athena\W*)?tally\W*$)", re.I)
REQUEST = re.compile(r"(\?|\b(picture|bogey dope|bearing|vector|where|what|which|how|say|give|any|status|threats?|"
                     r"declare|request|fuel|left|range|braa|bra|check|are there|is there|tell me|need)\b)", re.I)
PICTURE_ASK = re.compile(r"\b(picture|bogey dope|what do you see|what's out there|whats out there|threats? around)\b", re.I)
GROUND_ASK = re.compile(r"\b(ground|vehicles?|armor|infantry|trucks?|tanks?|sams?|triple a)\b", re.I)
LONG_ASK = re.compile(r"\b(long[- ]range|far|extended|one hundred fifty|150)\b", re.I)
TRAFFIC_ASK = re.compile(r"\b(traffic|blue only|only blue)\b", re.I)
ATC_ASK = re.compile(r"\b(take ?off|landing(?! (gear|lights?))|land|approach|taxi\w*|runway|altimeter|qnh|qfe)\b", re.I)
RUNWAY_INFO_ASK = re.compile(r"\b(runways?|active|winds?|altimeter|qnh|qfe)\b", re.I)
NO_RUNWAY_INFO = "I have no runway, wind or altimeter data."
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
CLASS_HINTS = {
    "dev_note": "DEV NOTE: explicit development note. Reply \"Prometheus, Athena, copy, noted for development: "
                "<one-line restatement>.\"",
    "kill_report": "PILOT KILL REPORT (a normal tactical call, NOT a development note): one short sentence. If "
                   "splash_to_announce is present say it, else \"copy, splash\" with the target type if known.",
    "ack": "ACK OR INTENT CALL: one short sentence. Acknowledge; add at most working_target.call when it helps. "
           "No picture, no other groups.",
    "long_picture": "LONG RANGE PICTURE: picture_long_range_top3 verbatim (bandits within 150 miles).",
    "picture": "AIR PICTURE: picture_top3 verbatim, the three nearest hostile air groups within fifty miles, "
               "every call whole (one sentence each). Every bearing, range, angels, clock and high/low/level must "
               "survive; if shorter, drop whole calls from the end, never cut one. No extra-group counts.",
    "ground_picture": "GROUND PICTURE: nearest requested ground groups, at most three, call lines verbatim.",
    "traffic": "TRAFFIC: aircraft only, including helos, from air_traffic. Blue means blue coalition. "
               "Do not substitute ground units. Read the nearest aircraft call lines verbatim.",
    "damage": "DAMAGE: acknowledge the pilot. Use ownship.damage events and life percentage first, "
              "cockpit warnings/RPM/fuel-loss trends second. No warnings is not evidence of no damage.",
    "kill_confirm": "KILL CONFIRMATION REQUEST, not a pilot kill report: answer from my_kills_this_mission "
                    "and my_shots_last_15_min. No air picture. Do not upgrade likely kills merely on a question.",
    "intent": "INTENT CALL: short acknowledgement only.",
    "impact": "IMPACT / RESULT CHECK: report the latest weapon's result only.",
    "loadout": "LOADOUT: remaining weapons and gun rounds only; if the pilot also asks what they are flying, name the aircraft type first.",
    "launch": "MISSILE LAUNCH REPORT: read EVERY missile in threats.missiles_in_flight (spoken_name, clock, range; one row per missile id; missiles_inbound only if absent), not only the named type; "
              "none = no missile in the air that I can see, but threats.missile_scan not on = \"missile tracking is limited\" and why, never an all-clear.",
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
# Flight 2026-11-11: the model answered these from the right data wrongly ("fuel unknown" with 3,640 lb in the
# picture, "no friendly airfield showing" with Creech at 7 nm, "Cleared to land"); they are fixed answers now.
_FUEL = r"(?:fuel|gas)(?!\s*(?:trucks?|tankers?|tanks?|depots?|farms?|stations?|storage|dumps?|bladders?|pumps?|lines?))"
FUEL_ASK = re.compile(r"(\b%s\s+(check|chick|czech|state|status|remaining|left|quantity|level|reading|report|on board|"
                      r"look\w*)\b|\b(my|our)\s+%s|\bhow much\s+(%s|juice)|\bwhat('?s| is| does)\s+(my |the )?%s|"
                      r"\b(say|check|give me|read me)\s+(my |the )?%s|\bam i (at )?(bingo|joker)\b|\bbingo\s*\?|"
                      r"\bhow (close|far)\b.{0,12}\bbingo\b|^\W*(athena\W*)?%s\W*$)" % ((_FUEL,) * 6), re.I)
WEAPON_WORD = re.compile(r"\b(weapons?|stores|ammo|winchester|loadout|rounds|missiles?|bombs?|mavericks?)\b", re.I)
FIELD_WORDS = r"(?:air ?force ?base|afb|air ?bases?|air ?fields?|air ?ports?|air ?strips?|landing (?:field|site)s?|fields?|bases?)"
NEAREST_FIELD = re.compile(r"\b(nearest|closest|nearby|divert)\s+(friendly\s+)?%s\b|\bhome ?plate\b|"
                           r"\b(vectors?|steer|heading|bearing)\s+(for |to |back )?(home|base|rtb)\b|"
                           r"\brtb\s+(vector|heading|bearing|steer)\b|\bdivert\b" % FIELD_WORDS, re.I)
VECTOR_ASK = re.compile(r"\b(vectors?|steer|heading to|bearing to|direct(ions)? to|how far|where('?s| is)|take me|"
                        r"point me|get me)\b", re.I)
FIELD_WORD = re.compile(r"\b%s\b" % FIELD_WORDS, re.I)
NOT_FIELD = re.compile(r"\b(sams?|bandits?|threats?|armou?r|tanks?|trucks?|targets?|enemy|hostiles?|triple a|aaa|"
                       r"missiles?|red|how many|captured|attack\w*|under fire|safe|secure|bomb\w*|shell\w*)\b", re.I)
# "Bingo fuel, RTB" / "joker, heading home": fuel plus the nearest friendly field (live 11-11: the model said
# "fuel unknown" twice).
BINGO_RTB = re.compile(r"\b(bingo|joker)\b.{0,30}\b(rtb|home|returning|divert\w*|recover\w*|heading back)\b|"
                       r"\b(rtb|returning)\b.{0,20}\b(bingo|joker)\b", re.I)
LANDING_ASK = re.compile(r"(\b(request\w*|permission|clearance|ready)\b[^.?!]{0,25}\b(land(ing)?|approach|recovery|"
                         r"take ?off|takeoff|depart\w*)\b(?![^.?!]{0,12}\btarget)|"
                         r"\b(inbound|coming in|returning)\s+(for|to)\s+(land(ing)?|recovery)\b|"
                         r"\b(starting|commencing|beginning|on)\s+(my |the |an |final )?approach\b|\b(on|short) final\b|"
                         r"\bcan i (land|take ?off)\b|\b(am i )?clear(ed)? (to land|for (landing|take ?off|takeoff))\b)",
                         re.I)
# Review A (11-11): an attack run is not a landing ("on approach to the target", "on final to the target"): target,
# attack, weapon or unit words veto the landing answer. A bare "starting approach" / "on final" (nothing that says
# airfield) is a landing only in a landing context (an RTB/field exchange just before, or a friendly field within
# LANDING_CONTEXT_NM); otherwise it is acknowledged. "commencing" counts as attack wording unless a landing cue is said.
ATTACK_RUN = re.compile(r"\b(targets?|runs?|running|run in|attack\w*|sites?|sams?|sa-?\s?\d+|ip|egress\w*|bomb\w*|"
                        r"rockets?|guns?|strafe|strafing|mavericks?|weapons?|missiles?|armou?r|tanks?|trucks?|convoy|"
                        r"bridge|bunker|enemy|hostiles?|bandits?|triple a|aaa|radar|launcher|objective|"
                        r"laser\w*|lasing|tally|spot|wings level|in hot|off target|pickle\w*|shooting|release|mark)\b", re.I)
LANDING_CUE = re.compile(r"\b(land|landing|lands|pattern|overhead|initial|base leg|downwind|gear|field|airfield|air ?base|"
                         r"airport|tower|runway|recovery|home ?plate|rtb|take ?off|takeoff|depart\w*|request\w*|"
                         r"permission|clearance|ready|can i|straight[- ]in)\b", re.I)
LANDING_CONTEXT = re.compile(r"\b(rtb|home ?plate|divert|recovery|returning|land\w*|air ?fields?|air ?bases?|"
                             r"airports?|nearest (friendly )?(field|base))\b", re.I)
LANDING_CONTEXT_NM = 15
BARE_APPROACH = re.compile(r"\b(starting|commencing|beginning|on)\s+(my |the |an |final )?approach\b|\b(on|short) final\b",
                           re.I)
# "any friendlies hit", "any friendlies near the target / in the area", "blue on blue": a fratricide or deconfliction
# question, never answered from the air-traffic list ("No blue aircraft showing" is a false all-clear near a target).
FRIENDLY_FIRE = re.compile(r"\b(hit|hits|fire|fired|shot|struck|targets?|area|ground|blue on blue|fratricide|bomb\w*|"
                           r"kill\w*|danger close)\b", re.I)
FRIENDLY_AIR_ASK = re.compile(r"(\b(any|where are|where's|wheres|show me|see any|got any|are there( any)?|is there( any)?)"
                              r"\s+(the |my )?(friendl(ies|y)|blues?)\b(?!\s+(fire|kills?))|"
                              r"\bfriendl(ies|y)\s+(in the air|airborne|aloft|up)\b|"
                              r"\bfriendly\s+(aircraft|air|planes?|jets?|fighters?|flights?|helos?|helicopters?)\b)", re.I)
# Re-review AB2: "any friendlies near the convoy / by the bridge / where I'm shooting" asks about a place, not the air
# traffic list. It gets the friendly units near the PILOT and "I can't see your target" (Athena has no target position
# for the pilot's aim point), never "No blue aircraft showing".
FRIENDLY_WORD = re.compile(r"\b(friendl(ies|y)|blues?)\b", re.I)
FRIENDLY_PLACE = re.compile(r"\b(near\w*|by|around|area|close|next to|vicinity|within|down there|aim\w*|shoot\w*|"
                            r"targets?|convoy|bridge|at (the|my|that|this|those))\b",
                            re.I)
FRIENDLY_HIT = re.compile(r"\b(hit|hits|fire|fired|shot|struck|blue on blue|fratricide|bomb\w*|kill\w*|"
                          r"danger close)\b", re.I)
FRIENDLY_NEAR_NM = 5


def friendly_near_calls(pic):
    """Friendly (and neutral) air and ground groups within FRIENDLY_NEAR_NM of the pilot, nearest first, always after
    "I can't see your target." (a list reply: at most three calls, then "Plus N more."); None without friendly data."""
    groups = pic.get("friendly_groups")
    if not isinstance(groups, list):
        return None
    near = sorted((g for g in groups if isinstance(g, dict) and g.get("call") and g.get("side") != "hostile"
                   and isinstance(g.get("range_nm"), (int, float)) and g["range_nm"] <= FRIENDLY_NEAR_NM),
                  key=lambda g: g["range_nm"])
    return ["I can't see your target."] + ([g["call"] for g in near] or ["No friendly units within five miles of you."])


# A spoken reply never clears or approves anything, and never gives airfield data Athena does not have (live 11-11
# 19:01: "Cleared to land, no traffic."; review A: "approved", "you may land", "runway two one", "wind calm").
_NUM = (r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|niner|ten|eleven|twelve|thirteen|fourteen|fifteen|"
        r"sixteen|seventeen|eighteen|nineteen|twenty|thirty)")
_ATC_ACT = (r"(land|landing|take ?off|takeoff|depart\w*|approach|(the )?(ils|visual|straight[- ]in|option|"
            r"overhead|missed)( approach)?|taxi|touch[- ]and[- ]go|touch ?down|the option|runway|pattern|circuit|final|"
            r"break|immediate|ils|visual|come in|(the )?(air)?field|in(?=\s*([,.;!?]|$))|hot|engage)")
# Re-review AB2: ATC asks never reach the model (fixed field + traffic answer). For every other ask this family is
# dropped from the model's reply sentence by sentence, never replaced; nothing left = "say again". Athena never
# clears weapons either ("cleared hot", "weapons free"): she has no friendly ground picture.
CLEARANCE = re.compile(
    r"\bclear(ed)?\s+((to|for)\s+(the\s+)?)?%s\b|\b(approved|approval|granted)\b|\bdiscretion\b|\bpermission\s+(is\s+)?"
    r"(granted|approved|given)\b|\b(may|can)\s+(now\s+)?(land|take ?off|depart|taxi)\b|"
    r"\b(good|ok|okay|free|safe|go ahead|you'?re fine)\s+to\s+(land|take ?off|depart|taxi|come in)\b|\bland at will\b|"
    r"\brunways?\s+(is\s+|are\s+)?(%s|in use|heading|left|right|center|cent(er|re)line|yours)\b|\baltimeter\b|"
    r"\bq(nh|fe)\b|\bsquawk\w*\b|\bwinds?\s+(is\s+|are\s+)?(calm|variable|light|from|at|%s)\b|"
    r"\b(contact|switch( to)?|monitor)\s+(the\s+)?(tower|ground|approach|departure|atis)\b|\bhold short\b|"
    r"\bline up( and wait)?\b|\btaxi (to|via|into)\b|\breport (\w+ )?(mile )?(base|final|downwind|initial|the break)\b|"
    r"\benter (the )?(left |right )?(downwind|base|pattern)\b|\bmake (left|right) traffic\b|\bextend\w* (your )?downwind\b|"
    r"\b(you('?re| are) )?number (one|two|three|\d) (for|to) (land|landing|take ?off|takeoff|departure)\b|"
    r"\ball yours\b|\b(pattern|runway|field|airfield|approach)('s| is) (all )?yours\b|\bactive( runway)? (is|in use)\b|"
    r"\bcontinue (your |the )?approach\b|\b(land|landing|use|expect)(\s+the)?(\s+ils|\s+runway)?\s+%s[\s-]+%s\b|"
    r"\bexpect (the )?(ils|runway|visual)\b|\bcome (on )?in\b|(,|\band)\s+land\b|"
    r"\bproceed with (the )?(landing|approach|takeoff|departure)\b|\bweapons (are )?free\b|"
    r"\bclear(ed)? to (fire|release|drop|shoot)\b" % (_ATC_ACT, _NUM, _NUM, _NUM, _NUM),
    re.I)


def clearance_in(reply):
    """True when a reply clears or approves anything, or gives a runway, wind or altimeter (Athena has none)."""
    return bool(CLEARANCE.search(reply or ""))


def strip_clearance(reply):
    """The reply without every sentence that clears, approves or gives airfield data; '' when nothing is left."""
    body = (reply or "").strip()
    address = body.startswith(ADDRESS)
    if address:
        body = body[len(ADDRESS):]
    sentences = _sentences(body)
    kept = [s for s in sentences if not clearance_in(s)]
    if len(kept) == len(sentences):
        return (reply or "").strip()
    text = " ".join(kept).strip()
    if not text.rstrip(" ,.") or text.rstrip(" ,.") in ("Prometheus", "Prometheus, Athena", "Athena"):
        return ""
    text = text[0].upper() + text[1:] if address and text[0].islower() else text
    return (ADDRESS + text) if address else text


def _plain_call(call):
    """The close-bandit warning is the compact call plus "Bandit "; the same call in a picture counts as said."""
    return call[len("Bandit "):] if call.startswith("Bandit ") else call

def landing_context(pic, recent=()):
    """A bare 'starting approach' is a landing call: an RTB/field exchange just before it, or a friendly field close."""
    if any(LANDING_CONTEXT.search(q or "") and not NOT_FIELD.search(q) and not re.search(r"\blanded\b", q, re.I)
           for q in recent or ()):  # "the bomb landed short" / "SAMs near the airfield?" are no landing context
        return True
    if "ownship" in (pic or {}) and not pic["ownship"]:
        return True  # no pilot position: no distance to judge by; the no-position answer is the honest one
    friendly = ((pic or {}).get("airbases") or {}).get("nearest_friendly") or []
    rng = friendly[0].get("range_nm") if friendly and isinstance(friendly[0], dict) else None
    return isinstance(rng, (int, float)) and rng <= LANDING_CONTEXT_NM


def _landing_class(t, pic, recent):
    """'landing', 'ack', None (model) or False (not an approach/landing call) for one transmission."""
    if not LANDING_ASK.search(t):
        return False
    if ATTACK_RUN.search(t):  # an attack run: acknowledged as before (a question goes to the model)
        return None if _QUESTION.search(t) else "ack"
    cue = LANDING_CUE.search(t) or named_field(t, pic)[0] is not None
    if cue or (BARE_APPROACH.search(t) and not re.search(r"\bcommenc\w*", t, re.I) and landing_context(pic, recent)):
        return "landing"
    return "ack"


def call_class(text, pic=None, recent=()):
    """dev_note | kill_report | ack | picture | kill_confirm | ... | None for one pilot transmission.
    recent = the pilot's previous transmissions (newest last), for a bare 'starting approach'."""
    t = (text or "").strip()
    if DEV_NOTE.search(t):
        return "dev_note"
    if TALLY_ASK.search(t):
        return "kill_confirm"  # TOTAL_ASK (tally) makes it the mission total
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
    if "loadout" in focus_of(t) and not KILL_ASK.search(t) and not (FUEL_ASK.search(t) and not WEAPON_WORD.search(t)):
        return "loadout"  # "how much fuel do I have left" is fuel
    if LAUNCH_ASK.search(t) and not OWN_LAUNCH.search(t):
        return "launch"
    landing = _landing_class(t, pic, recent)
    if landing is not False:
        return landing
    if BINGO_RTB.search(t):
        return "airfield"  # "Bingo fuel, RTB": fuel and the nearest friendly field
    if not NOT_FIELD.search(t) and (NEAREST_FIELD.search(t) or VECTOR_ASK.search(t) and (
            FIELD_WORD.search(t) or _field_said(t, pic))):
        return "airfield"
    if ATC_ASK.search(t) and not (ATTACK_RUN.search(t) or NOT_FIELD.search(t)):
        return "atc"  # never the model: the fixed field + traffic answer (re-review AB2)
    if TRAFFIC_ASK.search(t) and not GROUND_ASK.search(t):
        return "traffic"  # "blue traffic in the area": the pilot asked for aircraft
    if FRIENDLY_WORD.search(t) and FRIENDLY_PLACE.search(t) and not FRIENDLY_HIT.search(t):
        return "friendlies_near"  # "any friendlies near the convoy": never the air-traffic list (re-review AB2)
    if FRIENDLY_AIR_ASK.search(t) and not (GROUND_ASK.search(t) or KILL_ASK.search(t) or TOTALS_ASK.search(t)
                                           or FRIENDLY_FIRE.search(t)):
        return "traffic"  # "how many friendly aircraft" is a count, not a traffic call
    if FUEL_ASK.search(t) and not KILL_ASK.search(t):
        return "fuel"
    age = (pic or {}).get("last_attack_age_s")
    explicit = target_damage or focus_of(t) or BEARING_ASK.search(t) or PICTURE_ASK.search(t) or GROUND_ASK.search(t)
    if CONFIRM_ASK.search(t) and not explicit and age is not None and 0 <= age < 300:
        return "kill_confirm"
    if PICTURE_ASK.search(t) and LONG_ASK.search(t):
        return "long_picture"
    if PICTURE_ASK.search(t):
        return "ground_picture" if GROUND_ASK.search(t) else "picture"
    if ACK.search(t) and not REQUEST.search(t):
        return "ack"
    return None


def _splash_target(alert):
    """'Splash one T-55 (likely).' -> 't-55'; never empty (an empty target would match everything)."""
    t = re.sub(r"^\s*splash\s+(one|two|three|\w+)\s+", "", (alert or ""), flags=re.I).strip(" .!")
    t = _designation(re.sub(r"\s*\(?likely\)?$", "", t, flags=re.I).strip(" .,")).lower()
    return t or "\x00no-target\x00"


def _alert_needle(alert, field):
    """What a model reply must contain for a splash / RWR call to count as said (it may reword the call):
    'splash' + the target, else the threat designation (SA-6), else its clock position."""
    if field == "splash_to_announce":
        return ("splash", _splash_target(alert))
    m = re.search(r"\b(SA-?\d+|[A-Z][\w/]*-\d[\w-]*)", alert or "") or re.search(r"\b\w+ o'clock\b", alert or "")
    return m.group(0) if m else ("\x00no-needle\x00",)


def spoken_target(name, category=None, dcs_type=False):
    """Radio-safe target name: scenery codes become words, unknowns become 'target'.
    dcs_type=True: the name is a raw DCS type string (shot rows) -> plain controller name (SA-2, Ural truck)."""
    name = (name or "").strip().lstrip("- ").strip()
    for pattern, word in SCENERY_NAMES:
        if pattern.search(name):
            return word
    if not name or name.lower() == "unknown" or (category == "scenery" and re.search(r"[()_]", name)):
        return "structure" if category in ("scenery", "static") else "target"
    if not dcs_type:  # 'T 55' (a static T-55 named like scenery) and 'T-55' are one type, said and tallied once (11-11)
        name = _designation(name)
    return spoken_name(name, "target") if dcs_type else name


def _designation(name):
    """'T 55' -> 'T-55', 'BMP 2' -> 'BMP-2' (scenery naming drops the hyphen); other names are unchanged."""
    return re.sub(r"^([A-Z]{1,4}) (\d{1,4}[A-Za-z]{0,3})$", r"\1-\2", name)


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


def fuel_calls(fuel):
    """ownship.fuel as one sentence (flight 11-11: the model said "fuel unknown" with 3,640 lb in the picture)."""
    if not isinstance(fuel, dict):
        return None
    def real(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
    lb, pct = fuel.get("total_lb"), fuel.get("internal_pct")
    if fuel.get("known") is False or not (real(lb) or real(pct)):
        reason = fuel.get("reason")
        return ["Fuel unknown, %s." % reason if reason else "Fuel unknown."]
    if (real(lb) and lb == 0) or (not real(lb) and pct == 0):
        amount = "zero"  # a real reading of zero is zero, never "unknown"
    elif real(lb):
        amount = fuel.get("spoken") or "%s pounds" % format(int(fuel["total_lb"]), ",")
    else:
        amount = "%s percent" % words(fuel.get("total_pct") or fuel["internal_pct"])
    extra = [", estimated"] if fuel.get("estimated") else []
    if fuel.get("includes_external") or (fuel.get("external_lb") or 0) > 0:
        extra.append(", including external tanks")
    return ["Fuel %s%s." % (amount, "".join(extra))]


FIELD_NOISE = re.compile(r"\b(air ?force ?base|afb|air ?bases?|air ?fields?|air ?ports?|air ?strips?|airdrome|"
                         r"aerodrome|international|intl|executive|fields?|bases?)\b")
_NOT_A_NAME = (r"(?!(?i:the|an?|my|our|this|that|nearest|closest|friendly|home|base|landing|land|takeoff|take|approach|"
               r"final|recovery|me|you|athena|prometheus)\b)")
NAMED_FIELD = re.compile(r"\b(?i:to|at|into|for)\s+%s[A-Z][\w'\u2019-]+|"
                         r"\b%s(?i:[a-z][\w'\u2019-]*\s+(air ?force ?base|afb|air ?port|air ?base|air ?field))\b"
                         % (_NOT_A_NAME, _NOT_A_NAME))


def _field_key(name):
    """'Nelli's Air Force Base' -> 'nellis', 'McCarran International' -> 'mccarran' (case, accents, possessives,
    punctuation and generic field words ignored)."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower().replace("'", "")
    return " ".join(FIELD_NOISE.sub(" ", re.sub(r"[^a-z0-9]+", " ", s)).split())


def _edits(a, b, cap):
    """Levenshtein distance of a and b, or cap + 1 once it is certainly above cap."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def airfields_of(pic):
    """Every airfield the picture knows: the Eye's full mission list (_airfields, never shown to the model), else the
    listed nearest and nearest friendly fields. One row per name; a row with traffic data wins."""
    ab = (pic or {}).get("airbases") or {}
    rows = pic.get("_airfields") if isinstance((pic or {}).get("_airfields"), list) else (
        list(ab.get("nearest") or []) + list(ab.get("nearest_friendly") or []))
    out = {}
    for r in rows:
        if isinstance(r, dict) and r.get("name") and (r["name"] not in out or "traffic" not in out[r["name"]]):
            out[r["name"]] = r
    return list(out.values())


_NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "niner": "9", "ten": "10", "eleven": "11", "twelve": "12"}


def _name_tokens(text):
    """Field-name words: _field_key with number words as digits ('H four' -> h 4)."""
    out = []
    for w in _field_key(text).split():
        w = _NUMBER_WORDS.get(w, w)
        if w.isdigit() and out and out[-1].isdigit():
            out[-1] += w  # 'zero one' -> 01
        else:
            out.append(w)
    return out


def _digits(s):
    return re.sub(r"\D", "", s)


def named_field(text, pic):
    """(row, named): the airfield the pilot named. named is True when a field name was spoken, found or not, and
    'ambiguous' when the words were close to a field but not surely that one. Review A (11-11): never the wrong field.
    An exact (normalised) name wins; a speech-recognition slip ('Nelli's Air Force Base') is accepted only when it is
    unambiguous: within the edit tolerance (0 up to 4 letters, 1 up to 7, else 2; same first letter, same digits), at
    least two edits better than any other field, and not a short name inside a longer spoken name ('Tonopah Test' is
    not Tonopah, 'H four' is not H, 'H3 Southwet' is not H3)."""
    said = _name_tokens(text)
    found = []  # (edits, allowed, start, end, row, key tokens)
    for row in airfields_of(pic or {}):
        key = _name_tokens(row["name"])
        flat = "".join(key)
        if not flat:
            continue
        allowed = 0 if len(flat) <= 4 else 1 if len(flat) <= 7 else 2
        best = None
        n = len(key)
        for size in sorted({max(1, n - 1), n, n + 1}):
            for i in range(len(said) - size + 1):
                cand = "".join(said[i:i + size])
                if cand[0] != flat[0] or _digits(cand) != _digits(flat) or abs(len(cand) - len(flat)) > allowed + 1:
                    continue
                d = _edits(cand, flat, allowed + 1)
                if d <= allowed + 1 and (best is None or (d, -size) < (best[0], -(best[3] - best[2]))):
                    best = (d, allowed, i, i + size, row, key)
        if best is not None:
            found.append(best)
    matches = [m for m in found if m[0] <= m[1]]

    words_of = [(_name_tokens(r["name"]), r) for r in airfields_of(pic or {})]

    def other_word(w, m):
        """w (a spoken word next to the match) is a word of another field's name: 'H Northwest', 'pInvisible FARP'."""
        return len(w) >= 4 and any(r is not m[4] and w not in m[5] and any(
            w == k or (len(k) >= 5 and abs(len(k) - len(w)) <= 2 and _edits(w, k, 2) <= (1 if len(k) < 8 else 2))
            for k in key)
            for key, r in words_of)

    def continues(m):
        """The spoken name goes on past this field's name: a digit ('H four'), a word of another field's name, or the
        next word of a longer field that starts with this name ('Tonopah Test' -> Tonopah Test Range)."""
        if m[2] > 0 and other_word(said[m[2] - 1], m):
            return True
        if m[3] >= len(said):
            return False
        nxt = said[m[3]]
        if (nxt.isdigit() and not m[5][-1][-1:].isdigit()) or other_word(nxt, m):
            return True
        for r in airfields_of(pic or {}):
            longer = _name_tokens(r["name"])
            if len(longer) > len(m[5]) and longer[:len(m[5])] == m[5]:
                word = longer[len(m[5])]
                if nxt == word or (len(nxt) >= 3 and word.startswith(nxt)) or (
                        len(word) >= 5 and _edits(nxt, word, 1) <= 1):
                    return True
        return False
    # a match inside a longer match's words explains less of what was said (H3 inside 'H3 Southwet')
    kept = [m for m in matches if not any(o is not m and o[2] <= m[2] and m[3] <= o[3] and o[3] - o[2] > m[3] - m[2]
                                          for o in matches)]
    dropped = [m for m in matches if m not in kept]
    rejected = [m for m in kept if continues(m)]
    kept = [m for m in kept if m not in rejected]
    exact = [m for m in kept if m[0] == 0]
    if exact:
        return max(exact, key=lambda m: m[3] - m[2])[4], True
    if kept:
        best = min(kept, key=lambda m: (m[0], -(m[3] - m[2])))
        rivals = [m for m in found if m[4] is not best[4] and m not in dropped and m[0] - best[0] < 2
                  and not (best[2] <= m[2] and m[3] <= best[3] and m[3] - m[2] < best[3] - best[2])]
        if not rivals:
            return best[4], True
    if matches or rejected:
        return None, "ambiguous"
    return None, bool(NAMED_FIELD.search(text or ""))


def _field_said(text, pic):
    """A field was named: matched, or close to a field but not surely which one ('I don't have that field')."""
    row, named = named_field(text, pic)
    return row is not None or named == "ambiguous"


def _field_call(row, coalition):
    """The field's ready call line; a field that is not the pilot's side says its owner after the name."""
    call = row.get("call")
    if not call:
        return "%s, position unknown." % row.get("name")
    owner = row.get("owner")
    if owner in ("red", "blue", "neutral") and coalition and owner != coalition:
        word = "neutral field" if owner == "neutral" else "enemy field, %s" % owner
        call = call.replace(row["name"] + ", ", "%s, %s, " % (row["name"], word), 1)
    return call


def _traffic_lines(row, pic):
    """The field's traffic from the picture (aircraft on or near it); never a clearance, runway or wind.
    Review A (11-11): a hostile aircraft at the field is read first and as a bandit, with its distance from the
    field; then the other traffic (count and the nearest one)."""
    traffic = row.get("traffic")
    if not isinstance(traffic, dict):  # nearest_friendly rows carry no traffic: the same field's full row does
        same = [r for r in airfields_of(pic) if r.get("name") == row.get("name")]
        traffic = same[0].get("traffic") if same else None
    if not isinstance(traffic, dict):
        return ["Traffic unknown."]
    planes = [a for a in traffic.get("aircraft") or [] if isinstance(a, dict) and a.get("call")]
    n = traffic.get("count") or len(planes)
    if not n:
        return ["No traffic."]
    hostile = [a for a in planes if a.get("side") == "hostile"]
    others = [a["call"] for a in planes if a.get("side") != "hostile"]
    lines = []
    for a in hostile:
        call = a["call"] if a["call"].startswith("Bandit ") else "Bandit " + a["call"]
        far = a.get("field_range_nm")
        if a.get("on_field"):
            call = call.rstrip(". ") + ", on the field."
        elif isinstance(far, (int, float)):
            miles = max(1, round(far))
            call = call.rstrip(". ") + ", %s mile%s from %s." % (words(miles), "" if miles == 1 else "s", row.get("name"))
        lines.append(call)
    rest = n - len(hostile)
    if rest > 0:
        lines += ["%s, %s aircraft." % ("Other traffic" if hostile else "Traffic", words(rest))] + others[:1]
    return lines  # _fit_calls drops whole lines from the end: the bandits stay


def airfield_calls(klass, text, pic):
    """Nearest friendly field, a named field, or a landing/approach/takeoff request (field + its traffic)."""
    ab = pic.get("airbases")
    if not isinstance(ab, dict) or "nearest_friendly" not in ab:
        return None  # no pilot position on the fields: the model says what it has
    coalition = (pic.get("ownship") or {}).get("coalition")
    friendly = [r for r in ab["nearest_friendly"] if isinstance(r, dict) and r.get("call")]
    row, named = named_field(text, pic)
    lines = []
    if row is None:
        if named:
            lines.append("I don't have that field.")
        if not friendly:
            return lines + ["No friendly airfield known."]
        row = friendly[0]
        lines.append(("Nearest friendly, " if named else "") + row["call"])
    else:
        lines.append(_field_call(row, coalition))
    if klass == "landing":
        lines += _traffic_lines(row, pic)
    return lines


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
    return fit_reply(" ".join(parts), DEBRIEF_CHARS)[0]


# No pilot position (ownship null): these answers are a range/bearing from the pilot, so they cannot be given.
POSITION_CLASSES = ("picture", "long_picture", "ground_picture", "traffic", "atc", "launch", "airfield", "landing",
                    "friendlies_near")
TOTALS_ASK = re.compile(r"\b(how many|kill\w*|lost|left|alive|destroyed|dead|war)\b", re.I)
# Kill reports and status asks ("SA-2 is down", "tally SA-2", "mission status threats") need no position, unless
# they also ask for a range or bearing ("nearest", "where").
REPORT_ASK = re.compile(r"\b(down|shack|tally|neutrali[sz]ed|suppress\w*|splash|hits?|mission status|kill status|got the|burning|toast|is out|off the air|scratch|gone)\b", re.I)
RANGE_ASK = re.compile(r"\b(nearest|closest|nearby|near me|am i clear|vectors?|rtb|where|bearing|braa|any (other |more )?(threats?|sams?)|anything else|what else|there|range|low|ahead|area|route|target|safe|am i good|tracked|worry|threats? status|sams? status|rwr status|status of|threat picture|painting|(sams?|threats?) tally)\b", re.I)
NEEDS_POSITION = re.compile(r"\b(threats?|sams?|nearest|closest|nearby|near me|am i clear|rtb|vectors?|defending|sa-?\d+)\b", re.I)


def requested_calls(klass, text, pic):
    """Render fixed-format air calls directly: a model/speech cap must not drop their second sentence."""
    if not pic.get("ownship"):
        totals = bool(TOTALS_ASK.search(text) or (REPORT_ASK.search(text) and not RANGE_ASK.search(text)))  # kills / counts / reports need no position
        if "ownship" in pic and (klass in POSITION_CLASSES or (klass in (None, "ack") and not totals and (
                NEEDS_POSITION.search(text) or (klass is None and (
                    GROUND_ASK.search(text) or BEARING_ASK.search(text) or "rwr" in focus_of(text)))))):
            return [NO_POSITION]  # never an all-clear from an empty list (live 2026-11-11)
        return ["Airfield data unavailable."] if klass in ("atc", "landing") else None  # an ATC ask never reaches the model
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
        calls = [r["call"] if isinstance(r, dict) else r for r in rows]
        if calls:
            return calls  # nearest first; _fit_calls shows at most three that fit, then "Plus N more."
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
    if klass == "fuel":
        lines = fuel_calls(pic["ownship"].get("fuel"))
        if lines and WEAPON_WORD.search(text):  # "fuel and weapons": both halves
            lines = lines + (loadout_calls(pic["ownship"], text) or [])
        return lines
    if klass == "friendlies_near":
        return friendly_near_calls(pic)
    if klass in ("airfield", "atc", "landing"):
        lines = airfield_calls("airfield" if klass == "airfield" else "landing", text, pic)
        if klass != "airfield":  # re-review AB2: an ATC ask never reaches the model (it cleared, invented runways)
            lines = (lines or ["Airfield data unavailable."]) + ([NO_RUNWAY_INFO] if RUNWAY_INFO_ASK.search(text) else [])
        if lines and BINGO_RTB.search(text):  # "Bingo fuel, RTB": the fuel, then the nearest friendly field
            lines = (fuel_calls(pic["ownship"].get("fuel")) or []) + lines
        return lines
    if klass == "impact":
        return impact_calls(pic)
    if klass == "loadout":
        lines = loadout_calls(pic["ownship"], text)
        if lines and re.search(r"\b%s\b" % _FUEL, text, re.I):  # "fuel and ammo state": both halves
            lines = (fuel_calls(pic["ownship"].get("fuel")) or []) + lines
        return lines
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


# A spoken reply never carries a picture key name or the words of an old prompt rule (live 2026-11-11 11:19:
# the model said "No position call." with a pilot position). Lower-case snake_case only: FA-18C_hornet stays.
PARROT_KEY = re.compile(r"(?<![\w-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w-])")


def parroted(text):
    return bool(re.search(r"no[ _-]position[ _-]call|\bownship\b|\bjson\b", text, re.I) or PARROT_KEY.search(text))


def clean(text):
    text = "".join(" " if unicodedata.category(c) in _UNSAFE else c for c in (text or "").replace("*", ""))
    return " ".join(text.split())


def _sentences(answer):
    out, start = [], 0
    for m in SENTENCE_END.finditer(answer):
        out.append(answer[start:m.end()].strip())
        start = m.end()
    if answer[start:].strip():
        out.append(answer[start:].strip())
    return out


def _word_cut(sentence, cap):
    """One long sentence: cut at the last word boundary within cap and end with a period (never mid-word)."""
    sentence = (sentence or "").strip()
    if len(sentence) <= cap:
        return sentence
    cut = sentence[:cap - 1]
    if not sentence[cap - 1:cap - 0].isspace() and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:-.") + "."
TEST_PILOT = "Test pilot."
ADDRESS = "Prometheus, Athena, "


def with_test_marker(answer, test):
    """While the TEST stand-in is ownship every spoken reply says so right after the address, so an AI jet can
    never pass for the pilot. fit_reply trims from the end, so the marker is never the part that is cut."""
    answer = (answer or "").strip()
    if not test or answer[len(ADDRESS):].startswith(TEST_PILOT):
        return answer
    if answer.startswith(ADDRESS):
        return ADDRESS + TEST_PILOT + " " + answer[len(ADDRESS):]
    return ADDRESS + TEST_PILOT + " " + answer


def fit_marked(answer, test):
    """fit_reply for a spoken reply, with the stand-in marker added: the rest is shortened to make room for it
    (never the marker, never by dropping the address)."""
    answer = (answer or "").strip()
    if not test or answer[len(ADDRESS):].startswith(TEST_PILOT):
        return fit_reply(answer)
    fitted, trimmed = fit_reply(answer, MAX_REPLY_CHARS - len(TEST_PILOT) - 1)
    return with_test_marker(fitted, True), trimmed


def fit_reply(answer, limit=MAX_REPLY_CHARS, word_trim=True):
    """The one reply budget: trim whole sentences from the end (the callsign / close-bandit / alert sentences
    stay first) until the reply fits `limit`. Returns (text, trimmed); text is None when word_trim=False
    and not even the first sentence fits, else the first sentence is cut at a word boundary."""
    answer = (answer or "").strip()
    if len(answer) <= limit:
        return answer, False
    sentences = _sentences(answer)
    while len(sentences) > 1:
        sentences.pop()
        text = " ".join(sentences)
        if len(text) <= limit:
            return text, True
    if not word_trim:
        return None, True
    return _word_cut(sentences[0] if sentences else answer, limit), True


ASK_REST = "Ask again for the rest."
DEFER = {"airfield": "Ask again for the vector.", "landing": "Ask again for the field.", "fuel": "Ask again for fuel.",
         "loadout": "Ask again for weapons.", "traffic": "Ask again for traffic.", "picture": "Ask again for the picture.",
         "long_picture": "Ask again for the picture.", "ground_picture": "Ask again for ground targets.",
         "atc": "Ask again for the field.", "friendlies_near": "Ask again for friendlies."}
LIST_CLASSES = {"picture": None, "long_picture": None, "traffic": None, "ground_picture": 3,
                "friendlies_near": 4}  # the first line ("I can't see your target.") always stays


def _fit_calls(klass, calls, room):
    """Whole fixed lines within `room` characters, never a call clipped in half. Contact lists (nearest first)
    keep the calls that fit and say how many were left out ("Plus two more."); other fixed replies drop
    whole lines from the end. Returns (lines or None when not even one fits, trimmed)."""
    def size(items):
        return len(" ".join(items))
    if klass in LIST_CLASSES:
        total = len(calls)
        cap = min(total, LIST_CLASSES[klass] or total)
        for show in range(cap, 0, -1):
            kept = calls[:show] + (["Plus %s more." % words(total - show)] if total > show else [])
            if size(kept) <= room:
                return kept, show < cap
        return None, True
    kept = list(calls)
    while kept and size(kept) > room:
        kept.pop()
    if len(kept) < len(calls):  # never drop a requested line without a word
        while kept and size(kept + [ASK_REST]) > room:
            kept.pop()
        kept += [ASK_REST] if kept else []
    return kept or None, len(kept) < len(calls)


IMPACT_KILL_LINES = ("Good hit, splash", "Likely kill on")  # impact_calls lines that report a kill


def _alert_rows(alerts, keys, field):
    """[(alert ending in '.', [keys])] in order. Identical splash calls are merged with their count
    ('Splash two Container 40Ft.'): two kills of one type are two kills, never one call (live 11-09 20:09)."""
    texts, keyed, counts = [], {}, Counter()
    for i, alert in enumerate(alerts or []):
        alert = (alert or "").strip()
        if not alert:
            continue
        if alert[-1] not in ".!?":
            alert += "."
        if alert not in keyed:
            texts.append(alert)
            keyed[alert] = []
        counts[alert] += 1
        if keys and i < len(keys) and keys[i] is not None:
            keyed[alert].append(keys[i])
    rows = []
    for alert in texts:
        said = alert
        if field == "splash_to_announce" and counts[alert] > 1:
            said = re.sub(r"^splash one\b", "Splash " + words(counts[alert]), alert, flags=re.I)
        rows.append((said, keyed[alert]))
    return rows


def time_left(deadline):
    """Seconds until `deadline` (time.monotonic()); TimeoutError when it has passed."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("exchange_deadline")
    return left


def _named_missile(text):
    m = NAMED_MISSILE.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if m.group(2):
        return "SA-%s" % m.group(2)
    return {"stinger": "Stinger", "sam": None, "manpad": "MANPADS", "manpads": "MANPADS"}.get(word, word.capitalize())
def _missile_name(kind):
    kind = str(kind or "").strip()
    m = re.match(r"SA-?(\d+)", kind, re.I)
    return "SA-%s" % m.group(1) if m else (kind or "missile")
UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
NUMWORD = re.compile(r"\b(?:%s|hundred|thousand|niner|and)(?:[\s-]+(?:%s|hundred|thousand|niner|and))*\b" % (
    "|".join(list(UNITS) + list(TENS)), "|".join(list(UNITS) + list(TENS))), re.I)


def _parse_run(toks):
    """One run of number words -> list of numbers. A run of single digits is a digit string (bearing); otherwise
    a new number starts when a word cannot continue the current one ('thirty nine forty six' = 39, 46)."""
    if len(toks) >= 2 and all(t in UNITS and UNITS[t] < 10 for t in toks):
        return [int("".join(str(UNITS[t]) for t in toks))]
    out, total, cur, last = [], 0, 0, None
    for t in toks:
        starts_new = last is not None and (
            (t in TENS and (cur % 100) != 0) or
            (t in UNITS and last in UNITS) or
            (t in UNITS and UNITS[t] >= 10 and last in TENS))
        if starts_new:
            out.append(total + cur)
            total, cur = 0, 0
        if t in UNITS:
            cur += UNITS[t]
        elif t in TENS:
            cur += TENS[t]
        elif t == "hundred":
            cur = (cur or 1) * 100
        elif t == "thousand":
            total += (cur or 1) * 1000
            cur = 0
        last = t
    out.append(total + cur)
    return out


def words_to_numbers(text):
    """Spoken numbers in a reply: 'three four eight' -> 348 (digit run), 'two thousand five hundred eighty' -> 2580."""
    found = []
    for m in NUMWORD.finditer(text):
        toks = [("nine" if t == "niner" else t) for t in re.split(r"[\s-]+", m.group(0).lower()) if t]
        runs, run = [], []
        for i, t in enumerate(toks):  # 'and' joins only after hundred/thousand ('one hundred and five')
            if t == "and":
                if run and run[-1] in ("hundred", "thousand"):
                    continue
                runs.append(run)
                run = []
                continue
            run.append(t)
        runs.append(run)
        for r in runs:
            if r:
                found.extend(_parse_run(r))
    for m in re.finditer(r"(?<![\w-])(\d[\d,]*(?:\.\d+)?)(?![\w-])", text):
        try:
            found.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return found


def reply_check(reply, pic, ask):
    """Compatibility entry for offline checks; live revision is per sentence."""
    from .reply_guardrails import revise
    _, changes = revise(reply, pic, ask)
    return "; ".join(reason for change in changes for reason in change["reasons"]) or None

class SimpleController:
    # RadioServer: hold concurrent transmissions for this controller (answered in turn) instead of 503.
    queues = True

    def __init__(self, tools, transcript, *, model, api_key, base_url="https://api.openai.com/v1",
                 timeout=12.0, eye=None, llm=None, speaker="Prometheus,Player,Client"):
        # tools: unused (legacy call signature); the Eye is the only source of the picture
        self.transcript, self.eye = transcript, eye
        self.model, self._key = model, api_key
        self._host = base_url.split("//", 1)[1].split("/", 1)[0]
        self._path = "/" + base_url.split("//", 1)[1].split("/", 1)[1].rstrip("/") + "/chat/completions"
        self.timeout = timeout
        self.history = deque(maxlen=8)
        self._chat_state = None
        self._last_delivered = None  # actual/assumed-sent speech, independent of reasoning history
        self.speaker = speaker
        self._llm = llm or self.ask
        self._turn = threading.Lock()  # one exchange at a time; later arrivals wait their turn
        self._waiting = 0
        self._waiting_lock = threading.Lock()
        self.empty_transcripts = 0  # whisper returned nothing usable (suppressed, counted)
        self._confirmed_kills = []
        self._pending_ack = None  # round 6: (abs_time, target) already read back to the pilot
        self._launch_undo = None  # phase 1: missiles-called state before this reply's launch call
        self._launch_covered = {}
        self._reported_missiles = {}  # round 9: missile key -> (range_nm, time) already called to the pilot
        self._last_launch_reply = 0.0
        self._last_exchange = None  # phase 1: what the previous reply marked said, undone if it was not sent
        self._mission = None  # phase 1: (eye reset time, DCS session); a change clears the controller's memory
        self.transmissions = 0

    # ---------------------------------------------------------------- picture
    def picture(self):
        if self.eye is None:
            return {"available": False, "reason": "no Athena Eye configured"}
        tick = getattr(self.eye, "tick", None)
        if tick is not None:  # phase 1: picture() is a pure read; raise alerts / settle shots now, feeder or not
            tick()
        pic = self.eye.picture()
        # memory6: optional light enrichment; the link owns a <=120 ms whole-exchange deadline.
        memory = getattr(self.eye, "memory", None)
        if memory is not None:
            section = memory.summary()
            if section is not None:
                pic["memory"] = section
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
    def _with_close_bandits(answer, close_bandits, marks, limit=MAX_REPLY_CHARS):
        """Put the first not-yet-called bandit inside 20 nm (current geometry) right after the callsign prefix,
        if the result fits `limit` (pass a larger limit to give it priority and let fit_reply trim the tail).
        Every call is recorded in `marks` as (key, call); only calls present in the FINAL text are marked said."""
        prefix = "Prometheus, Athena, "
        added = False
        for row in close_bandits or []:
            call = row["call"]
            marks.append((row["key"], _plain_call(call)))
            if _plain_call(call) in answer or added:
                continue  # already in the reply, or one new call per reply (the rest stay pending)
            candidate = (prefix + call + " " + answer[len(prefix):]) if answer.startswith(prefix) else (
                call + " " + answer)
            if len(candidate) <= limit:
                added, answer = True, candidate
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
        """Record kills as read back; returns what to undo if that reply is never transmitted."""
        added, restored = [], []
        for row in rows:
            added.append({"owner": owner, "key": row[0], "target": row[1], "weapon": row[3],
                          "t": row[5], "confidence": row[4]})
        self._confirmed_kills.extend(added)
        for row, c in upgraded:
            restored.append((c, c["confidence"]))
            c["confidence"] = row[4]
        del self._confirmed_kills[:-1000]
        return added, restored

    def _forget(self, undo):
        added, restored = undo or ((), ())
        self._confirmed_kills = [c for c in self._confirmed_kills if not any(c is a for a in added)]
        for c, confidence in restored:
            c["confidence"] = confidence

    @staticmethod
    def _count_line(rows, label, grouped=False):
        """'Confirmed: two T-55, one Ural-375.'; grouped=True ('two trucks, one SAM unit') when the itemised
        list would not fit the reply budget."""
        if not grouped:
            counts = Counter(r[1] for r in rows)
            return "%s %s." % (label, ", ".join("%s %s" % (words(n), t) for t, n in counts.items()))
        counts = Counter(_kill_group({"category": r[2]}) for r in rows)
        return "%s %s." % (label, ", ".join("%s %s" % (words(n), KILL_GROUP_WORDS[g][0 if n == 1 else 1])
                                            for g, n in counts.most_common()))

    def kill_confirm_calls(self, text, pic, pic_time=None, commit=True, room=MAX_REPLY_CHARS - 20):
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
        health = pic.get("data_health")
        unconfirmed = isinstance(health, dict) and health.get("combat_events", "ok") != "ok"

        def build(grouped):
            lines = []
            if TOTAL_ASK.search(text):
                for confidence, label in (("confirmed", "Mission total:"), ("likely", "Likely:")):
                    sel = [r for r in rows if r[4] == confidence]
                    if sel:
                        lines.append(self._count_line(sel, label, grouped))
                if not lines:
                    lines.append("No %skills yet." % prefix)
                return lines
            confirmed = [r for r in new if r[4] == "confirmed"]
            likely = [r for r in new if r[4] != "confirmed"]
            if len(confirmed) == 1 and not likely:
                lines.append(("Confirmed, wingman splash one %s." if wingman else "Confirmed, splash one %s.")
                             % confirmed[0][1])
            else:
                if confirmed:
                    lines.append(self._count_line(confirmed, "Wingman confirmed:" if wingman else "Confirmed:", grouped))
                if likely:
                    lines.append(self._count_line(likely, "Likely:", grouped))
            if upgraded:
                lines.append(self._count_line([r for r, _ in upgraded], "Now confirmed, earlier likely:", grouped))
            if not new and not upgraded and not wingman and self.recent_release(pic, cats):
                lines.append("Not confirmed yet, checking.")
            elif not new and not upgraded and unconfirmed:
                lines.append("No new %skills showing, but DCS kill reports are missing: kills may be unconfirmed."
                             % prefix)
            elif not new and not upgraded:
                lines.append("No new %skills since your last check." % prefix if rows
                             else "No %skills showing." % prefix)
            return lines

        # "what remains" is part of the answer: it counts against the room, never silently dropped
        rest = self.remaining_line(pic, cats) if REMAIN_ASK.search(text) else None

        def size(items):
            return len(" ".join(items + ([rest] if rest else [])))
        lines = [" ".join(line.split()) for line in build(False)]
        if size(lines) > room:  # one reply budget: name kill groups instead of every target type
            lines = [" ".join(line.split()) for line in build(True)]
        if rest and size(lines) > room:
            rest = self.remaining_line(pic, cats, short=True)  # one count instead of every category
        # remember only what this answer actually reported (category-filtered rows), upgrades in place;
        # commit=False: the caller commits once these lines are in the final, fitted reply
        if commit:
            self._remember(owner, new, upgraded)
        else:
            self._pending_ack = (owner, new, upgraded, list(lines))
        if rest:
            lines.append(rest)
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
        self._launch_undo = (dict(self._reported_missiles), self._last_launch_reply)  # restored if never sent
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
            scan = str(threats.get("missile_scan"))
            if scan.startswith("off"):  # the scan cannot run: never an all-clear; the picture says why
                return ["No launch seen; missile tracking is limited, %s." % (scan[5:] or "missile scan is not answering")]
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
            if shown[i].get("call"):  # its pending launch alert is this line: one sentence per missile (11-11)
                self._launch_covered.setdefault(shown[i]["call"].strip(), lines[-1])
        if len(missiles) > 3:
            lines.append("Plus %s more." % words(len(missiles) - 3))
        return lines
    @staticmethod
    def remaining_line(pic, cats, short=False):
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
        if short:
            n = sum(v for _, v in ground)
            return "Remaining: %s enemy ground unit%s." % (words(n), "" if n == 1 else "s")
        parts = ["%s %s" % (words(v), CATEGORY_WORDS.get(k, "enemy " + str(k)))
                 for k, v in sorted(ground, key=lambda kv: -kv[1])]
        return "Remaining: %s." % ", ".join(parts)

    # ---------------------------------------------------------------- llm
    def ask(self, messages, deadline=None):
        """The OpenAI API model. `deadline` (time.monotonic()) bounds connect and every read."""
        deadline = time.monotonic() + self.timeout if deadline is None else deadline
        body = json.dumps({"model": self.model, "messages": messages, "max_completion_tokens": 400,
                           "store": False}).encode()
        conn = http.client.HTTPSConnection(self._host, timeout=time_left(deadline))
        try:
            conn.request("POST", self._path, body=body, headers={
                "Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
            sock = conn.sock
            sock.settimeout(time_left(deadline))
            reply = conn.getresponse()
            # chunked, every wait bounded by the time left: a trickling body ends at the deadline, so an abandoned
            # radio-model thread closes its socket then instead of living as long as the peer keeps sending
            raw = b"".join(read_chunks(reply, sock, lambda: time_left(deadline)))
        finally:
            conn.close()
        if reply.status != 200:
            raise RuntimeError("openai_http_%d: %s" % (reply.status, raw[:300].decode("utf-8", "replace")))
        return json.loads(raw)["choices"][0]["message"]["content"].strip()

    def _call_model(self, messages, deadline):
        """(text or None, meta): the model's answer by `deadline`, whatever the backend does. meta says who
        answered THIS call (backend, model, fallback_reason) for the transcript. A backend that is still busy at
        the deadline is abandoned (its reads are bounded by the same deadline, so it closes its socket and ends right
        after) and the pilot hears FALLBACK."""
        llm = self._llm
        box = {}

        def run():
            try:
                if hasattr(llm, "answer_by"):  # CodexLLM: Codex, then the API fallback, inside the deadline
                    box["text"], box["meta"] = llm.answer_by(messages, deadline)
                elif llm == self.ask:
                    box["text"] = self.ask(messages, deadline)
                else:  # injected callable (tests, replay)
                    box["text"] = llm(messages)
            except Exception as error:  # noqa: BLE001 - every failure becomes the spoken fallback
                box["error"] = error
        worker = threading.Thread(target=run, daemon=True, name="radio-model")
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
        meta = box.get("meta") or {"backend": getattr(llm, "label", "openai-api"),
                                   "model": getattr(llm, "model", self.model)}
        if worker.is_alive():
            return None, dict(meta, fallback_reason="exchange_deadline")
        if "error" in box:
            failure = box["error"]
            return None, dict(meta, fallback_reason=type(failure).__name__ + ": " + str(failure)[:300])
        return box.get("text"), meta

    # ---------------------------------------------------------------- radio
    def _log(self, request, kind, **values):
        self.transcript.log(request=request, kind=kind, **values)

    def stream(self, request):
        """Answer EVERY transmission from the pilot on the command frequency (no 'Athena' prefix needed)."""
        arrived = time.monotonic()  # the exchange deadline counts from here, queue wait included
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
            self._log(request, "route", route="pending", outcome="queued_behind_previous")
        try:
            with self._turn:
                yield from self._answer(request, text, arrived)
        finally:
            with self._waiting_lock:
                self._waiting -= 1

    def _say(self, request, answer, route, start, llm_ms=0, trimmed=False, meta=None):
        """Log and send the reply as ONE frame (it already fits MAX_REPLY_CHARS). Every reply record and frame
        carries its route (fixed | llm | debrief | fallback), the model that answered, if any, and latency."""
        model = (meta or {}).get("model") if route == "llm" else None  # a fallback line has no author model
        model = str(model) if model else None
        values = {"llm": dict(meta), "provider": meta.get("backend")} if meta else {}
        self._log(request, "reply_generated", text=answer, outcome="intended", part=1, route=route, model=model,
                  **values, **({"trimmed": True} if trimmed else {}),
                  timings_ms={"llm": llm_ms, "total": round((time.monotonic() - start) * 1000)})
        stand_in = None  # the flag is sent only while the TEST stand-in setting is configured
        if getattr(self.eye, "test_ownship", None):
            stand_in = answer[len(ADDRESS):].startswith(TEST_PILOT)  # the marker is on every stand-in reply
        return response(request, speech=answer, state="ok", route=route, model=model, test_pilot=stand_in)

    def _mark_said(self, marks, answer):
        """Splash / threat / close-bandit keys are announced only when their call is in the final, fitted
        reply (needle None = the model was asked to say it and answered). Returns the keys marked."""
        flat = []
        if self.eye is None or not hasattr(self.eye, "mark_announced"):
            return flat
        low = answer.lower()
        for key, needle in marks:
            needles = needle if isinstance(needle, tuple) else (needle,)
            if all(n is None or n.lower() in low for n in needles):
                flat.extend(key if isinstance(key, list) else [key])
        if flat:
            self.eye.mark_announced(flat)
        return flat

    def _rearm(self, keys):
        """Make announced calls pending again (their reply never went out)."""
        if hasattr(self.eye, "unmark_announced"):
            self.eye.unmark_announced(keys)

    def _settle_previous(self, request):
        """Phase 1, 'said only when delivered': the gateway is serial, so by the time the next request arrives it
        has written the previous exchange's outcome to the shared transcript. A reply that was not transmitted
        (say again, too long, expired) un-says its splash / threat / close-bandit calls and kill read-backs,
        and leaves the chat history. No record (no gateway, other transcript dir) = assumed delivered."""
        last, self._last_exchange = self._last_exchange, None
        outcome_of = getattr(self.transcript, "outcome_of", None)
        if not last:
            return
        outcome = outcome_of(last["id"]) if outcome_of is not None else None
        if outcome is None or outcome == "sent":
            if last.get("delivery"):
                self._last_delivered = last["delivery"]
            return
        if last["keys"]:
            self._rearm(last["keys"])
        self._forget(last["kills"])
        if last.get("missiles") is not None:  # 'same missile' bookkeeping of a launch call nobody heard
            self._reported_missiles, self._last_launch_reply = last["missiles"]
        if last["history"] is not None and self.history and self.history[-1] is last["history"]:
            self.history.pop()
        self._log(request, "route", route="rearm", outcome="previous_reply_not_sent", previous=last["id"],
                  previous_outcome=outcome, rearmed=len(last["keys"]))

    def _check_mission(self, request):
        """A new mission or DCS session clears the controller's own memory: chat history, kills read back, missiles
        called. Eye.reset_at changes on mission_start and on a new DCS session id (the feeder resets the Eye); a
        stream reconnect or unit sweep in the same session does not change it."""
        marker = getattr(self.eye, "reset_at", None)
        if self._mission is not None and marker != self._mission:
            self.history.clear()
            self._chat_state = None
            self._last_delivered = None
            self._confirmed_kills = []
            self._reported_missiles = {}
            self._last_launch_reply = 0.0
            self._last_exchange = None
            self._log(request, "route", route="reset", outcome="new_mission_controller_memory_cleared")
        self._mission = marker

    def _check_chat(self, request, pic):
        """Only dialogue is invalidated on first session, reconnection or a new pilot unit.

        The first GetSessionId is not an Eye reset and mission_start may precede the
        event subscription (11-11 18:26). Never depend on that event for chat safety.
        """
        own = pic.get("ownship") or {}
        old = self._chat_state
        unit, changed = pilot_identity(own, old[2] if old else None)
        state = (getattr(self.eye, "session_id", None), pic.get("connected", pic.get("available")),
                 unit, getattr(self.eye, "chat_generation", None))
        old = self._chat_state
        if old is not None and (state[0] != old[0] or (state[1] and not old[1]) or
                                changed or state[3] != old[3]):
            self.history.clear()
            self._last_delivered = None
            self._log(request, "route", route="reset", outcome="chat_context_cleared")
        self._chat_state = state

    def _answer(self, request, text, arrived=None):
        start = time.monotonic() if arrived is None else arrived
        deadline = start + EXCHANGE_DEADLINE_S
        self._check_mission(request)
        self._settle_previous(request)
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
            answer, trimmed = fit_reply(answer, DEBRIEF_CHARS)
            marks = []  # a close bandit still goes first; the debrief tail is trimmed to make room
            answer = self._with_close_bandits(answer, (dpic or {}).get("_close_bandits"), marks, MAX_SPEECH_CHARS)
            answer, net_trimmed = fit_marked(answer, isinstance(dpic, dict) and dpic.get("test_ownship"))
            trimmed = trimmed or net_trimmed
            if trimmed:
                self._log(request, "tool", tool="reply_budget", args={}, outcome="trimmed", result={"trimmed": True},
                          trimmed=True, route="debrief")
            keys = self._mark_said(marks, answer)
            self._last_exchange = {"id": request.transmission_id, "keys": keys, "kills": None, "history": None,
                                   "delivery": (text, answer, time.monotonic())}
            if memory is not None:
                memory.publish("radio_call", {"request": text, "reply": answer, "report_status": "queued" if queued else "unavailable"},
                               generation=generation)
            yield self._say(request, answer, "debrief", start, trimmed=trimmed)
            return
        klass = call_class(text)
        report = None
        if klass == "kill_report" and self.eye is not None and hasattr(self.eye, "pilot_report"):
            try:  # the Eye decides: never a kill on the pilot's word alone
                report = self.eye.pilot_report(text)  # before the picture: the confirmed kill is in it
            except Exception:  # noqa: BLE001 - never block the answer on bookkeeping
                report = None
        pic, error = self._picture_with_retry()
        self._check_chat(request, pic)
        repeat_answer = None
        if REPEAT_ASK.fullmatch(text):
            last = self._last_delivered
            if last and time.monotonic() - last[2] <= 120 and not re.search(r"say again", last[1], re.I):
                repeat_answer = last[1]
            elif last:
                text = last[0]  # re-answer the original question from this fresh picture
            else:
                repeat_answer = FALLBACK
        pic_time = time.time()  # round 6: kill identities use picture time, never post-LLM time
        klass = call_class(text, pic, [q for q, _ in list(self.history)[-3:]])
        if klass is None and time.time() - self._last_launch_reply <= 90 and LAUNCH_FOLLOWUP.search(text):
            klass = "launch"  # "do you see it?" right after a launch call
        splash_keys = pic.pop("_splash_keys", None) if isinstance(pic, dict) else None
        rwr_keys = pic.pop("_rwr_keys", None) if isinstance(pic, dict) else None
        close_bandits = pic.pop("_close_bandits", None) if isinstance(pic, dict) else None
        airfields = pic.pop("_airfields", None) if isinstance(pic, dict) else None  # resolved here, never in the prompt
        # Phase 0: the deterministic answer comes FIRST; the model is called only when there is none.
        self._pending_ack = None
        self._launch_undo = None
        self._launch_covered = {}  # missile alert call -> the launch line that says that missile
        self._confirm_covers_own_splashes = False
        self._confirm_is_own = False
        test = bool(isinstance(pic, dict) and pic.get("test_ownship"))
        prefix = ADDRESS + (TEST_PILOT + " " if test else "")  # fixed replies budget their room after the marker
        age = (pic.get("data_age_s") or {}).get("units")
        stale = ["Tracking disconnected; last known picture."] if not pic.get("connected", True) else []
        if age is not None and age > 60:
            stale.append("Contact data is %s seconds old." % words(age))
        if repeat_answer is not None:
            calls = [re.sub(r"^Prometheus,?\s*(Athena,?\s*)?", "", repeat_answer, flags=re.I)]
        elif klass == "launch" and pic.get("ownship"):
            calls = self.launch_calls(text, pic)
        elif klass == "kill_confirm" and pic.get("ownship") and "my_kills_this_mission" in pic:
            # the real room: a close bandit call goes first and takes its share (grouped kill wording then)
            first = [row["call"] for row in (close_bandits or [])[:1]]
            room = MAX_REPLY_CHARS - len(prefix + " ".join(first + stale + [""]))
            calls = self.kill_confirm_calls(text, pic, pic_time, commit=False, room=room)
        else:
            calls = requested_calls(klass, text, pic if airfields is None else dict(pic, _airfields=airfields))
        route = "fixed" if calls is not None else "llm"
        result = {"summary": self.picture_summary(pic), "picture": pic,
                  "error": (type(error).__name__ + ": " + str(error)[:200]) if error else ""}
        if klass:
            result["call_class"] = klass
        if report:
            result["pilot_report"] = report
        self._log(request, "tool", tool="picture", args={}, outcome="error" if error else "ok",
                  result=result, route=route,
                  counts={"empty_transcripts": self.empty_transcripts, "transmissions": self.transmissions},
                  timings_ms={"tool": round((time.monotonic() - start) * 1000)})
        marks = []  # (key, needle): announced only if the needle is in the final reply
        trimmed = False
        llm_ms = 0
        meta = None
        if calls is not None:
            lead = []  # one not-yet-called bandit inside 20 nm goes first; the requested lines fill the rest
            for row in close_bandits or []:
                needle = _plain_call(row["call"]).rstrip(". ")  # a traffic line may extend the call ("..., one mile from")
                marks.append((row["key"], needle))
                if not lead and needle not in " ".join(calls):
                    lead = [row["call"]]
            stale = lead + stale
            # round 9: drop whole contacts/lines from the end, never clip one mid-call
            room = MAX_REPLY_CHARS - len(prefix + " ".join(stale + [""]))
            # Review A (11-11): every pending missile-launch / SAM alert goes out first and whole; the requested
            # lines (field, traffic, fuel, contacts) get the room that is left, shortened or deferred
            # (re-review AB2: the alerts leave room for the "Ask again for ..." line; a further alert waits)
            reserve = 0
            defer = DEFER.get(klass, ASK_REST)
            for alert, _ in _alert_rows(pic.get("rwr_alert_to_announce"), rwr_keys, "rwr_alert_to_announce"):
                if (alert not in self._launch_covered and alert not in " ".join(calls)
                        and reserve + len(alert) + 1 <= room - len(defer) - 1):
                    reserve += len(alert) + 1
            fitted, trimmed = _fit_calls(klass, calls, room - reserve)
            if fitted is None and (lead or (reserve and _fit_calls(klass, calls, room)[0] is not None)):  # nothing requested fits: say so, the threats stay whole
                fitted, trimmed = ([defer] if len(defer) <= room - reserve else []), True
            if fitted is None:
                self._log(request, "controller_error", outcome="fixed_response_overflow", route=route,
                          counts={"speech_chars": len(prefix + " ".join(stale + calls))})
                answer = prefix + "requested calls are too long for one transmission. Ask for one group at a time."
                self._pending_ack = None  # nothing was read back: keep the previous acknowledgement state
                calls = []
            else:
                calls = fitted
                answer = prefix + " ".join(stale + calls)
                alerts = []
                remaining = MAX_REPLY_CHARS - len(answer)
                for field, keys in (("rwr_alert_to_announce", rwr_keys), ("splash_to_announce", splash_keys)):
                    rows = _alert_rows(pic.get(field), keys, field)
                    for alert, key in rows:
                        if field == "splash_to_announce" and klass == "kill_confirm" and self._pending_ack and (
                                self._confirm_covers_own_splashes
                                or (self._confirm_is_own and _splash_target(alert) in " ".join(calls).lower())):
                            # the confirmation itself reads the kill: said when its lines are in the reply
                            if key:
                                marks.append((key, self._pending_ack[3][0] if self._pending_ack[3] else None))
                            continue
                        if field == "rwr_alert_to_announce" and alert in self._launch_covered:
                            if key:  # the launch answer says this missile: said when its line is in the reply
                                marks.append((key, self._launch_covered[alert]))
                            continue
                        if key:
                            marks.append((key, alert))
                        if alert in alerts or alert in " ".join(calls):
                            continue  # already said in this reply
                        if len(alert) + 1 > remaining:
                            continue  # keep this alert pending for a later transmission
                        remaining -= len(alert) + 1
                        alerts.append(alert)
                if klass == "impact" and any(a.lower().startswith("splash") for a in alerts):
                    # "any hits?" with a splash pending: the splash call IS the result; never read the kill
                    # twice under two names (live 11-09 20:09)
                    calls = [c for c in calls if not c.startswith(IMPACT_KILL_LINES)]
                answer = prefix + " ".join(stale + alerts + calls)
        elif deadline - time.monotonic() < MIN_MODEL_S:
            # too old to answer usefully (it waited behind a slow exchange): no model call, an honest say again
            route, answer, meta = "fallback", FALLBACK, {"backend": None, "fallback_reason": "request_too_old"}
            self._log(request, "controller_error", outcome="request_too_old_no_model_call", route=route,
                      timings_ms={"total": round((time.monotonic() - start) * 1000)})
            answer = self._with_close_bandits(answer, close_bandits, marks, MAX_SPEECH_CHARS)
        else:
            messages = [{"role": "system", "content": PROMPT}]
            for q, a in self.history:
                messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
            messages.append({"role": "system", "content": "PICTURE " + json.dumps(
                pic, ensure_ascii=True, default=str, separators=(",", ":"))})
            hint = focus_hint(text)
            if hint:
                messages.append({"role": "system", "content": hint})
            if klass in CLASS_HINTS:
                messages.append({"role": "system", "content": CLASS_HINTS[klass]})
            messages.append({"role": "user", "content": text})
            t_llm = time.monotonic()
            answer, meta = self._call_model(messages, deadline)
            llm_ms = round((time.monotonic() - t_llm) * 1000)
            answer = clean(answer)
            if answer and clearance_in(answer):
                # Athena never clears, approves or invents airfield data (review A 11-11). ATC asks never get here;
                # any other reply loses only the offending sentences, never swapped for another answer (AB2)
                meta["clearance_removed"] = True
                answer = strip_clearance(answer)
                self._log(request, "controller_error", outcome="clearance_removed", route=route, llm=meta)
            if parroted(answer):  # never speak a key name / "no position call": fixed sentence or say again
                meta["fallback_reason"] = "parroted_picture_text"
                route, answer = "fallback", (ADDRESS + NO_POSITION if not pic.get("ownship") else FALLBACK)
                self._log(request, "controller_error", outcome="parroted_picture_text", route=route, llm=meta)
            if answer and route == "llm":
                from .reply_guardrails import revise
                self._guard_pic_time = pic_time
                answer, changes = revise(answer, pic, text, self)
                if changes:
                    meta["fallback_reason"] = "reply_check: " + "; ".join(
                        reason for change in changes for reason in change["reasons"])
                    meta["reply_changes"] = changes
                    route = "fallback"
                    self._log(request, "controller_error", outcome="reply_check_rejected", route=route, llm=meta)
            if not answer:
                route, answer = "fallback", FALLBACK
                meta.setdefault("fallback_reason", "empty answer")
                self._log(request, "controller_error", outcome=meta["fallback_reason"], route=route,
                          llm=meta, timings_ms={"llm": llm_ms, "total": round((time.monotonic() - start) * 1000)})
        if calls is None:
            # Same priority as fixed routes: close bandit, pending threats, splash,
            # then the model's answer. Reserve the deferral before admitting alerts;
            # never let final fitting cut a controller-owned call in half.
            from .reply_guardrails import sentences
            lead = [row["call"] for row in (close_bandits or [])[:1]]
            for row in (close_bandits or [])[:1]:
                marks.append((row["key"], row["call"]))
            pending = []
            for field, keys in (("rwr_alert_to_announce", rwr_keys), ("splash_to_announce", splash_keys)):
                pending.extend(_alert_rows(pic.get(field), keys, field))
            body = re.sub(r"^Prometheus,?\s*(Athena,?\s*)?", "", answer, flags=re.I)
            echoes = {a.rstrip(". ").casefold() for a, _ in pending} | {a.rstrip(". ").casefold() for a in lead}
            tail = [s for s in sentences(body) if s.rstrip(". ").casefold() not in echoes]
            defer = DEFER.get(klass, ASK_REST)
            alerts = []
            for alert, key in pending:
                if alert in lead + alerts:
                    if key:
                        marks.append((key, alert))
                    continue
                if len(prefix + " ".join(lead + alerts + [alert, defer])) <= MAX_REPLY_CHARS:
                    alerts.append(alert)
                    if key:
                        marks.append((key, alert))
            first = lead + alerts
            if first:
                room = MAX_REPLY_CHARS - len(prefix + " ".join(first)) - 1
                if len(" ".join(tail)) > room:
                    kept = []
                    for sentence in tail:
                        if len(" ".join(kept + [sentence, defer])) > room:
                            break
                        kept.append(sentence)
                    tail, trimmed = kept + [defer], True
                answer = prefix + " ".join(first + tail)
        answer, net_trimmed = fit_marked(answer, test)  # fixed replies already carry it; model / fallback get it here
        trimmed = trimmed or net_trimmed
        if trimmed:
            self._log(request, "tool", tool="reply_budget", args={}, outcome="trimmed", result={"trimmed": True},
                      trimmed=True, route=route)
        kills = None
        if self._pending_ack and all(line in answer for line in self._pending_ack[3]):
            kills = self._remember(*self._pending_ack[:3])  # kills read back only when every kill line was said
        self._pending_ack = None
        keys = self._mark_said(marks, answer)
        said = None
        factual = factual_history(answer)
        if factual:  # only the final spoken factual sentences, including repaired answers
            said = (text, factual)
            self.history.append(said)
        self._last_exchange = {"id": request.transmission_id, "keys": keys, "kills": kills, "history": said,
                               "missiles": self._launch_undo,
                               "delivery": (self._last_delivered[0] if repeat_answer is not None and self._last_delivered else text,
                                            answer, time.monotonic())}
        if memory is not None:  # memory6: generated radio reply, not a claim of SRS delivery
            memory.publish("radio_call", {"request": text, "reply": answer, "delivery": "generated"},
                           generation=pic.get("_memory_generation") if isinstance(pic, dict) else None)
        yield self._say(request, answer, route, start, llm_ms=llm_ms, trimmed=trimmed, meta=meta)

    def exchange(self, request):
        parts = list(self.stream(request))
        if not parts[0]["deliverable"]:
            return parts[0]
        return response(request, speech=" ".join(p["speech"] for p in parts), state="ok",
                        route=parts[0].get("route"), model=parts[0].get("model"),
                        test_pilot=parts[0].get("test_pilot"))
