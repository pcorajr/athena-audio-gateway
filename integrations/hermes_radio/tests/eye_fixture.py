"""Fixture DCS-gRPC traffic in the exact MessageToDict(preserving_proto_field_name=True) shape.

Scenario (Caucasus): Prometheus in an F-16C at 42.0N 42.0E, angels 20, heading 090 true, 250 m/s.
test_eye round-trips every message through the real DCS-gRPC protobuf types to prove the shape.
"""
import math

LAT0, LON0 = 42.0, 42.0


def offset(bearing_deg, range_nm, lat=LAT0, lon=LON0):
    d = range_nm / 60.0
    return (lat + d * math.cos(math.radians(bearing_deg)),
            lon + d * math.sin(math.radians(bearing_deg)) / math.cos(math.radians(lat)))


def unit(uid, name, type_name, coalition, group, gcat, lat, lon, alt, heading=0.0, speed=0.0, player=None):
    u = {"id": uid, "name": name, "callsign": name, "coalition": "COALITION_" + coalition.upper(), "type": type_name,
         "position": {"lat": lat, "lon": lon, "alt": alt, "u": 1.0, "v": 1.0},
         "orientation": {"heading": heading, "yaw": heading},
         "velocity": {"heading": heading, "speed": speed},
         "group": {"id": uid * 10, "name": group, "coalition": "COALITION_" + coalition.upper(),
                   "category": "GROUP_CATEGORY_" + gcat},
         "number_in_group": 1}
    if player:
        u["player_name"] = player
    return u


SU27_1 = offset(60, 20)
SU27_2 = offset(60, 20.5)
BUK_SR = offset(180, 10)
BUK_LN = offset(181, 10.2)
T72 = offset(270, 5)
TANKER = offset(0, 30)
AWACS = offset(0, 150)
SU25 = offset(95, 1.2)

PROMETHEUS = unit(100, "Prometheus-1", "F-16C_50", "blue", "Viper 1", "AIRPLANE", LAT0, LON0, 6096.0, 90.0, 250.0,
                  player="Prometheus")
UNITS = [
    PROMETHEUS,
    unit(201, "Red CAP 1-1", "Su-27", "red", "Red CAP 1", "AIRPLANE", *SU27_1, 7620.0, 240.0, 250.0),
    unit(202, "Red CAP 1-2", "Su-27", "red", "Red CAP 1", "AIRPLANE", *SU27_2, 7700.0, 240.0, 250.0),
    unit(301, "SAM North SR", "SA-11 Buk SR 9S18M1", "red", "SA-11 North", "GROUND", *BUK_SR, 300.0),
    unit(302, "SAM North LN", "SA-11 Buk LN 9A310M1", "red", "SA-11 North", "GROUND", *BUK_LN, 300.0),
    unit(401, "Armor 1", "T-72B", "red", "Red Armor", "GROUND", *T72, 200.0),
    unit(501, "Texaco", "KC-135", "blue", "Texaco 1", "AIRPLANE", *TANKER, 7000.0, 270.0, 200.0),
    unit(502, "Overlord", "E-3A", "blue", "Overlord 1", "AIRPLANE", *AWACS, 9000.0, 270.0, 200.0),
    unit(205, "Frogfoot", "Su-25T", "red", "Red CAS", "AIRPLANE", *SU25, 6000.0, 90.0, 180.0),
    unit(601, "Viper 2-1", "F-16C_50", "blue", "Viper 2", "AIRPLANE", *offset(200, 40), 5000.0, 180.0, 220.0),
]


def units_message(u, time_s=10.0):
    return {"time": time_s, "unit": u}


def gone_message(uid, name):
    return {"time": 10.0, "gone": {"id": uid, "name": name}}


def initiator(u):
    return {"unit": u}


def weapon(wid, type_name):
    return {"id": wid, "type": type_name, "position": {"lat": LAT0, "lon": LON0, "alt": 6000.0},
            "orientation": {"heading": 90.0}, "velocity": {"heading": 90.0, "speed": 900.0}}


def shot(u, wid, type_name, t=100.0):
    return {"time": t, "shot": {"initiator": initiator(u), "weapon": weapon(wid, type_name)}}


def hit(u, wid, type_name, target_unit, t=110.0):
    return {"time": t, "hit": {"initiator": initiator(u), "weapon": weapon(wid, type_name),
                               "target": {"unit": target_unit}, "weapon_name": type_name}}


def kill(u, wid, type_name, target_unit, t=111.0):
    return {"time": t, "kill": {"initiator": initiator(u), "weapon": weapon(wid, type_name),
                                "target": {"unit": target_unit}, "weapon_name": type_name}}


def gun_hit(u, shell, target_unit, t=200.0):
    # Gun hits in DCS-gRPC: no weapon object, only weapon_name.
    return {"time": t, "hit": {"initiator": initiator(u), "target": {"unit": target_unit}, "weapon_name": shell}}


def shooting(kind, u, gun, t=199.0):
    return {"time": t, kind: {"initiator": initiator(u), "weapon_name": gun}}


def dead(u, t=205.0):
    return {"time": t, "dead": {"initiator": initiator(u)}}


def takeoff(u, base, t=50.0):
    return {"time": t, "takeoff": {"initiator": initiator(u), "place": base}}


AIRBASES = [
    {"name": "Kutaisi", "display_name": "Kutaisi", "callsign": "Kutaisi", "coalition": "COALITION_BLUE",
     "category": "AIRBASE_CATEGORY_AIRDROME", "position": {"lat": 42.0, "lon": 42.03, "alt": 45.0}},
    {"name": "Senaki-Kolkhi", "display_name": "Senaki-Kolkhi", "coalition": "COALITION_RED",
     "category": "AIRBASE_CATEGORY_AIRDROME", "position": {"lat": 42.24, "lon": 42.05, "alt": 13.0}},
    {"name": "Kobuleti", "display_name": "Kobuleti", "coalition": "COALITION_RED",
     "category": "AIRBASE_CATEGORY_AIRDROME", "position": {"lat": 41.93, "lon": 41.86, "alt": 18.0}},
    {"name": "Batumi", "display_name": "Batumi", "coalition": "COALITION_RED",
     "category": "AIRBASE_CATEGORY_AIRDROME", "position": {"lat": 41.6, "lon": 41.6, "alt": 10.0}},
    {"name": "Sukhumi-Babushara", "display_name": "Sukhumi-Babushara", "coalition": "COALITION_NEUTRAL",
     "category": "AIRBASE_CATEGORY_AIRDROME", "position": {"lat": 42.85, "lon": 41.13, "alt": 13.0}},
    {"name": "Tbilisi-Lochini", "display_name": "Tbilisi-Lochini", "coalition": "COALITION_BLUE",
     "category": "AIRBASE_CATEGORY_AIRDROME", "position": {"lat": 41.67, "lon": 44.95, "alt": 479.0}},
    {"name": "FARP London", "display_name": "FARP London", "coalition": "COALITION_BLUE",
     "category": "AIRBASE_CATEGORY_HELIPAD", "position": {"lat": 42.1, "lon": 42.2, "alt": 100.0}},
]
