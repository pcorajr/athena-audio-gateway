"""Athena Eye round 4 (2026-11-07 session 2 review: F-4E + A-10C on Nevada).

1. kills: confirmed (not "likely") when the target stops showing right after my attack, on dead/unit_lost events,
   on a hit, or on a pilot report; raw event debug log; proto shapes of hit/kill/dead/unit_lost.
2. fuel: fraction vs kg, 0 = unknown, Unit:getFuel() x airframe max fallback, AthenaExport fuel encoding 2.
3. RWR fallback from DCS-gRPC: SAMs with Prometheus in range, hostile missile shots, missiles in flight (Eval).
4. verbosity: call classes, ack cap, picture top 3, sentence-boundary trimming.
5. dev notes only on explicit phrasing.
6. whisper prompt brevity words.
7. busy maps: 1,500 units, picture p95 < 50 ms, bounded size; grid clustering equals the old O(n^2) clustering.
"""
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
import unittest

from integrations.hermes_radio import cockpit as cp
from integrations.hermes_radio.contract import Request
from integrations.hermes_radio.eye import (BREVITY_PROMPT, CONTACT_RADIUS_NM, M_TO_FT, Eye, GrpcFeeder,
                                           bearing_range, domain, norm_unit, sam_profile)
from integrations.hermes_radio.simple_controller import (CLASS_HINTS, MAX_REPLY_CHARS, PROMPT, SimpleController,
                                                         call_class, fit_reply)
from integrations.hermes_radio.tests import eye_fixture as fx

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.environ.get("ATHENA_CLIENT_KIT",
                     os.path.expanduser("~/Ai-Workspace/projects/project-athena/infra/dcs-client"))
LUA = shutil.which("lua5.1") or shutil.which("luajit")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTranscript:
    def __init__(self):
        self.records = []

    def log(self, **record):
        self.records.append(record)
        return True


def req(text, tid="t1", speaker="player"):
    return Request(tid, text, speaker, 251000000, "AM", "2026-11-07T00:00:00Z", "2026-11-07T00:00:00Z")


# Single-player A-10C II over the fixture origin, heading 090, 3,000 ft; a moving T-55 pair on the nose.
A10 = fx.unit(100, "A-10C II Ligth CAS Modern -1", "A-10C_2", "blue", "Hawg 1", "AIRPLANE", fx.LAT0, fx.LON0,
              914.0, 90.0, 150.0, player="Prometheus")
T55 = fx.unit(11987, "Unit #650", "T-55", "red", "62-08 Moving Target", "GROUND", *fx.offset(90, 0.8), 900.0)
T55_B = fx.unit(11988, "Unit #651", "T-55", "red", "62-08 Moving Target", "GROUND", *fx.offset(91, 0.9), 900.0)
F4E = fx.unit(200, "Aerial-3-2", "F-4E-45MC", "blue", "Aerial-3", "AIRPLANE", fx.LAT0, fx.LON0, 3048.0, 90.0, 200.0,
              player="Prometheus")
SA11_SR = fx.unit(301, "SAM SR", "SA-11 Buk SR 9S18M1", "red", "SA-11 East", "GROUND", *fx.offset(100, 10), 300.0)
SA11_LN = fx.unit(302, "SAM LN", "SA-11 Buk LN 9A310M1", "red", "SA-11 East", "GROUND", *fx.offset(101, 10.2), 300.0)
SA10_FAR = fx.unit(303, "S300 SR", "S-300PS 40B6MD sr", "red", "SA-10 Far", "GROUND", *fx.offset(0, 60), 300.0)
EWR = fx.unit(304, "EWR", "1L13 EWR", "red", "EWR North", "GROUND", *fx.offset(350, 50), 300.0)
SHILKA = fx.unit(305, "AAA", "ZSU-23-4 Shilka", "red", "AAA", "GROUND", *fx.offset(270, 20), 300.0)


def a10_eye(clock=None, stores_dir=None, extra=()):
    clock = clock or Clock()
    eye = Eye("Prometheus,Player,Client", clock=clock, source="fixture", stores_dir=stores_dir)
    eye.set_connected(True)
    for u in (A10, T55, T55_B) + tuple(extra):
        eye.on_units_message(fx.units_message(u))
    eye.set_airbases(fx.AIRBASES)
    return eye, clock


