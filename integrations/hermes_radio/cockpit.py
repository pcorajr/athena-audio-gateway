"""Athena Eye cockpit feed (round 3): UDP JSON packets from AthenaExport.lua on the DCS PC.

AthenaExport.lua (infra/dcs-client in project-athena) runs inside DCS Export.lua and sends ~5 packets
a second to Athena UDP 7778: own position/attitude, IAS/TAS/GS, AGL, G, AoA, fuel (kg), gear/flaps,
payload by station + gun rounds, RWR emitters, locked target and warning lights. This module parses
the packets, keeps the latest cockpit state, tracks RWR lock/launch alerts and renders the
`cockpit` section of the eye picture. It is standalone (no DCS-gRPC needed).
"""
from collections import deque
import json
import logging
import re
import socket
import threading
import time

COCKPIT_PORT = 7778
FRESH_S = 5.0            # cockpit data older than this is stale
PAYLOAD_FRESH_S = 10.0   # payload comes at 1 Hz; prefer it over the Eval loadout while this fresh
ALERT_ANNOUNCE_S = 90.0  # a new RWR lock/launch is announced at the start of the next reply within this window
KG_TO_LB = 2.20462
M_TO_FT = 3.28084
MS_TO_KT = 1.943844
LOG = logging.getLogger("athena.cockpit")

# DCS wsType: level1 4 = weapon; level2 4 missile, 5 bomb, 6 shell, 7 rockets (NURS), 8 torpedo.
WS_WEAPON = 4
WS_KIND = {4: "missile", 5: "bomb", 6: "gun", 7: "rocket", 8: "torpedo"}
AMMO_CATEGORY = {"gun": 0, "missile": 1, "rocket": 2, "bomb": 3, "torpedo": 4}

# RWR signal types (DCS spells one "missle_radio_guided").
SIGNAL_LEVEL = {"search": 0, "track": 1, "lock": 2, "launch": 3}
_HOURS = "twelve one two three four five six seven eight nine ten eleven twelve".split()


def clean_name(name):
    return re.sub(r"^(?:-\s+)+", "", str(name or "").strip())


def _known_type(value):
    return bool(value) and str(value).strip().lower() not in ("unknown", "unknown weapon", "?") \
        and not str(value).strip().isdigit()


# Round 9: plain controller names. Key = DCS type with everything but letters/digits removed, lower case.
# The raw DCS type stays in the picture (types, type, raw_type) for the debrief; spoken names ride alongside.
SPOKEN_NAMES = {
    # SA-2 / S-75 family (missile SA2V755, launcher S_75M_Volhov, Fan Song SNR_75V)
    "sa2v755": "SA-2", "s75mvolhov": "SA-2", "s75volhov": "SA-2", "volhov": "SA-2",
    "snr75v": "SA-2 radar", "snr75": "SA-2 radar", "sa2": "SA-2",
    # SA-3 / S-125 family
    "p19s125sr": "P-19 radar", "p19": "P-19 radar", "snrs125tr": "SA-3 radar", "snrs125": "SA-3 radar",
    "5v27": "SA-3", "s125pechora": "SA-3", "s125": "SA-3", "s125m": "SA-3",
    # guns and trucks
    "zu23emplacementclosed": "ZU-23", "zu23emplacement": "ZU-23", "zu23closed": "ZU-23", "zu23": "ZU-23",
    "container40ft": "container", "container20ft": "container",
    "ural4320apa5d": "Ural power truck", "ural4320t": "Ural truck", "ural4320": "Ural truck",
    "zil131kung": "ZIL-131 truck", "zil131": "ZIL truck", "lazbus": "bus", "atmz5": "fuel truck",
    "invisiblefarp": "FARP", "sa18iglacomm": "SA-18 command post", "sa18iglas": "SA-18",
    "unit": "unit",
    # aircraft
    "f4e45mc": "F-4E", "fa18chornet": "F/A-18C", "a10c2": "A-10C", "f14b": "F-14B",
    # loadouts and pods
    "mk4ffarmk5heat": "Mk 5 rockets", "ffarmk5heat": "Mk 5 rockets", "ffarmk1hex": "Mk 1 rockets",
    "hbale403060": "chaff/flare pod", "hbordpavespikefast": "Pave Spike pod", "hbordpavespike": "Pave Spike pod",
    "agm65a": "Maverick", "m39f100": "M-39 cannon", "m61a1": "M-61 cannon",
    "m53apt": "armor piercing rounds", "m53aptred": "armor piercing rounds", "m56a3hered": "high explosive rounds",
    "supplyshells1": "shells", "supplyshells4": "shells",
}
_PLAIN_KEY = re.compile(r"[^a-z0-9]+")


