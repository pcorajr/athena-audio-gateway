"""Athena Eye: lean in-memory tracker fed by DCS-gRPC, producing one compact picture for the LLM.

User direction 2026-11-07: enrichment over guardrails. Track everything (units, events, shots,
airbases), no freshness gates; every picture carries its data age instead.

Eye is pure state (feed it plain dicts in DCS-gRPC JSON shape, i.e. protobuf MessageToDict with
proto field names). GrpcFeeder converts live DCS-gRPC streams into those dicts, reconnecting forever.

Kill picture contract (round 6): my_kills_this_mission is cumulative, without a time/count cap.
my_kills_recent is its last 600 seconds, NOT the mission tally. Each kill has target_id, target_kind,
seq (first observation, stable on duplicate/confirmation), t (Unix seconds), mission_time (DCS seconds),
shooter_id, confidence and category. kill_seq is the latest kill cursor; kills_since(seq, shooter="me")
returns new kills without a time window (also accepts "all_friendly" or a shooter id as a string).
Consumers retain their own confirmation cursor; merely reading the picture never acknowledges kills.
friendly_kills_this_mission / friendly_kills_recent include me and other friendly shooters.
kill_tallies contains me, all_friendly and by_shooter (stable shooter-id keys, shooter descriptor,
flight_member, total, confirmed, likely, by_category and by_type). Published category spellings are
armor, AAA, SAM, infantry, vehicles, scenery, artillery, fighter, attack, bomber, AWACS, tanker,
transport, helicopter, attack helo, carrier, ship, train, weapon, cargo, airbase and unknown
(ground vehicle becomes vehicles; static objects use scenery).
Tallies include likely kills, explicitly counted separately. Explicit kill events supersede inferred
ownership by (target_kind, target_id): one active target, original seq/t, no superseded pilot splash.
Superseded rows remain in the internal ledger row's attribution_history, not active counts. A later
explicit shooter correction also replaces the owner; inference cannot override explicit ownership.
Cursor queries report new targets, not ownership changes; reread mission tallies for corrections.
Shot results also drop superseded credit: a real hit remains "hit"; without a hit the result is
"kill credited to another shooter". Area shots retain "kill" (or a likely result) while another
target still supports pilot credit. my_shots_last_15_min.by_result uses those corrected results.
Mission start/reset clears all tallies/history; sequence numbers never rewind.

Phase 1 item 3 (2026-11-10): every earlier picture key keeps its name and meaning. New or changed keys:
- Unit category (every `category`): classify() is the ONE labeller. Order: TYPE_OVERRIDES (types the 2026-11 review
  proved wrong) > aircraft type sets > DCS's own attributes (Unit.getDescByName(type).attributes, read once per type
  over the Eval channel and cached in TYPE_INFO) > the old name patterns. Fail safe: attributes never downgrade a
  type the name patterns call SAM/AAA (only TYPE_OVERRIDES can).
- threats.missiles_in_flight: the live Eval weapon scan is the main source. A shot-event row bridges until a scan
  SENT after the shot has answered (at least BRIDGE_S, at most EVAL_OFF_KEEP_S) and is merged into the scan row (by
  launcher group + type + launch point); a missile a newer scan did not report is gone. If no newer scan has
  answered when BRIDGE_S ends (scan stalled), the shot event raises the launch alert itself; with the scan off
  (Eval disabled / stale) it alerts at once, as in r11. threats.missiles_inbound = the subset of the same rows aimed
  at Prometheus or closing (no second source). One launch alert per missile; an alert expires when its missile is
  gone and its call is rebuilt from the live row. Never fewer calls than r11: a missile whose shot event pointed at
  Prometheus stays alertable in the scan; a salvo's scan row joins the shot whose flight time fits; a nil scan
  reply (pilot's unit not found) is not an empty scan.
- threats.sam_in_range rows gain "confidence": "DCS radar: tracking you" | "DCS radar: sees you" |
  "range only: could be tracking". DCS radar rows come from a 1 Hz read-only Eval of
  hostile radars (Unit:getRadar + group radar detection, threats.sam_radar) and only ADD confidence: a group whose
  radar DCS reports off or searching keeps the range-table "could be tracking" row and alert (plus "radar": "off" |
  "searching"), because a dark radar can ambush. Only labels (not air defence), dead units and command/comm
  vehicles remove a range warning. Range table with RANGE_HYST_NM hysteresis (no edge flicker).
  New: threats.sam_source ("dcs_radar" | "range_table"), threats.sam_radar
  {available, age_s, source, confidence, tracking_you[], radars_on}.
- connected: True while ANY DCS-gRPC stream is up (one bad stream no longer reads as "DCS disconnected").
  New data_health: {connected, streams{name: up|down ...}, events, combat_events, unit_sync, note}.
  event_stream.state may now also read "stale (heartbeat stopped; resubscribing)" (30 s when the 1 Hz
  simulation_fps heartbeat was flowing) or "combat events missing (kills may be unconfirmed)".
- After a unit stream (re)subscribes, units of that category that DCS does not re-send within the sync window are
  removed (recent_kills_and_losses row "no longer showing (missed during a data outage)", no kill credit).
- ownship.fuel: Unit:getFuel() x DCS getDesc().fuelMassMax when DCS gives it ("source" says so, not estimated);
  the airframe table is the fallback. Fractions above 1 = "includes_external": true.
- picture() is a pure read (no shot settling, alerts, pruning or kill-row edits); tick() does those and is called
  by the feeder (1 Hz threats loop + watchdog) and on every event.
"""
from collections import Counter, OrderedDict, deque
import argparse
import itertools
import json
import logging
import math
import re
import sys
import threading
import time

from .cockpit import (CockpitListener, CockpitState, _f, _known_type, clean_name, fuel_lb_from_fraction, spoken_name,
                      weapon_name)

PLAYER = "Prometheus,Player,Client"
M_TO_FT = 3.28084
MS_TO_KT = 1.943844
NM = 1852.0
EVENT_KEEP_S = 30 * 60
SHOT_KEEP_S = 30 * 60
CONTACT_RADIUS_NM = 150.0  # = LONG_RANGE_NM tier (pilot decision 2026-11-08)
# Event-stream liveness (round 2): DCS-gRPC's StreamEvents went silent after a DCS client reboot while
# the unit streams recovered. Units fresh but no event message for this long -> force a resubscribe.
EVENTS_STALE_S = 120.0
VANISH_KEEP_S = 30 * 60
STORES_POLL_S = 3.0
# DCS Weapon.Category of Unit:getAmmo() entries.
AMMO_KIND = {0: "gun", 1: "missile", 2: "rocket", 3: "bomb", 4: "torpedo"}
DEATH_KINDS = ("kill", "dead", "unit_lost", "crash", "pilot_dead")
LOG = logging.getLogger("athena.eye")
# Said instead of any range/bearing answer when no pilot unit is known (live 2026-11-11: false all-clears).
NO_POSITION = "I don't have your position; you're not showing in an aircraft."
# Round 4 kill confirmation: a target that stops showing within this many seconds of my last attack on it
# (gun/rocket: last rounds; bomb/missile: release, covers the time of flight) is a CONFIRMED kill
# (still flagged inferred in the log). Later than that (up to 5 min) it is only "likely".
VANISH_CONFIRM_S = {"gun": 20.0, "rocket": 20.0, "bomb": 60.0, "missile": 60.0, "air-to-air missile": 60.0}
CREDIT_WINDOW_S = 300.0
RAW_SAMPLE_S = 60.0  # raw event debug log: one sample per kind per minute + per-minute kind counts
COMBAT_KINDS = ("shot", "hit", "kill", "dead", "unit_lost", "crash", "pilot_dead", "shooting_start", "ejection")
# Picture size on busy maps (round 4): full JSON only for the nearest groups; the rest are counted.
HOSTILE_GROUPS_MAX = 15
FRIENDLY_GROUPS_MAX = 8
PICTURE_TOP = 3
AIR_PICTURE_RADIUS_NM = 50.0
# Range tiers (pilot decision 2026-11-08): 20 nm = ground targets AND bandits (a bandit entering 20 nm is called
# once, unprompted, on the next transmission); 50 nm = tactical bandit picture; 150 nm = long-range bandits.
CLOSE_TIER_NM = 20.0
LONG_RANGE_NM = 150.0
SOFT_VANISH_CONFIRM_S = 45.0
RAW_SAMPLE_CHARS = 6000
# Pilot kill reports ("splash", "target destroyed", "armor dead") confirm the most recent pending kill.
KILL_REPORT = re.compile(
    r"\b(splash(ed)?|good kills?|kill confirmed|target (is )?(destroyed|dead|down)|"
    r"(armor|tank|vehicle|truck|target|sam|site|bandit|bogey|convoy|t-?\d+)s? (is |are |was |were )?"
    r"(dead|down|destroyed|burning)|destroyed (the |that )?(target|armor|tank|vehicle|truck|sam|bandit))\b", re.I)
_QUESTION = re.compile(r"(\?\s*$|^\W*(athena\W+)?(is|are|was|were|did|do|does|has|have|what|which|where|how|"
                       r"can|could|say|confirm)\b)", re.I)
# Dynamic whisper prompt brevity words (round 4 STT: "eyes on target" was heard as "I, son target").
BREVITY_PROMPT = ("Eyes on target, eyes on, splash, winchester, bingo, egress, egressing, ingress, RTB, magnum, "
                  "rifle, fox two, fox three, tally, no joy, BRAA, picture, bogey dope, Maverick, APKWS, GBU, "
                  "weapons hot, cleared hot, pickle, visual, RWR, spike, what's painting me, what do I have left, "
                  "fuel state, wheels up, gear down.")

# ----------------------------------------------------------------------------- classification
AWACS = {"E-3A", "E-2C", "A-50", "KJ-2000", "E-2D"}
TANKERS = {"KC-135", "KC135MPRS", "KC130", "KC-130J", "IL-78M", "S-3B Tanker", "KC_10_Extender",
           "KC_10_Extender_D", "MQ-25"}
TRANSPORTS = {"C-130", "C-130J-30", "C-17A", "IL-76MD", "An-26B", "An-30M", "Yak-40", "C-47", "Ju-52",
              "C-101EB", "Hercules"}
BOMBERS = {"B-1B", "B-52H", "Tu-22M3", "Tu-95MS", "Tu-142", "Tu-160", "H-6J", "B-17G", "Ju-88A4"}
ATTACK = {"A-10A", "A-10C", "A-10C_2", "Su-25", "Su-25T", "Su-25TM", "Su-24M", "Su-24MR", "Su-17M4",
          "AV8BNA", "Tornado GR4", "Tornado IDS", "AJS37", "MiG-27K", "F-117A", "A-4E-C", "L-39ZA",
          "MB-339A", "Su-34", "F-15E", "F-15ESE", "MQ-9 Reaper", "MQ-1A Predator", "WingLoong-I"}
ATTACK_HELO = {"AH-64D", "AH-64D_BLK_II", "AH-1W", "Ka-50", "Ka-50_3", "Mi-24P", "Mi-24V", "Mi-28N",
               "SA342M", "SA342L", "SA342Minigun", "SA342Mistral", "OH58D", "OH-58D"}
# Phase 1 item 3: types whose label the 2026-11 code review proved wrong (R2 F1, R3 T10). Checked before
# everything else, so they stay right even before (or without) DCS's own attributes.
TYPE_OVERRIDES = {
    "Soldier M4": "infantry", "Soldier M249": "infantry", "KAMAZ Truck": "ground vehicle",
    "M-60": "armor", "PT_76": "armor", "M1_37mm": "AAA", "RQ-1A Predator": "attack",
    "generator_5i57": "ground vehicle", "RPC_5N62V": "SAM",
}
# DCS unit-type attribute names (Unit.getDescByName(type).attributes) -> Eye category, first match wins.
# Missile launchers before guns (Tunguska carries both), guns before radars (radar triple-A also has "SAM TR").
ATTRIBUTE_CATEGORY = (
    (("SAM LL", "AA_missile", "IR Guided SAM", "MANPADS", "SR SAM", "MR SAM", "LR SAM"), "SAM"),
    (("AA_flak", "AAA", "Static AAA", "Mobile AAA"), "AAA"),
    (("SAM SR", "SAM TR", "SAM CC", "SAM AUX", "SAM elements", "SAM related", "EWR", "MANPADS AUX"), "SAM"),
    (("Infantry",), "infantry"),
    (("Tanks", "IFV", "APC", "Armored vehicles"), "armor"),
    (("Artillery", "MLRS", "Indirect fire"), "artillery"),
    (("Trucks", "Cars", "Unarmed vehicles", "Armed vehicles", "Ground vehicles", "Vehicles"), "ground vehicle"),
)
AIR_ATTRIBUTE_CATEGORY = ((("AWACS",), "AWACS"), (("Tankers",), "tanker"), (("Transports",), "transport"),
                          (("Bombers", "Strategic bombers"), "bomber"), (("UAVs",), "attack"))
# DCS type name -> {"attributes": frozenset, "fuel_max_kg": float|None} from Eval, once per type. A per-type DCS
# fact, the same for every Eye; an empty attribute set means "DCS gave nothing" (patterns decide).
TYPE_INFO = {}
# Attributes of things that emit or launch (SAM warnings by range only consider these when attributes are known).
AIR_DEFENCE_THREAT_ATTRS = frozenset(("SAM SR", "SAM TR", "SAM LL", "EWR", "AA_missile", "AA_flak", "IR Guided SAM",
                                      "MANPADS", "SR SAM", "MR SAM", "LR SAM"))


def _attr_category(attrs, ground=True):
    for names, category in (ATTRIBUTE_CATEGORY if ground else AIR_ATTRIBUTE_CATEGORY):
        if any(n in attrs for n in names):
            return category
    return None


def type_attributes(type_name):
    """DCS attribute set for a unit type (empty when DCS has not told us)."""
    info = TYPE_INFO.get(type_name or "")
    return info["attributes"] if info else frozenset()


SAM_PAT = re.compile(r"(sam|s-300|s_300|s-125|s_125|s-200|s_75|s-75|snr|patriot|hawk|roland|rapier|nasams|"
                     r"buk|kub|tor |tor_|9a331|osa|strela|igla|stinger|avenger|chaparral|tunguska|2s6|pantsir|"
                     r"hq-7|hq7|sa-\d|sa\d|9s18|9a310|5p85|40b6|64h6|54k6|1l13|55g6|ewr|dog ear|p-19|"
                     r"rls_19j6|st-68|fps-117|5n66|9s80|9p31|9a33|\bln\b|\bsr\b|\btr\b|str$|cwar|pcp|\bcp\b|"
                     r"manpad|mistral|iris|ra-|hq-|soldier stinger|sa-18|sa-24)", re.I)
AAA_PAT = re.compile(r"(zsu|zu-23|zu_23|shilka|gepard|vulcan|flak|bofors|ks-19|ks19|s-60|s_60|aaa|m45|"
                     r"m1939|type 59|zpu|dshk|l118|m163|bunker)", re.I)
ARTY_PAT = re.compile(r"(sau |sau_|2s1|2s3|2s9|2s19|m-109|m109|mlrs|grad|smerch|uragan|m12_gmc|plz05|"
                      r"pzh|tos-1|howitzer|mortar|2b11|l118)", re.I)
ARMOR_PAT = re.compile(r"(t-55|t-62|t-64|t-72|t-80|t-90|m-1 abrams|m1a2|leopard|leclerc|challenger|merkava|"
                       r"chieftain|centaur|ztz|type_96|bmp|btr|bmd|bradley|m-2|m-113|m113|m1126|m1128|m1134|"
                       r"lav-25|mtlb|marder|zbd|brdm|aav7|tpz|warrior|cobra|sturmgeschutz|tiger|panther|"
                       r"sherman|t-34|m4|stug|vab|m2a1|bmd)", re.I)
INFANTRY_PAT = re.compile(r"(soldier|infantry|paratrooper|insurgent|jtac)", re.I)
SHIP_CARRIER = re.compile(r"(cvn|stennis|vinson|lincoln|washington|truman|forrestal|kuznetsov|tarawa|"
                          r"lha|hms invincible|cv_|carrier)", re.I)


def classify(type_name, group_category=None):
    """Return a short human category for a DCS type name (fighter, AWACS, SAM, armor, ...). The ONE labeller:
    overrides > aircraft sets > DCS attributes (TYPE_INFO) > name patterns."""
    t = type_name or ""
    gc = (group_category or "").upper()
    if t in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[t]
    if t in AWACS:
        return "AWACS"
    if t in TANKERS:
        return "tanker"
    if t in TRANSPORTS:
        return "transport"
    if t in BOMBERS:
        return "bomber"
    if gc.endswith("HELICOPTER") or t in ATTACK_HELO or t.startswith(("Mi-", "Ka-", "UH-", "CH-", "AH-", "SA342", "OH-")):
        return "attack helo" if t in ATTACK_HELO else "helicopter"
    if t in ATTACK:
        return "attack"
    attrs = type_attributes(t)
    if gc.endswith("AIRPLANE"):
        return _attr_category(attrs, ground=False) or "fighter"
    if gc.endswith("SHIP"):
        return "carrier" if SHIP_CARRIER.search(t) else "ship"
    if gc.endswith("TRAIN"):
        return "train"
    if attrs:
        by_dcs = _attr_category(attrs)
        if by_dcs in ("SAM", "AAA"):
            return by_dcs
        # Fail safe (review P1): attributes never downgrade a type the name patterns call air defence; only the
        # explicit TYPE_OVERRIDES table can (KAMAZ Truck, generator). A truck shown as SAM is a false alarm; a SAM
        # shown as a truck is a missed threat.
        if not (AAA_PAT.search(t) or SAM_PAT.search(t)) and by_dcs:
            return by_dcs
    if AAA_PAT.search(t):
        return "AAA"
    if SAM_PAT.search(t):
        return "SAM"
    if INFANTRY_PAT.search(t):
        return "infantry"  # before armor: "Soldier M4" is not a Sherman
    if ARTY_PAT.search(t):
        return "artillery"
    if ARMOR_PAT.search(t):
        return "armor"
    if gc.endswith("GROUND") or re.search(r"ural|gaz-|uaz|zil-|kamaz|truck|hummer", t, re.I):
        return "ground vehicle"
    return "unknown"


def domain(category):
    if category in ("fighter", "attack", "bomber", "AWACS", "tanker", "transport", "helicopter", "attack helo"):
        return "air"
    if category in ("ship", "carrier"):
        return "sea"
    return "ground"


AAM_PAT = re.compile(r"(aim|r-\d|r_\d|r-2|r-3|r-4|r-6|r-7|mica|magic|python|derby|pl-|pl_|sd-10|meteor|iris-t|"
                     r"super530|super_530|r550|sidewinder|sparrow|phoenix|amraam|rb_74|rb-74|rb_24|"
                     r"aa-|\bah\b)", re.I)
BOMB_PAT = re.compile(r"(gbu|mk_8|mk-8|mk8|fab|kab|cbu|bdu|blu|sab|rbk|betab|m_117|(?<!9)m117|bomb|odab|"
                      r"agm_154|agm-154|jsow|b_8|mk_20|mk-20|rockeye|durandal|beta|sc_|sd_|lgb)", re.I)
ROCKET_PAT = re.compile(r"(hydra|s-5|s_5|s-8|s_8|s-13|s_13|s-24|s_24|s-25|s_25|zuni|sneb|c_8|c_5|c_13|c_24|"
                        r"lau|ffar|rocket|apkws|m151|m156|m257|m274|mk1|wp|hvar|rp-3)", re.I)


SHELL_PAT = re.compile(r"(shells?\.|\buor\b|-uor-|uor-|uof|ubr|ofz|bzt|ozt|\d+\s?mm)", re.I)
def group_alerts(alerts, limit=2):
    """(calls, keys): identical words are spoken once with all their keys acknowledged together; at most
    ``limit`` distinct calls per reply. Keys of calls not chosen are not returned, so they stay pending."""
    groups = {}
    for a in alerts:
        groups.setdefault(a["call"], []).append(a["key"])
    chosen = list(groups.items())[:limit]
    return [call for call, _ in chosen], [keys if len(keys) > 1 else keys[0] for _, keys in chosen]


def weapon_kind(name, burst=False):
    if burst:
        return "gun"
    n = name or ""
    if re.search(r"agr[-_]20|apkws", n, re.I):
        return "rocket"  # APKWS: R_20 inside AGR_20 must not read as an R-xx air-to-air missile (R3 T9)
    if ROCKET_PAT.search(n) and not AAM_PAT.search(n):
        return "rocket"
    if BOMB_PAT.search(n):
        return "bomb"
    if AAM_PAT.search(n) and not SHELL_PAT.search(n):
        return "air-to-air missile"
    if SHELL_PAT.search(n) or re.search(r"(gsh|m61|gau|cannon|gun|_he|_ap|mg_|bullet|shell|\d+\s?mm|vulcan|defa|aden|uor|uof|ubr|"
                 r"ofz|bzt|ozt|\bhei?\b|\bapi\b|tracer|\bflak\b|\bzu-?23\b|\bks-?19\b|\bs-?60\b)", n, re.I):
        return "gun"
    return "missile"


def _resolve_window(kind):
    return {"gun": 12.0, "rocket": 30.0, "bomb": 120.0}.get(kind, 120.0)