def f4e_eye(clock=None, extra=(SA11_SR, SA11_LN, SA10_FAR, EWR, SHILKA)):
    clock = clock or Clock()
    eye = Eye("Prometheus,Player,Client", clock=clock, source="fixture")
    eye.set_connected(True)
    for u in (F4E, T55) + tuple(extra):
        eye.on_units_message(fx.units_message(u))
    return eye, clock


def f4e_packet(**over):
    """F-4E cockpit packet: version-1 fuel (rounded fraction = 0), NO rwr key (LoGetTWSInfo returns nothing)."""
    p = {"v": 1, "seq": 1, "ac": "F-4E-45MC", "unit": "Aerial-3-2", "coal": 2, "pilot": "Prometheus",
         "lat": fx.LAT0, "lon": fx.LON0, "alt": 3048.0, "hdg": 90.0, "ias": 150.0, "gs": 200.0,
         "fuel_int": 0, "fuel_ext": 0}
    p.update(over)
    return p


def store_rows(tmp):
    rows = []
    for name in os.listdir(tmp):
        with open(os.path.join(tmp, name), encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh]
    return rows


# ============================================================================= 1. kills
class KillConfirmationTests(unittest.TestCase):
    def test_a10_gau8_replay_vanish_9s_after_last_burst_is_confirmed_splash(self):
        """21:33-21:34: nine GAU_8 shot events on the T-55, no hit/kill/dead; it vanished 9 s after the last."""
        with tempfile.TemporaryDirectory() as tmp:
            eye, clock = a10_eye(stores_dir=tmp)
            for i in range(9):
                eye.on_event(fx.shot(A10, 0, "GAU_8", t=100.0 + i))
                clock.t += 6
            clock.t += 9 - 6
            eye.on_units_message(fx.gone_message(11987, "Unit #650"))
            pic = eye.picture()
            self.assertEqual(pic["splash_to_announce"], ["Splash one T-55"])
            kill = pic["my_kills_this_mission"][0]
            self.assertEqual((kill["confidence"], kill["inferred"]), ("confirmed", True))
            self.assertIn("stopped showing 9 s after my attack", kill["how"])
            self.assertEqual(pic["my_shots_last_15_min"]["shots"][-1]["result"], "kill")
            row = pic["recent_kills_and_losses_last_10_min"][0]
            self.assertTrue(row["by_me"])
            self.assertNotIn("likely", row["event"])
            logged = [r for r in store_rows(tmp) if r["kind"] == "kill_by_me"]
            self.assertEqual(len(logged), 1)
            self.assertEqual((logged[0]["inferred"], logged[0]["confidence"]), (True, "confirmed"))

    def test_f4e_maverick_from_stores_diff_then_vanish_is_confirmed(self):
        """21:13: AGM-65D 1 -> 0 from the cockpit payload (no shot event), the locked T-55 vanished."""
        eye, clock = f4e_eye(extra=())
        pay = {"st": [{"i": 1, "n": "AGM-65D", "c": 1, "l": [4, 4, 8, 77]}], "gun": 0}
        eye.on_cockpit_packet(f4e_packet(pay=pay))
        clock.t += 1
        eye.on_cockpit_packet(f4e_packet(pay={"st": [{"i": 1, "n": "AGM-65D", "c": 0, "l": [4, 4, 8, 77]}],
                                              "gun": 0}))
        clock.t += 15
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        pic = eye.picture()
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-55"])
        self.assertEqual(pic["my_kills_this_mission"][0]["weapon"], "AGM-65D")

    def test_gun_vanish_long_after_is_only_likely(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 75
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        pic = eye.picture()
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-55, likely"])
        self.assertEqual(pic["my_kills_this_mission"][0]["confidence"], "likely")

    def test_vanish_without_my_attack_is_not_my_kill(self):
        eye, clock = a10_eye()
        clock.t += 5
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        pic = eye.picture()
        self.assertEqual(pic["my_kills_this_mission"], [])
        self.assertEqual(pic["recent_kills_and_losses_last_10_min"][0]["event"], "no longer showing, likely destroyed")

    def test_hit_then_vanish_after_window_is_still_confirmed(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 501, "AGM_65D"))
        clock.t += 5
        eye.on_event(fx.hit(A10, 501, "AGM_65D", T55))
        clock.t += 100
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        self.assertEqual(eye.picture()["my_kills_this_mission"][0]["confidence"], "confirmed")

    def test_dead_event_for_my_intended_target_without_hit_is_confirmed(self):
        """DCS 2.9 ground deaths: dead/unit_lost with the initiator only, no kill event, no hit event."""
        for kind in ("dead", "unit_lost"):
            with self.subTest(kind=kind):
                eye, clock = a10_eye()
                eye.on_event(fx.shot(A10, 0, "GAU_8"))
                clock.t += 4
                eye.on_event({"time": 205.0, kind: {"initiator": {"unit": T55}}})
                eye.on_units_message(fx.gone_message(11987, "Unit #650"))  # no double count
                pic = eye.picture()
                self.assertEqual(len(pic["my_kills_this_mission"]), 1)
                k = pic["my_kills_this_mission"][0]
                self.assertEqual((k["confidence"], k["inferred"]), ("confirmed", False))
                self.assertIn("%s event" % kind, k["how"])
                self.assertEqual(pic["splash_to_announce"], ["Splash one T-55"])

    def test_dead_event_of_unrelated_unit_is_not_mine(self):
        eye, clock = a10_eye(extra=(SA11_SR,))
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        eye.on_event(fx.dead(SA11_SR))
        self.assertEqual(eye.picture()["my_kills_this_mission"], [])

    def test_area_weapon_kills_second_vehicle_of_the_group(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 3
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        eye.on_units_message(fx.gone_message(11988, "Unit #651"))
        self.assertEqual(len(eye.picture()["my_kills_this_mission"]), 2)

    def test_pilot_report_confirms_likely_kill(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 75
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        self.assertIsNone(eye.pilot_report("Athena, is the target destroyed?"))
        out = eye.pilot_report("Athena, it's enemy armor vehicle dead.")
        self.assertEqual(out["confirmed"], "T-55")
        pic = eye.picture()
        self.assertEqual(pic["my_kills_this_mission"][0]["confidence"], "confirmed")
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-55"])

    def test_pilot_report_without_vanish_records_kill_on_last_attack(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 5
        out = eye.pilot_report("Athena, splash one T-55")
        self.assertEqual(out["how"], "pilot report on my last attack")
        pic = eye.picture()
        self.assertEqual(pic["my_kills_this_mission"][0]["how"], "pilot report")
        self.assertEqual(pic["my_shots_last_15_min"]["shots"][0]["result"], "kill")
        self.assertEqual(eye.pilot_report("target destroyed")["how"], "already confirmed")

    def test_controller_routes_kill_report_to_eye_and_hint(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 75
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))
        seen = []

        def llm(messages):
            seen.append(messages)
            return "Prometheus, Athena, copy, splash one T-55."
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm)
        list(c.stream(req("Athena, target destroyed")))
        system = [m["content"] for m in seen[0] if m["role"] == "system"]
        self.assertIn(CLASS_HINTS["kill_report"], system)
        self.assertFalse(any(s.startswith("DEV NOTE") for s in system))
        pic = json.loads([s for s in system if s.startswith("PICTURE ")][0][8:])
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-55"])
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        self.assertEqual(tool["result"]["call_class"], "kill_report")
        self.assertEqual(tool["result"]["pilot_report"]["confirmed"], "T-55")


class RawEventDebugTests(unittest.TestCase):
    def test_counts_samples_per_minute_and_durable_combat_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            eye, clock = a10_eye(stores_dir=tmp)
            with self.assertLogs("athena.eye", level="INFO") as logs:
                for _ in range(3):
                    eye.on_event({"time": 1.0, "simulation_fps": {"average": 60.0}})
                    eye.on_event(fx.hit(A10, 7, "GAU_8", T55))
                    clock.t += 1
                clock.t += 60
                eye.on_event(fx.hit(A10, 7, "GAU_8", T55))
            samples = [line for line in logs.output if "raw event sample" in line]
            self.assertEqual(sum("sample hit" in s for s in samples), 2)  # t=1000 and t=1063
            self.assertEqual(sum("sample simulation_fps" in s for s in samples), 1)
            self.assertTrue(any("raw events last" in line and '"hit":3' in line for line in logs.output))
            h = eye.stream_health()
            self.assertEqual(h["kinds_total"], {"hit": 4, "simulation_fps": 3})
            durable = [r for r in store_rows(tmp) if r["kind"] == "raw_event_sample"]
            self.assertEqual([r["event"] for r in durable], ["hit", "hit"])
            self.assertIn("GAU_8", durable[0]["sample"])

    def test_proto_shapes_of_death_events_map_to_the_eye(self):
        """Real DCS-gRPC protobuf: hit/kill with unit|static|weapon targets, dead/unit_lost with initiator only."""
        try:
            from google.protobuf.json_format import MessageToDict, ParseDict
            from dcs.mission.v0 import mission_pb2
        except ImportError:
            self.skipTest("DCS-gRPC protobuf modules not on PYTHONPATH")
        static = {"id": 77, "type": "FARP Tent", "name": "Tent 1", "coalition": "COALITION_RED",
                  "position": {"lat": fx.LAT0, "lon": fx.LON0 + 0.01, "alt": 900.0}}
        msgs = [fx.hit(A10, 9, "AGM_65D", T55), fx.kill(A10, 9, "AGM_65D", T55),
                {"time": 3.0, "kill": {"initiator": {"unit": A10}, "target": {"static": static},
                                       "weapon_name": "GAU_8"}},
                {"time": 4.0, "hit": {"initiator": {"weapon": fx.weapon(5, "9M38M1")}, "target": {"unit": A10},
                                      "weapon_name": "9M38M1"}},
                {"time": 5.0, "dead": {"initiator": {"unit": T55}}},
                {"time": 6.0, "unit_lost": {"initiator": {"unit": T55_B}}}]
        eye, _ = a10_eye()
        kinds = []
        for m in msgs:
            back = MessageToDict(ParseDict(m, mission_pb2.StreamEventsResponse()), preserving_proto_field_name=True)
            ev = eye.on_event(back)
            kinds.append((ev["kind"], (ev["initiator"] or {}).get("kind"), (ev["target"] or {}).get("kind")))
        self.assertEqual(kinds, [("hit", "unit", "unit"), ("kill", "unit", "unit"), ("kill", "unit", "static"),
                                 ("hit", "weapon", "unit"), ("dead", "unit", None), ("unit_lost", "unit", None)])
        self.assertEqual(eye.stream_health()["kinds_total"],
                         {"hit": 2, "kill": 2, "dead": 1, "unit_lost": 1})


# ============================================================================= 2. fuel
class FuelTests(unittest.TestCase):
    def test_cockpit_fuel_units(self):
        self.assertEqual(cp.cockpit_fuel({"fuel_int": 2500, "fuel_ext": 1200}),
                         {"internal_lb": 5510, "external_lb": 2650, "total_lb": 8160, "total_kg": 3700})
        a10 = cp.cockpit_fuel({"ac": "A-10C_2", "fu": 2, "fuel_int": 0.3969, "fuel_ext": 0})
        self.assertAlmostEqual(a10["total_lb"], 4400, delta=20)   # the gauge read 4,400 lb
        f4 = cp.cockpit_fuel({"ac": "F-4E-45MC", "fu": 2, "fuel_int": 0.3237, "fuel_ext": 0.5})
        self.assertAlmostEqual(f4["total_lb"], 3903, delta=20)    # the gauge read 3,903 lb
        self.assertEqual(f4["external_pct"], 50)
        self.assertIn("approximate", f4["source"])
        self.assertIsNone(cp.cockpit_fuel({"ac": "A-10C_2", "fuel_int": 0, "fuel_ext": 0}))  # v1 rounded fraction
        self.assertIsNone(cp.cockpit_fuel({"ac": "A-10C_2", "fuel_int": 1, "fuel_ext": 0}))  # v1, ambiguous
        self.assertIsNone(cp.cockpit_fuel({"ac": "A-10C_2", "fu": 2, "fuel_int": 0}))
        self.assertIsNone(cp.cockpit_fuel({"ac": "A-10C_2"}))
        odd = cp.cockpit_fuel({"ac": "Bf-109K-4", "fu": 2, "fuel_int": 0.5})
        self.assertEqual((odd["internal_pct"], "total_lb" in odd), (50, False))

    def test_zero_cockpit_fuel_falls_back_to_getfuel_times_airframe_max(self):
        eye, clock = f4e_eye(extra=())
        eye.on_cockpit_packet(f4e_packet())
        self.assertEqual(eye.picture()["ownship"]["fuel"]["known"], False)
        eye.on_stores(norm_unit(F4E), {"fuel": 0.3237})  # FUEL_LUA result
        fuel = eye.picture()["ownship"]["fuel"]
        self.assertAlmostEqual(fuel["total_lb"], 3903, delta=20)
        self.assertIn("getFuel", fuel["source"])
        self.assertNotIn("fuel", eye.picture()["cockpit"])
        self.assertIn("fuel_note", eye.picture()["cockpit"])

    def test_good_cockpit_fuel_wins(self):
        eye, clock = a10_eye()
        eye.on_cockpit_packet({"v": 1, "seq": 1, "ac": "A-10C_2", "unit": A10["name"], "coal": 2, "lat": fx.LAT0,
                               "lon": fx.LON0, "alt": 914.0, "hdg": 90.0, "fu": 2, "fuel_int": 0.3969,
                               "fuel_ext": 0})
        eye.on_stores(norm_unit(A10), {"fuel": 0.2})
        fuel = eye.picture()["ownship"]["fuel"]
        self.assertAlmostEqual(fuel["total_lb"], 4400, delta=20)
        self.assertEqual(eye.summary()["fuel_lb"], fuel["total_lb"])

    @unittest.skipUnless(LUA and os.path.exists(os.path.join(KIT, "AthenaExport.lua")), "lua5.1 or kit missing")
    def test_lua_export_sends_unrounded_fuel_and_no_rwr_module(self):
        out = subprocess.run([LUA, os.path.join(HERE, "athena_export_harness.lua"),
                              os.path.join(KIT, "AthenaExport.lua"), "fraction"],
                             capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
        self.assertIn("ERRORS 0", out)
        pkts = [cp.parse_packet(line[4:]) for line in out if line.startswith("PKT ")]
        data = [p for p in pkts if "ev" not in p]
        self.assertEqual((data[0]["fuel_int"], data[0]["fu"]), (0.3969, 2))
        self.assertNotIn("rwr", data[0])
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        for p in pkts[:2]:
            eye.on_cockpit_packet(p)
        self.assertAlmostEqual(eye.picture()["cockpit"]["fuel"]["total_lb"], 4400, delta=20)
        self.assertEqual(eye.cockpit.rwr_modules_json(), {"A-10C_2": False})


# ============================================================================= 3. RWR fallback
class GrpcThreatTests(unittest.TestCase):
    def test_sam_in_range_without_cockpit_rwr(self):
        eye, clock = f4e_eye()
        eye.on_cockpit_packet(f4e_packet())
        eye.tick()  # phase 1: SAM alerts are raised by the feeder's tick, not by drawing the picture
        pic = eye.picture()
        th = pic["threats"]
        self.assertEqual(th["rwr_source"], "grpc_inferred")
        self.assertEqual(th["rwr_export_by_module"], {"F-4E-45MC": False})
        sams = th["sam_in_range"]
        self.assertEqual(sams[0]["sam"], "SA-11")
        self.assertTrue(sams[0]["call"].startswith("SAM SA-11 could be tracking you, your twelve o'clock, ten miles"),
                        sams[0]["call"])  # heading 090, SAM bearing 100
        self.assertNotIn("SA-10 Far", [s["group"] for s in sams])        # 60 nm > 40 nm
        self.assertNotIn("AAA", [s["group"] for s in sams])              # Shilka 20 nm > 1.5 nm
        self.assertTrue(any(s["ewr"] for s in sams))                     # EWR listed, last
        self.assertEqual(pic["rwr_alert_to_announce"], [sams[0]["call"]])  # SAM announced, EWR not
        eye.mark_announced(pic["_rwr_keys"])
        self.assertNotIn("rwr_alert_to_announce", eye.picture())         # once per SAM group

    def test_cockpit_rwr_present_means_no_sam_announcements(self):
        eye, clock = f4e_eye()
        eye.on_cockpit_packet(f4e_packet(ac="F-16C_50", rwr={"mode": 0, "e": []}))
        pic = eye.picture()
        self.assertEqual(pic["threats"]["rwr_source"], "cockpit")
        self.assertTrue(pic["threats"]["sam_in_range"])
        self.assertNotIn("rwr_alert_to_announce", pic)

    def test_no_feed_at_all_is_none(self):
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        self.assertEqual(eye.picture()["threats"]["rwr_source"], "none")

    def hostile_shot(self, heading, weapon="SA9M38M1", initiator=SA11_LN, wid=900):
        lat, lon = fx.offset(101, 10.2)
        return {"time": 50.0, "shot": {"initiator": {"unit": initiator}, "weapon": {
            "id": wid, "type": weapon, "position": {"lat": lat, "lon": lon, "alt": 400.0},
            "velocity": {"heading": heading, "speed": 700.0}}}}

    def test_hostile_missile_shot_closing_is_a_launch_alert(self):
        eye, clock = f4e_eye(extra=(SA11_LN,))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(self.hostile_shot(heading=281.0))  # towards Prometheus (reciprocal of 101)
        pic = eye.picture()
        self.assertEqual(pic["rwr_alert_to_announce"][0], "Missile launch, SA9M38M1, your twelve o'clock, ten miles.")
        self.assertEqual(pic["threats"]["missiles_inbound"][0]["from"], "shot event")

    def test_hostile_missile_flying_away_or_bomb_is_no_alert(self):
        eye, clock = f4e_eye(extra=(SA11_LN,))
        eye.on_event(self.hostile_shot(heading=101.0))
        eye.on_event(self.hostile_shot(heading=281.0, weapon="FAB_500", wid=901))
        self.assertEqual(eye.picture()["threats"]["missiles_inbound"], [])

    def test_weapons_in_flight_from_eval(self):
        eye, clock = f4e_eye(extra=())
        lat, lon = fx.offset(180, 8)
        rows = [{"id": 5001, "type": "SA9M38M1", "lat": lat, "lon": lon, "alt": 2000, "vn": 600, "ve": 0,
                 "coal": 1, "target": "Aerial-3-2", "launcher": "SAM LN", "launcher_type": "SA-11 Buk LN 9A310M1"},
                {"id": 5002, "type": "AIM_120C", "lat": lat, "lon": lon, "alt": 2000, "vn": 600, "ve": 0,
                 "coal": 2, "target": "Aerial-3-2"},                                   # friendly: ignored
                {"id": 5003, "type": "R-27R", "lat": lat, "lon": lon, "alt": 2000, "vn": -600, "ve": 0,
                 "coal": 1, "target": "Someone else"}]                                 # going away, not at me
        new = eye.on_weapons(rows)
        self.assertEqual([a["call"] for a in new], ["Missile launch, SA9M38M1, your three o'clock, eight miles."])
        self.assertEqual(eye.on_weapons(rows), [])  # same missile: no second alert
        pic = eye.picture()
        inbound = pic["threats"]["missiles_inbound"]
        self.assertEqual((len(inbound), inbound[0]["targeting_me"]), (1, True))
        self.assertEqual(pic["rwr_alert_to_announce"][0], new[0]["call"])

    def test_feeder_poll_weapons_uses_eval(self):
        eye, clock = f4e_eye(extra=())
        lat, lon = fx.offset(90, 5)
        calls = []

        class Stub:
            def Eval(self, request, timeout=None):
                calls.append(request.lua)
                return type("R", (), {"json": json.dumps({"weapons": [
                    {"id": 1, "type": "R-73", "lat": lat, "lon": lon, "vn": 0, "ve": -500, "coal": 1}]})})()
        f = GrpcFeeder(eye, "127.0.0.1", 1)
        alerts = f.poll_weapons(Stub())
        self.assertIn("world.searchObjects", calls[0])
        self.assertIn('"Aerial-3-2"', calls[0])
        self.assertEqual(alerts[0]["call"], "Missile launch, R-73, your twelve o'clock, five miles.")

    def test_sam_profile_table(self):
        self.assertEqual(sam_profile("SA-11 Buk SR 9S18M1")[:2], (19.0, "SA-11"))
        self.assertEqual(sam_profile("S-300PS 5P85C ln")[1], "SA-10")
        self.assertEqual(sam_profile("Kub 1S91 str")[1], "SA-6")
        self.assertEqual(sam_profile("Osa 9A33 ln")[1], "SA-8")
        self.assertEqual(sam_profile("1L13 EWR")[2], True)
        self.assertEqual(sam_profile("Igla manpad INS")[1], None)


# ============================================================================= 4 + 5. verbosity and dev notes
class CallClassTests(unittest.TestCase):
    def test_classes(self):
        cases = {
            "Athena, copy.": "ack", "Egressing.": "ack", "Athena, going to investigate": "ack",
            "Athena, eyes on target": "ack", "Wheels up": "ack", "Athena, switching missions": "ack",
            "Athena, it's enemy armor vehicle dead.": "kill_report", "Splash one T-55": "kill_report",
            "target destroyed": "kill_report", "Is the target destroyed?": None,
            "Athena, feature request: call my bingo fuel": "dev_note",
            "Note for development, the picture is too long": "dev_note",
            "Athena should be able to read my fuel": "dev_note", "bug report, fuel says zero": "dev_note",
            "Athena, what's the picture?": "picture", "Athena, bearing to enemy armor": None,
            "bearing to all enemy armor": None, "Athena, fuel state": None, "copy, what's my fuel?": None,
        }
        for text, want in cases.items():
            self.assertEqual(call_class(text), want, text)

    def test_fit_reply_cuts_at_sentence_boundary(self):
        s = "Prometheus, Athena, picture. " + " ".join("Group %d is far away to the north." % i for i in range(20))
        out = fit_reply(s)[0]
        self.assertLessEqual(len(out), MAX_REPLY_CHARS)
        self.assertTrue(out.endswith("."))
        self.assertIn(out, s)
        self.assertEqual(fit_reply("Short.")[0], "Short.")
        self.assertTrue(fit_reply("word " * 200)[0].endswith("."))

    def test_ack_gets_hint_and_short_answer(self):
        eye, clock = a10_eye()
        seen = []
        long_answer = ("Prometheus, Athena, copy. Enemy armor, bearing zero nine zero, one mile, ground target. "
                       "Target is at your twelve o'clock. " + "Another group is out there somewhere far away. " * 6)

        def llm(messages):
            seen.append(messages)
            return long_answer
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm)
        parts = list(c.stream(req("Athena, egressing")))
        system = [m["content"] for m in seen[0] if m["role"] == "system"]
        self.assertIn(CLASS_HINTS["ack"], system)
        spoken = " ".join(p["speech"] for p in parts)
        self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)
        self.assertTrue(spoken.endswith("."))

    def test_dev_note_hint_only_on_explicit_phrasing(self):
        eye, clock = a10_eye()
        seen = []

        def llm(messages):
            seen.append(messages)
            return "Prometheus, Athena, copy."
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm)
        list(c.stream(req("Athena, switching missions")))
        list(c.stream(req("Athena, feature request: call my bingo", tid="t2")))
        dev = [[m["content"] for m in msgs if m["content"].startswith("DEV NOTE")] for msgs in seen]
        self.assertEqual([len(d) for d in dev], [0, 1])
        self.assertIn('ONLY when a "DEV NOTE" system hint is present', PROMPT)
        self.assertIn("never noted for development", PROMPT)

    def test_working_target_and_picture_top3(self):
        eye, clock = a10_eye(extra=(SA11_SR, SA11_LN, SA10_FAR))
        su27 = fx.unit(901, "Flanker", "Su-27", "red", "Red CAP", "AIRPLANE", *fx.offset(0, 40), 7000.0, 180.0, 250.0)
        eye.on_units_message(fx.units_message(su27))
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        pic = eye.picture()
        self.assertTrue(pic["working_target"]["call"].startswith("T-55, bearing zero nine zero"))
        top = pic["picture_top3"]
        self.assertEqual(len(top), 1)  # round 5: air only within 50 nm, never pad with SAMs
        self.assertTrue(top[0].startswith("Su-27, ") and "single" not in top[0])
        self.assertNotIn("picture_more_groups", pic)
        self.assertIn("picture_top3", PROMPT)
        self.assertIn("NEAREST matching group only", PROMPT)


