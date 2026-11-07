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

PLAYER = "Prometheus,Player,Client"
M_TO_FT = 3.28084
MS_TO_KT = 1.943844
NM = 1852.0
EVENT_KEEP_S = 30 * 60
SHOT_KEEP_S = 30 * 60
CONTACT_RADIUS_NM = 80.0
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
    def __init__(self, player=PLAYER, *, clock=time.time, source="unconfigured"):
        self.player, self.clock, self.source = player, clock, source
        self.lock = threading.RLock()
        self._seq = itertools.count(1)
        self.on_mission_start = None
        self.reset("init")
        self.connected = False
        self.last_error = None
        self.airbases, self.airbases_at = {}, None
        self.declination, self.declination_at = None, None
        self.fuel, self.fuel_at = None, None
        self.theatre = None
        self.session_id = None

    # -- lifecycle
    def reset(self, reason):
        with self.lock:
            self.units = {}
            self.last_known = OrderedDict()
            self.events = deque()
            self.shots = deque()
            self.units_at = None
            self.events_at = None
            self.reset_reason, self.reset_at = reason, self.clock()

    def set_connected(self, ok, error=None):
        with self.lock:
            self.connected = ok
            if error:
                self.last_error = error

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
        with self.lock:
            self.units.pop(int(unit_id), None)
            self.units_at = self.clock()

    def on_units_message(self, msg):
        if "unit" in msg:
            self.on_unit(msg["unit"])
        elif "gone" in msg:
            self.on_gone(msg["gone"].get("id", 0))

    def ownship(self):
        with self.lock:
            for u in self.units.values():
                if _is_pilot(u.get("player"), self.player):
                    return u
        return None

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
        with self.lock:
            self.events_at = now
            if kind == "mission_start":
                # New mission: drop old events/shots. Units are NOT wiped here (the first unit snapshot
                # may already have arrived); the feeder resubscribes the unit streams for a full resend.
                self.events.clear()
                self.shots.clear()
                if self.on_mission_start is not None:
                    self.on_mission_start()
            self.events.append(ev)
            self._prune(now)
            self._apply(ev)
        return ev

    def _is_me(self, obj):
        return bool(obj) and _is_pilot(obj.get("player"), self.player)

    def _apply(self, ev):
        kind, ini, tgt = ev["kind"], ev["initiator"], ev["target"]
        if kind == "birth" and ini and ini.get("kind") == "unit":
            u = dict(ini, seen=ev["t"])
            u.pop("kind", None)
            self.units[u["id"]] = u
            self.last_known[u["id"]] = u
        if kind in ("dead", "unit_lost", "crash") and ini and ini.get("kind") == "unit":
            self.units.pop(ini["id"], None)
            self._credit_death(ini["id"], ev)
        if kind == "kill" and tgt and tgt.get("kind") == "unit":
            self.units.pop(tgt["id"], None)
        if kind == "shot" and self._is_me(ini):
            self._new_shot(ev, ev["weapon"], ev["weapon_id"], burst=False)
        elif kind == "shooting_start" and self._is_me(ini):
            self._new_shot(ev, ev["weapon"], None, burst=True)
        elif kind == "shooting_end" and self._is_me(ini):
            for s in reversed(self.shots):
                if s["kind"] == "gun" and s["burst_end"] is None:
                    s["burst_end"] = ev["t"]
                    break
        elif kind == "hit" and self._is_me(ini):
            self._resolve(ev, "hit")
        elif kind == "kill" and self._is_me(ini):
            self._resolve(ev, "kill")

    def _new_shot(self, ev, weapon, weapon_id, burst):
        kind = weapon_kind(weapon, burst=burst)
        shooter = ev["initiator"]
        guess = self._guess_target(shooter, kind)
        self.shots.append({"id": ev["seq"], "t": ev["t"], "mission_time": ev["mission_time"], "weapon": weapon,
                           "weapon_id": weapon_id, "kind": kind, "intended_target": guess, "result": "pending",
                           "hit_target": None, "hits": 0, "resolved_t": None,
                           "burst_end": None if burst else ev["t"]})

    def _guess_target(self, shooter, kind):
        """DCS-gRPC shot events carry no target; guess the enemy nearest the nose at launch."""
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

    def _match_shot(self, ev):
        wid, wname = ev.get("weapon_id"), _norm_weapon_name(ev.get("weapon"))
        tgt = ev.get("target") or {}
        open_shots = [s for s in self.shots if s["result"] in ("pending", "hit")]
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
        return s

    def _credit_death(self, unit_id, ev):
        for s in self.shots:
            if s["result"] == "hit" and s["hit_target"] and s["hit_target"].get("id") == unit_id \
                    and ev["t"] - (s["resolved_t"] or s["t"]) < 300:
                s["result"] = "kill"
                s["resolved_t"] = ev["t"]

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
            g["call"] = ("bandit" if dom == "air" else
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
        names = sorted({m[2]["group"] for m in members if m[2]["group"]})
        if names:
            g["groups"] = names[:3]
        players = sorted({m[2]["player"] for m in members if m[2].get("player")})
        if players:
            g["players"] = players
        return g

    def picture(self):
        with self.lock:
            now = self.clock()
            self._prune(now)
            self._settle_shots(now)
            own = None
            for u in self.units.values():
                if _is_pilot(u.get("player"), self.player):
                    own = u
                    break
            age = lambda t: None if t is None else round(now - t, 1)  # noqa: E731
            pic = {
                "source": "Athena Eye (DCS-gRPC %s)" % self.source,
                "connected": self.connected,
                "data_age_s": {"units": age(self.units_at), "last_event": age(self.events_at),
                               "airbases": age(self.airbases_at), "ownship": age(own["seen"]) if own else None},
                "theatre": self.theatre,
                "bearings": ("magnetic (declination %.1f deg from DCS)" % self.declination
                             if self.declination is not None else "TRUE (magnetic declination unavailable)"),
                "notes": [
                    "Every unit listed in contacts/totals is ALIVE as of data_age_s.",
                    "Kills, losses and deaths come ONLY from recent_kills_and_losses / my_shots; never infer a kill otherwise.",
                    "clock is relative to Prometheus' nose (12 = ahead).",
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
                    "alt_ft": int(round((own["alt_m"] or 0) * M_TO_FT)),
                    "speed_kt": int(round((own["speed_ms"] or 0) * MS_TO_KT)),
                    "heading_true": _three(own["heading"] or 0),
                    "fuel_fraction": None if self.fuel is None else round(self.fuel, 2),
                    "lat": round(own["lat"], 4) if own["lat"] is not None else None,
                    "lon": round(own["lon"], 4) if own["lon"] is not None else None,
                }
                if self.declination is not None:
                    pic["ownship"]["heading_mag"] = _three((own["heading"] or 0) - self.declination)
                others = [u for u in self.units.values() if u["id"] != own["id"] and u.get("lat") is not None]
                groups = [self._group_json(own, dom, members) for dom, members in self._cluster(own, others)]
                hostile = [g for g in groups if g["side"] == "hostile"]
                friendly = [g for g in groups if g["side"] != "hostile"]
                pic["contacts_within_nm"] = CONTACT_RADIUS_NM
                pic["hostile_groups"] = hostile[:25]
                pic["friendly_groups"] = friendly[:12]
                pic["hostile_air_groups"] = sum(1 for g in hostile if g["domain"] == "air")
            pic["airbases"] = self._airbases_json(own)
            pic["my_shots_last_15_min"] = self._shots_json(own, now)
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
        return out

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
        return {"connected": pic["connected"], "ownship": own.get("type"),
                "hostile_groups": len(pic.get("hostile_groups", [])),
                "friendly_groups": len(pic.get("friendly_groups", [])),
                "airbases": pic["airbases"]["count"], "shots": pic["my_shots_last_15_min"]["total"],
                "kills_losses": len(pic["recent_kills_and_losses_last_10_min"]),
                "units_age_s": pic["data_age_s"]["units"]}


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

    def __init__(self, eye, host, port=50051, *, units_poll=1, ground_poll=5):
        self.eye, self.host, self.port = eye, host, port
        self.units_poll, self.ground_poll = units_poll, ground_poll
        self.stop = threading.Event()
        self.threads = []
        self.eval_ok = True
        import grpc  # noqa: F401  (fail early if missing)
        from google.protobuf.json_format import MessageToDict
        self._to_dict = lambda m: MessageToDict(m, preserving_proto_field_name=True)
        self.channel = None
        self._calls_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._unit_calls = set()
        self._refresh_world = False
        eye.on_mission_start = self.resubscribe_units

    def _chan(self):
        import grpc
        if self.channel is None:
            self.channel = grpc.insecure_channel("%s:%d" % (self.host, self.port), options=[
                ("grpc.keepalive_time_ms", 20000), ("grpc.keepalive_timeout_ms", 10000),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024)])
        return self.channel

    def start(self):
        jobs = [("units-air", lambda: self._units("GROUP_CATEGORY_AIRPLANE", self.units_poll, self.units_poll)),
                ("units-helo", lambda: self._units("GROUP_CATEGORY_HELICOPTER", self.units_poll, self.units_poll)),
                ("units-ground", lambda: self._units("GROUP_CATEGORY_GROUND", self.ground_poll, 30)),
                ("units-ship", lambda: self._units("GROUP_CATEGORY_SHIP", self.ground_poll, 30)),
                ("events", self._events), ("world", self._world)]
        for name, fn in jobs:
            t = threading.Thread(target=self._forever, args=(name, fn), name="eye-" + name, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def close(self):
        self.stop.set()
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
                self.eye.set_connected(False, msg)
                if failures % 60 == 0:  # first failure, then about every 10 minutes
                    LOG.info("eye %s disconnected (retrying forever): %s", name, msg)
                failures += 1
            if time.monotonic() - started > 30:
                delay, failures = 1.0, 0
            self.stop.wait(delay)
            delay = min(delay * 2, 10.0)

    def _units(self, category, poll, backoff):
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
        try:
            for resp in call:
                if self.stop.is_set():
                    return
                self.eye.set_connected(True)
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

    def _events(self):
        from dcs.mission.v0 import mission_pb2, mission_pb2_grpc
        stub = mission_pb2_grpc.MissionServiceStub(self._chan())
        self._check_session(stub)
        for resp in stub.StreamEvents(mission_pb2.StreamEventsRequest()):
            if self.stop.is_set():
                return
            self.eye.set_connected(True)
            self.eye.on_event(self._to_dict(resp))

    def _world(self):
        from dcs.common.v0 import common_pb2
        from dcs.custom.v0 import custom_pb2, custom_pb2_grpc
        from dcs.mission.v0 import mission_pb2_grpc
        from dcs.world.v0 import world_pb2, world_pb2_grpc
        chan = self._chan()
        world = world_pb2_grpc.WorldServiceStub(chan)
        mission = mission_pb2_grpc.MissionServiceStub(chan)
        custom = custom_pb2_grpc.CustomServiceStub(chan)
        due = {"airbases": 0.0, "declination": 0.0, "fuel": 0.0}
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
                if self.eval_ok and now >= due["fuel"]:
                    due["fuel"] = now + 30
                    try:
                        lua = "local u = Unit.getByName(%s) if u then return u:getFuel() end return nil" % \
                            json.dumps(own["name"])
                        raw = custom.Eval(custom_pb2.EvalRequest(lua=lua), timeout=5).json
                        value = json.loads(raw) if raw else None
                        self.eye.set_fuel(float(value) if isinstance(value, (int, float)) else None)
                    except Exception as error:  # noqa: BLE001 - eval disabled on the client
                        if any(c in _code(error) for c in ("PERMISSION_DENIED", "UNIMPLEMENTED", "UNAUTHENTICATED")):
                            self.eval_ok = False
            self.stop.wait(2)


def build(host, port, player=PLAYER):
    eye = Eye(player, source="%s:%d" % (host, port))
    return eye, GrpcFeeder(eye, host, port)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Athena Eye live check: connect, wait, print picture")
    parser.add_argument("--host", default="10.90.10.113")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    eye, feeder = build(args.host, args.port)
    feeder.start()
    time.sleep(args.seconds)
    pic = eye.picture()
    print(json.dumps(pic, indent=1, default=str))
    print("SUMMARY", json.dumps(eye.summary(pic)))
    feeder.close()
    ok = pic.get("ownship") and (pic.get("hostile_groups") or pic.get("friendly_groups")) and pic["airbases"]["count"]
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