# ----------------------------------------------------------------------------- threats without a cockpit RWR
# Nominal engagement ranges (nm) of radar SAM/AAA/EWR types, first match wins. Used when the module exports no
# RWR (F-4E: LoGetTWSInfo returns nothing): a hostile emitter with Prometheus inside its range "could be tracking".
SAM_RANGES = (
    (re.compile(r"s-?300|s_300|5p85|40b6|64h6|54k6|ps 5p|sa-10|sa-20", re.I), 40.0, "SA-10"),
    (re.compile(r"s-?200|s_200|5n62|sa-5", re.I), 80.0, "SA-5"),
    (re.compile(r"patriot", re.I), 50.0, "Patriot"),
    (re.compile(r"hawk", re.I), 24.0, "Hawk"),
    (re.compile(r"s_75|s-75|snr_75|snr 75|volhov|sa-2\b", re.I), 23.0, "SA-2"),
    (re.compile(r"buk|9a310|9s18|sa-11|sa-17", re.I), 19.0, "SA-11"),
    (re.compile(r"s-125|s_125|5p73|snr s-125|p-19|sa-3\b", re.I), 13.0, "SA-3"),
    (re.compile(r"kub|1s91|2p25|sa-6", re.I), 13.0, "SA-6"),
    (re.compile(r"nasams", re.I), 13.0, "NASAMS"),
    (re.compile(r"(?<![a-z])tor(\b|_)|9a331|sa-15", re.I), 6.5, "SA-15"),
    (re.compile(r"hq-7|hq7", re.I), 6.5, "HQ-7"),
    (re.compile(r"osa|9a33|sa-8", re.I), 5.5, "SA-8"),
    (re.compile(r"roland", re.I), 4.3, "Roland"),
    (re.compile(r"tunguska|2s6|sa-19", re.I), 4.3, "SA-19"),
    (re.compile(r"pantsir|sa-22", re.I), 10.0, "SA-22"),
    (re.compile(r"rapier", re.I), 3.7, "Rapier"),
    (re.compile(r"strela-10|9a35|sa-13", re.I), 2.7, "SA-13"),
    (re.compile(r"strela-1|9p31|sa-9", re.I), 2.3, "SA-9"),
    (re.compile(r"gepard|shilka|zsu-23-4|zsu_23_4|vulcan|m163", re.I), 1.5, "radar triple A"),
    (re.compile(r"1l13|55g6|ewr|fps-117|dog ear|9s80|rls_19j6|st-68", re.I), 80.0, "EWR"),
)
# Guidance describes the weapon, not whether a radar is emitting right now.
SAM_GUIDANCE = {
    "SA-13": "heat seeker", "SA-9": "heat seeker",
    "SA-2": "radar", "SA-3": "radar", "SA-5": "radar", "SA-6": "radar",
    "SA-10": "radar", "SA-11": "radar", "Patriot": "radar", "Hawk": "radar", "NASAMS": "radar",
    "SA-8": "command guided", "SA-15": "command guided", "SA-19": "command guided",
    "SA-22": "command guided", "HQ-7": "command guided", "Roland": "command guided", "Rapier": "command guided",
    "radar triple A": "radar",
}
SAM_DEFAULT_NM = 2.5  # unknown SAM-category unit (MANPADS, IR SAM): close range only
# Air-defence support vehicles that neither emit nor launch: no range-table SAM warning while DCS has not described
# the type (live 2026-11-09: "SAM SA-18 Igla comm could be tracking you" 13 times in one F-4E flight).
SAM_SUPPORT_PAT = re.compile(r"(\bcomm\b|command|\bpcp\b|\bcp\b|generator)", re.I)
MISSILE_PICTURE_NM = 20.0  # missiles_in_flight lists every hostile missile this close, aimed at Prometheus or not
MISSILE_SHOT_KEEP_S = 60.0  # kept for compatibility; shot rows now live BRIDGE_S (scan on) or EVAL_OFF_KEEP_S (scan off)
BRIDGE_S = 2.5  # a shot-event row bridges only the first ~2 s before the 1 Hz Eval scan sees the missile
EVAL_OFF_KEEP_S = 20.0  # no live scan (Eval disabled): a shot-event row is all we have, about one SAM flight time
WEAPONS_FRESH_S = 10.0  # the live scan counts as "on" while its last reply is this recent
WEAPONS_SCAN_CAP = 20  # WEAPONS_LUA returns at most this many missiles (a full reply may have missed one)
MISSILE_ALERT_NM = 25.0  # a hostile missile shot (event) within this range of Prometheus and closing -> launch alert
THREAT_ANNOUNCE_S = 90.0
RANGE_HYST_NM = 2.0  # range-table fallback: once inside, a SAM stays listed until this far past its nominal range
SAM_REARM_S = 60.0  # a SAM group out of range / no longer tracking this long gets a fresh alert next time
RADAR_POLL_NM = 100.0  # hostile radar units polled for "is it tracking me" (nearest first)
RADAR_POLL_MAX = 30
RADAR_FRESH_S = 5.0
TYPE_POLL_MAX = 25  # unit types asked from DCS per Eval call
HEARTBEAT_STALE_S = 30.0  # no event message at all this long while the 1 Hz simulation_fps heartbeat was flowing
COMBAT_MISSING_WINDOW_S = 600.0
COMBAT_MISSING_SHOTS = 3  # my loadout dropped this often in 10 min with not one shot event: combat events missing


def sam_profile(type_name):
    """(nominal range nm, NATO-ish label, is_ewr) for a hostile SAM/AAA/EWR type name."""
    for pattern, rng, label in SAM_RANGES:
        if pattern.search(type_name or ""):
            return rng, label, label == "EWR"
    return SAM_DEFAULT_NM, None, False


def missile_spoken_name(weapon, launcher=None):
    """The shooter owns the SAM designation; never parse SA9M333 as SA-9."""
    if launcher:
        label = sam_profile(launcher)[1]
        if label and label not in ("EWR", "radar triple A"):
            return label
        return "missile"  # a known but unclassified shooter does not license a guessed SAM designation
    name = str(weapon or "").strip()
    # Only already-spoken designations are safe fallbacks, not arbitrary DCS IDs.
    if re.fullmatch(r"SA-?\d+", name, re.I):
        return "SA-" + re.sub(r"\D", "", name)
    if re.fullmatch(r"(?:AIM|R|AA|PL)-\d+[A-Z0-9-]*|Stinger|Igla|Strela|MANPADS|Patriot|Hawk|NASAMS|Roland|Rapier|HQ-7", name, re.I):
        return name
    return "missile"


# DCS attributes of air-defence support vehicles (command post, comm, auxiliaries): no range warning on their own.
AIR_DEFENCE_SUPPORT_ATTRS = frozenset(("SAM CC", "SAM AUX", "MANPADS AUX"))


def _air_defence_support(type_name):
    """True for a command/comm/generator vehicle of an air-defence site: by name, or DCS attributes that name only a
    support role (never for a type the range table knows as a radar or launcher)."""
    if SAM_SUPPORT_PAT.search(type_name or ""):
        return True
    attrs = type_attributes(type_name)
    return bool(attrs & AIR_DEFENCE_SUPPORT_ATTRS) and not attrs & AIR_DEFENCE_THREAT_ATTRS and \
        sam_profile(type_name)[1] is None