# ============================================================================= 6. whisper prompt
class WhisperPromptTests(unittest.TestCase):
    def test_brevity_words_and_airfields(self):
        eye, _ = a10_eye()
        text = eye.whisper_vocabulary()
        for word in ("eyes on", "splash", "winchester", "bingo", "egress", "ingress", "RTB", "magnum", "rifle",
                     "fox two", "tally", "no joy", "BRAA", "picture", "Maverick", "APKWS", "GBU"):
            self.assertIn(word.lower(), text.lower(), word)
        self.assertIn("Kutaisi", text)
        self.assertIn(BREVITY_PROMPT, text)

    def test_feeder_writes_prompt_file(self):
        eye, _ = a10_eye()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "whisper-prompt-dynamic.txt")
            f = GrpcFeeder(eye, "127.0.0.1", 1, prompt_path=path)
            self.assertTrue(f.write_prompt())
            with open(path, encoding="utf-8") as fh:
                self.assertIn("Eyes on target", fh.read())


# ============================================================================= 7. busy maps
def busy_units(n=1500, seed=7):
    """Syria-sized fixture around Prometheus: armor/SAM/AAA groups, CAP flights, friendlies, far units."""
    rng = random.Random(seed)
    units = [fx.PROMETHEUS]
    uid = 1000
    kinds = [("T-72B", "GROUND"), ("BMP-2", "GROUND"), ("Ural-375", "GROUND"), ("SA-11 Buk LN 9A310M1", "GROUND"),
             ("ZSU-23-4 Shilka", "GROUND"), ("Osa 9A33 ln", "GROUND"), ("Su-27", "AIRPLANE"), ("MiG-29S", "AIRPLANE")]
    while len(units) < n:
        type_name, gcat = rng.choice(kinds)
        coal = "red" if rng.random() < 0.8 else "blue"
        far = rng.random() < 0.2
        brg, dist = rng.uniform(0, 360), rng.uniform(85, 200) if far else rng.uniform(1, 79)
        size = 2 if gcat == "AIRPLANE" else rng.randint(4, 15)
        group = "G%d" % uid
        for k in range(size):
            if len(units) >= n:
                break
            lat, lon = fx.offset(brg + rng.uniform(-0.5, 0.5), dist + rng.uniform(-0.3, 0.3))
            alt = 7000.0 if gcat == "AIRPLANE" else 300.0
            units.append(fx.unit(uid, "%s-%d" % (group, k), type_name, coal, group, gcat, lat, lon, alt,
                                 rng.uniform(0, 360), 200.0 if gcat == "AIRPLANE" else 0.0))
            uid += 1
    return units