def spoken_key(name):
    return _PLAIN_KEY.sub("", str(name or "").lower())


def spoken_name(name, fallback="contact"):
    """Plain controller name for any DCS type string: table first, then a generic DCS-suffix strip."""
    raw = str(name or "").strip()
    if not raw:
        return fallback
    raw = re.sub(r"^weapons\.[^.]+\.", "", raw)
    hit = SPOKEN_NAMES.get(spoken_key(raw))
    if hit:
        return hit
    t = re.sub(r"^static[ _-]+", "", raw, flags=re.I)
    t = re.sub(r"[ _-]*copy[ _-]*\d+$", "", t, flags=re.I)  # mission-editor clone suffix
    m = re.match(r"^(.*[A-Za-z]{2,})-\d{1,2}-\d{1,2}$", t)  # mission-editor group suffix, never a designation
    if m and not re.search(r"(?:^|[\s_-])[A-Z]{1,4}$", m.group(1)):  # ZSU-23-4: short all-caps prefix = designation
        t = m.group(1)
    t = re.sub(r"_\d+$", "", t)
    t = re.sub(r"[ _-]+(?:emplacement|closed|open|hard|shelter)$", "", t, flags=re.I)
    hit = SPOKEN_NAMES.get(spoken_key(t))
    if hit:
        return hit
    t = t.replace("_", " ").strip()
    return t or fallback


def weapon_name(name):
    """Shared Eye/cockpit display label; retain raw DCS types in state for correlation."""
    if not _known_type(name):
        return "unknown weapon"
    name = re.sub(r"^weapons\.[^.]+\.", "", str(name))
    hit = SPOKEN_NAMES.get(spoken_key(name))
    if hit and spoken_key(name) != "agm65a":  # keep the AGM-65A designation in weapon labels
        return hit
    gun = re.match(r"(GAU[-_]?8|GAU[-_]?12|M61|GSH[-_]?23|GSH[-_]?30)(?:[-_]|$)", name, re.I)
    if gun:
        return re.sub(r"^(GAU|GSH)[-_]?", r"\1-", gun[1].upper())
    return name.replace("_", "-")


def signal_of(sig):
    s = str(sig or "").lower()
    if "missile" in s or "missle" in s or "launch" in s:
        return "launch"
    if s == "lock" or "lock" in s:
        return "lock"
    if "track" in s:
        return "track"
    return "search"