# ----------------------------------------------------------------------------- geometry
def bearing_range(lat1, lon1, lat2, lon2):
    """Great-circle initial bearing (deg true) and range (nm)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist_m = 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))
    return brg, dist_m / NM


def clock_of(relative_deg):
    hour = int(((relative_deg % 360.0) + 15.0) // 30.0) % 12
    return 12 if hour == 0 else hour


_CLOCK_SIDE = {12: "on the nose", 1: "ahead right", 2: "ahead right", 3: "right", 4: "behind right",
               5: "behind right", 6: "behind", 7: "behind left", 8: "behind left", 9: "left",
               10: "ahead left", 11: "ahead left"}


def clock_phrase(relative_deg):
    hour = clock_of(relative_deg)
    return "%d o'clock, %s" % (hour, _CLOCK_SIDE[hour])


def aspect_of(bearing_from_own, target_track, target_speed_ms):
    if target_speed_ms is None or target_speed_ms < 3:
        return "stationary"
    recip = (bearing_from_own + 180.0) % 360.0
    off = abs(((recip - target_track) + 180.0) % 360.0 - 180.0)
    if off <= 30:
        return "hot"
    if off <= 70:
        return "flanking"
    if off <= 110:
        return "beaming"
    return "cold"


def closure_kt(bearing, own_track, own_speed, tgt_track, tgt_speed):
    """Positive = closing. Flat-earth approximation from horizontal velocities."""
    def vec(track, speed):
        r = math.radians(track or 0.0)
        return (math.cos(r) * (speed or 0.0), math.sin(r) * (speed or 0.0))
    vo, vt = vec(own_track, own_speed), vec(tgt_track, tgt_speed)
    los = (math.cos(math.radians(bearing)), math.sin(math.radians(bearing)))
    return ((vo[0] - vt[0]) * los[0] + (vo[1] - vt[1]) * los[1]) * MS_TO_KT


def _is_pilot(name, allowed):
    """Case-insensitive match of name against a comma-separated pilot list ("Prometheus,Player,Client")."""
    name = clean_name(name).casefold()
    return bool(name) and any(name == clean_name(item).casefold() for item in str(allowed).split(","))


def _three(deg):
    return "%03d" % (int(round(deg)) % 360 or 360)


# ----------------------------------------------------------------------------- spoken call lines
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def words(n):
    """0..999 as English words ("one hundred twenty one"); larger numbers as digits."""
    n = int(round(n))
    if n < 0 or n > 999:
        return str(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    rest = n % 100
    return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + words(rest))


def digit_words(text):
    """'038' -> 'zero three eight' (bearings are read digit by digit)."""
    return " ".join(_ONES[int(c)] for c in str(text) if c.isdigit())


def range_words(nm):
    r = int(round(nm or 0))
    if r < 1:
        return "less than one mile"
    return "one mile" if r == 1 else "%s miles" % words(r)


def altitude_words(alt_ft):
    if alt_ft is None:
        return "altitude unknown"
    if alt_ft < 1000:
        return "below one thousand feet"
    return "angels %s" % words(round(alt_ft / 1000.0))


def bandit_call(g):
    """A hostile air call outside a hostile-only picture (traffic lists, the unprompted close-bandit warning)
    starts with "Bandit " so it is heard as hostile; air pictures keep the compact call (three fit 280)."""
    return "Bandit " + g["call"] if g.get("side") == "hostile" and g.get("domain") == "air" else g["call"]
def spoken_type(type_name):
    """Plain controller name (round 9): table + generic DCS-suffix strip; the raw type stays in the picture."""
    return spoken_name(type_name, "contact")


def call_line(label, bearing3, range_nm, clock_hour, dom, alt_ft=None, vertical=None):
    """The user's required order: BRA first, then the clock position (2026-11-07). ONE sentence per call
    (phase 0, 280-char reply budget): three air calls fit one transmission and a reply trimmed by whole
    sentences never cuts a call in half.

    air:    "<label>, zero three eight, twelve, angels fifteen, ten o'clock high."  (BRA brevity: no "bearing",
            no "miles"; on 103 real calls 97% of three-bandit pictures fit, 17% with "bearing"/"miles"/"single").
            Under 10 nm the range keeps "miles" ("two seven zero, eight miles"), so it is not heard as a bearing.
    ground: "<label>, bearing zero three eight, three miles, ground target, eleven o'clock."
    """
    label = (label or "Contact")[:1].upper() + (label or "Contact")[1:]
    if dom == "air":
        altitude = "altitude unknown" if alt_ft is None else "angels %s" % words(max(0, round(alt_ft / 1000)))
        rng = range_words(range_nm)  # "one mile" / "less than one mile" / "eight miles" stay
        if int(round(range_nm or 0)) >= 10:
            rng = re.sub(r" miles$", "", rng)
        return "%s, %s, %s, %s, %s o'clock %s." % (label, digit_words(bearing3), rng, altitude,
                                                  words(clock_hour), vertical or "level")
    what = "surface target" if dom == "sea" else "ground target"
    return "%s, bearing %s, %s, %s, %s o'clock." % (label, digit_words(bearing3), range_words(range_nm), what,
                                                   words(clock_hour))


GROUND_MERGE_DEG, GROUND_MERGE_NM = 10.0, 2.0
AIR_DEFENCE = ("SAM", "AAA")
AIR_DEFENCE_WORDS = {"SAM": "SAM site", "AAA": "triple-A site"}
def _bearing_of(g):
    try:
        return int(g.get("bearing_mag") or g.get("bearing_true"))
    except (TypeError, ValueError):
        return None
def _air_defence(row):
    """True when any member of the row is a SAM/AAA/radar air-defence group (never merged with other kinds)."""
    cats = [row.get("category")] + list(row.get("member_categories") or [])
    return any(c in AIR_DEFENCE for c in cats)
def _can_merge(a_row, b_row):
    if _air_defence(a_row) or _air_defence(b_row):
        return (_air_defence(a_row) and _air_defence(b_row) and a_row.get("category") == b_row.get("category")
                and a_row.get("category") in AIR_DEFENCE
                and set(a_row.get("member_categories") or []) <= set(AIR_DEFENCE)
                and set(b_row.get("member_categories") or []) <= set(AIR_DEFENCE))
    return True
def _merge_ground_rows(rows):
    """Round 9: ground groups at the same clock position, within ~10 deg of bearing and ~2 nm of range of the
    closest one are ONE call ('Enemy vehicles, six contacts, ...'). Closest first; each row keeps `merged_groups`."""
    out, used = [], [False] * len(rows)
    for i, row in enumerate(rows):
        if used[i]:
            continue
        used[i] = True
        a = row["_g"]
        a_hour, a_brg = int(a["clock"].split()[0]), _bearing_of(a)
        mates = [row]
        for j in range(i + 1, len(rows)):
            if used[j] or a_brg is None:
                continue
            b = rows[j]["_g"]
            b_brg = _bearing_of(b)
            if b_brg is None or not _can_merge(row, rows[j]):
                continue
            if int(b["clock"].split()[0]) == a_hour and abs((b_brg - a_brg + 180) % 360 - 180) <= GROUND_MERGE_DEG \
                    and abs(b["range_nm"] - a["range_nm"]) <= GROUND_MERGE_NM:
                used[j] = True
                mates.append(rows[j])
        if len(mates) == 1:
            out.append({k: v for k, v in row.items() if k != "_g"})
            continue
        cats = sorted({c for m in mates for c in m["member_categories"]})
        count = sum(m["_g"]["count"] for m in mates)
        same = len({m["category"] for m in mates}) == 1
        air_def = same and row.get("category") in AIR_DEFENCE
        labels = list(dict.fromkeys(m["_g"]["label"] for m in mates))
        label = (labels[0] if len(labels) == 1 else AIR_DEFENCE_WORDS[row["category"]]) if air_def else a["label"] if same else "enemy vehicles"
        label += ", %s contacts" % words(count)
        call = call_line(label, a.get("bearing_mag") or a.get("bearing_true"), a["range_nm"], a_hour, "ground")
        out.append({"call": call, "category": a["category"] if same else "ground vehicle", "member_categories": cats,
                    "merged_groups": len(mates), "contacts": count})
    return out
# ----------------------------------------------------------------------------- normalization
def descriptor_type(d):
    for source in (d, d.get("descriptor") or {}, d.get("desc") or {}):
        for key in ("type", "type_name", "typeName", "display_name", "displayName"):
            if _known_type(source.get(key)):
                return source[key]
    return ""


SCENERY_NAMES = (
    (r"most|bridge", "bridge"),
    (r"hangar|angar", "hangar"),
    (r"bunker|pillbox", "bunker"),
    (r"oil|fuel|tank|cistern|reservoir", "storage tank"),
    (r"powertranspole|power.?line", "power pole"),
    (r"tower|bashn", "tower"),
    (r"building|house|sklad|warehouse|\bbdg\b", "building"),
)


def scenery_name(name):
    for pattern, label in SCENERY_NAMES:
        if re.search(pattern, name or "", re.I):
            return label
    # hyphens stay: a static 'T-55' is the T-55, never a second type 'T 55' in the tally (11-11)
    return re.sub(r"[^\w-]+|_", " ", name or "scenery").strip(" -").title()


def object_type(obj):
    name = obj.get("type") or obj.get("name") or obj.get("kind") or "unknown"
    if obj.get("kind") in ("scenery", "static") or obj.get("category") == "scenery":
        return scenery_name(name if _known_type(name) else obj.get("kind", "scenery"))
    if obj.get("kind") == "weapon":
        return weapon_name(name)
    return name


def _coal(value):
    v = (value or "").upper()
    if v.endswith("RED"):
        return "red"
    if v.endswith("BLUE"):
        return "blue"
    if v.endswith("NEUTRAL"):
        return "neutral"
    return "unknown"


def _pos(d):
    p = (d or {}).get("position") or {}
    if "lat" not in p and "lon" not in p:
        return None, None, None
    return p.get("lat", 0.0), p.get("lon", 0.0), p.get("alt", 0.0)


def norm_unit(d):
    lat, lon, alt = _pos(d)
    group = d.get("group") or {}
    vel = d.get("velocity") or {}
    ori = d.get("orientation") or {}
    gcat = group.get("category", "")
    type_name = descriptor_type(d)
    return {
        "id": int(d.get("id", 0)), "name": d.get("name", ""), "type": type_name, "callsign": d.get("callsign", ""),
        "group": group.get("name", ""), "coalition": _coal(d.get("coalition")), "group_category": gcat,
        "category": classify(type_name, gcat), "player": clean_name(d.get("player_name")) or None,
        "lat": lat, "lon": lon, "alt_m": alt,
        "heading": ori.get("heading", vel.get("heading", 0.0)), "track": vel.get("heading", ori.get("heading", 0.0)),
        "speed_ms": vel.get("speed", 0.0),
    }


def norm_object(wrapper):
    """Initiator/Target oneof -> compact dict."""
    if not wrapper:
        return None
    if "unit" in wrapper:
        u = norm_unit(wrapper["unit"])
        u["kind"] = "unit"
        return u
    for kind in ("static", "scenery", "weapon", "cargo", "airbase", "unknown"):
        if kind in wrapper:
            d = wrapper[kind] or {}
            lat, lon, alt = _pos(d)
            name = d.get("name") or d.get("display_name") or ""
            type_name = descriptor_type(d) or ("airbase" if kind == "airbase" else "")
            return {"kind": kind, "id": int(d.get("id", 0) or 0), "name": name, "type": type_name,
                    "coalition": _coal(d.get("coalition")), "category": "scenery" if kind == "static" else kind,
                    "player": None, "lat": lat, "lon": lon, "alt_m": alt}
    return None


def _brief(obj):
    if not obj:
        return None
    out = {"type": object_type(obj), "name": clean_name(obj.get("name")), "coalition": obj.get("coalition"),
           "category": obj.get("category")}
    if obj.get("player"):
        out["player"] = obj["player"]
    return out


def _label(obj):
    if not obj:
        return "unknown"
    who = obj.get("player") or object_type(obj)
    if obj.get("player") and obj.get("type"):
        who = "%s (%s)" % (clean_name(obj["player"]), object_type(obj))
    coal = obj.get("coalition")
    return "%s %s" % (coal, who) if coal and coal != "unknown" else who


def _norm_weapon_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _raw_compact(value):
    """Keep event objects/weapon/target, omit bulky orientation vectors recursively."""
    if isinstance(value, dict):
        return {k: _raw_compact(v) for k, v in value.items() if k != "orientation"}
    if isinstance(value, list):
        return [_raw_compact(v) for v in value]
    return value


# ----------------------------------------------------------------------------- the eye
def pilot_identity(own, previous=None):
    """Last non-null identity; cockpit id -1 is a feed fallback, not a spawn.

    Preserve a real id across fallback so a different real id still means respawn.
    Name/type changes mean a slot/aircraft change even across source switching.
    """
    if not own:
        return previous, False
    unit = tuple(own.get(k) for k in ("id", "name", "type"))
    same_aircraft = previous is not None and unit[1:] == previous[1:]
    same = same_aircraft and (unit[0] == previous[0] or -1 in (unit[0], previous[0]))
    if same and unit[0] == -1:
        return previous, False
    return unit, not same


class Eye:
    def __init__(self, player=PLAYER, *, clock=time.time, source="unconfigured", stores_dir=None, test_ownship=None):
        self.player, self.clock, self.source = player, clock, source
        # TEST ONLY (ATHENA_TEST_OWNSHIP): exact DCS unit name used as the pilot when no real player unit exists.
        self.test_ownship = (test_ownship or "").strip() or None
        self.memory = None  # memory6: optional nonblocking observation publisher
        self.lock = threading.RLock()
        self._seq = itertools.count(1)
        self.on_mission_start = None
        self.on_player_shot = None  # feeder hook: refresh stores right after a shot
        self.stores_dir = stores_dir  # durable stores/shots log (stores-YYYYMMDD.jsonl); None = off
        self.chat_generation = 0  # dialogue only: reconnect/spawn may happen between radio calls
        self._chat_unit = None
        self.reset("init")
        self.connected = False
        self.streams = {}  # stream name -> {"up": bool, "t": eye clock, "error": str|None}
        self.last_error = None
        self.airbases, self.airbases_at = {}, None
        self.declination, self.declination_at = None, None
        self.fuel, self.fuel_at = None, None
        self.stores_status = "not polled yet"
        self.theatre = None
        self.session_id = None
        # Event-stream health (raw messages incl. noise kinds such as simulation_fps).
        self.event_rx = deque()
        self.events_rx_at = None
        self.events_total = 0
        self.heartbeat_at = None  # last simulation_fps (1 Hz heartbeat: proves the pipe, not the combat feed)
        self.mission_events_at = None  # last real mission event (anything but heartbeat/noise kinds)
        self.resubscribes = Counter()
        self.last_resubscribe = None
        # Round 4: raw event debug (kind counts since start, per-minute counts, one sample per kind per minute).
        self.raw_kinds = Counter()
        self._raw_minute = Counter()
        self._raw_minute_at = None
        self._raw_sample_at = {}
        # Round 3: cockpit feed (AthenaExport.lua -> UDP 7778): flight data, payload, RWR, locked target.
        self.cockpit = CockpitState(clock=clock)

    # -- lifecycle
    def reset(self, reason):
        with self.lock:
            self._real_seen = self._stand_used = False  # test stand-in safety flags: per mission
            if self.memory is not None:
                self.memory.invalidate()
            self.cockpit = CockpitState(clock=self.clock)
            self.fuel = self.fuel_at = None
            self._own_cache = None
            self.connected, self.streams, self.last_error = False, {}, None
            self.heartbeat_at = self.events_rx_at = self.mission_events_at = None
            self.units = {}
            self.last_known = OrderedDict()
            self.events = deque()
            self.shots = deque()
            self.vanished = deque()
            self.stores = {}  # unit id -> latest loadout snapshot
            self.consumption = deque()  # stores-diff drops (weapon used), kept 30 min
            self.used_session = Counter()  # my weapons used this mission (from stores diff or shot events)
            self.fired_events = Counter()  # my shot events this mission (fallback when stores are unavailable)
            self.my_kills = []  # confirmed/inferred kills by the pilot this mission
            self.other_kills = []  # explicit other-shooter kills, retained for the mission
            self.known_objects = {}  # (kind, id) -> descriptor, including scenery hit before kill
            self.weapon_types = {}  # weapon id -> known shot/hit type, retained for the mission
            self.life = None
            self.damage_events = deque(maxlen=100)
            self.announced = set()
            # Round 4: threats from DCS-gRPC when the cockpit has no RWR (missiles in flight via Eval, shot events).
            self.weapons_in_flight = {}  # weapon id -> row (Eval world.searchObjects around ownship)
            self.weapons_at = None
            self.weapons_asked_at = None  # when the last completed scan was SENT (a scan sent after a shot sees it)
            self.missiles_near = {}  # weapon id -> row: every hostile missile within MISSILE_PICTURE_NM or aimed at me
            self.missile_shots = {}  # weapon id -> bridge row from the shot event (until the live scan sees it)
            self.own_loss = None  # explicit own crash/pilot_dead/ejection; never inferred from missing units
            self.threat_alerts = deque(maxlen=50)
            self._sam_alerted = {}  # SAM group -> t called (re-armed after SAM_REARM_S out of range / not tracking)
            self._sam_inside = {}  # SAM group -> last t listed (re-arm timing; written by tick only)
            self._sam_listed = set()  # SAM groups listed at the last tick (range hysteresis; tick only)
            self._sam_arm = Counter()  # SAM group -> alert generation (new key after a re-arm)
            self._sam_cautioned = set()  # re-arm only on an observed exit, not a missing site/feed
            self._sam_caution_arm = Counter()
            self.radars = {}  # unit name -> latest Eval radar row {on, tracking_me, detected, has_radar}
            self.radars_at = None
            self.unit_sync = {}  # group category -> {"since", "window", "stream"}: sweep units not re-sent
            self.last_sweep = None
            self.units_at = None
            self.events_at = None
            self.reset_reason, self.reset_at = reason, self.clock()

    def set_connected(self, ok, error=None, stream=None, idle=False, alive=None):
        """Per-stream connection state (R2 F5). `connected` is True while ANY stream is up, so one failing stream
        never reads as "DCS disconnected" (Memory ends a mission after 60 s of that). stream=None = all streams.
        idle=True: subscribed and open, but nothing of that kind sent yet (a mission with no ships or helicopters).
        alive: for an idle stream, a callable that is False once the subscription call has ended.

        What an idle stream's label can and cannot tell (review A 11-11): it reads "up, no units for Ns" only while
        the subscription call is open (alive); a call that ends or fails reads "down". A dead connection is found by
        the channel keepalive (about 30 s) and then reads "down". A server that keeps the call open but stops sending
        (a hang) cannot be told apart from a mission with none of that unit kind: the growing N is the only sign."""
        with self.lock:
            self.streams[stream or "_all"] = {"up": bool(ok), "t": self.clock(), "error": error, "idle": idle,
                                              "alive": alive if idle else None}
            if stream is None:
                for s in self.streams.values():
                    s["up"] = bool(ok)
            connected = any(s["up"] for s in self.streams.values())
            if connected and not self.connected:
                self.chat_generation += 1
            self.connected = connected
            if error:
                self.last_error = error

    def streams_json(self, now):
        def _alive(check):
            try:
                return bool(check())
            except Exception:  # noqa: BLE001 - a call that cannot say is not shown as up
                return False

        out = {}
        for name, s in sorted(self.streams.items()):
            if name == "_all":
                continue
            alive = s.get("alive")
            if s["up"] and s.get("idle") and alive is not None and not _alive(alive):
                out[name] = "down %ds (subscription ended)" % int(now - s["t"])
            elif s["up"] and s.get("idle"):
                out[name] = "up, no units for %ds" % int(now - s["t"])
            else:
                out[name] = "up" if s["up"] else "down %ds (%s)" % (int(now - s["t"]), s["error"] or "error")
        return out

    def note_type_info(self, rows):
        """DCS unit-type descriptions from Eval (once per type): {type: {"attributes": [...], "fuel_max_kg": n} or
        None/False when DCS gave nothing}. Relabels every unit of those types through classify()."""
        from . import cockpit as _cp
        changed = set()
        for t, info in (rows or {}).items():
            if not t:
                continue
            attrs, fuel = frozenset(), None
            if isinstance(info, dict):
                raw = info.get("attributes")
                if isinstance(raw, dict):  # Lua may send {name = true} or a list
                    raw = [k for k, v in raw.items() if v] if not all(str(k).isdigit() for k in raw) \
                        else list(raw.values())
                attrs = frozenset(str(a) for a in (raw or []) if a)
                fuel = _f(info.get("fuel_max_kg"))
            TYPE_INFO[t] = {"attributes": attrs, "fuel_max_kg": fuel if fuel and fuel > 0 else None}
            if fuel and fuel > 0:
                _cp.DCS_FUEL_MAX_KG[t] = fuel
            changed.add(t)
        with self.lock:
            for table in (self.units, self.last_known):
                for u in table.values():
                    if u.get("type") in changed:
                        u["category"] = classify(u["type"], u.get("group_category"))
        return len(changed)

    def types_to_describe(self, limit=TYPE_POLL_MAX):
        """Unit types seen but not yet described by DCS (bounded per Eval)."""
        with self.lock:
            seen = []
            for u in list(self.units.values()):
                t = u.get("type")
                if t and t not in TYPE_INFO and t not in seen and _known_type(t):
                    seen.append(t)
                    if len(seen) >= limit:
                        break
            return seen

    def begin_unit_sync(self, category, since, window, stream=None):
        """A unit stream (re)subscribed at `since`: DCS-gRPC re-sends every live unit of that category, so units
        not re-sent within `window` seconds died or left while the stream was down (R2 F2). tick() sweeps them."""
        with self.lock:
            self.unit_sync[category] = {"since": since, "window": window, "stream": stream}

    def _sweep_units(self, now):
        for category, sync in list(self.unit_sync.items()):
            if now - sync["since"] < sync["window"]:
                continue
            del self.unit_sync[category]
            stream = self.streams.get(sync.get("stream") or "")
            if stream is not None and not stream["up"]:
                continue  # the stream failed again inside the window: its resend was not complete
            stale = [u for u in self.units.values() if u.get("group_category") == category
                     and (u.get("seen") or 0) < sync["since"]]
            for u in stale:
                self.units.pop(u["id"], None)
                self.vanished.append({"t": now, "unit": u, "reason": "reconnect_sweep"})
                self._log_store({"kind": "unit_swept", "unit": _brief(u), "category": category,
                                 "note": "not re-sent after the unit stream reconnected"})
            self.last_sweep = {"category": category, "removed": len(stale), "t": now}
            if stale:
                LOG.info("eye: %d %s units not re-sent after resubscribe; removed", len(stale), category)

    def tick(self, now=None):
        """Every state change that used to happen inside picture() (R2 F6): prune, settle shots, drop finished
        missile bridges, the SAM range hysteresis + alerts, and the post-reconnect unit sweep. Idempotent."""
        with self.lock:
            now = self.clock() if now is None else now
            self._prune(now)
            self._settle_shots(now)
            self._prune_missiles(now)
            self._sweep_units(now)
            own = self._own_locked()
            if own is not None and own.get("lat") is not None:
                self._update_sam_state(own, now)

    def note_resubscribe(self, reason):
        with self.lock:
            self.resubscribes[reason] += 1
            self.last_resubscribe = {"reason": reason, "t": self.clock()}

    # -- durable stores/shot log
    def _log_store(self, record):
        record = dict(record, **self._test_tag())
        if self.memory is not None:  # memory6: every new Eye fact = publish an observation
            self.memory.publish(record.get("kind", "derived_fact"), record, at=self.clock())
        if not self.stores_dir:
            return
        try:
            import os
            os.makedirs(self.stores_dir, mode=0o700, exist_ok=True)
            now = self.clock()
            path = os.path.join(self.stores_dir, "stores-%s.jsonl" % time.strftime("%Y%m%d", time.localtime(now)))
            row = dict(record, time=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), t=round(now, 3),
                       session=self.session_id)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=True, default=str, separators=(",", ":")) + "\n")
        except (OSError, ValueError, TypeError) as error:
            LOG.info("eye: stores log write failed: %s", type(error).__name__)

    # -- units
    def on_unit(self, unit_dict):
        u = norm_unit(unit_dict)
        u["seen"] = self.clock()
        with self.lock:
            self.units[u["id"]] = u
            self.last_known[u["id"]] = u
            self.last_known.move_to_end(u["id"])
            while len(self.last_known) > 20000:
                self.last_known.popitem(last=False)
            self.units_at = u["seen"]

    def on_gone(self, unit_id):
        now = self.clock()
        with self.lock:
            u = self.units.pop(int(unit_id), None)
            self.units_at = now
            if u is not None:
                # Kept so a unit that vanishes WITHOUT a death event (event stream down) is still reported
                # as "no longer showing, likely destroyed" (flagged inferred).
                self.vanished.append({"t": now, "unit": u})
                while self.vanished and now - self.vanished[0]["t"] > VANISH_KEEP_S:
                    self.vanished.popleft()
                self._credit_vanish(u, now)

    def on_units_message(self, msg):
        if "unit" in msg:
            self.on_unit(msg["unit"])
        elif "gone" in msg:
            self.on_gone(msg["gone"].get("id", 0))

    def _own_locked(self):
        """The pilot's unit (see _resolve_own). Also keeps the two stand-in safety flags: once a real pilot has
        been ownship in this mission the stand-in is never used again, and what the stand-in did is not his."""
        cached = getattr(self, "_own_cache", None)
        if cached is not None:
            return cached[0]  # picture() resolves ownship once per build (busy maps: thousands of _is_me calls)
        own = self._resolve_own()
        self._chat_unit, changed = pilot_identity(own, self._chat_unit)
        if changed:
            self.chat_generation += 1
        if own is not None:
            if self._stand_in(own):
                self._stand_used = True
            elif not self._real_seen:
                self._real_seen = True
                if self._stand_used:
                    self._drop_stand_in_work()
        return own
    def _drop_stand_in_work(self):
        """A real pilot has shown up after the test stand-in was ownship: its kills stay as other pilots' kills
        and its shots / weapons used are forgotten, so none of it is read back as the human's."""
        self.other_kills = sorted(self.other_kills + self.my_kills, key=lambda k: k["seq"])
        self.my_kills = []
        self.shots.clear()
        self.used_session.clear()
        self.fired_events.clear()
    def _resolve_own(self):
        """The pilot's unit: the unit named in the cockpit feed, else player name in the allowed list, else
        the ONLY player unit (single player), else a pseudo unit built from the cockpit feed (gRPC down).
        The TEST stand-in only when there is no player unit at all, no fresh cockpit feed, and no real pilot
        has been ownship in this mission."""
        players = []
        cockpit = getattr(self, "cockpit", None)
        cp_unit = None
        if cockpit is not None and cockpit.latest is not None and cockpit.fresh():
            cp_unit = cockpit.latest.get("unit") or None
        if cp_unit:
            for u in self.units.values():
                if u.get("name") == cp_unit and u.get("lat") is not None:
                    return u
        for u in self.units.values():
            if _is_pilot(u.get("player"), self.player):
                return u
            if u.get("player"):
                players.append(u)
        if len(players) == 1:
            return players[0]
        own = cockpit.ownship() if cockpit is not None else None
        name = getattr(self, "test_ownship", None)
        cockpit_live = cockpit is not None and cockpit.latest is not None and cockpit.fresh()
        if own is None and name and not players and not cockpit_live and not self._real_seen:
            return next((u for u in self.units.values() if u.get("name") == name and u.get("lat") is not None), None)
        return own
    def _stand_in(self, own):
        """The label when `own` is the TEST stand-in (an AI unit), else None."""
        name = getattr(self, "test_ownship", None)
        if own is None or not name or own.get("name") != name or own.get("player") or own.get("source") == "cockpit":
            return None
        return "TEST stand-in pilot: %s (an AI unit, NOT a human pilot)" % name
    def _test_tag(self):
        """Marks Memory / stores-log records made while the stand-in is the pilot (the debrief says so)."""
        if not getattr(self, "test_ownship", None):
            return {}
        with self.lock:
            return {"test_ownship": self.test_ownship} if self._stand_in(self._own_locked()) else {}

    def ownship(self):
        with self.lock:
            return self._own_locked()

    def player_units(self, limit=8):
        with self.lock:
            own = self._own_locked()
            rows = [u for u in self.units.values() if u.get("player") and u is not own]
            return ([own] if own else []) + rows[:max(0, limit - 1)]

    # -- airbases & misc
    def set_airbases(self, rows):
        out = {}
        for d in rows:
            lat, lon, alt = _pos(d)
            cat = (d.get("category") or "").upper()
            category = "airdrome" if cat.endswith("AIRDROME") else "FARP/helipad" if cat.endswith("HELIPAD") else \
                "ship" if cat.endswith("SHIP") else "airbase"
            name = d.get("display_name") or d.get("name") or d.get("callsign") or "?"
            out[d.get("name") or name] = {"name": name, "coalition": _coal(d.get("coalition")), "category": category,
                                          "lat": lat, "lon": lon, "alt_m": alt}
        with self.lock:
            self.airbases, self.airbases_at = out, self.clock()

    def set_declination(self, value):
        with self.lock:
            self.declination, self.declination_at = value, self.clock()

    def set_fuel(self, value):
        with self.lock:
            self.fuel, self.fuel_at = value, self.clock()

    # -- events
    def on_event(self, msg):
        now = self.clock()
        kind = next((k for k in msg if k != "time"), None)
        with self.lock:
            if kind == "mission_start":
                # Fence before raw-debug or any derived publication. A full reset
                # prevents old units/cockpit data being sampled under the next identity.
                self.reset("mission_start")
            # Raw stream health: every message counts (also noise kinds), it proves the stream is alive.
            self.events_rx_at = now
            self.events_total += 1
            self.event_rx.append(now)
            while self.event_rx and now - self.event_rx[0] > 300:
                self.event_rx.popleft()
            if kind == "simulation_fps":
                self.heartbeat_at = now
            elif kind not in (None, "mark_change", "score", "weapon_add"):
                self.mission_events_at = now
            self._raw_debug(kind, msg, now)
            memory_generation = self.memory.generation if self.memory is not None else None
        if kind is None:
            return None
        body = msg.get(kind) or {}
        ev = {"seq": next(self._seq), "t": now, "mission_time": msg.get("time"), "kind": kind,
              "initiator": norm_object(body.get("initiator")), "target": norm_object(body.get("target")),
              "weapon": None, "weapon_id": None, "place": None}
        weapon = body.get("weapon")
        if isinstance(weapon, dict):
            ev["weapon"] = descriptor_type(weapon) or None
            ev["weapon_id"] = int(weapon.get("id", 0) or 0) or None
            wlat, wlon, walt = _pos(weapon)
            if wlat is not None:
                vel = weapon.get("velocity") or {}
                ev["weapon_pos"] = {"lat": wlat, "lon": wlon, "alt_m": walt,
                                    "heading": vel.get("heading", (weapon.get("orientation") or {}).get("heading")),
                                    "speed_ms": vel.get("speed")}
        if _known_type(body.get("weapon_name")):
            ev["weapon"] = ev["weapon"] or body["weapon_name"]
        place = body.get("place")
        if isinstance(place, dict):
            ev["place"] = place.get("display_name") or place.get("name") or None
        for key in ("comment", "text", "message", "name"):
            if isinstance(body.get(key), str) and body[key]:
                ev[key] = body[key][:200]
        if kind in ("simulation_fps", "mark_change", "score", "weapon_add"):
            return None  # noise
        wake = False
        with self.lock:
            if self.memory is not None and memory_generation != self.memory.generation:
                with self.memory.lock:
                    self.memory.dropped += 1
                return None  # normalized across a reset: the mission is ambiguous
            self.events_at = now
            if kind == "mission_start":
                # Full reset above; request a current-session unit snapshot.
                if self.on_mission_start is not None:
                    self.on_mission_start()
            ev["initiator"] = self._enrich_object(ev["initiator"])
            ev["target"] = self._enrich_object(ev["target"])
            wid = ev["weapon_id"]
            if wid:
                if _known_type(ev["weapon"]):
                    self.weapon_types[wid] = ev["weapon"]
                else:
                    ev["weapon"] = self.weapon_types.get(wid)
            self.events.append(ev)
            self._prune(now)
            self._settle_shots(now)  # settle at event time (was: whenever a picture happened to be built)
            wake = self._apply(ev)
            if self.memory is not None:  # memory6: include takeoff/landing, loss and damage evidence
                self.memory.publish("event", dict(ev, mine=self._is_me(ev.get("initiator")), **self._test_tag()), at=now)
        if wake and self.on_player_shot is not None:
            try:
                self.on_player_shot()
            except Exception:  # noqa: BLE001 - a hook must never break the event stream
                pass
        return ev

    def _raw_debug(self, kind, msg, now):
        """Raw event debug log (round 4, why do hit/kill events not show?): counts per kind since start, one
        LOG line per minute with the last minute's kind counts, one sample per kind per minute (combat kinds
        also go to the durable stores log)."""
        k = kind or "(empty)"
        if self._raw_minute_at is None:
            self._raw_minute_at = now
        elif now - self._raw_minute_at >= RAW_SAMPLE_S:
            LOG.info("eye raw events last %ds: %s", int(now - self._raw_minute_at),
                     json.dumps(dict(self._raw_minute.most_common()), separators=(",", ":")))
            self._raw_minute.clear()
            self._raw_minute_at = now
        self.raw_kinds[k] += 1
        self._raw_minute[k] += 1
        last = self._raw_sample_at.get(k)
        if last is None or now - last >= RAW_SAMPLE_S:
            self._raw_sample_at[k] = now
            try:
                sample = json.dumps(_raw_compact(msg), default=str, separators=(",", ":"))[:RAW_SAMPLE_CHARS]
            except (TypeError, ValueError):
                sample = "unserializable event"
            LOG.info("eye raw event sample %s: %s", k, sample)
            if k in COMBAT_KINDS:
                self._log_store({"kind": "raw_event_sample", "event": k, "sample": sample})

    def raw_event_counts(self):
        with self.lock:
            return dict(self.raw_kinds.most_common())

    def _is_me(self, obj):
        """Pilot match by allowed-name list, or by the current ownship unit id (single player: 'player')."""
        if not obj:
            return False
        if _is_pilot(obj.get("player"), self.player):
            return True
        own = self._own_locked()
        return own is not None and obj.get("kind", "unit") == "unit" and bool(obj.get("id")) \
            and obj.get("id") == own["id"]

    def _enrich_object(self, obj):
        if not obj:
            return obj
        key = (obj.get("kind", "unit"), obj.get("id"))
        previous = self.known_objects.get(key) or {}
        if key[0] == "unit":
            previous = self.last_known.get(key[1]) or previous
        out = dict(obj)
        for k, v in previous.items():
            if out.get(k) in (None, "", "unknown"):
                out[k] = v
        if not _known_type(obj.get("type")) and previous.get("category"):
            out["category"] = previous["category"]
        if not _known_type(out.get("type")):
            out["type"] = obj.get("kind", "unknown")
        if key[1]:
            self.known_objects[key] = out.copy()
        return out

    def _apply(self, ev):
        """Apply one event; returns True when a player fired (stores refresh wanted)."""
        kind, ini, tgt = ev["kind"], ev["initiator"], ev["target"]
        if kind in ("crash", "pilot_dead", "ejection") and self._is_me(ini) and self.own_loss is None:
            self.own_loss = {"key": "own-loss-%s" % ev["seq"], "kind": kind, "t": ev["t"],
                             "unit_id": ini.get("id"), "call": "you're down."}
        if kind == "birth" and ini and ini.get("kind") == "unit":
            if self._is_me(ini):
                self.own_loss = None
                self.life = None
                self.damage_events.clear()
            u = dict(ini, seen=ev["t"])
            u.pop("kind", None)
            self.units[u["id"]] = u
            self.last_known[u["id"]] = u
            self.stores.pop(u["id"], None)  # fresh airframe: next stores poll is a new baseline
        if kind in ("dead", "unit_lost", "crash") and ini and ini.get("kind") == "unit":
            gone = self.units.pop(ini["id"], None)
            self._credit_death(dict(gone or {}, **{k: v for k, v in ini.items() if v not in (None, "")}), ev)
        if kind == "kill" and tgt and tgt.get("kind") == "unit":
            self.units.pop(tgt["id"], None)
        me = self._is_me(ini)
        if kind == "hit" and self._is_me(tgt):
            weapon = ev.get("weapon") or (ini or {}).get("type") or "unknown weapon"
            name = missile_spoken_name(weapon, (ini or {}).get("type")) if weapon_kind(weapon) not in ("gun", "bomb", "rocket") else spoken_type(weapon)
            self._record_damage("hit event", "You were hit by %s." % name,
                                ev["t"], weapon=weapon, attacker=_brief(ini))
        if kind == "human_failure" and me:
            self._record_damage("human_failure", "Systems damage reported.", ev["t"])
        if kind == "shot" and not me and ini:
            self._hostile_shot(ev)
        if kind == "shot" and me:
            self._shot_event(ev, burst=False)
        elif kind == "shooting_start" and me:
            self._shot_event(ev, burst=True)
        elif kind == "shooting_end" and me:
            for s in reversed(self.shots):
                if s["kind"] == "gun" and s["burst_end"] is None:
                    s["burst_end"] = ev["t"]
                    break
        elif kind == "hit" and me:
            self._resolve(ev, "hit")
        elif kind == "kill" and me:
            self._resolve(ev, "kill")
            self._record_my_kill(tgt, ev.get("weapon"), "kill event", ev["t"],
                                 mission_time=ev.get("mission_time"), shooter=ini)
        elif kind == "kill" and ini and tgt:
            self._record_kill(self.other_kills, tgt, ev.get("weapon"), "kill event", ev["t"],
                              mission_time=ev.get("mission_time"), shooter=ini)
        return (kind in ("shot", "shooting_start", "shooting_end") and bool(ini) and
                (me or bool(ini.get("player")))) or (kind == "birth" and bool(ini) and bool(ini.get("player")))

    def _shot_event(self, ev, burst):
        """A shot event; merges with a shot already inferred from a stores drop (stores poll was first)."""
        kind = weapon_kind(ev["weapon"], burst=burst)
        for s in reversed(self.shots):
            if ev["t"] - s["t"] > 10:
                break
            if s.get("source") == "stores_diff" and not s.get("event_seen") and s["kind"] == kind:
                s.update(event_seen=True, weapon_id=ev["weapon_id"], mission_time=ev["mission_time"])
                s["weapon"] = s["weapon"] or ev["weapon"]
                return s
        s = self._new_shot(ev, ev["weapon"], ev["weapon_id"], burst=burst)
        s["source"], s["event_seen"] = "shot event", True
        self.fired_events[(ev["weapon"] or "gun burst") if not burst else "gun burst"] += 1
        self._log_store({"kind": "shot", "weapon": s["weapon"], "weapon_kind": s["kind"], "source": "shot event",
                         "intended_target": s["intended_target"]})
        return s

    def _new_shot(self, ev, weapon, weapon_id, burst, kind=None):
        kind = kind or weapon_kind(weapon, burst=burst)
        shooter = ev["initiator"]
        guess = self._guess_target(shooter, kind)
        if guess and guess.get("group"):
            guess["group_count_at_launch"] = sum(u.get("group") == guess["group"] for u in self.units.values())
        s = {"id": ev["seq"], "t": ev["t"], "mission_time": ev["mission_time"], "weapon": weapon,
             "weapon_id": weapon_id, "kind": kind, "intended_target": guess, "result": "pending",
             "hit_target": None, "hits": 0, "resolved_t": None, "burst_end": None if burst else ev["t"]}
        self.shots.append(s)
        return s

    def _guess_target(self, shooter, kind):
        """DCS-gRPC shot events carry no target: use the cockpit's locked target (radar/TGP lock from the
        Export feed) when there is one, else guess the enemy nearest the nose at launch."""
        lock = self._cockpit_lock_target()
        if lock is not None:
            return lock
        if not shooter or shooter.get("lat") is None:
            return None
        best = None
        want_air = kind == "air-to-air missile"
        for u in self.units.values():
            if u["id"] == shooter.get("id") or u.get("lat") is None:
                continue
            if u["coalition"] == shooter.get("coalition") or u["coalition"] == "neutral":
                continue
            is_air = domain(u["category"]) == "air"
            if want_air != is_air and kind != "gun":
                continue
            brg, rng = bearing_range(shooter["lat"], shooter["lon"], u["lat"], u["lon"])
            off = abs(((brg - (shooter.get("heading") or 0.0)) + 180) % 360 - 180)
            if off > 45 or rng > (100 if is_air else 40):
                continue
            score = off / 10.0 + rng
            if best is None or score < best[0]:
                best = (score, u, brg, rng, off)
        if best is None:
            return None
        _, u, brg, rng, off = best
        return {"id": u["id"], "type": u["type"], "name": u["name"], "group": u["group"],
                "category": u["category"], "coalition": u["coalition"], "player": u.get("player"),
                "range_nm_at_launch": round(rng, 1), "off_nose_deg": round(off),
                "method": "guess: enemy nearest the nose at launch"}

    def _cockpit_lock_target(self):
        locks = self.cockpit.locked_targets()
        if not locks:
            return None
        t = locks[0]
        own = self._own_locked()
        u = self._match_unit(t.get("lat"), t.get("lon"), exclude=own["id"] if own else None)
        dist = t.get("dist")
        rng = round(dist / NM, 1) if isinstance(dist, (int, float)) else None
        if u is None:
            return {"id": None, "type": t.get("type") or "unknown", "name": None, "group": None, "category": None,
                    "coalition": None, "player": None, "range_nm_at_launch": rng,
                    "method": "locked target in the cockpit (not matched to a DCS unit)"}
        return {"id": u["id"], "type": u["type"], "name": u["name"], "group": u["group"],
                "category": u["category"], "coalition": u["coalition"], "player": u.get("player"),
                "range_nm_at_launch": rng, "method": "locked target in the cockpit"}

    def _match_shot(self, ev):
        wid, wname = ev.get("weapon_id"), _norm_weapon_name(ev.get("weapon"))
        tgt = ev.get("target") or {}
        open_shots = [s for s in self.shots if s["result"] in ("pending", "hit") or s["result"].startswith("likely")]
        if wid:
            for s in reversed(self.shots):
                if s["weapon_id"] == wid:
                    return s
        if ev["kind"] == "kill" and tgt.get("id"):
            for s in reversed(open_shots):
                if s["hit_target"] and s["hit_target"].get("id") == tgt["id"] and \
                        s["hit_target"].get("kind", "unit") == tgt.get("kind", "unit"):
                    return s
        for s in open_shots:
            if wname and _norm_weapon_name(s["weapon"]) == wname and s["result"] == "pending":
                return s
        for s in open_shots:
            if wname and _norm_weapon_name(s["weapon"]) == wname:
                return s
        if ev["kind"] == "hit":
            now = ev["t"]
            for s in reversed(open_shots):  # gun hits sometimes report a shell name, not the gun
                if s["kind"] == "gun" and (s["burst_end"] is None or now - s["burst_end"] < _resolve_window("gun")):
                    return s
        return None

    def _resolve(self, ev, outcome):
        s = self._match_shot(ev)
        if s is None:
            return None
        tgt = ev.get("target")
        s["hits"] += 1 if outcome == "hit" else 0
        if tgt:
            s["hit_target"] = {"id": tgt.get("id"), "type": tgt.get("type"), "name": tgt.get("name"),
                               "kind": tgt.get("kind", "unit"),
                               "coalition": tgt.get("coalition"), "category": tgt.get("category"),
                               "player": tgt.get("player")}
        if outcome == "kill" and tgt:
            self._set_shot_kill(s, tgt, "kill")
        elif s["result"] != "kill":
            s["result"] = outcome
        s["resolved_t"] = ev["t"]
        self._log_store({"kind": "shot_result", "weapon": s["weapon"], "weapon_kind": s["kind"], "result": outcome,
                         "target": s["hit_target"], "source": "%s event" % outcome})
        return s

    @staticmethod
    def _set_shot_kill(s, target, result):
        """Keep each target's evidence so correcting one owner cannot erase an area shot's other kills."""
        key = (target.get("kind", "unit"), target.get("id"))
        credits = s.setdefault("kill_targets", {})
        if credits.get(key) != "kill":
            credits[key] = result
        s["result"] = "kill" if "kill" in credits.values() else result

    def _reconcile_shot_ownership(self, target):
        key = (target.get("kind", "unit"), target.get("id"))
        for s in self.shots:
            credits = s.get("kill_targets", {})
            if key not in credits:
                continue
            del credits[key]
            s.get("vanished_targets", {}).pop(key, None)
            s.pop("kill_evidence", None)
            if credits:
                s["result"] = "kill" if "kill" in credits.values() else next(iter(credits.values()))
            else:
                s["result"] = "hit" if s["hits"] else "kill credited to another shooter"

    @staticmethod
    def _last_activity(s, now):
        """When my attack last touched the target: last rounds of a gun burst/rocket ripple, else release."""
        if s["kind"] == "gun" and s.get("burst_end") is None and s.get("source") != "stores_diff":
            return min(now, max(s["t"], s.get("stores_last_t") or 0) + 60)  # tolerate a missing shooting_end
        return max(s["t"], s.get("burst_end") or 0.0, s.get("stores_last_t") or 0.0)

    def _attack_on(self, unit, now):
        """My most recent shot (within 5 min) aimed at or hitting this unit; area weapons (gun, rockets,
        bombs) also count for the other units of the target's group."""
        uid = unit.get("id")
        group = unit.get("group")
        for s in reversed(self.shots):
            if now - s["t"] > CREDIT_WINDOW_S:
                break
            hit = bool(uid) and (s["hit_target"] or {}).get("id") == uid and \
                (s["hit_target"] or {}).get("kind", "unit") == unit.get("kind", "unit")
            aimed = bool(uid) and (s["intended_target"] or {}).get("id") == uid
            area = s["kind"] in ("bomb", "rocket", "gun")
            same_group = bool(group) and area and group in ((s["intended_target"] or {}).get("group"),
                                                            (s["hit_target"] or {}).get("group"))
            if s["result"] == "kill" and not area and not (hit or aimed):
                continue
            if hit or aimed or same_group:
                return s, hit
        return None, False

    def _my_hit_on(self, uid, now):
        return any(ev["kind"] == "hit" and now - ev["t"] <= CREDIT_WINDOW_S and
                   (ev["target"] or {}).get("id") == uid and self._is_me(ev["initiator"]) for ev in self.events)

    def _credit_death(self, unit, ev):
        """dead / unit_lost / crash for a unit: credit a CONFIRMED kill to my recent attack on it (or its group)."""
        uid = unit.get("id")
        if not uid or unit.get("player"):
            return
        if any(k["target_id"] == uid and k["target_kind"] == "unit" for k in self.other_kills):
            return  # explicit other-shooter kill must not become mine on dead/unit_lost
        s, hit = self._attack_on(unit, ev["t"])
        if s is None:
            return
        s["resolved_t"] = ev["t"]
        target = s["hit_target"] if hit else unit
        self._set_shot_kill(s, target, "kill")
        how = ("hit then %s event" if hit else "%s event after my attack on it") % ev["kind"]
        self._record_my_kill(target, s["weapon"], how, ev["t"], mission_time=ev.get("mission_time"))

    def _record_kill(self, rows, target, weapon, how, t, inferred=False, likely=False, mission_time=None,
                     shooter=None):
        if not target:
            return None
        tid = target.get("id")
        target_kind = target.get("kind", "unit")
        if rows is self.other_kills and how == "kill event":
            self._reconcile_shot_ownership(target)
        splash = "Splash one %s" % spoken_type(object_type(target))
        own = self._own_locked() or {}
        shooter = shooter or own
        attribution = {
            "shooter_id": str(shooter.get("id") or shooter.get("name") or "me"), "shooter": dict(shooter),
            "friendly": rows is self.my_kills or (own.get("coalition") in ("blue", "red") and
                                                   shooter.get("coalition") == own["coalition"]),
            "flight_member": bool(own.get("group") and shooter.get("group") == own["group"]),
        }
        # Ownership belongs to the target, not to a particular shooter's list. Death and pilot-report
        # credit can have inferred=False too; only a kill event explicitly identifies the shooter.
        for old in self.my_kills + self.other_kills:
            if tid and old["target_id"] == tid and old["target_kind"] == target_kind:
                if how != "kill event" and old["how"] == "kill event":
                    return old  # authoritative mission fact survives pruning of the original event
                if how == "kill event":
                    if old["shooter_id"] != attribution["shooter_id"]:
                        previous = {k: v for k, v in old.items() if k != "attribution_history"}
                        old.setdefault("attribution_history", []).append(previous)
                    source = self.my_kills if old in self.my_kills else self.other_kills
                    if source is not rows:
                        source.remove(old)
                        rows.append(old)
                        rows.sort(key=lambda k: k["seq"])
                    old.update(attribution)
                    old.update(inferred=False, likely=False, confidence="confirmed", how=how,
                               splash_call=splash)
                if _known_type(target.get("type")):
                    old.update(type=object_type(target), category=target.get("category"))
                if old["mission_time"] is None and mission_time is not None:
                    old["mission_time"] = mission_time
                if _known_type(weapon):
                    old["weapon"] = weapon
                if old.get("likely") and not likely:  # stronger evidence later: no more "likely"
                    old.update(likely=False, confidence="confirmed", how=how, weapon=weapon or old["weapon"],
                               splash_call=splash)
                if old["inferred"] and not inferred:  # confirmed later by a real event
                    old.update(inferred=False, how=how, weapon=weapon or old["weapon"], splash_call=splash)
                return old
        k = {"key": "%s-%s-%s" % (target_kind, tid, round(t, 1)), "target_id": tid, "target_kind": target_kind,
             "seq": next(self._seq), "t": t, "mission_time": mission_time, "type": object_type(target),
             **attribution,
             "category": target.get("category"), "group": target.get("group"), "weapon": weapon, "how": how,
             "inferred": inferred, "likely": likely, "confidence": "likely" if likely else "confirmed",
             "splash_call": splash + (", likely" if likely else "")}
        rows.append(k)
        return k

    def _record_my_kill(self, target, weapon, how, t, inferred=False, likely=False, mission_time=None, shooter=None):
        before = len(self.my_kills)
        k = self._record_kill(self.my_kills, target, weapon, how, t, inferred, likely, mission_time, shooter)
        if k is None or len(self.my_kills) == before:
            return k
        tid = target.get("id")
        shooter = self._own_locked() or {}
        self._log_store({"kind": "kill_by_me", "target": dict(_brief(target), id=tid), "weapon": weapon, "how": how,
                         "shooter": dict(_brief(shooter) or {}, id=shooter.get("id")), "mission_time": mission_time,
                         "inferred": inferred, "confidence": k["confidence"]})
        return k

    def _credit_vanish(self, unit, now):
        """A unit disappeared from the unit stream without a death event. If my recent attack was on it (or on
        its group with an area weapon): CONFIRMED kill when it vanished within VANISH_CONFIRM_S of the attack or
        after my hit on it (flagged inferred in the log); otherwise a likely kill."""
        uid = unit["id"]
        if any(k["target_id"] == uid and k["target_kind"] == "unit" for k in self.other_kills):
            return  # keep explicit other-shooter ownership even after the event window expires
        for ev in self.events:
            victim = (ev["target"] if ev["kind"] == "kill" else ev["initiator"]) or {}
            if ev["kind"] in DEATH_KINDS and victim.get("id") == uid and victim.get("kind", "unit") == "unit":
                return
        if unit.get("player"):
            return
        s, hit = self._attack_on(unit, now)
        if s is None:
            self._log_store({"kind": "unit_vanished", "unit": _brief(unit), "note": "no death event; likely destroyed"})
            return
        dt = max(0.0, now - self._last_activity(s, now))
        hit = hit or self._my_hit_on(uid, now)
        window = VANISH_CONFIRM_S.get(s["kind"], 20.0)
        if unit.get("category") in ("infantry", "ground vehicle"):
            window = max(window, SOFT_VANISH_CONFIRM_S)
        confirmed = hit or dt <= window
        if confirmed:
            self._set_shot_kill(s, unit, "kill")
            s["kill_evidence"] = "target stopped showing %d s after my attack%s" % (dt, " (hit seen)" if hit else "")
            how = "target stopped showing %d s after my attack (no death event)" % dt
        else:
            self._set_shot_kill(s, unit, "likely kill (inferred: target no longer showing)")
            how = "target vanished %d s after my shot (weak evidence)" % dt
        target = s.get("intended_target") or {}
        before = target.get("group_count_at_launch")
        if before and unit.get("group") == target.get("group"):
            after = sum(u.get("group") == unit["group"] for u in self.units.values())
            if after < before:
                how += "; targeted group count %d to %d" % (before, after)
                s["group_count_drop"] = {"from": before, "to": after}
        s["resolved_t"] = now
        s.setdefault("vanished_targets", {})[(unit.get("kind", "unit"), uid)] = spoken_type(unit.get("type"))
        self._record_my_kill(unit, s["weapon"], how, now, inferred=True, likely=not confirmed)

    def pilot_report(self, text):
        """The pilot says "splash" / "target destroyed" / "armor dead". The ONE rule for pilot kill reports (phase 1):
        the pilot's words only upgrade a kill DCS already saw (a likely kill within 10 min) or acknowledge a kill
        within 2 min; they never create a kill without DCS evidence. Returns what happened (for the tool log) or
        None when the text is not a kill report."""
        text = (text or "").strip()
        if not KILL_REPORT.search(text) or _QUESTION.search(text):
            return None
        with self.lock:
            now = self.clock()
            for k in reversed(self.my_kills):
                if now - k["t"] > 600:
                    break
                if k.get("likely"):
                    k.update(likely=False, confidence="confirmed", how=k["how"] + "; confirmed by pilot report",
                             splash_call="Splash one %s" % spoken_type(k["type"]))
                    self._log_store({"kind": "kill_confirmed_by_pilot", "target_type": k["type"], "text": text[:120]})
                    return {"confirmed": spoken_type(k["type"]), "how": "pilot report upgraded a likely kill"}
                if now - k["t"] <= 120:
                    return {"confirmed": spoken_type(k["type"]), "how": "already confirmed"}
            return {"confirmed": None, "how": "not recorded: no DCS kill to confirm"}

    # -- threats without a cockpit RWR (DCS-gRPC sensor fallback)
    def _hostile_to(self, coalition, own):
        return own is not None and coalition not in (own.get("coalition"), "neutral", "unknown", None)

    def _threat_alert(self, key, call, kind, now, **extra):
        if any(a["key"] == key for a in self.threat_alerts):
            return None
        a = dict({"key": key, "t": now, "call": call, "kind": kind}, **extra)
        self.threat_alerts.append(a)
        self._log_store({"kind": "threat_alert", "alert": kind, "call": call, "source": extra.get("source")})
        return a

    def _eval_scan_on(self, now):
        """The live Eval weapon scan answered recently (it replies every second, also with no missiles)."""
        return self.weapons_at is not None and now - self.weapons_at <= WEAPONS_FRESH_S

    def _hostile_shot(self, ev):
        """A hostile missile shot event near Prometheus. Phase 1: only a BRIDGE row for the first ~2 s until the
        live scan (real DCS weapon id) sees the missile; the launch alert comes from the scan when it is on."""
        ini = ev["initiator"]
        own = self._own_locked()
        if own is None or own.get("lat") is None or not self._hostile_to(ini.get("coalition"), own):
            return None
        kind = weapon_kind(ev["weapon"])
        if kind in ("gun", "bomb", "rocket"):
            return None
        src = ev.get("weapon_pos") or ini
        if src.get("lat") is None:
            return None
        res = self._where(own, src["lat"], src["lon"])
        if res is None:
            return None
        where, brg, rng = res
        if rng > MISSILE_ALERT_NM:
            return None
        hour = clock_of(brg - (own.get("heading") or 0.0))
        wtype = weapon_name(ev["weapon"] or "missile")
        spoken = missile_spoken_name(ev["weapon"], ini.get("type"))
        # The overlay's shot-event weapon id is invented per event (R3 T1): never equal to the scan's real id.
        wid = str(ev["weapon_id"]) if ev.get("weapon_id") else "shot-%s" % ev["seq"]
        call = "Missile launch, %s, your %s o'clock, %s." % (spoken, words(hour), range_words(rng))
        hdg = (ev.get("weapon_pos") or {}).get("heading")
        closing = hdg is None or abs(((brg + 180.0 - hdg) + 180.0) % 360.0 - 180.0) <= 45.0
        if rng <= MISSILE_PICTURE_NM or closing:
            self.missile_shots[wid] = {
                "id": wid, "type": wtype, "raw_type": ev["weapon"], "spoken_name": spoken,
                "bearing": where.get("bearing_mag") or where.get("bearing_true"), "range_nm": round(rng, 1),
                "clock": hour, "call": call, "launcher": spoken_type(ini.get("type")), "first_seen": ev["t"],
                "from": "shot event", "closing": closing, "targeting_me": False,
                "_launch": (src["lat"], src["lon"]), "_group": ini.get("group"), "_launcher": ini.get("name")}
        if not closing:
            return None  # flying away from Prometheus
        if self._eval_scan_on(ev["t"]):
            return None  # the scan normally sees it within a second; _prune_missiles alerts if it does not
        return self._shot_alert(self.missile_shots[wid], ev["t"])

    def _shot_alert(self, b, now):
        """The launch alert from a shot-event row (scan off, or the scan did not report in time). Once per row."""
        a = self._threat_alert("launch-%s" % b["id"], b["call"], "missile launch", now, source="shot event",
                               type=b["type"], launcher=b["launcher"], range_nm=b["range_nm"], clock=b["clock"],
                               id=b["id"], raw_type=b["raw_type"])
        if a is not None:
            b["_alert"] = a["key"]
        return a

    def _newer_scan(self, b):
        """A scan SENT after this shot has completed (it would have seen the missile)."""
        return self.weapons_asked_at is not None and self.weapons_asked_at > b["first_seen"]

    def _shot_live(self, b, now):
        """A shot-event row lives until a scan newer than the shot has run (that scan merged it or did not see
        it), at least BRIDGE_S and at most EVAL_OFF_KEEP_S (the old shot-event timeout, about one SAM flight)."""
        age = now - b["first_seen"]
        return age <= BRIDGE_S or (age <= EVAL_OFF_KEEP_S and not self._newer_scan(b))

    def _match_bridge(self, r, now, launcher_group):
        """The shot-event bridge row of a missile the scan sees for the first time: same type and (launcher unit,
        launcher group, or close to the launch point). DCS reports the SA-2 radar as getLauncher() while the shot
        event names the launcher, so the group and the launch point carry the match. Pops the bridge row.
        Ties (a salvo from one site) go to the shot whose distance flown best fits its time since launch, never to
        the oldest: an older, already-called missile must not hand its alert to a new one (review P1b)."""
        rtype = _norm_weapon_name(r.get("type"))
        best = None
        for key, b in self.missile_shots.items():
            if now - b["first_seen"] > EVAL_OFF_KEEP_S:
                continue
            btype = _norm_weapon_name(b.get("raw_type"))
            if rtype and btype and btype != rtype:
                continue
            same = bool(r.get("launcher")) and r.get("launcher") == b.get("_launcher") or \
                bool(launcher_group) and launcher_group == b.get("_group")
            _, d = bearing_range(b["_launch"][0], b["_launch"][1], r["lat"], r["lon"])
            if not same and d > 3.0 + max(0.0, now - b["first_seen"]):
                continue
            score = (0 if same else 1, abs(d - 0.35 * max(0.0, now - b["first_seen"])))  # ~0.35 nm/s average
            if best is None or score < best[0]:
                best = (score, key)
        return self.missile_shots.pop(best[1]) if best else None

    def on_weapons(self, rows, asked_at=None):
        """Missiles in flight near Prometheus (Eval world.searchObjects): rows {id, type, lat, lon, alt, vn, ve,
        target, launcher, launcher_type, coal}. THE source of threats.missiles_in_flight (real DCS weapon ids).
        asked_at = when this scan was sent (eye clock; default now): only a scan sent after a shot can clear it."""
        now = self.clock()
        with self.lock:
            own = self._own_locked()
            if rows is None or len(rows) < WEAPONS_SCAN_CAP:  # a full scan may have left a missile out
                self.weapons_asked_at = now if asked_at is None else asked_at
            if own is None or own.get("lat") is None:
                self.weapons_in_flight, self.weapons_at = {}, now
                self.missiles_near = {}
                return []
            current, near, new = {}, {}, []
            by_name = None
            for r in rows or []:
                if not isinstance(r, dict) or r.get("lat") is None or r.get("lon") is None:
                    continue
                raw_coal = r.get("coal")
                coal = ({1: "red", 2: "blue", 0: "neutral"}.get(raw_coal, "unknown") if isinstance(raw_coal, int)
                        else _coal(str(raw_coal or "")))
                if r.get("launcher") and r.get("launcher") == own.get("name"):
                    continue
                if not self._hostile_to(coal, own):
                    continue
                if weapon_kind(r.get("type")) in ("gun", "bomb", "rocket"):
                    continue  # AAA shells, bombs and rockets are not missile launches
                res = self._where(own, r["lat"], r["lon"], r.get("alt"))
                if res is None:
                    continue
                where, brg, rng = res
                vn, ve = r.get("vn") or 0.0, r.get("ve") or 0.0
                speed = math.hypot(vn, ve)
                hdg = (math.degrees(math.atan2(ve, vn)) + 360.0) % 360.0
                closing = speed > 50 and abs(((brg + 180.0 - hdg) + 180.0) % 360.0 - 180.0) <= 30.0
                at_me = bool(r.get("target")) and r.get("target") == own.get("name")
                hour = clock_of(brg - (own.get("heading") or 0.0))
                wid = str(r.get("id") or "%s-%s" % (r.get("type"), r.get("launcher")))
                wtype = weapon_name(r.get("type") or "missile")
                if by_name is None:
                    by_name = {u.get("name"): u for u in self.units.values()}
                launcher_type = r.get("launcher_type") or (by_name.get(r.get("launcher")) or {}).get("type")
                spoken = missile_spoken_name(r.get("type"), launcher_type)
                row = {"id": wid, "type": wtype, "raw_type": r.get("type"), "spoken_name": spoken,
                       "bearing": where.get("bearing_mag") or where.get("bearing_true"),
                       "range_nm": round(rng, 1), "clock": hour, "targeting_me": at_me,
                       "closing": closing, "launcher": launcher_type,
                       "speed_kt": int(round(speed * MS_TO_KT))}
                alertable = at_me or (closing and rng <= MISSILE_ALERT_NM)
                prior = self.missiles_near.get(wid)
                if prior is None and (alertable or rng <= max(MISSILE_PICTURE_NM, MISSILE_ALERT_NM)):
                    bridge = self._match_bridge(r, now, (by_name.get(r.get("launcher")) or {}).get("group"))
                    prior = {"first_seen": bridge["first_seen"], "_alert": bridge.get("_alert"),
                             "spoken_name": bridge["spoken_name"], "launcher": bridge["launcher"],
                             "_shot_closing": bool(bridge.get("closing"))} if bridge else None
                    if bridge and bridge.get("_alert"):
                        for a in self.threat_alerts:  # the shot-event alert now follows the live missile
                            if a["key"] == bridge["_alert"]:
                                a["id"] = wid
                prior = prior or {}
                if not launcher_type and prior.get("launcher"):
                    row["launcher"] = prior["launcher"]
                    row["spoken_name"] = missile_spoken_name(r.get("type"), prior["launcher"])
                row["call"] = "Missile launch, %s, your %s o'clock, %s." % (row["spoken_name"], words(hour), range_words(rng))
                # Never worse than r11 (review P1b): r11 called a launch whose shot event pointed at the pilot (+-45
                # deg, within MISSILE_ALERT_NM). The scan's own rule (target / +-30 deg) may not take that call
                # away from a missile with no DCS target, so a shot-event "closing" missile stays alertable.
                alertable = alertable or bool(prior.get("_shot_closing") and rng <= MISSILE_ALERT_NM)
                if not (alertable or rng <= MISSILE_PICTURE_NM):
                    continue
                near[wid] = dict(row, first_seen=prior.get("first_seen", now), _alert=prior.get("_alert"),
                                 _shot_closing=prior.get("_shot_closing", False))
                if not alertable:
                    continue
                current[wid] = row
                if wid not in self.weapons_in_flight and not near[wid]["_alert"]:
                    a = self._threat_alert("missile-%s" % wid, row["call"], "missile launch", now,
                                           source="DCS-gRPC weapon in flight", type=wtype, range_nm=row["range_nm"],
                                           clock=hour, id=wid)
                    if a:
                        near[wid]["_alert"] = a["key"]
                        new.append(a)
            self.weapons_in_flight, self.weapons_at = current, now
            self.missiles_near = near
            return new

    def _prune_missiles(self, now):
        """tick(): a shot-event row whose bridge time is over and that no newer scan has reported raises its launch
        alert now (once; review P1: a stalled scan must never swallow a launch); rows end per _shot_live."""
        for wid, b in list(self.missile_shots.items()):
            if not self._shot_live(b, now):
                del self.missile_shots[wid]
            elif now - b["first_seen"] > BRIDGE_S and b.get("closing") and not b.get("_alert"):
                self._shot_alert(b, now)

    def radar_candidates(self, limit=RADAR_POLL_MAX):
        """Names of hostile SAM/AAA/EWR units to ask DCS 'is your radar on / tracking Prometheus' (nearest first,
        known radars before the rest, bounded)."""
        with self.lock:
            own = self._own_locked()
            if own is None or own.get("lat") is None:
                return []
            rows = []
            for u in self.units.values():
                if u["category"] not in ("SAM", "AAA") or u.get("lat") is None or not u.get("name"):
                    continue
                if not self._hostile_to(u["coalition"], own):
                    continue
                _, rng = bearing_range(own["lat"], own["lon"], u["lat"], u["lon"])
                if rng > RADAR_POLL_NM:
                    continue
                attrs = type_attributes(u["type"])
                radar = bool(attrs & {"SAM SR", "SAM TR", "EWR"}) if attrs else sam_profile(u["type"])[1] is not None
                rows.append((not radar, rng, u["name"]))
            rows.sort()
            return [name for _, _, name in rows[:limit]]

    def on_radars(self, rows):
        """Eval radar state (1 Hz): rows {name, has_radar, on, tracking_me, detected}. Missing fields = unknown."""
        now = self.clock()
        with self.lock:
            by_name = {u.get("name"): u for u in self.units.values()}
            out = {}
            for r in rows or []:
                if not isinstance(r, dict) or not r.get("name"):
                    continue
                u = by_name.get(r["name"]) or {}
                out[r["name"]] = {"has_radar": bool(r.get("has_radar")), "on": bool(r.get("on")),
                                  "tracking_me": bool(r.get("tracking_me")), "detected": bool(r.get("detected")),
                                  "group": u.get("group") or r["name"], "type": u.get("type")}
            self.radars, self.radars_at = out, now
            return out

    def _radar_fresh(self, now):
        return self.radars_at is not None and now - self.radars_at <= RADAR_FRESH_S

    _RADAR_LEVEL = {"off": 0, "searching": 1, "sees you": 2, "tracking you": 3}

    def _radar_groups(self, now):
        """SAM group -> strongest DCS radar state among its polled radar units ('off' when every radar is off)."""
        out = {}
        if not self._radar_fresh(now):
            return out
        for r in self.radars.values():
            if not r["has_radar"]:
                continue
            state = ("tracking you" if r["tracking_me"] else "sees you" if r["on"] and r["detected"]
                     else "searching" if r["on"] else "off")
            if self._RADAR_LEVEL[state] >= self._RADAR_LEVEL.get(out.get(r["group"]), -1):
                out[r["group"]] = state
        return out

    def _sam_threats(self, own, now, all_sites=False):
        """Hostile SAM/AAA/EWR threats, one row per group, nearest first. Never hides a site (review P1): every
        live air-defence group inside its nominal range is listed with the range-table "could be tracking" call
        (RANGE_HYST_NM hysteresis), whatever DCS says about its radar -- a radar reported off or only searching can
        light up seconds before a launch. DCS radar data only ADDS: "sees you" / "tracking you" upgrade the call,
        and "tracking you" lists the group at any polled range. Only non-air-defence types (labels), dead units
        and command/comm vehicles are left out. all_sites also returns sites outside weapon range,
        with factual site geometry for approach cautions and pilot statements. Pure read."""
        best = {}
        own_alt = own.get("alt_m") or 0.0
        radar = self._radar_groups(now)
        for u in self.units.values():
            if u["category"] not in ("SAM", "AAA") or u.get("lat") is None:
                continue
            if not self._hostile_to(u["coalition"], own):
                continue
            if _air_defence_support(u["type"]):
                continue  # command post, comm, generator: neither emits nor launches
            nominal, label, ewr = sam_profile(u["type"])
            if u["category"] == "AAA" and label is None:
                continue  # optical triple A: not a radar threat
            key = u.get("group") or str(u["id"])
            state = radar.get(key)
            limit = nominal + (RANGE_HYST_NM if key in self._sam_listed else 0.0)
            _, rng = bearing_range(own["lat"], own["lon"], u["lat"], u["lon"])
            slant = math.hypot(rng, (own_alt - (u.get("alt_m") or 0.0)) / NM)
            if not all_sites and state != "tracking you" and (rng > limit + 1 or slant > limit):
                continue
            prev = best.get(key)
            if prev is None or (nominal, -slant) > (prev[0], -prev[1]):
                best[key] = (nominal, slant, u, label, ewr, state)
        rows = []
        for key, (nominal, slant, u, label, ewr, state) in best.items():
            res = self._where(own, u["lat"], u["lon"])
            if res is None:
                continue
            _, brg, _ = res
            hour = clock_of(brg - (own.get("heading") or 0.0))
            name = label or spoken_type(u["type"])
            where = "your %s o'clock, %s." % (words(hour), range_words(slant))
            radar_state = state
            if state in ("off", "searching"):
                state = None  # no tracking data: the range-table warning stands
            if state is not None:
                what = "Early warning radar %s" % spoken_type(u["type"]) if ewr else \
                    "Radar triple A %s" % spoken_type(u["type"]) if label == "radar triple A" else "SAM %s" % name
                verb = {"tracking you": "tracking you", "sees you": "radar sees you"}[state]
                call, confidence = "%s %s, %s" % (what, verb, where), "DCS radar: " + state
            elif ewr:
                call = "Early warning radar %s could be watching you, %s" % (spoken_type(u["type"]), where)
                confidence = "range only: could be tracking"
            elif label == "radar triple A":
                call = "Radar triple A %s could be tracking you, %s" % (spoken_type(u["type"]), where)
                confidence = "range only: could be tracking"
            else:
                call = "SAM %s could be tracking you, %s" % (name, where)
                confidence = "range only: could be tracking"
            row = {"group": key, "type": u["type"], "sam": name, "ewr": ewr, "range_nm": round(slant, 1),
                   "nominal_range_nm": nominal, "clock": hour, "call": call, "confidence": confidence}
            if all_sites:
                row["sam"] = label or "SAM site"
                row["caution_range_nm"] = min(LONG_RANGE_NM, max(1.5 * nominal, 8.0))
                row["outside_weapon_range"] = slant > nominal
                row["inside_caution_range"] = slant <= row["caution_range_nm"]
                guidance = SAM_GUIDANCE.get(label)
                row["caution_call"] = "Caution, %s%s, your %s o'clock, %s, lethal inside %s." % (
                    label or "SAM site", ", " + guidance if guidance else "", words(hour), range_words(slant),
                    words(math.ceil(nominal)))
                if slant > nominal and state is None:
                    row["call"] = "%s site, %s Lethal inside %s." % (label or "SAM", where, words(math.ceil(nominal)))
                    row["confidence"] = "live site; outside nominal weapon range, not evidence of tracking"
            if radar_state in ("off", "searching"):
                row["radar"] = radar_state  # DCS's word, shown; a dark radar can still ambush
            rows.append(row)
        rows.sort(key=lambda r: (r["ewr"], r["confidence"] != "DCS radar: tracking you", r["range_nm"]))
        return rows

    def _update_sam_state(self, own, now):
        """tick(): range hysteresis memory + SAM alerts (once per group; again after SAM_REARM_S away, and once
        more when DCS says the radar went from anything else to tracking you)."""
        for r in self._sam_threats(own, now, all_sites=True):
            if r["ewr"]:
                continue
            g = r["group"]
            if not r["inside_caution_range"]:
                if g in self._sam_cautioned:
                    self._sam_cautioned.remove(g)
                    self._sam_caution_arm[g] += 1
            elif g not in self._sam_cautioned:
                self._sam_cautioned.add(g)
                if r["outside_weapon_range"]:
                    self._threat_alert("approach-%s-%d" % (g, self._sam_caution_arm[g]), r["caution_call"],
                                       "SAM approach", now, group=g, source="DCS-gRPC SAM range")
        rows = self._sam_threats(own, now)
        listed = set()
        for r in rows:
            g = r["group"]
            listed.add(g)
            self._sam_inside[g] = now
            if r["ewr"]:
                continue
            tracking = r["confidence"] == "DCS radar: tracking you"
            # Range alerts also fire when the cockpit exports an RWR (review P1, minor c): an RWR cannot see a
            # radar that is off, so it cannot replace the range warning for a dark site; a duplicate call next to
            # an RWR spike is the accepted false-alarm cost.
            prev = self._sam_alerted.get(g)
            if prev is not None and (prev[1] or not tracking):
                continue
            self._sam_alerted[g] = (now, tracking)
            kind = "SAM tracking" if tracking else "SAM in range"
            self._threat_alert("sam-%s-%d%s" % (g, self._sam_arm[g], "-track" if tracking else ""), r["call"],
                               kind, now, group=g,
                               source="DCS radar state" if r["confidence"].startswith("DCS") else "DCS-gRPC SAM range")
        self._sam_listed = listed
        for g, last in list(self._sam_inside.items()):
            if g not in listed and now - last > SAM_REARM_S:
                del self._sam_inside[g]
                if self._sam_alerted.pop(g, None) is not None:
                    self._sam_arm[g] += 1

    def _rwr_source(self):
        latest = self.cockpit.latest
        if latest is not None and self.cockpit.fresh() and isinstance(latest.get("rwr"), dict):
            return "cockpit"
        if self.connected and self.units_at is not None:
            return "grpc_inferred"
        return "none"

    def _threats_json(self, own, now):
        """Pure read: (threats section, sam rows, all missile rows) -- the last two feed the pending alerts."""
        source = self._rwr_source()
        out = {"rwr_source": source, "rwr_export_by_module": self.cockpit.rwr_modules_json()}
        radar_fresh = self._radar_fresh(now)
        out["sam_source"] = "dcs_radar" if radar_fresh else "range_table"
        tracking = []
        if radar_fresh:
            for r in self.radars.values():
                if r["has_radar"] and r["tracking_me"]:
                    tracking.append({"group": r["group"], "type": r["type"], "name": spoken_type(r["type"])})
        out["sam_radar"] = {
            "available": radar_fresh, "age_s": None if self.radars_at is None else round(now - self.radars_at, 1),
            "source": "DCS Unit:getRadar() + group radar detection (Eval, 1 Hz)",
            "confidence": "DCS truth" if radar_fresh else "unavailable (range table used)",
            "tracking_you": tracking[:5],
            "radars_on": sum(1 for r in self.radars.values() if r["has_radar"] and r["on"]) if radar_fresh else 0}
        if own is None or own.get("lat") is None:
            # None, not []: with no pilot position nothing was checked, so an empty list would read as all-clear.
            out["sam_in_range"], out["missiles_inbound"] = None, None
            out["note"] = "no position for Prometheus: SAM range and missile checks are NOT running (not an all-clear)"
            return out, [], []
        # "off" = the Eval missile scan is not answering (Eval disabled / stale): only shot events can show a missile
        # not "on": the picture says why (read aloud as the reason missile tracking is limited)
        out["missile_scan"] = "on" if self._eval_scan_on(now) else (
            "off: DCS Eval is off" if str(self.stores_status).startswith("unavailable") else "off: missile scan is not answering")
        sams = self._sam_threats(own, now)
        sites = [r for r in self._sam_threats(own, now, all_sites=True) if not r["ewr"] and r["range_nm"] <= LONG_RANGE_NM]
        sites.sort(key=lambda r: r["range_nm"])
        out["sam_sites"] = sites[:PICTURE_TOP]
        out["sam_in_range"] = sams[:PICTURE_TOP]
        if len(sams) > PICTURE_TOP:
            out["more_sam_groups_in_range"] = len(sams) - PICTURE_TOP
        flight = self._missiles_in_flight(now)
        out["missiles_inbound"] = [m for m in flight if m.get("targeting_me") or m.get("closing")][:PICTURE_TOP]
        out["missiles_in_flight"] = flight[:PICTURE_TOP + 2]
        if source != "cockpit":
            out["note"] = ("no RWR feed from this aircraft. sam_in_range confidence 'DCS radar: ...' is DCS's own radar "
                           "state; 'range only' rows are inferred from nominal range: say 'could be' for those")
        return out, sams + sites, flight

    def _missiles_in_flight(self, now, fresh_weapons=None):
        """One row per missile: the live scan's rows (real DCS weapon ids), plus shot-event rows while they bridge
        (_shot_live: until a scan sent after the shot has answered, BRIDGE_S..EVAL_OFF_KEEP_S). Pure read."""
        scan = self._eval_scan_on(now)
        rows = {}
        if scan:
            for wid, a in self.missiles_near.items():
                rows[wid] = dict({k: v for k, v in a.items() if not k.startswith("_")}, age_s=int(now - a["first_seen"]))
        for wid, a in self.missile_shots.items():
            if self._shot_live(a, now) and wid not in rows:
                rows[wid] = dict({k: v for k, v in a.items() if not k.startswith("_")},
                                 age_s=int(now - a["first_seen"]),
                                 scan="not yet in the live scan" if scan else "no live scan (Eval off)")
        return sorted(rows.values(), key=lambda m: (not m.get("targeting_me"), m.get("range_nm") or 0))

    def _pending_threat_alerts(self, now, sams=None, flight=None):
        """Alerts not yet announced. Pure read. A missile alert whose missile is gone is dropped and a live one is
        re-worded from the live row (R3 T5); a SAM alert only while that group is still listed."""
        cockpit_launch = any(a["signal"] == "launch" and now - a["t"] <= 15 for a in self.cockpit.alerts)
        live = {m["id"]: m for m in (flight or [])}
        groups = {r["group"]: r for r in (sams or []) if "caution_range_nm" not in r}
        approaches = {r["group"]: r for r in (sams or []) if r.get("inside_caution_range") and r.get("outside_weapon_range")}
        out = []
        for a in self.threat_alerts:
            if a["key"] in self.announced or (a["kind"] != "SAM approach" and now - a["t"] > THREAT_ANNOUNCE_S):
                continue
            if a["kind"] == "SAM approach":
                r = approaches.get(a.get("group"))
                if r is None or a["key"] != "approach-%s-%d" % (r["group"], self._sam_caution_arm[r["group"]]):
                    continue
                a = dict(a, call=r["caution_call"])
            elif a["kind"] == "missile launch":
                if cockpit_launch:
                    continue  # the cockpit RWR already called the launch
                m = live.get(a.get("id"))
                if m is None:
                    continue  # that missile is gone: no stale launch call
                a = dict(a, call=m["call"])
            elif a.get("group") is not None:
                if a["group"] not in groups:
                    continue
                a = dict(a, call=groups[a["group"]]["call"])
            out.append(a)
        out.sort(key=lambda a: (a["kind"] != "missile launch", a["kind"] == "SAM approach"))
        return out

    # -- stores / loadout (Unit:getAmmo() via DCS-gRPC Eval)
    def _record_damage(self, source, call, now, **details):
        own = self._own_locked()
        row = dict(t=now, source=source, call=call, unit_id=own["id"] if own else None, **details)
        self.damage_events.append(row)
        self.events.append({"seq": next(self._seq), "t": now, "kind": "own_damage",
                            "initiator": None, "target": own, "call": call})
        self._log_store(dict(row, kind="own_damage"))

    def on_life(self, unit, snapshot):
        """Life is primary damage evidence even when the cockpit reports no warnings."""
        life, maximum = _f(snapshot.get("life")), _f(snapshot.get("life0"))
        if life is None or maximum is None or life < 0 or maximum <= 0:
            return
        with self.lock:
            own = self._own_locked()
            if own is None or unit["id"] != own["id"]:
                return
            now, prev = self.clock(), self.life
            pct = int(round(100 * life / maximum))
            self.life = {"unit_id": unit["id"], "life": life, "life0": maximum, "percent": pct, "t": now}
            if prev and prev["unit_id"] == unit["id"] and prev["life0"] == maximum and life < prev["life"]:
                self._record_damage("DCS-gRPC getLife", "You're hit, airframe at %s percent." % words(pct),
                                    now, life_percent=pct, life_drop=prev["life"] - life)

    def _damage_json(self, own, now, cockpit):
        life = self.life if self.life and self.life["unit_id"] == own["id"] else None
        out = {"life_available": life is not None,
               "events": [dict(r, age_s=round(now - r["t"], 1)) for r in self.damage_events
                          if r["unit_id"] == own["id"] and now - r["t"] <= 600][-10:],
               "note": "No warnings does not mean no damage. Hit events and life drops are primary evidence."}
        if life:
            out.update(life_percent=life["percent"], life_age_s=round(now - life["t"], 1))
        if cockpit.get("fresh"):
            out["secondary"] = {"warning_lights": cockpit.get("warning_lights", []),
                                "engine": cockpit.get("engine", {}), "fuel_trend": cockpit.get("fuel_trend", {})}
        return out

    def set_stores_status(self, status):
        with self.lock:
            self.stores_status = status

    def on_cockpit_packet(self, pkt, src=None):
        """One AthenaExport packet (cockpit.CockpitListener). Payload -> the same stores diff as the Eval loadout
        (source "cockpit"); new RWR locks/launches -> durable log + rwr_alert_to_announce."""
        with self.lock:
            res = self.cockpit.ingest(pkt, src)
            if self.memory is not None:  # same reset fence as derived cockpit facts
                self.memory.publish("cockpit_input", {"packet": pkt, "derived": res}, at=self.clock())
            for a in res["alerts"]:
                self._log_store({"kind": "rwr_alert", "signal": a["signal"], "emitter": a["type"],
                                 "clock": a["clock"], "call": a["call"]})
            if res["event"] in ("start", "stop"):
                self._log_store({"kind": "cockpit_" + res["event"], "aircraft": pkt.get("ac"), "source": src})
            if res["payload"] is not None:
                own = self._own_locked()
                if own is not None:
                    self.on_stores(own, res["payload"], source="cockpit")
        return res

    def cockpit_payload_fresh(self):
        return self.cockpit.payload_fresh()

    def _match_unit(self, lat, lon, max_nm=1.0, exclude=None):
        """Nearest live unit to a position (cockpit locked target -> DCS unit)."""
        if lat is None or lon is None:
            return None
        best = None
        for u in self.units.values():
            if u.get("lat") is None or (exclude is not None and u["id"] == exclude):
                continue
            _, rng = bearing_range(lat, lon, u["lat"], u["lon"])
            if rng <= max_nm and (best is None or rng < best[0]):
                best = (rng, u)
        return best[1] if best else None

    def on_stores(self, unit, snapshot, source="eval"):
        """One loadout poll for a unit. snapshot = {"fuel": 0.53, "ammo": [{"type", "name", "count", "category"}]}.
        Diffs against the previous poll: a count drop = weapon used (attributed to my shot, or creates one when
        the shot event is missing); a count rise = rearmed."""
        now = self.clock()
        if source == "eval":
            self.on_life(unit, snapshot)
        items = {}
        ammo = snapshot.get("ammo") or []
        if isinstance(ammo, dict):  # an empty Lua table may encode as {}
            ammo = list(ammo.values())
        for a in ammo:
            if not isinstance(a, dict):
                continue
            t = a.get("type") or a.get("name") or "unknown"
            row = items.setdefault(t, {"name": a.get("name") or t, "count": 0,
                                       "kind": AMMO_KIND.get(a.get("category"), weapon_kind(t))})
            try:
                row["count"] += int(a.get("count") or 0)
            except (TypeError, ValueError):
                pass
        fuel = snapshot.get("fuel")
        fuel = float(fuel) if isinstance(fuel, (int, float)) else None
        fuel_max = _f(snapshot.get("fuel_max_kg"))
        if fuel_max and fuel_max > 0 and unit.get("type"):
            from . import cockpit as _cp
            _cp.DCS_FUEL_MAX_KG[unit["type"]] = fuel_max  # DCS getDesc().fuelMassMax beats the airframe table
        with self.lock:
            self.stores_status = "ok"
            uid = unit["id"]
            prev = self.stores.get(uid)
            own = self._own_locked()
            mine = own is not None and own["id"] == uid
            if source == "eval" and mine and fuel is not None and fuel > 0:
                self.fuel, self.fuel_at = fuel, now  # Unit:getFuel(): the fuel fallback when the cockpit has none
            if source == "eval" and mine and self.cockpit.payload_fresh():
                return []  # the cockpit payload (1 Hz) is the ownship stores source while it is fresh
            self.stores[uid] = {"t": now, "unit_type": unit.get("type"), "player": unit.get("player"),
                                "items": items, "fuel": fuel, "source": source}
            if mine and fuel is not None:
                self.fuel, self.fuel_at = fuel, now
            base = {"unit": unit.get("type"), "player": unit.get("player"), "unit_name": unit.get("name"),
                    "mine": mine, "source": source}
            if prev is not None and prev.get("source", "eval") != source:
                # Eval and cockpit name weapons differently: a source switch is a new baseline, not a diff.
                self._log_store(dict(base, kind="stores_source_switch", items=items, fuel=fuel,
                                     previous_source=prev.get("source", "eval")))
                return []
            if prev is None:
                self._log_store(dict(base, kind="stores_snapshot", items=items, fuel=fuel))
                return []
            changes, used = [], []
            for t in sorted(set(prev["items"]) | set(items)):
                before = prev["items"].get(t, {}).get("count", 0)
                after = items.get(t, {}).get("count", 0)
                if before == after:
                    continue
                info = items.get(t) or prev["items"][t]
                changes.append({"weapon": info["name"], "type": t, "from": before, "to": after})
                if after < before:
                    used.append({"t": now, "unit_id": uid, "weapon": info["name"], "type": t, "kind": info["kind"],
                                 "count": before - after, "mine": mine})
            if changes:
                self._log_store(dict(base, kind="stores_change", changes=changes, items=items, fuel=fuel))
            gun = [u for u in used if u["kind"] == "gun"]
            if len(gun) > 1:  # HE + AP belts of one gun = one burst
                used = [u for u in used if u["kind"] != "gun"] + [dict(gun[0], weapon="gun rounds", type="gun",
                                                                       count=sum(g["count"] for g in gun))]
            for u in used:
                self.consumption.append(u)
                if mine:
                    self.used_session["gun rounds" if u["kind"] == "gun" else u["weapon"]] += u["count"]
                    self._attribute_use(own, u)
            while self.consumption and now - self.consumption[0]["t"] > SHOT_KEEP_S:
                self.consumption.popleft()
            return used

    def _attribute_use(self, own, use):
        """Name the weapon of my recent shot from the stores drop; if no shot event exists, create the shot."""
        now = use["t"]
        if use["kind"] == "gun":
            # The 1 Hz cockpit payload sees one burst as several drops: add them to the burst still running.
            for s in reversed(self.shots):
                if now - s["t"] > 60:
                    break
                if s["kind"] == "gun" and s.get("stores_weapon") and now - s.get("stores_last_t", s["t"]) <= 4:
                    s["stores_count"] = (s.get("stores_count") or 0) + use["count"]
                    s["stores_last_t"] = now
                    s["weapon"] = "gun, %s rounds" % s["stores_count"]
                    if s["burst_end"] is not None:
                        s["burst_end"] = max(s["burst_end"], now)
                    return s
        for s in reversed(self.shots):
            if now - s["t"] > 20:
                break
            if s.get("stores_weapon"):
                continue
            if s["kind"] == "gun" and use["kind"] != "gun":
                continue
            if use["kind"] == "gun" and s["kind"] != "gun":
                continue
            if s["weapon"] and use["kind"] != "gun" and \
                    _norm_weapon_name(s["weapon"]) not in _norm_weapon_name(use["type"] + use["weapon"]) and \
                    _norm_weapon_name(use["weapon"]) not in _norm_weapon_name(s["weapon"]):
                continue
            s["stores_weapon"] = use["weapon"]
            s["stores_count"] = use["count"]
            s["stores_last_t"] = now
            if not s["weapon"] or s["kind"] == "gun":
                s["weapon"] = use["weapon"] if use["kind"] != "gun" else "gun, %s rounds" % use["count"]
                s["weapon_source"] = "stores_diff"
            else:
                s.setdefault("weapon_source", "shot event")
            self._log_store({"kind": "shot_weapon", "weapon": s["weapon"], "count": use["count"],
                             "source": "stores_diff matched to shot event"})
            return s
        # No shot event (event stream down, or it lacks the weapon): the stores drop IS the shot.
        ev = {"seq": next(self._seq), "t": now, "mission_time": None, "initiator": dict(own, kind="unit")}
        name = use["weapon"] if use["kind"] != "gun" else "gun, %s rounds" % use["count"]
        kind = use["kind"] if use["kind"] in ("gun", "rocket", "bomb") else weapon_kind(use["type"])
        s = self._new_shot(ev, name, None, burst=False, kind=kind)
        s["burst_end"] = now
        s.update(source="stores_diff", event_seen=False, weapon_source="stores_diff", stores_weapon=use["weapon"],
                 stores_count=use["count"], stores_last_t=now)
        self._log_store({"kind": "shot", "weapon": name, "weapon_kind": s["kind"], "count": use["count"],
                         "source": "stores_diff (no shot event)", "intended_target": s["intended_target"]})
        return s

    def _prune(self, now):
        while self.events and now - self.events[0]["t"] > EVENT_KEEP_S:
            self.events.popleft()
        while self.shots and now - self.shots[0]["t"] > SHOT_KEEP_S:
            self.shots.popleft()

    def _settle_shots(self, now):
        for s in self.shots:
            if s["result"] == "pending" and self._settled_miss(s, now):
                s["result"] = "miss"
                s["resolved_t"] = now

    @staticmethod
    def _settled_miss(s, now):
        start = s["burst_end"] if s["burst_end"] is not None else s["t"]
        if s["kind"] == "gun" and s["burst_end"] is None and now - s["t"] < 60:
            return False
        return now - start > _resolve_window(s["kind"])

    # -- picture
    def _where(self, own, lat, lon, alt_m=None):
        if own is None or own.get("lat") is None or lat is None:
            return None
        brg, rng = bearing_range(own["lat"], own["lon"], lat, lon)
        rel = brg - (own.get("heading") or 0.0)
        out = {"range_nm": round(rng, 1), "clock": clock_phrase(rel)}
        if self.declination is not None:
            out["bearing_mag"] = _three(brg - self.declination)
        else:
            out["bearing_true"] = _three(brg)
        if alt_m is not None:
            out["alt_ft"] = int(round(alt_m * M_TO_FT, -2))
        return out, brg, rng

    def _cluster(self, own, units):
        """Group contacts within CONTACT_RADIUS_NM, nearest first (same coalition + domain, 3 nm air / 1.5 nm
        ground). Busy maps (round 4): flat-earth prefilter + a 3 nm spatial grid, so it is ~linear in units."""
        rows = []
        lat0, lon0 = own["lat"], own["lon"]
        coslat = math.cos(math.radians(lat0))
        for u in units:
            dy = (u["lat"] - lat0) * 60.0
            dx = (u["lon"] - lon0) * 60.0 * coslat
            if dx * dx + dy * dy > (CONTACT_RADIUS_NM + 5.0) ** 2:
                continue
            brg, rng = bearing_range(lat0, lon0, u["lat"], u["lon"])
            if rng <= CONTACT_RADIUS_NM:
                rows.append((rng, brg, u, dx, dy))
        rows.sort(key=lambda r: r[0])
        cell = 3.2  # >= the largest radius x 1.05 flat-earth margin: neighbours are always in the 3x3 block
        grid = {}
        doms = []
        for i, (rng, brg, u, dx, dy) in enumerate(rows):
            grid.setdefault((int(dx // cell), int(dy // cell)), []).append(i)
            doms.append(domain(u["category"]))
        used = [False] * len(rows)
        groups = []
        for i, (rng, brg, u, dx, dy) in enumerate(rows):
            if used[i]:
                continue
            used[i] = True
            dom = doms[i]
            members = [i]
            radius = 3.0 if dom != "ground" else 1.5
            cx, cy = int(dx // cell), int(dy // cell)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j in grid.get((gx, gy), ()):
                        if used[j] or doms[j] != dom:
                            continue
                        v = rows[j][2]
                        if v["coalition"] != u["coalition"]:
                            continue
                        if (rows[j][3] - dx) ** 2 + (rows[j][4] - dy) ** 2 > (radius * 1.05) ** 2:
                            continue
                        _, d = bearing_range(u["lat"], u["lon"], v["lat"], v["lon"])
                        if d > radius:
                            continue
                        if dom == "ground" and (u["category"] in AIR_DEFENCE or v["category"] in AIR_DEFENCE) \
                                and u["category"] != v["category"]:
                            continue  # round 9 review: SAM/AAA never fold into vehicle/armor groups
                        if dom == "air" and abs((v["alt_m"] or 0) - (u["alt_m"] or 0)) * M_TO_FT > 5000:
                            continue
                        used[j] = True
                        members.append(j)
            members.sort()
            groups.append((dom, [rows[k][:3] for k in members]))
        return groups

    def _group_json(self, own, dom, members):
        rng, brg, lead = members[0]
        mine = own["coalition"]
        coal = lead["coalition"]
        side = "friendly" if coal == mine else "neutral" if coal in ("neutral", "unknown") else "hostile"
        types = Counter(m[2]["type"] for m in members)
        cats = Counter(m[2]["category"] for m in members)
        cat = cats.most_common(1)[0][0]
        rel = brg - (own.get("heading") or 0.0)
        g = {"side": side, "coalition": coal, "domain": dom, "category": cat, "count": len(members),
             "types": dict(types), "spoken_types": dict(Counter(spoken_type(t) for t in types.elements())),
             "range_nm": round(rng, 1), "clock": clock_phrase(rel)}
        if self.declination is not None:
            g["bearing_mag"] = _three(brg - self.declination)
        else:
            g["bearing_true"] = _three(brg)
        if side == "hostile":
            g["label"] = ("bandit" if dom == "air" else
                          {"SAM": "SAM site", "AAA": "triple A", "armor": "enemy armor", "ship": "enemy ship",
                           "carrier": "enemy carrier"}.get(cat, "enemy " + cat))
            if cat in AIR_DEFENCE:
                labels = list(dict.fromkeys(label for t in types if (label := sam_profile(t)[1])))
                if labels:
                    g["label"] = " and ".join(labels) + " site"
        if dom == "air":
            alts = [m[2]["alt_m"] or 0 for m in members]
            alt_ft = sum(alts) / len(alts) * M_TO_FT
            g["alt_ft"] = int(round(alt_ft, -2))
            g["angels"] = "low" if alt_ft < 1000 else int(round(alt_ft / 1000.0))
            own_ft = (own.get("alt_m") or 0) * M_TO_FT
            g["vertical"] = "high" if alt_ft - own_ft > 1000 else "low" if own_ft - alt_ft > 1000 else "level"
            g["speed_kt"] = int(round((lead["speed_ms"] or 0) * MS_TO_KT))
            g["heading_true"] = _three(lead["track"] or 0)
            g["aspect"] = aspect_of(brg, lead["track"] or 0, lead["speed_ms"])
            g["closure_kt"] = int(round(closure_kt(brg, own.get("track"), own.get("speed_ms"),
                                                    lead["track"], lead["speed_ms"])))
        else:
            g["moving"] = (lead["speed_ms"] or 0) > 2
            g["vertical"] = "ground target"
        # Ready-made radio call in the user's required order (round 2): the model reads it verbatim.
        if side == "hostile":
            label = g["label"]
        elif side == "friendly":
            label = "friendly " + (spoken_type(types.most_common(1)[0][0]) if len(types) == 1 else cat)
        else:
            label = "neutral " + cat
        if side == "hostile" and dom == "air":
            label = " and ".join(spoken_type(t) for t in types)  # a single says no count (budget)
            if len(members) > 1:
                label += " %s-ship" % words(len(members))
        elif len(members) > 1:
            label += ", %s contacts" % words(len(members))
        g["call"] = call_line(label, g.get("bearing_mag") or g.get("bearing_true"), rng, clock_of(rel), dom,
                              g.get("alt_ft"), g.get("vertical"))
        g["overhead_allowed"] = dom == "air" and rng <= 3.0
        names = sorted({clean_name(m[2]["group"]) for m in members if m[2]["group"]})
        if names:
            g["groups"] = names[:3]
        players = sorted({m[2]["player"] for m in members if m[2].get("player")})
        if players:
            g["players"] = players
        return g

    def _combat_events_missing(self, now):
        """(missing?, my loadout drops without a shot event in the window): R2 F3. The 1 Hz heartbeat proves the
        pipe is up, not that DCS-gRPC still forwards shot/hit/kill events."""
        drops = event_shots = 0
        for s in self.shots:
            if now - s["t"] > COMBAT_MISSING_WINDOW_S:
                continue
            if s.get("event_seen"):
                event_shots += 1
            elif s.get("source") == "stores_diff":
                drops += 1
        return drops >= COMBAT_MISSING_SHOTS and event_shots == 0, drops

    def stream_health(self, now=None):
        """Event-stream liveness for the picture, the transcript tool log and the feeder watchdog."""
        with self.lock:
            now = self.clock() if now is None else now
            units_fresh = self.units_at is not None and now - self.units_at < 60
            ev_age = None if self.events_rx_at is None else round(now - self.events_rx_at, 1)
            heartbeat = self.heartbeat_at is not None and self.events_rx_at is not None and \
                self.events_rx_at - self.heartbeat_at < 10  # the last message seen was (near) a heartbeat
            missing, drops = self._combat_events_missing(now)
            if ev_age is None:
                state = "never received" if units_fresh else "waiting"
            elif ev_age > EVENTS_STALE_S and units_fresh:
                state = "stale (units update, events silent; resubscribing)"
            elif heartbeat and ev_age >= HEARTBEAT_STALE_S and units_fresh:
                state = "stale (heartbeat stopped; resubscribing)"
            elif missing:
                state = "combat events missing (kills may be unconfirmed)"
            else:
                state = "live"
            recent = sum(1 for t in self.event_rx if now - t <= 300)
            out = {"state": state, "last_event_age_s": ev_age, "events_per_min": round(recent / 5.0, 1),
                   "events_total": self.events_total, "units_age_s": None if self.units_at is None
                   else round(now - self.units_at, 1),
                   "last_mission_event_age_s": None if self.mission_events_at is None
                   else round(now - self.mission_events_at, 1)}
            if drops:
                out["shots_without_event_10_min"] = drops
            if self.resubscribes:
                out["resubscribes"] = dict(self.resubscribes)
            if self.raw_kinds:
                out["kinds_total"] = dict(self.raw_kinds.most_common(12))
            return out

    def _data_health(self, now, events):
        out = {"connected": self.connected, "streams": self.streams_json(now), "events": events["state"],
               "combat_events": "missing: %d of my shots seen only as loadout drops, no shot event" %
               events.get("shots_without_event_10_min", 0) if events["state"].startswith("combat events missing")
               else "ok" if events["state"] == "live" else "unknown (event stream %s)" % events["state"]}
        if self.last_sweep is not None:
            out["unit_sync"] = {"category": self.last_sweep["category"], "removed": self.last_sweep["removed"],
                                "age_s": round(now - self.last_sweep["t"], 1)}
        if self.unit_sync:
            out["unit_sync_pending"] = sorted(self.unit_sync)
        if events["state"] != "live":
            out["note"] = "shot/hit/kill reports may be missing: kills may be unconfirmed; say so if asked."
        return out

    def mark_announced(self, keys):
        """The controller gave these splash calls to the model; don't ask for them again."""
        with self.lock:
            self.announced.update(keys)

    def unmark_announced(self, keys):
        """The reply carrying these calls was never transmitted: make them pending again."""
        with self.lock:
            self.announced.difference_update(keys)

    def _loadout_json(self, own, now):
        snap = self.stores.get(own["id"]) if own else None
        if snap is None:
            return {"available": False, "reason": self.stores_status}
        weapons, gun = [], 0
        for t, it in sorted(snap["items"].items(), key=lambda kv: (kv[1]["kind"] == "gun", kv[1]["name"])):
            if it["kind"] == "gun":
                gun += it["count"]
                continue
            weapons.append({"weapon": weapon_name(it["name"]), "type": weapon_name(t), "raw_weapon": it["name"],
                            "raw_type": t, "spoken_name": weapon_name(it["name"]),
                            "kind": it["kind"], "count": it["count"]})
        out = {"available": True, "age_s": round(now - snap["t"], 1), "weapons": weapons, "gun_rounds": gun}
        out["source"] = {"cockpit": "cockpit (Export.lua payload)", "eval": "DCS-gRPC Eval getAmmo"}.get(
            snap.get("source", "eval"), snap.get("source"))
        if not weapons:
            out["note"] = "no bombs, missiles or rockets left" if gun else "no stores reported (winchester)"
        return out

    def _cockpit_json(self, own, now):
        """The cockpit section: Export feed state + the locked target placed on the picture (BRAA, clock, unit)."""
        cp = self.cockpit.to_picture(now)
        if not cp.get("available") or not cp.get("fresh"):
            return cp
        locks = self.cockpit.locked_targets()
        if not locks:
            cp["locked_target"] = None
            cp["locked_note"] = "nothing locked"
            return cp
        t = locks[0]
        row = {"type": t.get("type") or "unknown"}
        if isinstance(t.get("dist"), (int, float)):
            row["range_nm"] = round(t["dist"] / NM, 1)
        if isinstance(t.get("cv"), (int, float)):
            row["closure_kt"] = int(round(t["cv"] * MS_TO_KT))
        if isinstance(t.get("mach"), (int, float)):
            row["mach"] = t["mach"]
        u = self._match_unit(t.get("lat"), t.get("lon"), exclude=own["id"] if own else None)
        if u is not None:
            row["unit"] = _brief(u)
            row["side"] = ("friendly" if own and u["coalition"] == own["coalition"] else
                           "neutral" if u["coalition"] in ("neutral", "unknown") else "hostile")
        res = self._where(own, t.get("lat"), t.get("lon"), t.get("alt"))
        if res is not None:
            where, brg, rng = res
            rng = row.get("range_nm", rng)  # the radar's own range wins over the geometric one
            row.update({k: v for k, v in where.items() if k != "range_nm" or "range_nm" not in row})
            dom = domain(u["category"]) if u is not None else (
                "air" if (t.get("mach") or 0) > 0.15 else "ground")
            alt_ft = where.get("alt_ft")
            vertical = None
            if dom == "air" and alt_ft is not None:
                own_ft = (own.get("alt_m") or 0) * M_TO_FT
                vertical = "high" if alt_ft - own_ft > 1000 else "low" if own_ft - alt_ft > 1000 else "level"
            label = "locked " + spoken_type(u["type"] if u is not None else row["type"])
            row["call"] = call_line(label, where.get("bearing_mag") or where.get("bearing_true"), rng,
                                    clock_of(brg - (own.get("heading") or 0.0)), dom, alt_ft, vertical)
        cp["locked_target"] = row
        if len(locks) > 1:
            cp["locked_count"] = len(locks)
        return cp

    def picture(self):
        with self.lock:
            self._own_cache = None
            own = self._own_locked()
            self._own_cache = (own,)
            try:
                pic = self._picture_locked(own)
                if self.own_loss:
                    pic["own_loss"] = dict(self.own_loss)
                    if self.own_loss["key"] not in self.announced:
                        pic["own_loss_to_announce"] = self.own_loss["call"]
                        pic["_own_loss_key"] = self.own_loss["key"]
                if self.memory is not None:
                    pic["_memory_generation"] = self.memory.generation if self.memory.identity is not None else -1
                return pic
            finally:
                self._own_cache = None

    def _fuel_json(self, own, cp, now):
        """One fuel answer: cockpit (kg or fraction x airframe max) > DCS-gRPC Unit:getFuel() x max > unknown."""
        if cp.get("available") and cp.get("fresh") and isinstance(cp.get("fuel"), dict) \
                and cp["fuel"].get("total_lb"):
            return dict(cp["fuel"], source=cp["fuel"].get("source") or "cockpit (kg)")
        if own is not None and self.fuel is not None and self.fuel > 0:
            out = fuel_lb_from_fraction(self.fuel, own.get("type"))
            if out is not None:
                out["age_s"] = None if self.fuel_at is None else round(now - self.fuel_at, 1)
                return out
        return {"known": False, "reason": "no cockpit or DCS fuel reading",
                "note": "fuel unknown (no usable cockpit or DCS-gRPC fuel value): say fuel unknown"}

    def _working_target(self, own, cp, now):
        """The ONE target being worked: cockpit lock, else the target of my last attack (still alive)."""
        lock = cp.get("locked_target") if isinstance(cp, dict) else None
        if isinstance(lock, dict) and lock.get("call"):
            return {"call": lock["call"], "type": lock.get("type"), "source": "cockpit lock"}
        if own is None or own.get("lat") is None:
            return None
        for s in reversed(self.shots):
            if now - s["t"] > CREDIT_WINDOW_S:
                break
            t = s["hit_target"] or s["intended_target"] or {}
            u = self.units.get(t.get("id")) if t.get("id") else None
            if u is None or u.get("lat") is None:
                continue
            res = self._where(own, u["lat"], u["lon"], u.get("alt_m"))
            if res is None:
                continue
            where, brg, rng = res
            dom = domain(u["category"])
            vertical = None
            if dom == "air":
                diff = ((u.get("alt_m") or 0) - (own.get("alt_m") or 0)) * M_TO_FT
                vertical = "high" if diff > 1000 else "low" if diff < -1000 else "level"
            call = call_line(spoken_type(u["type"]), where.get("bearing_mag") or where.get("bearing_true"), rng,
                             clock_of(brg - (own.get("heading") or 0.0)), dom, where.get("alt_ft"), vertical)
            return {"call": call, "type": spoken_type(u["type"]), "source": "target of my last attack"}
        return None

    def _contacts(self, own, pic):
        others = [u for u in self.units.values() if u["id"] != own["id"] and u.get("lat") is not None]
        mine = own["coalition"]
        hostile_raw, friendly_raw = [], []
        for dom, members in self._cluster(own, others):
            coal = members[0][2]["coalition"]
            if coal == mine or coal in ("neutral", "unknown"):
                friendly_raw.append((dom, members))
            else:
                hostile_raw.append((dom, members))
        # Full JSON only for the nearest groups, plus the nearest air and SAM groups wherever they are.
        keep = set(range(min(HOSTILE_GROUPS_MAX, len(hostile_raw))))
        air = [i for i, (dom, _) in enumerate(hostile_raw) if dom == "air"]
        sam = [i for i, (_, m) in enumerate(hostile_raw) if m[0][2]["category"] in ("SAM", "AAA")]
        keep.update(air[:PICTURE_TOP])
        keep.update(sam[:PICTURE_TOP])
        hostile = [self._group_json(own, *hostile_raw[i]) for i in sorted(keep)]
        friendly = [self._group_json(own, dom, m) for dom, m in friendly_raw[:FRIENDLY_GROUPS_MAX]]
        pic["contacts_within_nm"] = CONTACT_RADIUS_NM
        pic["hostile_groups"] = hostile
        pic["friendly_groups"] = friendly
        # Aircraft must not disappear behind the nearest eight friendly ground groups.
        pic["air_traffic"] = {}
        for dom, members in friendly_raw + hostile_raw:
            if dom != "air":
                continue
            rows = pic["air_traffic"].setdefault(members[0][2]["coalition"], [])
            if len(rows) < FRIENDLY_GROUPS_MAX:
                g = self._group_json(own, dom, members)
                g["call"] = bandit_call(g)  # a mixed list: hostiles say "Bandit", friendlies "Friendly"
                rows.append(g)
        pic["hostile_air_groups"] = len(air)
        pic["hostile_groups_total"] = len(hostile_raw)
        if len(hostile_raw) > len(hostile):
            more = Counter()
            for i, (dom, m) in enumerate(hostile_raw):
                if i not in keep:
                    more[m[0][2]["category"]] += 1
            pic["more_hostile_groups_not_listed"] = dict(more)
        if len(friendly_raw) > len(friendly):
            pic["more_friendly_groups_not_listed"] = len(friendly_raw) - len(friendly)
        dist = lambda i: hostile_raw[i][1][0][0]  # noqa: E731 - nearest member of group i, nm
        nearby_air = [i for i in air if dist(i) <= AIR_PICTURE_RADIUS_NM]
        pic["picture_top3"] = [self._group_json(own, *hostile_raw[i])["call"] for i in nearby_air[:PICTURE_TOP]]
        pic["air_picture_radius_nm"] = AIR_PICTURE_RADIUS_NM
        long_air = [i for i in air if dist(i) <= LONG_RANGE_NM]
        pic["picture_long_range_top3"] = [self._group_json(own, *hostile_raw[i])["call"]
                                          for i in long_air[:PICTURE_TOP]]
        pic["long_range_radius_nm"] = LONG_RANGE_NM
        far = [i for i in long_air if dist(i) > AIR_PICTURE_RADIUS_NM]
        pic["air_picture_empty_call"] = (
            "Nothing inside fifty miles. " + self._group_json(own, *hostile_raw[far[0]])["call"] if far
            else "No bandits within one hundred fifty miles.")
        ground = [i for i, (dom, _) in enumerate(hostile_raw) if dom == "ground" and dist(i) <= CLOSE_TIER_NM]
        rows = []
        for i in ground:  # every hostile ground group inside 20 nm, nearest first; the request filters, then caps
            g = self._group_json(own, *hostile_raw[i])
            rows.append({"call": g["call"], "category": g.get("category"),
                         "member_categories": sorted({m[2]["category"] for m in hostile_raw[i][1]}),
                         "_g": g})
        pic["ground_targets_within_20nm"] = _merge_ground_rows(rows)
        pic["ground_empty_call"] = "No ground targets within twenty miles."
        close = []
        for i in air:
            if dist(i) > CLOSE_TIER_NM:
                continue
            key = "close-bandit-%s" % hostile_raw[i][1][0][2].get("id")
            if key not in self.announced:  # once per group lead; geometry is current on every picture
                close.append({"key": key, "call": bandit_call(self._group_json(own, *hostile_raw[i]))})
        pic["_close_bandits"] = close

    @staticmethod
    def _kill_row(k, now):
        return {"target": spoken_type(k["type"]), "target_id": k["target_id"], "target_kind": k["target_kind"],
                "category": "vehicles" if k["category"] == "ground vehicle" else k["category"],
                "weapon": weapon_name(k["weapon"]), "seq": k["seq"], "t": k["t"],
                "mission_time": k["mission_time"], "shooter_id": k["shooter_id"],
                "age_s": int(now - k["t"]), "confidence": k["confidence"], "inferred": k["inferred"],
                "how": k["how"]}

    def _friendly_kills(self, own):
        coalition = (own or {}).get("coalition")
        if coalition not in ("blue", "red") and self.my_kills:
            coalition = self.my_kills[-1]["shooter"].get("coalition")
        friendly = [k for k in self.other_kills if k["friendly"] or
                    (coalition in ("blue", "red") and k["shooter"].get("coalition") == coalition)]
        return sorted(self.my_kills + friendly, key=lambda k: k["seq"])  # pure read: no k["friendly"] write

    def kills_since(self, seq, shooter="me"):
        """Non-consuming cursor query; duplicates/upgrades retain their original sequence number."""
        with self.lock:
            rows = self.my_kills if shooter == "me" else self._friendly_kills(self._own_locked())
            return [self._kill_row(k, self.clock()) for k in rows if k["seq"] > seq and
                    (shooter in ("me", "all_friendly") or k["shooter_id"] == str(shooter))]

    @staticmethod
    def _kill_counts(rows):
        categories: dict[str, int] = dict.fromkeys(("armor", "AAA", "SAM", "infantry", "vehicles", "scenery"), 0)
        categories.update(Counter(k["category"] or "unknown" for k in rows))
        return {"total": len(rows), "confirmed": sum(k["confidence"] == "confirmed" for k in rows),
                "likely": sum(k["confidence"] == "likely" for k in rows), "by_category": categories,
                "by_type": dict(Counter(k["target"] for k in rows))}

    def _mission_kills_json(self, pic, own, now):
        mine = [self._kill_row(k, now) for k in self.my_kills]
        friendly = self._friendly_kills(own)
        rows = [self._kill_row(k, now) for k in friendly]
        pic["my_kills_this_mission"] = mine
        pic["my_kills_recent"] = [k for k in mine if now - k["t"] <= 600]
        pic["friendly_kills_this_mission"] = rows
        pic["friendly_kills_recent"] = [k for k in rows if now - k["t"] <= 600]
        pic["kill_seq"] = max((k["seq"] for k in self.my_kills + self.other_kills), default=0)
        shooters = {}
        # Include flight members with zero kills as well as shooters already lost/despawned.
        for u in self.units.values():
            if own and u.get("coalition") == own.get("coalition") and u.get("group") == own.get("group"):
                shooters[str(u["id"])] = u
        shooters.update({k["shooter_id"]: k["shooter"] for k in friendly})
        flight_members = {k["shooter_id"] for k in friendly if k["flight_member"]}
        by_shooter = {}
        for sid, shooter in shooters.items():
            counts = self._kill_counts([k for k in rows if k["shooter_id"] == sid])
            by_shooter[sid] = dict(counts, shooter=_brief(shooter),
                                  flight_member=sid in flight_members or bool(own and own.get("group") and
                                                                             shooter.get("group") == own["group"]))
        pic["kill_tallies"] = {"me": self._kill_counts(mine), "all_friendly": self._kill_counts(rows),
                               "by_shooter": by_shooter}

    def _picture_locked(self, own):
        now = self.clock()  # pure read (R2 F6): pruning, settling and alerts happen in tick()
        age = lambda t: None if t is None else round(now - t, 1)  # noqa: E731
        events = self.stream_health(now)
        pic = {
            "source": "Athena Eye (DCS-gRPC %s)" % self.source,
            "connected": self.connected,
            "data_age_s": {"units": age(self.units_at), "last_event": age(self.events_at),
                           "airbases": age(self.airbases_at), "ownship": age(own["seen"]) if own else None},
            "event_stream": events,
            "data_health": self._data_health(now, events),
            "theatre": self.theatre,
            "bearings": ("magnetic (declination %.1f deg from DCS)" % self.declination
                         if self.declination is not None else "TRUE (magnetic declination unavailable)"),
            "notes": [
                "Every unit listed in contacts/totals is ALIVE as of data_age_s.",
                "Kills, losses and deaths come ONLY from recent_kills_and_losses / my_shots / my_kills. A kill with "
                "confidence confirmed is a kill (say splash, no 'likely'); only confidence likely is 'likely'.",
                "Mission totals come from kill_tallies / my_kills_this_mission, never recent lists. "
                "For fresh confirmations use my_kills_recent seq after your last confirmation cursor.",
                "clock is relative to Prometheus' nose (12 = ahead).",
                "Each group/airbase 'call' is the ready radio call in the required order; read it as written.",
                "picture_top3 = the three closest hostile air groups within 50 nm, verbatim. No extra-group count.",
                "Traffic means aircraft (including helos), not ground units. Use air_traffic and airbase traffic.",
                "Never say overhead unless the contact has overhead_allowed true (within 3 nm).",
                "cockpit = live data from Prometheus' own aircraft (DCS Export.lua, about 5 per second): "
                "config, flight, payload, warning_lights; rwr/locked_target only when cockpit.rwr.available is true "
                "(no module has exported them so far). Fuel: ownship.fuel.",
            ],
        }
        if self.last_error and not self.connected:
            pic["last_error"] = self.last_error
        cockpit = self._cockpit_json(own, now)
        if own is None:
            pic["ownship"] = None
            pic["ownship_note"] = "Prometheus is not currently in an aircraft (no unit with player name %s)" % self.player
            if self.test_ownship:
                pic["ownship_note"] += ("; TEST stand-in pilot unit '%s' is not in the mission (no such unit, or it "
                                        "died)" % self.test_ownship)
            # No pilot position: nothing range/bearing-based can be answered, and an empty list is NOT an all-clear.
            pic["no_position_call"] = NO_POSITION
            pic["notes"].append("ownship is null: Athena has NO position for Prometheus. Anything needing range or "
                                "bearing (picture, ground targets, SAMs, threats, traffic) = say you have no position; "
                                "never 'no ... within N miles' or 'no SAMs in range'. Coalition-wide facts "
                                "(alive_totals_by_coalition, kills) are still valid.")
        else:
            pic["ownship"] = {
                "type": own["type"], "coalition": own["coalition"], "group": clean_name(own["group"]),
                "player_name": clean_name(own.get("player")) or None,
                "alt_ft": int(round((own["alt_m"] or 0) * M_TO_FT)),
                "speed_kt": int(round((own["speed_ms"] or 0) * MS_TO_KT)),
                "heading_true": _three(own["heading"] or 0),
                "fuel": self._fuel_json(own, cockpit, now),
                "fuel_fraction": None if self.fuel is None else round(self.fuel, 2),
                "loadout": self._loadout_json(own, now),
                "damage": self._damage_json(own, now, cockpit),
                "lat": round(own["lat"], 4) if own["lat"] is not None else None,
                "lon": round(own["lon"], 4) if own["lon"] is not None else None,
            }
            if self.declination is not None:
                pic["ownship"]["heading_mag"] = _three((own["heading"] or 0) - self.declination)
            if own.get("source") == "cockpit":
                pic["ownship"]["source"] = "cockpit feed only (DCS-gRPC has no unit for Prometheus)"
            stand_in = self._stand_in(own)
            if stand_in:  # never mistaken for a real pilot: said on the ownship, at top level and in the notes
                pic["ownship"]["source"] = pic["test_ownship"] = stand_in
                pic["notes"].append(stand_in + ". Positions, fuel, loadout, shots and kills are that unit's.")
            if own.get("lat") is not None:
                self._contacts(own, pic)
            pic["working_target"] = self._working_target(own, cockpit, now)
        pic["airbases"] = self._airbases_json(own)
        pic["_airfields"] = pic["airbases"].pop("_all", [])
        pic["cockpit"] = cockpit
        pic["threats"], sams, flight = self._threats_json(own, now)
        alerts = self.cockpit.pending_alerts(self.announced, now) + self._pending_threat_alerts(now, sams, flight)
        if alerts:
            pic["rwr_alert_to_announce"], pic["_rwr_keys"] = group_alerts(alerts)
        pic["my_shots_last_15_min"] = self._shots_json(own, now)
        pic["last_attack_age_s"] = (round(now - max(self._last_activity(s, now) for s in self.shots), 1)
                                    if self.shots else None)
        self._mission_kills_json(pic, own, now)
        pending = [k for k in self.my_kills if k["key"] not in self.announced and now - k["t"] < 600]
        if pending:
            pic["splash_to_announce"] = [k["splash_call"] for k in pending]
            pic["_splash_keys"] = [k["key"] for k in pending]
        used = self.used_session if self.used_session else self.fired_events
        named_used = Counter()
        for name, count in used.items():
            named_used[weapon_name(name)] += count
        pic["my_weapons_used_this_mission"] = {
            "source": "stores (loadout diff)" if self.used_session else "shot events",
            "by_weapon": dict(named_used)}
        pic["recent_kills_and_losses_last_10_min"] = self._kills_json(own, now)
        pic["recent_events_last_10_min"] = self._events_text(own, now)
        totals = {}
        for u in self.units.values():
            totals.setdefault(u["coalition"], Counter())[u["category"]] += 1
        pic["alive_totals_by_coalition"] = {k: dict(v) for k, v in sorted(totals.items())}
        return pic

    def _airbases_json(self, own):
        rows = []
        for ab in self.airbases.values():
            if own is None or own.get("lat") is None or ab["lat"] is None:
                rows.append((0, {"name": ab["name"], "owner": ab["coalition"], "category": ab["category"]}))
                continue
            res = self._where(own, ab["lat"], ab["lon"])
            if res is None:
                continue
            where, _, rng = res
            rows.append((rng, dict({"name": ab["name"], "owner": ab["coalition"], "category": ab["category"],
                                   "_field": ab}, **where)))
        rows.sort(key=lambda r: r[0])
        mine = []
        if own is not None and own.get("lat") is not None:
            # "Friendly" = the pilot's own coalition. Real airdromes first, then FARPs/helipads, then ships; the
            # category says which. Neutral and enemy fields are never in this list.
            tier = {"FARP/helipad": 1, "ship": 2}
            mine = sorted(((tier.get(r["category"], 0), rng, r) for rng, r in rows
                           if r.get("owner") == own.get("coalition") and r.get("range_nm") is not None),
                          key=lambda m: m[:2])[:3]
        for rng, row in rows[:5] + [(m[1], m[2]) for m in mine if m[1:] not in rows[:5]]:
            ab = row.get("_field")
            if ab is not None:  # nearest five and nearest friendly three: ATC answers read this
                row["traffic"] = self._field_traffic(own, ab)
        for rng, row in rows:  # every field has its ready call: a named field is resolved at reply time (11-11)
            if row.get("clock") and (row.get("bearing_mag") or row.get("bearing_true")):
                hour = int(row["clock"].split()[0])
                row["call"] = "%s, bearing %s, %s, at your %s o'clock." % (
                    row["name"], digit_words(row.get("bearing_mag") or row.get("bearing_true")), range_words(rng),
                    words(hour))
        for _, row in rows:
            row.pop("_field", None)
        out = {"count": len(rows), "nearest": [r[1] for r in rows[:5]]}  # any owner: every row says its owner
        if own is not None and own.get("lat") is not None:
            out["nearest_friendly"] = [{k: r[k] for k in ("name", "owner", "category", "range_nm", "bearing_mag",
                                                           "bearing_true", "clock", "call") if k in r}
                                       for _, _, r in mine]
            if not mine:
                out["nearest_friendly_note"] = "no friendly airfield known; do not offer one"
            out["_all"] = [r for _, r in rows]  # moved to pic["_airfields"]; the model never sees all fields
        if own is not None and rows and rows[0][1].get("range_nm") is not None and rows[0][0] <= 5:
            on_ground = (own["speed_ms"] or 0) * MS_TO_KT < 50
            out["at_or_near"] = dict(rows[0][1], on_ground=on_ground)
        return out

    def _field_traffic(self, own, field):
        """Aircraft within 2 nm on the field, plus arrivals/departures within 5 nm/5,000 ft of field elevation."""
        traffic = []
        for u in self.units.values():
            if u["id"] == own["id"] or domain(u["category"]) != "air" or u.get("lat") is None:
                continue
            _, distance = bearing_range(field["lat"], field["lon"], u["lat"], u["lon"])
            height = ((u.get("alt_m") or 0) - (field.get("alt_m") or 0)) * M_TO_FT
            on_field = distance <= 2 and height <= 100
            if not on_field and not (distance <= 5 and 100 < height <= 5000):
                continue
            brg, rng = bearing_range(own["lat"], own["lon"], u["lat"], u["lon"])
            row = self._group_json(own, "air", [(rng, brg, u)])
            row.update(field_range_nm=round(distance, 1), on_field=on_field,
                       phase="on/near runway or taxiway" if on_field else "arriving/departing or circuit traffic")
            traffic.append(row)
        traffic.sort(key=lambda r: r["field_range_nm"])
        return {"count": len(traffic), "aircraft": traffic[:12]}

    def _shots_json(self, own, now):
        out = []
        for s in self.shots:
            if now - s["t"] > 15 * 60:
                continue
            row = {"weapon": weapon_name(s["weapon"]), "kind": s["kind"], "age_s": int(now - s["t"]),
                   "result": "miss" if s["result"] == "pending" and self._settled_miss(s, now) else s["result"]}
            if s.get("weapon_source") == "stores_diff":
                row["weapon_from"] = "loadout change"
            if s.get("source") == "stores_diff" and not s.get("event_seen"):
                row["seen_by"] = "loadout change only (no shot event)"
            if s.get("stores_count"):
                row["count"] = s["stores_count"]
            if s.get("vanished_targets"):
                row["no_longer_showing"] = list(s["vanished_targets"].values())
            if s["intended_target"]:
                row["intended_target"] = dict(s["intended_target"], **(_brief(s["intended_target"]) or {}))
            if s["hit_target"]:
                row["hit"] = dict(s["hit_target"], **(_brief(s["hit_target"]) or {}))
            if s["kind"] == "gun":
                row["hits"] = s["hits"]
            out.append(row)
        counts = Counter(r["result"] for r in out)
        return {"total": len(out), "by_result": dict(counts), "shots": out[-15:]}

    def _kills_json(self, own, now):
        recent = [ev for ev in self.events if now - ev["t"] <= 600 and
                  ev["kind"] in ("kill", "dead", "unit_lost", "crash", "pilot_dead", "ejection")]
        # DCS reports one death several ways (kill, dead, unit_lost); keep the kill (it names the killer).
        killed = {(ev["target"] or {}).get("id") for ev in recent if ev["kind"] == "kill"} - {None, 0}
        out, seen = [], set()
        for ev in reversed(recent):
            victim = ev["target"] if ev["kind"] == "kill" else ev["initiator"]
            killer = ev["initiator"] if ev["kind"] == "kill" else None
            vid = (victim or {}).get("id")
            death = ev["kind"] in ("kill", "dead", "unit_lost", "crash")
            if death and ev["kind"] != "kill" and vid in killed:
                continue
            key = (vid, death)
            if vid and key in seen:
                continue
            if vid:
                seen.add(key)
            what = {"kill": "killed", "dead": "destroyed", "unit_lost": "lost", "crash": "crashed",
                    "pilot_dead": "pilot dead", "ejection": "ejected"}[ev["kind"]]
            row = {"age_s": int(now - ev["t"]), "event": what, "victim": _brief(victim)}
            if killer:
                row["killer"] = _brief(killer)
                row["weapon"] = weapon_name(ev.get("weapon"))
                row["by_me"] = self._is_me(killer)
            if victim and own is not None:
                last = self.last_known.get(vid) or victim
                res = self._where(own, last.get("lat"), last.get("lon"))
                if res:
                    row["where"] = res[0]
            if victim and own is not None:
                row["friendly_loss"] = victim.get("coalition") == own["coalition"]
            out.append(row)
            if len(out) >= 20:
                break
        # Units that left the unit stream without any death event (e.g. event stream down): likely destroyed.
        dead_ids = set()
        for ev in self.events:
            if ev["kind"] in DEATH_KINDS:
                dead_ids.add(((ev["target"] if ev["kind"] == "kill" else ev["initiator"]) or {}).get("id"))
        for v in reversed(self.vanished):
            if now - v["t"] > 600 or len(out) >= 25:
                break
            u = v["unit"]
            if u["id"] in dead_ids or u.get("player") or self._land_or_leave(u["id"], v["t"]):
                continue
            row = {"age_s": int(now - v["t"]), "event": "no longer showing, likely destroyed", "inferred": True,
                   "victim": _brief(u)}
            if v.get("reason") == "reconnect_sweep":
                row["event"] = "no longer showing (missed during a data outage)"
            mine = next((k for k in self.my_kills if k["target_id"] == u["id"]), None)
            if mine:
                row["by_me"], row["weapon"] = True, weapon_name(mine["weapon"])
                if mine.get("confidence", "confirmed") == "confirmed":
                    row["event"] = "destroyed by me (stopped showing right after my attack; no death report)"
                    row["confidence"] = "confirmed"
            if own is not None:
                res = self._where(own, u.get("lat"), u.get("lon"))
                if res:
                    row["where"] = res[0]
                row["friendly_loss"] = u.get("coalition") == own["coalition"]
            out.append(row)
        return out

    def _land_or_leave(self, uid, t):
        for ev in self.events:
            ini = ev["initiator"] or {}
            if ini.get("id") == uid and ev["kind"] in ("land", "runway_touch", "player_leave_unit", "engine_shutdown") \
                    and abs(ev["t"] - t) < 900:
                return True
        return False

    def _events_text(self, own, now):
        lines = []
        for ev in reversed(self.events):
            if now - ev["t"] > 600 or len(lines) >= 15:
                break
            k = ev["kind"]
            ini = ev["initiator"]
            ago = "%ds ago" % int(now - ev["t"])
            if k in ("takeoff", "runway_takeoff"):
                text = "%s took off from %s" % (_label(ini), ev.get("place") or "?")
            elif k in ("land", "runway_touch"):
                text = "%s landed at %s" % (_label(ini), ev.get("place") or "?")
            elif k == "base_capture":
                text = "%s captured by %s" % (ev.get("place") or "base", _label(ini))
            elif k == "hit":
                text = "%s hit %s with %s" % (_label(ini), _label(ev["target"]), weapon_name(ev.get("weapon")))
            elif k == "own_damage":
                text = ev["call"]
            elif k == "shot" and ini and (self._is_me(ini) or domain(ini.get("category", "")) == "air"):
                text = "%s fired %s" % (_label(ini), weapon_name(ev.get("weapon")))
            elif k == "birth" and ini and ini.get("player"):
                text = "%s spawned" % _label(ini)
            elif k in ("mission_start", "mission_end"):
                text = k.replace("_", " ")
            elif k in ("player_enter_unit", "player_leave_unit") and ini:
                text = "%s %s" % (_label(ini), "entered aircraft" if k.endswith("enter_unit") else "left aircraft")
            elif k == "landing_quality_mark":
                text = "LSO grade: %s" % ev.get("comment", "")
            else:
                continue
            if k == "hit" and own is not None and self._is_me(ev["target"]):
                text = "PROMETHEUS WAS HIT by %s (%s)" % (_label(ini), weapon_name(ev.get("weapon")))
            lines.append("%s: %s" % (ago, text))
        return lines

    def summary(self, pic=None):
        pic = pic or self.picture()
        own = pic.get("ownship") or {}
        es = pic.get("event_stream") or {}
        loadout = own.get("loadout") or {}
        cp = pic.get("cockpit") or {}
        rwr = cp.get("rwr") or {}
        cockpit = ({"fresh": cp.get("fresh"), "age_s": cp.get("age_s"), "rwr": rwr.get("count"),
                    "rwr_highest": rwr.get("highest"), "gear": (cp.get("config") or {}).get("gear"),
                    "fuel_lb": (cp.get("fuel") or {}).get("total_lb"),
                    "lock": (cp.get("locked_target") or {}).get("type")}
                   if cp.get("available") else "n/a")
        return {"connected": pic["connected"], "ownship": own.get("type"),
                "hostile_groups": len(pic.get("hostile_groups", [])),
                "friendly_groups": len(pic.get("friendly_groups", [])),
                "airbases": pic["airbases"]["count"], "shots": pic["my_shots_last_15_min"]["total"],
                "kills_losses": len(pic["recent_kills_and_losses_last_10_min"]),
                "my_kills": len(pic.get("my_kills_this_mission") or []),
                "events": es.get("state"), "events_per_min": es.get("events_per_min"),
                "last_event_age_s": es.get("last_event_age_s"),
                "loadout": ({w["weapon"]: w["count"] for w in loadout.get("weapons", [])}
                            if loadout.get("available") else loadout.get("reason")),
                "cockpit": cockpit,
                "fuel_lb": (own.get("fuel") or {}).get("total_lb"),
                "rwr_source": (pic.get("threats") or {}).get("rwr_source"),
                "units_age_s": pic["data_age_s"]["units"]}

    def whisper_vocabulary(self, max_airbases=10):
        """Words whisper should expect right now: callsign, Athena, nearby airfields, brevity (round 2 STT fix;
        round 4: the brevity words that were misheard, e.g. "eyes on target" -> "I, son target")."""
        with self.lock:
            own = self._own_locked()
            names = [n.strip() for n in str(self.player).split(",")
                     if n.strip() and n.strip().casefold() not in ("player", "client")]
            bases = list(self.airbases.values())
            if own is not None and own.get("lat") is not None:
                bases = [b for b in bases if b.get("lat") is not None]
                bases.sort(key=lambda b: bearing_range(own["lat"], own["lon"], b["lat"], b["lon"])[1])
            fields = []
            for b in bases:
                if b["category"] == "airdrome" and b["name"] not in fields:
                    fields.append(b["name"])
                if len(fields) >= max_airbases:
                    break
        parts = ["%s, Athena." % (names[0] if names else "Prometheus")]
        if fields:
            parts.append("Airfields: %s." % ", ".join(fields))
        parts.append(BREVITY_PROMPT)
        return " ".join(parts)


# ----------------------------------------------------------------------------- DCS-gRPC feeder
def _code(error):
    code = getattr(error, "code", None)
    if callable(code):
        try:
            return str(code())
        except Exception:  # noqa: BLE001
            return ""
    return ""


class GrpcFeeder:
    """Live DCS-gRPC -> Eye. Read-only RPCs only. Reconnects forever; survives mission/map changes."""

    def __init__(self, eye, host, port=50051, *, units_poll=1, ground_poll=5, stores_poll=STORES_POLL_S,
                 events_stale_s=EVENTS_STALE_S, watchdog_period=5.0, prompt_path=None, prompt_period=30.0):
        self.eye, self.host, self.port = eye, host, port
        self.units_poll, self.ground_poll = units_poll, ground_poll
        self.stores_poll, self.events_stale_s, self.watchdog_period = stores_poll, events_stale_s, watchdog_period
        self.prompt_path, self.prompt_period = prompt_path, prompt_period
        self.stop = threading.Event()
        self.threads = []
        self.eval_ok = True
        self._eval_retry_at = 0.0
        import grpc  # noqa: F401  (fail early if missing)
        from google.protobuf.json_format import MessageToDict
        self._to_dict = lambda m: MessageToDict(m, preserving_proto_field_name=True)
        self.channel = None
        self._calls_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._metadata_lock = threading.Lock()
        self._metadata_thread = None
        self._unit_calls = set()
        self._events_call = None
        self._events_subscribed = None  # eye clock
        self._errored = set()  # jobs whose last attempt failed (their next live message = a reconnect)
        self._stores_wake = threading.Event()
        self._refresh_world = False
        eye.on_mission_start = self.resubscribe_units
        eye.on_player_shot = self._stores_wake.set

    def _chan(self):
        import grpc
        if self.channel is None:
            self.channel = grpc.insecure_channel("%s:%d" % (self.host, self.port), options=[
                ("grpc.keepalive_time_ms", 20000), ("grpc.keepalive_timeout_ms", 10000),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024)])
        return self.channel

    def start(self):
        jobs = [("units-air", lambda: self._units("units-air", "GROUP_CATEGORY_AIRPLANE", self.units_poll,
                                                  self.units_poll)),
                ("units-helo", lambda: self._units("units-helo", "GROUP_CATEGORY_HELICOPTER", self.units_poll,
                                                   self.units_poll)),
                ("units-ground", lambda: self._units("units-ground", "GROUP_CATEGORY_GROUND", self.ground_poll, 30)),
                ("units-ship", lambda: self._units("units-ship", "GROUP_CATEGORY_SHIP", self.ground_poll, 30)),
                ("events", self._events), ("world", self._world), ("stores", self._stores_loop),
                ("threats", self._threats_loop), ("watchdog", self._watchdog)]
        if self.prompt_path:
            jobs.append(("prompt", self._prompt_loop))
        for name, fn in jobs:
            t = threading.Thread(target=self._forever, args=(name, fn), name="eye-" + name, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def close(self):
        self.stop.set()
        self._stores_wake.set()
        if self.eye.memory is not None:  # memory6: publisher lifetime follows feeder
            self.eye.memory.close()
        if self.channel is not None:
            self.channel.close()

    def _forever(self, name, fn):
        delay, failures = 1.0, 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                fn()
            except Exception as error:  # noqa: BLE001 - reconnect forever, report reason
                msg = "%s: %s %s" % (name, type(error).__name__, _code(error))
                if name not in ("watchdog", "prompt", "stores", "threats"):
                    self.eye.set_connected(False, msg, stream=name)
                self._errored.add(name)
                if failures % 60 == 0:  # first failure, then about every 10 minutes
                    LOG.info("eye %s disconnected (retrying forever): %s", name, msg)
                failures += 1
            if time.monotonic() - started > 30:
                delay, failures = 1.0, 0
            self.stop.wait(delay)
            delay = min(delay * 2, 10.0)

    def _live(self, name, call):
        """First message on a fresh call. After a failure (= reconnect), resubscribe every OTHER stream:
        after the 2026-11-07 DCS client reboot the unit streams recovered but StreamEvents stayed silent."""
        if name in self._errored:
            self._errored.clear()  # one round per outage: the others are about to resubscribe anyway
            LOG.info("eye: %s reconnected; resubscribing the other streams", name)
            self._resubscribe_others(call, "reconnect:" + name)

    def _resubscribe_others(self, keep, reason):
        with self._calls_lock:
            calls = [c for c in self._unit_calls if c is not keep]
            if self._events_call is not None and self._events_call is not keep:
                calls.append(self._events_call)
        self.eye.note_resubscribe(reason)
        for call in calls:
            call.cancel()

    @staticmethod
    def _opened(call):
        """True once the server answered the subscription (response headers) and the call is still open. Blocks
        until headers or the end of the call; a failed call returns False and its error is raised by iteration."""
        try:
            call.initial_metadata()
            return not call.done()
        except Exception:  # noqa: BLE001 - the stream loop reports the real error
            return False

    def _units(self, name, category, poll, backoff):
        import grpc
        from dcs.mission.v0 import mission_pb2, mission_pb2_grpc
        from dcs.common.v0 import common_pb2
        stub = mission_pb2_grpc.MissionServiceStub(self._chan())
        self._check_session(stub)
        req = mission_pb2.StreamUnitsRequest(poll_rate=poll, max_backoff=backoff,
                                             category=common_pb2.GroupCategory.Value(category))
        subscribed = self.eye.clock()
        call = stub.StreamUnits(req)
        with self._calls_lock:
            self._unit_calls.add(call)
        first = True
        try:
            # 11-11: units-helo/units-ship read "down 2306s" all flight: subscribed fine, but a mission with no
            # helicopters or ships never sends a message. The server's response headers prove the subscription.
            if self._opened(call):
                self.eye.set_connected(True, stream=name, idle=True, alive=lambda: not call.done())
            for resp in call:
                if self.stop.is_set():
                    return
                self.eye.set_connected(True, stream=name)
                if first:
                    first = False
                    self._live(name, call)
                    # DCS-gRPC re-sends every live unit on subscribe: what is not re-sent soon is gone (R2 F2).
                    self.eye.begin_unit_sync(category, subscribed, max(10.0, 3.0 * poll), stream=name)
                self.eye.on_units_message(self._to_dict(resp))
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.CANCELLED and not self.stop.is_set():
                with self.eye.lock:  # the resend was cut short: no sweep on it, the next subscription starts one
                    self.eye.unit_sync.pop(category, None)
                return  # resubscribe requested (mission change): reconnect for a full unit snapshot
            raise
        finally:
            with self._calls_lock:
                self._unit_calls.discard(call)

    def _check_session(self, stub):
        """New DCS-gRPC session id = new mission: wipe the eye and resubscribe unit streams."""
        from dcs.mission.v0 import mission_pb2
        generation = self.eye.memory.generation if self.eye.memory is not None else None
        try:
            sid = stub.GetSessionId(mission_pb2.GetSessionIdRequest(), timeout=5).session_id
        except Exception as error:  # noqa: BLE001 - older servers may lack it
            if "UNAVAILABLE" in _code(error):
                raise
            return
        with self._session_lock:
            with self.eye.lock:
                if self.eye.memory is not None and generation != self.eye.memory.generation:
                    return  # a boundary was observed during this session lookup
                old, self.eye.session_id = self.eye.session_id, sid
                if old is not None and sid != old:
                    LOG.info("eye: new DCS session (mission change); resetting picture")
                    self.eye.reset("session_changed")
                    self._refresh_world = True
                    self.resubscribe_units()
                elif old is None and self.eye.memory is not None:
                    self.eye.memory.invalidate()
                generation = self.eye.memory.generation if self.eye.memory is not None else None
        self._resolve_memory_identity(sid, generation)

    def _resolve_memory_identity(self, sid, generation):
        """Optional metadata never runs on a subscription thread or under session lock."""
        memory = self.eye.memory
        if memory is None or self.stop.is_set():
            return
        with self._metadata_lock:
            if generation != memory.generation or memory.identity is not None or (self._metadata_thread and self._metadata_thread.is_alive()):
                return

            def resolve():
                from dcs.hook.v0 import hook_pb2, hook_pb2_grpc
                try:
                    filename = hook_pb2_grpc.HookServiceStub(self._chan()).GetMissionFilename(
                        hook_pb2.GetMissionFilenameRequest(), timeout=2).name
                    with self.eye.lock:
                        if not self.stop.is_set() and self.eye.session_id == sid:
                            memory.set_identity(sid, filename, "%s:%d" % (self.host, self.port), generation=generation)
                except Exception:
                    LOG.info("eye: memory identity unavailable; Eye continues without memory")

            self._metadata_thread = threading.Thread(target=resolve, name="eye-memory-identity", daemon=True)
            self._metadata_thread.start()

    def resubscribe_units(self):
        """Cancel live unit streams; they reconnect at once and DCS-gRPC resends every unit."""
        with self._calls_lock:
            calls = list(self._unit_calls)
        for call in calls:
            call.cancel()

    def resubscribe_events(self, reason):
        with self._calls_lock:
            call = self._events_call
        self.eye.note_resubscribe(reason)
        LOG.info("eye: resubscribing StreamEvents (%s)", reason)
        if call is not None:
            call.cancel()

    def _events(self):
        import grpc
        from dcs.mission.v0 import mission_pb2, mission_pb2_grpc
        stub = mission_pb2_grpc.MissionServiceStub(self._chan())
        self._check_session(stub)
        call = stub.StreamEvents(mission_pb2.StreamEventsRequest())
        with self._calls_lock:
            self._events_call = call
            self._events_subscribed = self.eye.clock()
        first = True
        try:
            for resp in call:
                if self.stop.is_set():
                    return
                self.eye.set_connected(True, stream="events")
                if first:
                    first = False
                    self._live("events", call)
                self.eye.on_event(self._to_dict(resp))
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.CANCELLED and not self.stop.is_set():
                return  # watchdog / reconnect resubscribe
            raise
        finally:
            with self._calls_lock:
                if self._events_call is call:
                    self._events_call = None

    def check_liveness(self):
        """Units update but no event-stream message for events_stale_s -> resubscribe StreamEvents.
        Returns the reason when it resubscribed."""
        now = self.eye.clock()
        with self.eye.lock:
            units_at, events_at = self.eye.units_at, self.eye.events_rx_at
            heartbeat_at = self.eye.heartbeat_at
        with self._calls_lock:
            subscribed = self._events_subscribed
        if units_at is None or now - units_at > 60 or subscribed is None:
            return None
        # The 1 Hz simulation_fps heartbeat makes a stall visible fast: 30 s of silence after it was flowing
        # (recovered or announced within 60 s), else the old 2-minute window.
        stale_s = self.events_stale_s
        if heartbeat_at is not None and events_at is not None and events_at - heartbeat_at < 10 \
                and heartbeat_at >= subscribed:
            stale_s = min(stale_s, HEARTBEAT_STALE_S)
        if now - subscribed < stale_s:
            return None  # give a fresh subscription time to deliver
        if events_at is not None and now - events_at < stale_s:
            return None
        with self._calls_lock:
            self._events_subscribed = now  # one resubscribe per stale window
        self.resubscribe_events("events_stale")
        return "events_stale"

    def _watchdog(self):
        while not self.stop.wait(self.watchdog_period):
            self.eye.tick()
            self.check_liveness()

    # -- loadout / stores (DCS-gRPC CustomService.Eval -> Unit:getAmmo(); needs evalEnabled = true on the DCS PC)
    STORES_LUA = ("local u = Unit.getByName(%s) "
                  "if not u or not u:isExist() then return nil end "
                  "local ammo = {} "
                  "for _, a in ipairs(u:getAmmo() or {}) do local d = a.desc or {} "
                  "ammo[#ammo + 1] = {type = d.typeName, name = d.displayName, count = a.count, "
                  "category = d.category} end "
                  "local ok, life = pcall(function() return u:getLife() end) "
                  "local ok0, life0 = pcall(function() return u:getLife0() end) "
                  "local okm, fmax = pcall(function() return u:getDesc().fuelMassMax end) "
                  "return {fuel = u:getFuel(), ammo = ammo, life = ok and life or nil, life0 = ok0 and life0 or nil, "
                  "fuel_max_kg = okm and fmax or nil}")
    EVAL_OFF = ("unavailable: DCS-gRPC Eval is disabled on the DCS PC. Set evalEnabled = true in "
                "Saved Games/DCS/Config/dcs-grpc.lua (infra/dcs-client kit) and restart the mission")
    # Fuel only (round 4): the cockpit payload replaces getAmmo for ownship, but its fuel can be unusable.
    FUEL_LUA = ("local u = Unit.getByName(%s) "
                "if not u or not u:isExist() then return nil end "
                "local ok, life = pcall(function() return u:getLife() end) "
                "local ok0, life0 = pcall(function() return u:getLife0() end) "
                "local okm, fmax = pcall(function() return u:getDesc().fuelMassMax end) "
                "return {fuel = u:getFuel(), life = ok and life or nil, life0 = ok0 and life0 or nil, "
                "fuel_max_kg = okm and fmax or nil}")
    # Round 4 RWR fallback: missiles in flight within 60 km of ownship (target, launcher, position, velocity).
    WEAPONS_LUA = ("local own = Unit.getByName(%s) "
                   "if not own or not own:isExist() then return nil end "
                   "local out = {} "
                   "local vol = {id = world.VolumeType.SPHERE, params = {point = own:getPoint(), radius = 60000}} "
                   "world.searchObjects(Object.Category.WEAPON, vol, function(w) "
                   "pcall(function() "
                   "local d = w:getDesc() or {} "
                   "if d.category ~= Weapon.Category.MISSILE or #out >= 20 then return end "
                   "if w:getCoalition() == own:getCoalition() then return end "
                   "local p = w:getPoint() local lat, lon, alt = coord.LOtoLL(p) "
                   "local v = w:getVelocity() or {x = 0, y = 0, z = 0} "
                   "local t = w:getTarget() local l = w:getLauncher() "
                   "out[#out + 1] = {id = w.id_, type = w:getTypeName(), lat = lat, lon = lon, alt = alt, "
                   "vn = v.x, ve = v.z, coal = w:getCoalition(), target = t and t.getName and t:getName() or nil, "
                   "launcher = l and l.getName and l:getName() or nil, "
                   "launcher_type = l and l.getTypeName and l:getTypeName() or nil} "
                   "end) return true end) "
                   "return {weapons = out}")
    WEAPONS_POLL_S = 1.0
    # Phase 1 item 3, read-only. Unit-type description once per type (DCS's own attributes + max internal fuel);
    # a missing Unit.getDescByName (older DCS) or a nil desc answers false = "DCS gave nothing" (patterns decide).
    TYPES_LUA = ("local out = {} "
                 "for _, t in ipairs(%s) do "
                 "local ok, d = pcall(function() return Unit.getDescByName(t) end) "
                 "if ok and type(d) == 'table' then "
                 "local a = {} for k, v in pairs(d.attributes or {}) do if v and type(k) == 'string' then "
                 "a[#a + 1] = k end end "
                 "out[t] = {attributes = a, fuel_max_kg = d.fuelMassMax} "
                 "else out[t] = false end end "
                 "return {types = out}")
    # Is a hostile radar on / tracking / detecting Prometheus (names chosen by Eye.radar_candidates, bounded).
    # Every call is pcall-wrapped: a missing API leaves that field out and the Eye falls back to the range table.
    RADARS_LUA = ("local own = Unit.getByName(%s) "
                  "if not own or not own:isExist() then return nil end "
                  "local me = own:getName() local out = {} "
                  "for _, n in ipairs(%s) do pcall(function() "
                  "local u = Unit.getByName(n) "
                  "if not u or not u:isExist() then return end "
                  "local r = {name = n} "
                  "local okh, has = pcall(function() return u:hasSensors(Unit.SensorType.RADAR) end) "
                  "if okh then r.has_radar = has and true or false end "
                  "local okr, on, tgt = pcall(function() return u:getRadar() end) "
                  "if okr then r.on = on and true or false "
                  "if tgt then local okn, tn = pcall(function() return tgt:getName() end) "
                  "r.tracking_me = (okn and tn == me) and true or false end end "
                  "local okd, det = pcall(function() "
                  "return u:getGroup():getController():isTargetDetected(own, Controller.Detection.RADAR) end) "
                  "if okd then r.detected = det and true or false end "
                  "out[#out + 1] = r end) end "
                  "return {radars = out}")
    TYPES_RETRY_S = 30.0

    @staticmethod
    def _lua_list(names):
        return "{" + ",".join(json.dumps(str(n), ensure_ascii=False) for n in names) + "}"

    def _eval_json(self, custom, lua):
        from dcs.custom.v0 import custom_pb2
        raw = custom.Eval(custom_pb2.EvalRequest(lua=lua), timeout=5).json
        return json.loads(raw) if raw else None

    def poll_weapons(self, custom=None):
        """One Eval of missiles in flight around ownship -> eye.on_weapons. Returns the new alerts."""
        own = self.eye.ownship()
        if own is None or not own.get("name") or own.get("source") == "cockpit":
            return []
        if custom is None:
            from dcs.custom.v0 import custom_pb2_grpc
            custom = custom_pb2_grpc.CustomServiceStub(self._chan())
        asked = self.eye.clock()
        value = self._eval_json(custom, self.WEAPONS_LUA % json.dumps(own["name"]))
        if not isinstance(value, dict):
            # nil: DCS could not find the pilot's unit, so nothing was searched. Not an empty scan (that would
            # drop every shot-event row unannounced); the scan goes stale and shot events alert (review P1b).
            return []
        rows = value.get("weapons")
        if isinstance(rows, dict):  # an empty Lua table encodes as {}
            rows = list(rows.values())
        return self.eye.on_weapons(rows or [], asked_at=asked)

    def poll_types(self, custom=None):
        """Describe unit types DCS has not described yet (once per type). Returns how many were cached."""
        names = self.eye.types_to_describe()
        if not names:
            return 0
        if custom is None:
            from dcs.custom.v0 import custom_pb2_grpc
            custom = custom_pb2_grpc.CustomServiceStub(self._chan())
        value = self._eval_json(custom, self.TYPES_LUA % self._lua_list(names))
        rows = value.get("types") if isinstance(value, dict) else None
        if not isinstance(rows, dict):
            return 0  # no answer: ask again later, never cache a failure of the call itself
        return self.eye.note_type_info({t: rows.get(t) for t in names})

    def poll_radars(self, custom=None):
        """One Eval of hostile radar state (on / tracking me / detecting me) -> eye.on_radars."""
        own = self.eye.ownship()
        if own is None or not own.get("name") or own.get("source") == "cockpit":
            return None
        names = self.eye.radar_candidates()
        if custom is None:
            from dcs.custom.v0 import custom_pb2_grpc
            custom = custom_pb2_grpc.CustomServiceStub(self._chan())
        if not names:
            return self.eye.on_radars([])
        value = self._eval_json(custom, self.RADARS_LUA % (json.dumps(own["name"]), self._lua_list(names)))
        rows = value.get("radars") if isinstance(value, dict) else None
        if rows is None:
            return None  # no answer: the radar picture goes stale and the range table takes over
        if isinstance(rows, dict):
            rows = list(rows.values())
        return self.eye.on_radars(rows)

    def _threats_loop(self):
        from dcs.custom.v0 import custom_pb2_grpc
        custom = custom_pb2_grpc.CustomServiceStub(self._chan())
        next_at = {}
        while not self.stop.wait(self.WEAPONS_POLL_S):
            if self.eval_ok:
                for name, poll, retry in (("types", self.poll_types, self.TYPES_RETRY_S),
                                          ("weapons", self.poll_weapons, 5.0), ("radars", self.poll_radars, 5.0)):
                    if time.monotonic() < next_at.get(name, 0.0):
                        continue
                    try:
                        poll(custom)
                    except Exception as error:  # noqa: BLE001 - Eval off or mission loading: retry later
                        if "UNAVAILABLE" in _code(error):
                            raise
                        next_at[name] = time.monotonic() + retry
            self.eye.tick()

    def poll_stores(self, custom=None):
        """One loadout poll for every player unit (ownship first)."""
        if custom is None:
            from dcs.custom.v0 import custom_pb2_grpc
            custom = custom_pb2_grpc.CustomServiceStub(self._chan())
        from dcs.custom.v0 import custom_pb2
        own = self.eye.ownship()
        skip_own = own is not None and self.eye.cockpit_payload_fresh()
        for unit in self.eye.player_units():
            if not unit.get("name"):
                continue
            fuel_only = skip_own and unit["id"] == own["id"]
            lua = (self.FUEL_LUA if fuel_only else self.STORES_LUA) % json.dumps(unit["name"])
            try:
                raw = custom.Eval(custom_pb2.EvalRequest(lua=lua), timeout=5).json
            except Exception as error:  # noqa: BLE001
                code = _code(error)
                if any(c in code for c in ("PERMISSION_DENIED", "UNIMPLEMENTED", "UNAUTHENTICATED")):
                    if self.eval_ok:
                        LOG.info("eye: loadout/fuel unavailable (Eval %s): set evalEnabled = true on the DCS PC",
                                 code)
                    self.eval_ok = False
                    self._eval_retry_at = time.monotonic() + 60
                    self.eye.set_stores_status(self.EVAL_OFF)
                    return False
                if "UNAVAILABLE" in code:
                    raise
                self.eye.set_stores_status("eval error: %s %s" % (type(error).__name__, code))
                continue
            self.eval_ok = True
            value = json.loads(raw) if raw else None
            if isinstance(value, dict):
                self.eye.on_stores(unit, value)
        return True

    def _stores_loop(self):
        from dcs.custom.v0 import custom_pb2_grpc
        custom = custom_pb2_grpc.CustomServiceStub(self._chan())
        while not self.stop.is_set():
            self._stores_wake.wait(self.stores_poll)
            self._stores_wake.clear()
            if self.stop.is_set():
                return
            if not self.eval_ok and time.monotonic() < self._eval_retry_at:
                continue
            self.poll_stores(custom)

    # -- dynamic whisper prompt (gateway --athena-dynamic-prompt-file)
    def write_prompt(self):
        import os
        text = self.eye.whisper_vocabulary()
        path = self.prompt_path
        try:
            with open(path, encoding="utf-8") as fh:
                same = fh.read() == text
            fresh = time.time() - os.stat(path).st_mtime < 300
        except OSError:
            same = fresh = False
        if same and fresh:
            return False
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        tmp = "%s.tmp" % path
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return True

    def _prompt_loop(self):
        while not self.stop.is_set():
            self.write_prompt()
            self.stop.wait(self.prompt_period)

    def _world(self):
        from dcs.common.v0 import common_pb2
        from dcs.custom.v0 import custom_pb2, custom_pb2_grpc
        from dcs.mission.v0 import mission_pb2_grpc
        from dcs.world.v0 import world_pb2, world_pb2_grpc
        chan = self._chan()
        world = world_pb2_grpc.WorldServiceStub(chan)
        mission = mission_pb2_grpc.MissionServiceStub(chan)
        custom = custom_pb2_grpc.CustomServiceStub(chan)
        due = {"airbases": 0.0, "declination": 0.0}
        while not self.stop.is_set():
            now = time.monotonic()
            self._check_session(mission)
            if self._refresh_world:
                self._refresh_world = False
                due = dict.fromkeys(due, 0.0)
            if now >= due["airbases"]:
                resp = world.GetAirbases(world_pb2.GetAirbasesRequest(coalition=common_pb2.COALITION_ALL), timeout=10)
                self.eye.set_airbases([self._to_dict(a) for a in resp.airbases])
                self.eye.set_connected(True, stream="world")
                try:
                    self.eye.theatre = world.GetTheatre(world_pb2.GetTheatreRequest(), timeout=5).theatre
                except Exception:  # noqa: BLE001
                    pass
                due["airbases"] = now + 60
            own = self.eye.ownship()
            if own is not None and own.get("lat") is not None:
                if now >= due["declination"]:
                    try:
                        d = custom.GetMagneticDeclination(custom_pb2.GetMagneticDeclinationRequest(
                            lat=own["lat"], lon=own["lon"], alt=own["alt_m"] or 0), timeout=5).declination
                        self.eye.set_declination(d)
                    except Exception:  # noqa: BLE001
                        pass
                    due["declination"] = now + 60
            # fuel now comes with the loadout poll (stores loop)
            self.stop.wait(2)


def build(host, port, player=PLAYER, *, stores_dir=None, prompt_path=None, test_ownship=None):
    # memory6: one source setting for both independent readers.
    import os
    if "ATHENA_DCS_SOURCE" in os.environ or os.environ.get("ATHENA_MEMORY_ENABLED") == "1":
        try:
            from athena_memory.memory_source import resolve_source
        except ImportError:  # older memory builds ship it under metis
            from metis.state_service.memory_source import resolve_source
        source = resolve_source()
        host, port = source.host, source.port
    eye = Eye(player, source="%s:%d" % (host, port), stores_dir=stores_dir,
              test_ownship=os.environ.get("ATHENA_TEST_OWNSHIP") if test_ownship is None else test_ownship)
    if eye.test_ownship:
        LOG.warning("eye: !!!!!!!! TEST STAND-IN PILOT ACTIVE: unit '%s' is treated as Prometheus when no real "
                    "player is in the mission (ATHENA_TEST_OWNSHIP). NOT FOR REAL FLIGHTS. !!!!!!!!", eye.test_ownship)
    from .memory_link import attach  # memory6: isolated optional integration
    attach(eye)
    return eye, GrpcFeeder(eye, host, port, prompt_path=prompt_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Athena Eye live check: connect, wait, print picture")
    parser.add_argument("--host", default="10.90.10.113")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--cockpit-port", type=int, default=0,
                        help="also listen for the AthenaExport.lua cockpit feed on this UDP port (0 = off; the "
                             "radio adapter normally owns 7778)")
    parser.add_argument("--test-ownship", default=None,
                        help="TEST ONLY: exact DCS unit name used as the pilot when no real player is in the mission "
                             "(default: env ATHENA_TEST_OWNSHIP)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    eye, feeder = build(args.host, args.port, test_ownship=args.test_ownship)
    feeder.start()
    listener = CockpitListener(eye, port=args.cockpit_port).start() if args.cockpit_port else None
    time.sleep(args.seconds)
    pic = eye.picture()
    print(json.dumps(pic, indent=1, default=str))
    print("SUMMARY", json.dumps(eye.summary(pic)))
    feeder.close()
    if listener is not None:
        listener.close()
    ok = pic.get("ownship") and (pic.get("hostile_groups") or pic.get("friendly_groups")) and pic["airbases"]["count"]
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