def naive_clusters(eye, own, units):
    """The round-3 O(n^2) clustering, kept as the reference for the grid version."""
    rows = []
    for u in units:
        brg, rng = bearing_range(own["lat"], own["lon"], u["lat"], u["lon"])
        if rng <= CONTACT_RADIUS_NM:
            rows.append((rng, brg, u))
    rows.sort(key=lambda r: r[0])
    groups, used = [], set()
    for i, (rng, brg, u) in enumerate(rows):
        if u["id"] in used:
            continue
        used.add(u["id"])
        dom = domain(u["category"])
        members = [u["id"]]
        radius = 3.0 if dom != "ground" else 1.5
        for rng2, brg2, v in rows[i + 1:]:
            if v["id"] in used or v["coalition"] != u["coalition"] or domain(v["category"]) != dom:
                continue
            if bearing_range(u["lat"], u["lon"], v["lat"], v["lon"])[1] > radius:
                continue
            if dom == "ground" and u["category"] != v["category"] and (
                    {u["category"], v["category"]} & {"SAM", "AAA"}):
                continue  # round 9 review: air defence never joins other categories
            if dom == "air" and abs((v["alt_m"] or 0) - (u["alt_m"] or 0)) * M_TO_FT > 5000:
                continue
            used.add(v["id"])
            members.append(v["id"])
        groups.append(members)
    return groups