def rwr_clock(azimuth_deg):
    """Relative azimuth (deg, clockwise from the nose; negative = left) -> clock hour 1..12."""
    hour = int(((float(azimuth_deg) % 360.0) + 15.0) // 30.0) % 12
    return 12 if hour == 0 else hour


def clock_words(hour):
    return "%s o'clock" % _HOURS[hour]


def _f(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def _r(value, places=0, scale=1.0):
    v = _f(value)
    if v is None:
        return None
    v = round(v * scale, places)
    return int(v) if places == 0 else v


def _list(value):
    """Lua tables may arrive as [] or {} (empty) or {"1": ...} objects."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value[k] for k in sorted(value, key=lambda k: (len(str(k)), str(k)))]
    return []


# ----------------------------------------------------------------------------- fuel (round 4)
# Max internal fuel (lb) per airframe, for fraction-only values (LoGetEngineInfo on some modules, Unit:getFuel()).
# Approximate DCS values; anything not listed is reported as a percentage.
MAX_INTERNAL_FUEL_LB = {
    "A-10A": 11087, "A-10C": 11087, "A-10C_2": 11087, "F-4E-45MC": 12057, "F-16C_50": 7162,
    "FA-18C_hornet": 10803, "F-15ESE": 13123, "F-15E": 13123, "AV8BNA": 7759, "M-2000C": 6978,
    "Mirage-F1CE": 7400, "Mirage-F1EE": 7400, "Mirage-F1M-EE": 7400, "Su-25T": 8355, "Su-25": 6614,
    # F-100D: 1,189 US gal internal (fuselage + wing) x 6.5 lb/gal JP-4 = 7,729 lb ~ 7,730. Source:
    # https://www.supersabre.com/f-100specs.htm (F-100D "Internal Fuel quantities 1,189 gall"). Not read from DCS.
    "F-100D": 7730,
    # F-14B: 16,200 lb internal (Grumman F-14 data, e.g. detailandscale.com F-14 Tomcat data sheet).
    "F-14B": 16200, "F-14A-135-GR": 16200,
}
FRACTION_MAX = 1.5  # LoGetEngineInfo fuel values at or below this are a fraction of capacity, above it kilograms


def fuel_lb_from_fraction(fraction, aircraft, source="DCS-gRPC Unit:getFuel()"):
    """Fuel fraction -> {"total_lb", "internal_pct", "source"} (pounds only for airframes in the table)."""
    f = _f(fraction)
    if f is None or f <= 0:
        return None
    out: dict = {"internal_pct": int(round(f * 100))}
    max_lb = MAX_INTERNAL_FUEL_LB.get(aircraft or "")
    if max_lb:
        out["total_lb"] = int(round(f * max_lb, -1))
        out["internal_lb"] = out["total_lb"]
        out["source"] = "%s fraction x %s max internal (approximate)" % (source, aircraft)
        out["estimated"] = True  # table value, not a DCS reading: say "about N pounds"
        out["spoken"] = "about %s pounds" % format(out["total_lb"], ",")
    else:
        out["source"] = "%s fraction (pounds unknown for this airframe: give percent)" % source
    return out


def cockpit_fuel(p):
    """Packet fuel -> picture dict, or None when unknown. 0/None = unknown. Values > 1.5 are kilograms (DCS Export
    documents kg); values <= 1.5 are a fraction of capacity (A-10C, F-4E give fractions) and need the raw
    4-decimal values of AthenaExport fuel encoding 2 ("fu": 2). Version-1 packets rounded them to 0 or 1."""
    fi, fe = _f(p.get("fuel_int")), _f(p.get("fuel_ext"))
    raw = p.get("fu") is not None
    if fi is None or fi <= 0:
        return None
    ext_kg = fe if fe is not None and fe > FRACTION_MAX else None
    if fi > FRACTION_MAX:
        out = {"internal_lb": _r(fi, -1, KG_TO_LB), "external_lb": _r(ext_kg, -1, KG_TO_LB),
               "total_lb": _r(fi + (ext_kg or 0), -1, KG_TO_LB), "total_kg": _r(fi + (ext_kg or 0), 0)}
        return {k: int(v) for k, v in out.items() if v is not None}
    if not raw:
        return None  # rounded fraction from the version-1 export script: unusable
    out = fuel_lb_from_fraction(fi, p.get("ac"), source="cockpit")
    if out is not None and fe is not None and 0 < fe <= FRACTION_MAX:
        out["external_pct"] = int(round(fe * 100))
    return out


def parse_packet(data):
    """bytes/str -> packet dict, or None for anything that is not an AthenaExport v1 JSON object."""
    try:
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "replace")
        pkt = json.loads(data)
    except (ValueError, TypeError):
        return None
    if not isinstance(pkt, dict) or pkt.get("v") != 1:
        return None
    return pkt


def payload_to_stores(pay):
    """Packet `pay` -> eye stores snapshot {"ammo": [{type, name, count, category}], "fuel": None}.
    Same shape as the DCS-gRPC Eval getAmmo() snapshot, so the round-2 stores diff/attribution works
    with either source. Fuel tanks and empty pylons/pods are skipped (wsType level1 != weapon)."""
    if not isinstance(pay, dict):
        return None
    totals = {}
    for st in _list(pay.get("st")):
        if not isinstance(st, dict):
            continue
        count = _f(st.get("c"))
        if not count or count <= 0:
            continue
        lv = st.get("l") if isinstance(st.get("l"), list) else []
        if lv and lv[0] != WS_WEAPON:
            continue
        name = st.get("n") or ("weapon %s" % ".".join(str(x) for x in lv) if lv else "unknown weapon")
        kind = WS_KIND.get(lv[1]) if len(lv) > 1 else None
        row = totals.setdefault(name, {"type": name, "name": name, "count": 0,
                                       "category": AMMO_CATEGORY.get(kind) if kind else None})
        row["count"] += int(count)
    ammo = list(totals.values())
    gun = _f(pay.get("gun"))
    if gun is not None and gun > 0:
        ammo.append({"type": "gun", "name": "gun rounds", "count": int(gun), "category": 0})
    return {"ammo": ammo, "fuel": None}


def _gear_word(v):
    if v is None:
        return None
    if v >= 0.99:
        return "down"
    if v <= 0.01:
        return "up"
    return "in transit"


class CockpitState:
    """Latest cockpit packet + slow parts (payload, mechanics, warnings, radar targets) + RWR alerts."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.latest, self.at, self.src = None, None, None
        self.packets, self.bad, self.started_at, self.stopped_at = 0, 0, None, None
        self.slow = {}       # key -> (value, t) for pay/mech/warn/tgt
        self.emitters = {}   # emitter key -> highest signal level seen while it stays on the RWR
        self.alerts = deque(maxlen=50)
        self.status = "no cockpit packets yet"
        self.rwr_modules = {}  # aircraft type -> True when its packets carry RWR (LoGetTWSInfo), else False
        self.engine_samples = deque(maxlen=21)  # 3-second samples, one minute of secondary damage clues

    def rwr_modules_json(self):
        with self.lock:
            return dict(self.rwr_modules)

    # -- ingest
    def ingest(self, pkt, src=None):
        """Apply one parsed packet. Returns {"payload": snapshot-or-None, "alerts": [new alerts]}."""
        now = self.clock()
        out = {"payload": None, "alerts": [], "event": pkt.get("ev")}
        with self.lock:
            self.packets += 1
            self.src = src
            if pkt.get("ev") == "start":
                self.started_at, self.stopped_at = now, None
                self.engine_samples.clear()
                self.slow.clear()
                self.emitters.clear()
                self.status = "cockpit feed started"
                return out
            if pkt.get("ev") == "stop":
                self.stopped_at = now
                self.emitters.clear()
                self.status = "cockpit feed stopped (left the aircraft or mission ended)"
                return out
            if self.latest and pkt.get("unit") != self.latest.get("unit"):
                self.engine_samples.clear()
                self.slow.clear()
            self.latest, self.at = pkt, now
            if not self.engine_samples or now - self.engine_samples[-1][0] >= 3:
                self.engine_samples.append((now, pkt))
            self.status = "ok"
            ac = pkt.get("ac")
            if ac:
                if isinstance(pkt.get("rwr"), dict):
                    self.rwr_modules[ac] = True
                else:
                    self.rwr_modules.setdefault(ac, False)
            for key in ("pay", "mech", "warn", "tgt"):
                if key in pkt:
                    self.slow[key] = (pkt[key], now)
            if "pay" in pkt:
                out["payload"] = payload_to_stores(pkt["pay"])
            if isinstance(pkt.get("rwr"), dict):
                out["alerts"] = self._rwr_alerts(pkt["rwr"], now)
        return out

    def _rwr_alerts(self, rwr, now):
        seen, new = set(), []
        for e in _list(rwr.get("e")):
            if not isinstance(e, dict):
                continue
            key = str(e.get("id") if e.get("id") is not None else e.get("type"))
            sig = signal_of(e.get("sig"))
            level = SIGNAL_LEVEL[sig]
            seen.add(key)
            before = self.emitters.get(key, -1)
            if level > before:
                self.emitters[key] = level
                if sig in ("lock", "launch"):
                    az = _f(e.get("az"))
                    hour = rwr_clock(az) if az is not None else None
                    what = "Missile launch" if sig == "launch" else "Spike"
                    where = (" at your %s" % clock_words(hour)) if hour else ""
                    alert = {"key": "rwr-%s-%s-%d" % (key, sig, int(now)), "t": now, "signal": sig,
                             "type": e.get("type") or "unknown emitter", "clock": hour,
                             "call": "%s, %s%s." % (what, e.get("type") or "unknown emitter", where)}
                    self.alerts.append(alert)
                    new.append(alert)
        for key in list(self.emitters):  # gone from the RWR: a new lock later is a new alert
            if key not in seen:
                del self.emitters[key]
        return new

    # -- queries
    def age(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            return None if self.at is None else now - self.at

    def fresh(self, now=None):
        a = self.age(now)
        return a is not None and a <= FRESH_S and self.stopped_at is None

    def payload_fresh(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            pay = self.slow.get("pay")
            return pay is not None and now - pay[1] <= PAYLOAD_FRESH_S and self.stopped_at is None

    def pending_alerts(self, announced, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            return [a for a in self.alerts if a["key"] not in announced and now - a["t"] <= ALERT_ANNOUNCE_S]

    def ownship(self):
        """Pseudo ownship from the cockpit (used when DCS-gRPC has no own unit). None when stale."""
        with self.lock:
            p = self.latest
            if p is None or not self.fresh() or _f(p.get("lat")) is None:
                return None
            coal = {1: "red", 2: "blue"}.get(p.get("coal"), "unknown")
            spd = _f(p.get("gs")) if _f(p.get("gs")) is not None else _f(p.get("tas"))
            return {"id": -1, "name": p.get("unit") or "", "type": p.get("ac") or "", "callsign": "",
                    "group": p.get("group") or "", "coalition": coal, "group_category": "", "category": "fighter",
                    "player": clean_name(p.get("pilot")) or None,
                    "lat": _f(p.get("lat")), "lon": _f(p.get("lon")),
                    "alt_m": _f(p.get("alt")), "heading": _f(p.get("hdg")) or 0.0, "track": _f(p.get("hdg")) or 0.0,
                    "speed_ms": spd or 0.0, "seen": self.at, "source": "cockpit"}

    def locked_targets(self):
        with self.lock:
            p = self.latest or {}
            return [t for t in _list(p.get("lock")) if isinstance(t, dict)] if self.fresh() else []

    # -- picture
    def to_picture(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            p = self.latest
            if p is None:
                return {"available": False, "reason": "no cockpit feed yet: AthenaExport.lua on the DCS PC sends "
                        "UDP %d (Export.lua loader line + Athena firewall)" % COCKPIT_PORT,
                        "packets": self.packets, "status": self.status}
            age = now - self.at
            out = {"available": True, "fresh": age <= FRESH_S and self.stopped_at is None, "age_s": round(age, 1),
                   "aircraft": p.get("ac"),
                   "pilot": clean_name(p.get("pilot")) or None}
            if self.stopped_at is not None:
                out["note"] = self.status
            if age > 60:
                out["note"] = "cockpit feed silent for %d s (old values not shown)" % int(age)
                return out
            mech = (self.slow.get("mech") or (None, None))[0] or {}
            if isinstance(mech, dict) and mech:
                cfg = {"gear": _gear_word(_f(mech.get("gear"))),
                       "flaps_pct": _r(mech.get("flaps"), 0, 100.0),
                       "speedbrake": None if _f(mech.get("sb")) is None else ("out" if _f(mech["sb"]) > 0.05 else "in"),
                       "hook": None if _f(mech.get("hook")) is None else ("down" if _f(mech["hook"]) > 0.5 else "up"),
                       "canopy": None if _f(mech.get("canopy")) is None else
                       ("open" if _f(mech["canopy"]) > 0.05 else "closed"),
                       "wheel_brakes": None if _f(mech.get("brakes")) is None else _f(mech["brakes"]) > 0.05}
                out["config"] = {k: v for k, v in cfg.items() if v is not None}
            flight = {"ias_kt": _r(p.get("ias"), 0, MS_TO_KT), "tas_kt": _r(p.get("tas"), 0, MS_TO_KT),
                      "gs_kt": _r(p.get("gs"), 0, MS_TO_KT), "mach": _r(p.get("mach"), 2),
                      "alt_msl_ft": _r(p.get("alt_msl") if p.get("alt_msl") is not None else p.get("alt"), -1,
                                       M_TO_FT),
                      "agl_ft": _r(p.get("agl"), -1, M_TO_FT), "vs_fpm": _r(p.get("vs"), -1, M_TO_FT * 60),
                      "g": _r(p.get("g"), 1), "aoa_deg": _r(p.get("aoa"), 1),
                      "heading_true": _r(p.get("hdg"), 0), "heading_mag": _r(p.get("hdg_mag"), 0),
                      "pitch_deg": _r(p.get("pitch"), 0), "bank_deg": _r(p.get("bank"), 0)}
            out["flight"] = {k: (int(v) if k.endswith("_ft") or k == "vs_fpm" else v)
                             for k, v in flight.items() if v is not None}
            rpm = [_f(v) for v in _list(p.get("rpm"))]
            if rpm:
                out["engine"] = {"rpm_pct": rpm, "note": "RPM changes are secondary clues, not proof of a hit."}
            samples = [(t, s) for t, s in self.engine_samples if now - t <= 60]
            if len(samples) >= 2:
                then, previous = samples[0]
                elapsed = now - then
                old_rpm = [_f(v) for v in _list(previous.get("rpm"))]
                if rpm and len(rpm) == len(old_rpm):
                    out["engine"]["rpm_change"] = [round(b - a, 1) if a is not None and b is not None else None
                                                     for a, b in zip(old_rpm, rpm)]
                    out["engine"]["window_s"] = round(elapsed, 1)
                old_fuel, new_fuel = cockpit_fuel(previous), cockpit_fuel(p)
                if elapsed > 0 and old_fuel and new_fuel and old_fuel.get("source") == new_fuel.get("source"):
                    before, after = old_fuel.get("total_lb"), new_fuel.get("total_lb")
                    if before is not None and after is not None:
                        out["fuel_trend"] = {"window_s": round(elapsed, 1),
                                             "loss_lb_per_min": round((before - after) * 60 / elapsed, 1),
                                             "note": "Observed fuel loss (burn, leak or jettison), not a confirmed leak; "
                                                     "engine fuel-flow telemetry unavailable."}
            fuel = cockpit_fuel(p)
            if fuel is not None:
                out["fuel"] = fuel
            elif p.get("fuel_int") is not None:
                out["fuel_note"] = "cockpit fuel unreadable (0 or rounded fraction): use ownship.fuel"
            pay = self.slow.get("pay")
            if pay is not None:
                snap = payload_to_stores(pay[0]) or {"ammo": []}
                stations = []
                for st in _list((pay[0] or {}).get("st")):
                    if isinstance(st, dict) and _f(st.get("c")) and (not st.get("l") or st["l"][0] == WS_WEAPON):
                        stations.append({"station": st.get("i"), "weapon": weapon_name(st.get("n")),
                                         "count": int(st["c"])})
                by_weapon = {}
                for a in snap["ammo"]:
                    if a["type"] != "gun":
                        name = weapon_name(a["name"])
                        by_weapon[name] = by_weapon.get(name, 0) + a["count"]
                out["payload"] = {"age_s": round(now - pay[1], 1), "stations": stations,
                                  "by_weapon": by_weapon,
                                  "gun_rounds": next((a["count"] for a in snap["ammo"] if a["type"] == "gun"), 0),
                                  "selected_station": (pay[0] or {}).get("cur")}
            out["rwr"] = self._rwr_json(p)
            warn = self.slow.get("warn")
            if warn is not None and _list(warn[0]):
                out["warning_lights"] = [str(w) for w in _list(warn[0])][:12]
            tgt = self.slow.get("tgt")
            if tgt is not None:
                out["radar_contacts"] = len([t for t in _list(tgt[0]) if isinstance(t, dict)])
            return out

    def _rwr_json(self, p):
        rwr = p.get("rwr")
        if not isinstance(rwr, dict):
            return {"available": False,
                    "reason": "no RWR data (aircraft without exportable RWR, or sensor export disabled on the server)"}
        threats = []
        for e in _list(rwr.get("e")):
            if not isinstance(e, dict):
                continue
            sig = signal_of(e.get("sig"))
            az = _f(e.get("az"))
            hour = rwr_clock(az) if az is not None else None
            row = {"type": e.get("type") or "unknown emitter", "signal": sig, "clock": hour,
                   "clock_words": clock_words(hour) if hour else None,
                   "power": _r(e.get("pw"), 2), "priority": e.get("pr")}
            verb = {"search": "searching", "track": "tracking you", "lock": "locked on you",
                    "launch": "MISSILE LAUNCH"}[sig]
            row["call"] = "%s %s%s" % (row["type"], verb, (" at your %s" % row["clock_words"]) if hour else "")
            threats.append({k: v for k, v in row.items() if v is not None})
        threats.sort(key=lambda t: (-SIGNAL_LEVEL[t["signal"]], -(t.get("priority") or 0), -(t.get("power") or 0)))
        out = {"threats": threats[:10], "count": len(threats)}
        if not threats:
            out["note"] = "RWR clean"
        else:
            out["highest"] = threats[0]["signal"]
        return out


class CockpitListener:
    """UDP listener thread: AthenaExport packets -> eye.on_cockpit_packet(pkt, src)."""

    def __init__(self, eye, host="0.0.0.0", port=COCKPIT_PORT):
        self.eye, self.host, self.port = eye, host, port
        self.stop = threading.Event()
        self.sock = None
        self.thread = None
        self.error = None

    def start(self):
        try:
            # No SO_REUSEADDR: a second adapter on the same port must fail loudly, not split the packets.
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.host, self.port))
            self.sock.settimeout(0.5)
        except OSError as error:
            self.error = "%s: %s" % (type(error).__name__, error)
            LOG.warning("cockpit: cannot listen on UDP %s:%d (%s)", self.host, self.port, self.error)
            self.eye.cockpit.status = "listener failed: %s" % self.error
            return self
        self.port = self.sock.getsockname()[1]
        LOG.info("cockpit: listening on UDP %s:%d", self.host, self.port)
        self.thread = threading.Thread(target=self._run, name="eye-cockpit", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                time.sleep(0.2)
                continue
            pkt = parse_packet(data)
            if pkt is None:
                with self.eye.cockpit.lock:
                    self.eye.cockpit.bad += 1
                continue
            try:
                self.eye.on_cockpit_packet(pkt, src=addr[0])
            except Exception:  # noqa: BLE001 - one bad packet must never kill the listener
                LOG.exception("cockpit: packet handling failed")

    def close(self):
        self.stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
