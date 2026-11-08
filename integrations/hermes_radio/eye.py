"""Athena Eye: lean in-memory tracker fed by DCS-gRPC, producing one compact picture for the LLM.

User direction 2026-11-07: enrichment over guardrails. Track everything (units, events, shots,
airbases), no freshness gates; every picture carries its data age instead.

Eye is pure state (feed it plain dicts in DCS-gRPC JSON shape, i.e. protobuf MessageToDict with
proto field names). GrpcFeeder converts live DCS-gRPC streams into those dicts, reconnecting forever.
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

from .cockpit import CockpitListener, CockpitState

PLAYER = "Prometheus,Player,Client"
M_TO_FT = 3.28084
MS_TO_KT = 1.943844
NM = 1852.0
EVENT_KEEP_S = 30 * 60
SHOT_KEEP_S = 30 * 60
CONTACT_RADIUS_NM = 80.0
# Event-stream liveness (round 2): DCS-gRPC's StreamEvents went silent after a DCS client reboot while
# the unit streams recovered. Units fresh but no event message for this long -> force a resubscribe.
EVENTS_STALE_S = 120.0
VANISH_KEEP_S = 30 * 60
STORES_POLL_S = 3.0
# DCS Weapon.Category of Unit:getAmmo() entries.
AMMO_KIND = {0: "gun", 1: "missile", 2: "rocket", 3: "bomb", 4: "torpedo"}
DEATH_KINDS = ("kill", "dead", "unit_lost", "crash", "pilot_dead")
LOG = logging.getLogger("athena.eye")

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
SAM_PAT = re.compile(r"(sam|s-300|s_300|s-125|s_125|s-200|s_75|s-75|snr|patriot|hawk|roland|rapier|nasams|"
                     r"buk|kub|tor |tor_|9a331|osa|strela|igla|stinger|avenger|chaparral|tunguska|2s6|pantsir|"
                     r"hq-7|hq7|sa-\d|sa\d|9s18|9a310|5p85|40b6|64h6|54k6|1l13|55g6|ewr|dog ear|p-19|"
                     r"rls_19j6|st-68|fps-117|5n66|9s80|9p31|9a33|ln |sr$| sr|tr$| tr|str$|cwar|pcp|cp$|"
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
    """Return a short human category for a DCS type name (fighter, AWACS, SAM, armor, ...)."""
    t = type_name or ""
    gc = (group_category or "").upper()
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
    if gc.endswith("AIRPLANE"):
        return "fighter"
    if gc.endswith("SHIP"):
        return "carrier" if SHIP_CARRIER.search(t) else "ship"
    if gc.endswith("TRAIN"):
        return "train"
    if AAA_PAT.search(t):
        return "AAA"
    if SAM_PAT.search(t):
        return "SAM"
    if ARTY_PAT.search(t):
        return "artillery"
    if ARMOR_PAT.search(t):
        return "armor"
    if INFANTRY_PAT.search(t):
        return "infantry"
    if gc.endswith("GROUND"):
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
BOMB_PAT = re.compile(r"(gbu|mk_8|mk-8|mk8|fab|kab|cbu|bdu|blu|sab|rbk|betab|m_117|m117|bomb|odab|"
                      r"agm_154|agm-154|jsow|b_8|mk_20|mk-20|rockeye|durandal|beta|sc_|sd_|lgb)", re.I)
ROCKET_PAT = re.compile(r"(hydra|s-5|s_5|s-8|s_8|s-13|s_13|s-24|s_24|s-25|s_25|zuni|sneb|c_8|c_5|c_13|c_24|"
                        r"lau|ffar|rocket|apkws|m151|m156|m257|m274|mk1|wp|hvar|rp-3)", re.I)


def weapon_kind(name, burst=False):
    if burst:
        return "gun"
    n = name or ""
    if ROCKET_PAT.search(n) and not AAM_PAT.search(n):
        return "rocket"
    if BOMB_PAT.search(n):
        return "bomb"
    if AAM_PAT.search(n):
        return "air-to-air missile"
    if re.search(r"(gsh|m61|gau|cannon|gun|_he|_ap|mg_|bullet|shell|20mm|30mm|23mm|vulcan|defa|aden)", n, re.I):
        return "gun"
    return "missile"


def _resolve_window(kind):
    return {"gun": 12.0, "rocket": 30.0, "bomb": 120.0}.get(kind, 120.0)


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
    name = (name or "").strip().casefold()
    return bool(name) and any(name == item.strip().casefold() for item in str(allowed).split(","))


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


def spoken_type(type_name):
    t = re.sub(r"_\d+$", "", type_name or "")
    return t.replace("_", " ").strip() or "contact"


def call_line(label, bearing3, range_nm, clock_hour, dom, alt_ft=None, vertical=None):
    """The user's required order: BRAA first, then the clock position (2026-11-07).

    air:    "<label>, bearing zero three eight, twelve miles, angels fifteen. Target is at your ten o'clock, high."
    ground: "<label>, bearing zero three eight, three miles, ground target. Target is at your eleven o'clock."
    """
    label = (label or "Contact")[:1].upper() + (label or "Contact")[1:]
    head = "%s, bearing %s, %s" % (label, digit_words(bearing3), range_words(range_nm))
    if dom == "air":
        return "%s, %s. Target is at your %s o'clock, %s." % (head, altitude_words(alt_ft), words(clock_hour),
                                                               vertical or "level")
    what = "surface target" if dom == "sea" else "ground target"
    return "%s, %s. Target is at your %s o'clock." % (head, what, words(clock_hour))


# ----------------------------------------------------------------------------- normalization
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
    type_name = d.get("type", "")
    return {
        "id": int(d.get("id", 0)), "name": d.get("name", ""), "type": type_name, "callsign": d.get("callsign", ""),
        "group": group.get("name", ""), "coalition": _coal(d.get("coalition")), "group_category": gcat,
        "category": classify(type_name, gcat), "player": d.get("player_name") or None,
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
            type_name = d.get("type", "") or ("airbase" if kind == "airbase" else "")
            return {"kind": kind, "id": int(d.get("id", 0) or 0), "name": name, "type": type_name,
                    "coalition": _coal(d.get("coalition")), "category": kind if kind != "static" else classify(type_name),
                    "player": None, "lat": lat, "lon": lon, "alt_m": alt}
    return None


def _brief(obj):
    if not obj:
        return None
    out = {"type": obj.get("type"), "name": obj.get("name"), "coalition": obj.get("coalition"),
           "category": obj.get("category")}
    if obj.get("player"):
        out["player"] = obj["player"]
    return out


def _label(obj):
    if not obj:
        return "unknown"
    who = obj.get("player") or obj.get("type") or obj.get("name") or obj.get("kind") or "unknown"
    if obj.get("player") and obj.get("type"):
        who = "%s (%s)" % (obj["player"], obj["type"])
    coal = obj.get("coalition")
    return "%s %s" % (coal, who) if coal and coal != "unknown" else who


def _norm_weapon_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# ----------------------------------------------------------------------------- the eye
class Eye:
    def __init__(self, player=PLAYER, *, clock=time.time, source="unconfigured", stores_dir=None):
        self.player, self.clock, self.source = player, clock, source
        self.lock = threading.RLock()
        self._seq = itertools.count(1)
        self.on_mission_start = None
        self.on_player_shot = None  # feeder hook: refresh stores right after a shot
        self.stores_dir = stores_dir  # durable stores/shots log (stores-YYYYMMDD.jsonl); None = off
        self.reset("init")
        self.connected = False
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
        self.resubscribes = Counter()
        self.last_resubscribe = None
        # Round 3: cockpit feed (AthenaExport.lua -> UDP 7778): flight data, payload, RWR, locked target.
        self.cockpit = CockpitState(clock=clock)

    # -- lifecycle
    def reset(self, reason):
        with self.lock:
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
            self.announced = set()
            self.units_at = None
            self.events_at = None
            self.reset_reason, self.reset_at = reason, self.clock()

    def set_connected(self, ok, error=None):
        with self.lock:
            self.connected = ok
            if error:
                self.last_error = error

    def note_resubscribe(self, reason):
        with self.lock:
            self.resubscribes[reason] += 1
            self.last_resubscribe = {"reason": reason, "t": self.clock()}

    # -- durable stores/shot log
    def _log_store(self, record):
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
        """The pilot's unit: the unit named in the cockpit feed, else player name in the allowed list, else
        the ONLY player unit (single player), else a pseudo unit built from the cockpit feed (gRPC down)."""
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
        return cockpit.ownship() if cockpit is not None else None

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
            # Raw stream health: every message counts (also noise kinds), it proves the stream is alive.
            self.events_rx_at = now
            self.events_total += 1
            self.event_rx.append(now)
            while self.event_rx and now - self.event_rx[0] > 300:
                self.event_rx.popleft()
        if kind is None:
            return None
        body = msg.get(kind) or {}
        ev = {"seq": next(self._seq), "t": now, "mission_time": msg.get("time"), "kind": kind,
              "initiator": norm_object(body.get("initiator")), "target": norm_object(body.get("target")),
              "weapon": None, "weapon_id": None, "place": None}
        weapon = body.get("weapon")
        if isinstance(weapon, dict):
            ev["weapon"] = weapon.get("type") or None
            ev["weapon_id"] = int(weapon.get("id", 0) or 0) or None
        if body.get("weapon_name"):
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
            self.events_at = now
            if kind == "mission_start":
                # New mission: drop old events/shots. Units are NOT wiped here (the first unit snapshot
                # may already have arrived); the feeder resubscribes the unit streams for a full resend.
                self.events.clear()
                self.shots.clear()
                self.vanished.clear()
                self.stores.clear()
                self.consumption.clear()
                self.used_session.clear()
                self.fired_events.clear()
                self.my_kills = []
                self.announced = set()
                if self.on_mission_start is not None:
                    self.on_mission_start()
            self.events.append(ev)
            self._prune(now)
            wake = self._apply(ev)
        if wake and self.on_player_shot is not None:
            try:
                self.on_player_shot()
            except Exception:  # noqa: BLE001 - a hook must never break the event stream
                pass
        return ev

    def _is_me(self, obj):
        """Pilot match by allowed-name list, or by the current ownship unit id (single player: 'player')."""
        if not obj:
            return False
        if _is_pilot(obj.get("player"), self.player):
            return True
        own = self._own_locked()
        return own is not None and obj.get("kind", "unit") == "unit" and bool(obj.get("id")) \
            and obj.get("id") == own["id"]

    def _apply(self, ev):
        """Apply one event; returns True when a player fired (stores refresh wanted)."""
        kind, ini, tgt = ev["kind"], ev["initiator"], ev["target"]
        if kind == "birth" and ini and ini.get("kind") == "unit":
            u = dict(ini, seen=ev["t"])
            u.pop("kind", None)
            self.units[u["id"]] = u
            self.last_known[u["id"]] = u
            self.stores.pop(u["id"], None)  # fresh airframe: next stores poll is a new baseline
        if kind in ("dead", "unit_lost", "crash") and ini and ini.get("kind") == "unit":
            self.units.pop(ini["id"], None)
            self._credit_death(ini["id"], ev)
        if kind == "kill" and tgt and tgt.get("kind") == "unit":
            self.units.pop(tgt["id"], None)
        me = self._is_me(ini)
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
            self._record_my_kill(tgt, ev.get("weapon"), "kill event", ev["t"])
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

    def _new_shot(self, ev, weapon, weapon_id, burst):
        kind = weapon_kind(weapon, burst=burst)
        shooter = ev["initiator"]
        guess = self._guess_target(shooter, kind)
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
            for s in open_shots:
                if s["weapon_id"] == wid:
                    return s
        if ev["kind"] == "kill" and tgt.get("id"):
            for s in reversed(open_shots):
                if s["hit_target"] and s["hit_target"].get("id") == tgt["id"]:
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
                               "coalition": tgt.get("coalition"), "category": tgt.get("category"),
                               "player": tgt.get("player")}
        if outcome == "kill" or s["result"] != "kill":
            s["result"] = outcome
        s["resolved_t"] = ev["t"]
        self._log_store({"kind": "shot_result", "weapon": s["weapon"], "weapon_kind": s["kind"], "result": outcome,
                         "target": s["hit_target"], "source": "%s event" % outcome})
        return s

    def _credit_death(self, unit_id, ev):
        for s in self.shots:
            if s["result"] == "hit" and s["hit_target"] and s["hit_target"].get("id") == unit_id \
                    and ev["t"] - (s["resolved_t"] or s["t"]) < 300:
                s["result"] = "kill"
                s["resolved_t"] = ev["t"]
                self._record_my_kill(s["hit_target"], s["weapon"], "hit then dead event", ev["t"])

    def _record_my_kill(self, target, weapon, how, t, inferred=False):
        if not target:
            return
        tid = target.get("id")
        for old in self.my_kills:
            if tid and old["target_id"] == tid:
                if old["inferred"] and not inferred:  # confirmed later by a real event
                    old.update(inferred=False, how=how, weapon=weapon or old["weapon"],
                               splash_call="Splash one %s" % spoken_type(target.get("type")))
                return
        k = {"key": "%s-%s" % (tid, round(t, 1)), "target_id": tid, "t": t, "type": target.get("type"),
             "category": target.get("category"), "group": target.get("group"), "weapon": weapon, "how": how,
             "inferred": inferred,
             "splash_call": "Splash one %s" % spoken_type(target.get("type")) + (", likely" if inferred else "")}
        self.my_kills.append(k)
        self._log_store({"kind": "kill_by_me", "target": _brief(target), "weapon": weapon, "how": how,
                         "inferred": inferred})

    def _credit_vanish(self, unit, now):
        """A unit disappeared from the unit stream. If no death event is known and one of my recent shots
        was aimed at it (or hit it), credit a likely kill (inferred)."""
        uid = unit["id"]
        if any(ev["kind"] in DEATH_KINDS and ((ev["target"] or {}).get("id") == uid if ev["kind"] == "kill"
                                               else (ev["initiator"] or {}).get("id") == uid) for ev in self.events):
            return
        if unit.get("player"):
            return
        for s in reversed(self.shots):
            if now - s["t"] > 300:
                break
            if s["result"] == "kill":
                continue
            aimed = (s["hit_target"] or {}).get("id") == uid or (s["intended_target"] or {}).get("id") == uid
            same_group = unit.get("group") and (s["intended_target"] or {}).get("group") == unit.get("group")
            if aimed or (same_group and s["kind"] in ("bomb", "rocket", "gun")):
                if s["result"] != "kill":
                    s["result"] = "likely kill (inferred: target no longer showing)"
                    s["resolved_t"] = now
                s.setdefault("vanished_targets", []).append(spoken_type(unit.get("type")))
                self._record_my_kill(unit, s["weapon"], "target vanished after my shot", now, inferred=True)
                return
        self._log_store({"kind": "unit_vanished", "unit": _brief(unit), "note": "no death event; likely destroyed"})

    # -- stores / loadout (Unit:getAmmo() via DCS-gRPC Eval)
    def set_stores_status(self, status):
        with self.lock:
            self.stores_status = status

    def on_cockpit_packet(self, pkt, src=None):
        """One AthenaExport packet (cockpit.CockpitListener). Payload -> the same stores diff as the Eval loadout
        (source "cockpit"); new RWR locks/launches -> durable log + rwr_alert_to_announce."""
        res = self.cockpit.ingest(pkt, src)
        with self.lock:
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
        with self.lock:
            self.stores_status = "ok"
            uid = unit["id"]
            prev = self.stores.get(uid)
            own = self._own_locked()
            mine = own is not None and own["id"] == uid
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
        s = self._new_shot(ev, name, None, burst=False)
        s["kind"] = use["kind"] if use["kind"] in ("gun", "rocket", "bomb") else weapon_kind(use["type"])
        s["burst_end"] = now
        s.update(source="stores_diff", event_seen=False, weapon_source="stores_diff", stores_weapon=use["weapon"],
                 stores_count=use["count"], stores_last_t=now,
                 intended_target=self._guess_target(dict(own, kind="unit"), s["kind"]))
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
            if s["result"] != "pending":
                continue
            start = s["burst_end"] if s["burst_end"] is not None else s["t"]
            if s["kind"] == "gun" and s["burst_end"] is None and now - s["t"] < 60:
                continue
            if now - start > _resolve_window(s["kind"]):
                s["result"] = "miss"
                s["resolved_t"] = now

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
        rows = []
        for u in units:
            res = self._where(own, u["lat"], u["lon"])
            if res is None:
                continue
            _, brg, rng = res
            if rng <= CONTACT_RADIUS_NM:
                rows.append((rng, brg, u))
        rows.sort(key=lambda r: r[0])
        groups, used = [], set()
        for i, (rng, brg, u) in enumerate(rows):
            if u["id"] in used:
                continue
            used.add(u["id"])
            dom = domain(u["category"])
            members = [(rng, brg, u)]
            radius = 3.0 if dom != "ground" else 1.5
            for rng2, brg2, v in rows[i + 1:]:
                if v["id"] in used or v["coalition"] != u["coalition"] or domain(v["category"]) != dom:
                    continue
                _, d = bearing_range(u["lat"], u["lon"], v["lat"], v["lon"])
                if d > radius:
                    continue
                if dom == "air" and abs((v["alt_m"] or 0) - (u["alt_m"] or 0)) * M_TO_FT > 5000:
                    continue
                used.add(v["id"])
                members.append((rng2, brg2, v))
            groups.append((dom, members))
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
             "types": dict(types), "range_nm": round(rng, 1), "clock": clock_phrase(rel)}
        if self.declination is not None:
            g["bearing_mag"] = _three(brg - self.declination)
        else:
            g["bearing_true"] = _three(brg)
        if side == "hostile":
            g["label"] = ("bandit" if dom == "air" else
                          {"SAM": "SAM site", "AAA": "triple A", "armor": "enemy armor", "ship": "enemy ship",
                           "carrier": "enemy carrier"}.get(cat, "enemy " + cat))
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
        if len(members) > 1:
            label += ", %s contacts" % words(len(members))
        g["call"] = call_line(label, g.get("bearing_mag") or g.get("bearing_true"), rng, clock_of(rel), dom,
                              g.get("alt_ft"), g.get("vertical"))
        names = sorted({m[2]["group"] for m in members if m[2]["group"]})
        if names:
            g["groups"] = names[:3]
        players = sorted({m[2]["player"] for m in members if m[2].get("player")})
        if players:
            g["players"] = players
        return g

    def stream_health(self, now=None):
        """Event-stream liveness for the picture, the transcript tool log and the feeder watchdog."""
        with self.lock:
            now = self.clock() if now is None else now
            units_fresh = self.units_at is not None and now - self.units_at < 60
            ev_age = None if self.events_rx_at is None else round(now - self.events_rx_at, 1)
            if ev_age is None:
                state = "never received" if units_fresh else "waiting"
            elif ev_age > EVENTS_STALE_S and units_fresh:
                state = "stale (units update, events silent; resubscribing)"
            else:
                state = "live"
            recent = sum(1 for t in self.event_rx if now - t <= 300)
            out = {"state": state, "last_event_age_s": ev_age, "events_per_min": round(recent / 5.0, 1),
                   "events_total": self.events_total, "units_age_s": None if self.units_at is None
                   else round(now - self.units_at, 1)}
            if self.resubscribes:
                out["resubscribes"] = dict(self.resubscribes)
            return out

    def mark_announced(self, keys):
        """The controller gave these splash calls to the model; don't ask for them again."""
        with self.lock:
            self.announced.update(keys)

    def _loadout_json(self, own, now):
        snap = self.stores.get(own["id"]) if own else None
        if snap is None:
            return {"available": False, "reason": self.stores_status}
        weapons, gun = [], 0
        for t, it in sorted(snap["items"].items(), key=lambda kv: (kv[1]["kind"] == "gun", kv[1]["name"])):
            if it["kind"] == "gun":
                gun += it["count"]
                continue
            weapons.append({"weapon": it["name"], "type": t, "kind": it["kind"], "count": it["count"]})
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
            now = self.clock()
            self._prune(now)
            self._settle_shots(now)
            own = self._own_locked()
            age = lambda t: None if t is None else round(now - t, 1)  # noqa: E731
            pic = {
                "source": "Athena Eye (DCS-gRPC %s)" % self.source,
                "connected": self.connected,
                "data_age_s": {"units": age(self.units_at), "last_event": age(self.events_at),
                               "airbases": age(self.airbases_at), "ownship": age(own["seen"]) if own else None},
                "event_stream": self.stream_health(now),
                "theatre": self.theatre,
                "bearings": ("magnetic (declination %.1f deg from DCS)" % self.declination
                             if self.declination is not None else "TRUE (magnetic declination unavailable)"),
                "notes": [
                    "Every unit listed in contacts/totals is ALIVE as of data_age_s.",
                    "Kills, losses and deaths come ONLY from recent_kills_and_losses / my_shots / my_kills; "
                    "entries flagged inferred mean the unit stopped showing without a death report (likely destroyed).",
                    "clock is relative to Prometheus' nose (12 = ahead).",
                    "Each group/airbase 'call' is the ready radio call in the required order; read it as written.",
                    "cockpit = live data from Prometheus' own aircraft (DCS Export.lua, about 5 per second): "
                    "config, flight, fuel (pounds), payload, rwr threats, locked_target, warning_lights.",
                ],
            }
            if self.last_error and not self.connected:
                pic["last_error"] = self.last_error
            if own is None:
                pic["ownship"] = None
                pic["ownship_note"] = "Prometheus is not currently in an aircraft (no unit with player name %s)" % self.player
            else:
                pic["ownship"] = {
                    "type": own["type"], "coalition": own["coalition"], "group": own["group"],
                    "player_name": own.get("player"),
                    "alt_ft": int(round((own["alt_m"] or 0) * M_TO_FT)),
                    "speed_kt": int(round((own["speed_ms"] or 0) * MS_TO_KT)),
                    "heading_true": _three(own["heading"] or 0),
                    "fuel_fraction": None if self.fuel is None else round(self.fuel, 2),
                    "loadout": self._loadout_json(own, now),
                    "lat": round(own["lat"], 4) if own["lat"] is not None else None,
                    "lon": round(own["lon"], 4) if own["lon"] is not None else None,
                }
                if self.declination is not None:
                    pic["ownship"]["heading_mag"] = _three((own["heading"] or 0) - self.declination)
                if own.get("source") == "cockpit":
                    pic["ownship"]["source"] = "cockpit feed only (DCS-gRPC has no unit for Prometheus)"
                others = [u for u in self.units.values() if u["id"] != own["id"] and u.get("lat") is not None]
                groups = [self._group_json(own, dom, members) for dom, members in self._cluster(own, others)]
                hostile = [g for g in groups if g["side"] == "hostile"]
                friendly = [g for g in groups if g["side"] != "hostile"]
                pic["contacts_within_nm"] = CONTACT_RADIUS_NM
                pic["hostile_groups"] = hostile[:25]
                pic["friendly_groups"] = friendly[:12]
                pic["hostile_air_groups"] = sum(1 for g in hostile if g["domain"] == "air")
            pic["airbases"] = self._airbases_json(own)
            pic["cockpit"] = self._cockpit_json(own, now)
            alerts = self.cockpit.pending_alerts(self.announced, now)
            if alerts:
                pic["rwr_alert_to_announce"] = [a["call"] for a in alerts]
                pic["_rwr_keys"] = [a["key"] for a in alerts]
            pic["my_shots_last_15_min"] = self._shots_json(own, now)
            pic["my_kills_this_mission"] = [
                {"target": spoken_type(k["type"]), "category": k["category"], "weapon": k["weapon"],
                 "age_s": int(now - k["t"]), "inferred": k["inferred"], "how": k["how"]} for k in self.my_kills[-15:]]
            pending = [k for k in self.my_kills if k["key"] not in self.announced and now - k["t"] < 600]
            if pending:
                pic["splash_to_announce"] = [k["splash_call"] for k in pending]
                pic["_splash_keys"] = [k["key"] for k in pending]
            used = self.used_session if self.used_session else self.fired_events
            pic["my_weapons_used_this_mission"] = {
                "source": "stores (loadout diff)" if self.used_session else "shot events",
                "by_weapon": dict(used)}
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
            rows.append((rng, dict({"name": ab["name"], "owner": ab["coalition"], "category": ab["category"]}, **where)))
        rows.sort(key=lambda r: r[0])
        for rng, row in rows[:5]:
            if row.get("clock") and (row.get("bearing_mag") or row.get("bearing_true")):
                hour = int(row["clock"].split()[0])
                row["call"] = "%s, bearing %s, %s, at your %s o'clock." % (
                    row["name"], digit_words(row.get("bearing_mag") or row.get("bearing_true")), range_words(rng),
                    words(hour))
        out = {"count": len(rows), "nearest": [r[1] for r in rows[:5]]}
        if own is not None and rows and rows[0][1].get("range_nm") is not None and rows[0][0] <= 5:
            on_ground = (own["speed_ms"] or 0) * MS_TO_KT < 50
            out["at_or_near"] = dict(rows[0][1], on_ground=on_ground)
        return out

    def _shots_json(self, own, now):
        out = []
        for s in self.shots:
            if now - s["t"] > 15 * 60:
                continue
            row = {"weapon": s["weapon"], "kind": s["kind"], "age_s": int(now - s["t"]), "result": s["result"]}
            if s.get("weapon_source") == "stores_diff":
                row["weapon_from"] = "loadout change"
            if s.get("source") == "stores_diff" and not s.get("event_seen"):
                row["seen_by"] = "loadout change only (no shot event)"
            if s.get("stores_count"):
                row["count"] = s["stores_count"]
            if s.get("vanished_targets"):
                row["no_longer_showing"] = s["vanished_targets"]
            if s["intended_target"]:
                row["intended_target"] = s["intended_target"]
            if s["hit_target"]:
                row["hit"] = s["hit_target"]
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
                row["weapon"] = ev.get("weapon")
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
            mine = next((k for k in self.my_kills if k["target_id"] == u["id"]), None)
            if mine:
                row["by_me"], row["weapon"] = True, mine["weapon"]
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
                text = "%s hit %s with %s" % (_label(ini), _label(ev["target"]), ev.get("weapon") or "?")
            elif k == "shot" and ini and (self._is_me(ini) or domain(ini.get("category", "")) == "air"):
                text = "%s fired %s" % (_label(ini), ev.get("weapon") or "?")
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
                text = "PROMETHEUS WAS HIT by %s (%s)" % (_label(ini), ev.get("weapon") or "?")
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
                "units_age_s": pic["data_age_s"]["units"]}

    def whisper_vocabulary(self, max_airbases=8):
        """Words whisper should expect right now: callsign, Athena, nearby airfields, brevity (round 2 STT fix)."""
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
        parts.append("Picture, bogey dope, BRAA, splash, weapons hot, rifle, pickle, winchester, bingo, RTB, "
                     "tally, visual, cleared hot, what do I have left, RWR, spike, what's painting me, "
                     "fuel state, gear down.")
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
                ("watchdog", self._watchdog)]
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
                if name not in ("watchdog", "prompt", "stores"):
                    self.eye.set_connected(False, msg)
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
            self._errored.discard(name)
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

    def _units(self, name, category, poll, backoff):
        import grpc
        from dcs.mission.v0 import mission_pb2, mission_pb2_grpc
        from dcs.common.v0 import common_pb2
        stub = mission_pb2_grpc.MissionServiceStub(self._chan())
        self._check_session(stub)
        req = mission_pb2.StreamUnitsRequest(poll_rate=poll, max_backoff=backoff,
                                             category=common_pb2.GroupCategory.Value(category))
        call = stub.StreamUnits(req)
        with self._calls_lock:
            self._unit_calls.add(call)
        first = True
        try:
            for resp in call:
                if self.stop.is_set():
                    return
                self.eye.set_connected(True)
                if first:
                    first = False
                    self._live(name, call)
                self.eye.on_units_message(self._to_dict(resp))
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.CANCELLED and not self.stop.is_set():
                return  # resubscribe requested (mission change): reconnect for a full unit snapshot
            raise
        finally:
            with self._calls_lock:
                self._unit_calls.discard(call)

    def _check_session(self, stub):
        """New DCS-gRPC session id = new mission: wipe the eye and resubscribe unit streams."""
        from dcs.mission.v0 import mission_pb2
        try:
            sid = stub.GetSessionId(mission_pb2.GetSessionIdRequest(), timeout=5).session_id
        except Exception as error:  # noqa: BLE001 - older servers may lack it
            if "UNAVAILABLE" in _code(error):
                raise
            return
        with self._session_lock:
            old, self.eye.session_id = self.eye.session_id, sid
            if old is not None and sid != old:
                LOG.info("eye: new DCS session (mission change); resetting picture")
                self.eye.reset("session_changed")
                self._refresh_world = True
                self.resubscribe_units()

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
                self.eye.set_connected(True)
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
        with self._calls_lock:
            subscribed = self._events_subscribed
        if units_at is None or now - units_at > 60 or subscribed is None:
            return None
        if now - subscribed < self.events_stale_s:
            return None  # give a fresh subscription time to deliver
        if events_at is not None and now - events_at < self.events_stale_s:
            return None
        with self._calls_lock:
            self._events_subscribed = now  # one resubscribe per stale window
        self.resubscribe_events("events_stale")
        return "events_stale"

    def _watchdog(self):
        while not self.stop.wait(self.watchdog_period):
            self.check_liveness()

    # -- loadout / stores (DCS-gRPC CustomService.Eval -> Unit:getAmmo(); needs evalEnabled = true on the DCS PC)
    STORES_LUA = ("local u = Unit.getByName(%s) "
                  "if not u or not u:isExist() then return nil end "
                  "local ammo = {} "
                  "for _, a in ipairs(u:getAmmo() or {}) do local d = a.desc or {} "
                  "ammo[#ammo + 1] = {type = d.typeName, name = d.displayName, count = a.count, "
                  "category = d.category} end "
                  "return {fuel = u:getFuel(), ammo = ammo}")
    EVAL_OFF = ("unavailable: DCS-gRPC Eval is disabled on the DCS PC. Set evalEnabled = true in "
                "Saved Games/DCS/Config/dcs-grpc.lua (infra/dcs-client kit) and restart the mission")

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
            if skip_own and unit["id"] == own["id"]:
                continue  # the cockpit payload (Export.lua) is fresher; don't diff two sources
            try:
                raw = custom.Eval(custom_pb2.EvalRequest(lua=self.STORES_LUA % json.dumps(unit["name"])),
                                  timeout=5).json
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
                self.eye.set_connected(True)
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


def build(host, port, player=PLAYER, *, stores_dir=None, prompt_path=None):
    eye = Eye(player, source="%s:%d" % (host, port), stores_dir=stores_dir)
    return eye, GrpcFeeder(eye, host, port, prompt_path=prompt_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Athena Eye live check: connect, wait, print picture")
    parser.add_argument("--host", default="10.90.10.113")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--cockpit-port", type=int, default=0,
                        help="also listen for the AthenaExport.lua cockpit feed on this UDP port (0 = off; the "
                             "radio adapter normally owns 7778)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    eye, feeder = build(args.host, args.port)
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