class BusyMapTests(unittest.TestCase):
    def setUp(self):
        self.eye = Eye("Prometheus", clock=Clock(), source="fixture")
        self.eye.set_connected(True)
        for u in busy_units():
            self.eye.on_units_message(fx.units_message(u))
        self.eye.set_airbases(fx.AIRBASES)

    def test_picture_p95_under_50ms_and_small(self):
        self.assertEqual(len(self.eye.units), 1500)
        self.eye.picture()  # warm up
        times = []
        for _ in range(40):
            t0 = time.perf_counter()
            pic = self.eye.picture()
            times.append(time.perf_counter() - t0)
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        self.assertLess(p95, 0.050, "picture p95 %.1f ms" % (p95 * 1000))
        self.assertLessEqual(len(pic["hostile_groups"]), 15 + 6)
        self.assertLessEqual(len(pic["friendly_groups"]), 8)
        self.assertEqual(len(pic["picture_top3"]), 3)
        self.assertGreater(pic["hostile_groups_total"], len(pic["hostile_groups"]))
        self.assertTrue(pic["more_hostile_groups_not_listed"])
        self.assertLessEqual(len(pic["threats"]["sam_in_range"]), 3)
        size = len(json.dumps(pic, default=str))
        self.assertLess(size, 60000, size)
        ranges = [g["range_nm"] for g in pic["hostile_groups"][:15]]
        self.assertEqual(ranges, sorted(ranges))  # nearest first
        print("\nbusy map: 1500 units, picture p50 %.1f ms p95 %.1f ms, %d bytes, %d hostile groups" % (
            times[len(times) // 2] * 1000, p95 * 1000, size, pic["hostile_groups_total"]))

    def test_grid_clustering_matches_reference(self):
        own = self.eye.ownship()
        others = [u for u in self.eye.units.values() if u["id"] != own["id"]]
        grid = [[m[2]["id"] for m in members] for _, members in self.eye._cluster(own, others)]
        self.assertEqual(sorted(map(sorted, grid)), sorted(map(sorted, naive_clusters(self.eye, own, others))))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
