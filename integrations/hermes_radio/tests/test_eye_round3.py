"""Athena Eye round 3 (2026-11-07): cockpit feed from DCS Export.lua (AthenaExport.lua -> UDP 7778).

Covers packet parsing, RWR clock math and alerts, payload -> stores (with the round-2 diff/attribution),
cockpit/eye merging (ownship identity, locked target, intended target), prompt focus routing, the real
UDP listener, and the Lua script itself under lua5.1 with a stubbed DCS Export API.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from integrations.hermes_radio import cockpit as cp
from integrations.hermes_radio.contract import Request
from integrations.hermes_radio.eye import Eye, GrpcFeeder, clock_of, norm_unit
from integrations.hermes_radio.simple_controller import PROMPT, SimpleController, focus_hint, focus_of
from integrations.hermes_radio.tests import eye_fixture as fx

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.environ.get("ATHENA_CLIENT_KIT",
                     os.path.expanduser("~/Ai-Workspace/projects/project-athena/infra/dcs-client"))
LUA = shutil.which("lua5.1") or shutil.which("luajit")
OWN = norm_unit(fx.PROMETHEUS)  # the normalized unit dict the eye keeps for Prometheus


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def station(i, name, count, levels=(4, 4, 7, 24)):
    return {"i": i, "n": name, "c": count, "l": list(levels)}


AMRAAM = (4, 4, 7, 24)
AIM9 = (4, 4, 7, 7)
GBU12 = (4, 5, 36, 38)
TANK = (1, 3, 43, 11)


def packet(seq=1, **over):
    """A full slow packet for Prometheus' F-16 at the fixture origin, heading 090."""
    p = {"v": 1, "seq": seq, "t": 100.0 + seq * 0.2, "ac": "F-16C_50", "unit": "Prometheus-1", "group": "Viper 1",
         "coal": 2, "pilot": "Prometheus", "lat": fx.LAT0, "lon": fx.LON0, "alt": 6096.0, "hdg": 90.0,
         "hdg_mag": 84.0, "pitch": 2.0, "bank": -10.0, "ias": 180.0, "tas": 230.0, "gs": 250.0, "mach": 0.72,
         "alt_msl": 6096.0, "agl": 5900.0, "vs": -5.08, "aoa": 4.6 * 57.29578, "g": 4.5, "fuel_int": 2500, "fuel_ext": 1200,
         "rpm": [88.5, 0],
         "rwr": {"mode": 0, "e": []},
         "pay": {"cur": 3, "gun": 510, "st": [station(1, "AIM-120C", 1, AMRAAM), station(2, "AIM-120C", 1, AMRAAM),
                                             station(3, "GBU-12", 2, GBU12), station(4, "AIM-9X", 0, AIM9),
                                             station(5, None, 1, TANK)]},
         "mech": {"gear": 1, "gear_st": 1, "flaps": 0.5, "sb": 0, "hook": 0},
         "warn": ["FuelLow", "MasterWarning"]}
    p.update(over)
    return p


def emitter(eid, name, sig, az, pw=0.5, pr=100):
    return {"id": eid, "type": name, "sig": sig, "az": az, "pw": pw, "pr": pr}


def scenario(clock=None, stores_dir=None, units=None):
    clock = clock or Clock()
    eye = Eye("Prometheus,Player,Client", clock=clock, source="fixture", stores_dir=stores_dir)
    eye.set_connected(True)
    for u in (fx.UNITS if units is None else units):
        eye.on_units_message(fx.units_message(u))
    eye.set_airbases(fx.AIRBASES)
    return eye, clock


# ----------------------------------------------------------------------------- parsing
class PacketParsingTests(unittest.TestCase):
    def test_parse_valid_and_reject_garbage(self):
        self.assertEqual(cp.parse_packet(b'{"v":1,"seq":3}')["seq"], 3)
        self.assertEqual(cp.parse_packet('{"v":1}'), {"v": 1})
        for bad in (b"not json", b'{"v":2}', b"[1,2]", b"", b"\xff\xfe", None, b'"v"'):
            self.assertIsNone(cp.parse_packet(bad), bad)

    def test_payload_to_stores_aggregates_and_skips_tanks_and_empty(self):
        snap = cp.payload_to_stores(packet()["pay"])
        by = {a["name"]: a for a in snap["ammo"]}
        self.assertEqual(by["AIM-120C"]["count"], 2)
        self.assertEqual(by["AIM-120C"]["category"], 1)  # missile
        self.assertEqual(by["GBU-12"]["category"], 3)    # bomb
        self.assertEqual(by["gun rounds"], {"type": "gun", "name": "gun rounds", "count": 510, "category": 0})
        self.assertNotIn("AIM-9X", by)                    # count 0
        self.assertEqual(len(snap["ammo"]), 3)            # fuel tank skipped
        self.assertIsNone(snap["fuel"])

    def test_payload_lua_table_shapes(self):
        # Empty Lua table encodes as [] ; a sparse station table may arrive as an object.
        self.assertEqual(cp.payload_to_stores({"st": [], "gun": 0})["ammo"], [])
        snap = cp.payload_to_stores({"st": {"1": station(1, "Mk-82", 4, (4, 5, 9, 31))}})
        self.assertEqual(snap["ammo"][0]["count"], 4)
        rockets = cp.payload_to_stores({"st": [station(2, "Hydra 70 M151", 7, (4, 7, 33, 147))]})
        self.assertEqual(rockets["ammo"][0]["category"], 2)
        self.assertIsNone(cp.payload_to_stores(None))

    def test_signal_types(self):
        self.assertEqual(cp.signal_of("scan"), "search")
        self.assertEqual(cp.signal_of("track_while_scan"), "track")
        self.assertEqual(cp.signal_of("lock"), "lock")
        self.assertEqual(cp.signal_of("missle_radio_guided"), "launch")  # DCS spelling
        self.assertEqual(cp.signal_of("missile_radio_guided"), "launch")
        self.assertEqual(cp.signal_of(None), "search")


# ----------------------------------------------------------------------------- RWR clock math
class RwrClockTests(unittest.TestCase):
    def test_cardinal_and_negative_azimuths(self):
        cases = {0: 12, 14.9: 12, 15: 1, 30: 1, 45: 2, 90: 3, 135: 5, 180: 6, -180: 6, -90: 9, -45: 11, -15: 12,
                 -16: 11, 270: 9, 359: 12, 720 + 90: 3}
        for az, hour in cases.items():
            self.assertEqual(cp.rwr_clock(az), hour, az)

    def test_agrees_with_eye_clock(self):
        for az in range(-360, 361, 7):
            self.assertEqual(cp.rwr_clock(az), clock_of(az), az)

    def test_threats_sorted_with_clock_calls(self):
        st = cp.CockpitState(clock=Clock())
        st.ingest(packet(rwr={"mode": 0, "e": [emitter(1, "Su-27", "scan", -90, pw=0.9),
                                              emitter(2, "SA-11 Buk SR 9S18M1", "lock", 90),
                                              emitter(3, "SA-11 Buk LN 9A310M1", "missle_radio_guided", 100)]}))
        rwr = st.to_picture()["rwr"]
        self.assertEqual([t["signal"] for t in rwr["threats"]], ["launch", "lock", "search"])
        self.assertEqual(rwr["highest"], "launch")
        self.assertEqual(rwr["threats"][1]["clock"], 3)
        self.assertEqual(rwr["threats"][1]["call"], "SA-11 Buk SR 9S18M1 locked on you at your three o'clock")
        self.assertEqual(rwr["threats"][2]["call"], "Su-27 searching at your nine o'clock")
        self.assertEqual(rwr["count"], 3)

    def test_clean_and_unavailable(self):
        st = cp.CockpitState(clock=Clock())
        st.ingest(packet())
        self.assertEqual(st.to_picture()["rwr"]["note"], "RWR clean")
        p = packet()
        del p["rwr"]
        st.ingest(p)
        self.assertFalse(st.to_picture()["rwr"]["available"])


# ----------------------------------------------------------------------------- cockpit state
class CockpitStateTests(unittest.TestCase):
    def test_picture_fields(self):
        st = cp.CockpitState(clock=Clock())
        st.ingest(packet())
        pic = st.to_picture()
        self.assertTrue(pic["fresh"])
        self.assertEqual(pic["aircraft"], "F-16C_50")
        self.assertEqual(pic["config"], {"gear": "down", "flaps_pct": 50, "speedbrake": "in", "hook": "up"})
        f = pic["flight"]
        self.assertEqual(f["ias_kt"], 350)
        self.assertEqual(f["gs_kt"], 486)
        self.assertEqual(f["agl_ft"], 19360)
        self.assertEqual(f["alt_msl_ft"], 20000)
        self.assertEqual(f["vs_fpm"], -1000)
        self.assertEqual(f["g"], 4.5)
        self.assertEqual(f["aoa_deg"], 4.6)  # the export sends deg(degrees): R3 T11
        self.assertEqual(f["heading_mag"], 84)
        self.assertEqual(pic["fuel"], {"internal_lb": 5510, "external_lb": 2650, "total_lb": 8160, "total_kg": 3700})
        self.assertEqual(pic["payload"]["by_weapon"], {"AIM-120C": 2, "GBU-12": 2})
        self.assertEqual(pic["payload"]["gun_rounds"], 510)
        self.assertEqual(len(pic["payload"]["stations"]), 3)
        self.assertEqual(pic["warning_lights"], ["FuelLow", "MasterWarning"])

    def test_gear_states_and_slow_parts_persist(self):
        clock = Clock()
        st = cp.CockpitState(clock=clock)
        st.ingest(packet(mech={"gear": 0}))
        self.assertEqual(st.to_picture()["config"]["gear"], "up")
        fast = packet(seq=2)
        for k in ("pay", "mech", "warn"):
            del fast[k]
        clock.t += 0.2
        st.ingest(fast)  # fast packet: payload/mech kept from the last slow packet
        pic = st.to_picture()
        self.assertEqual(pic["config"]["gear"], "up")
        self.assertEqual(pic["payload"]["gun_rounds"], 510)
        st.ingest(packet(seq=3, mech={"gear": 0.4}))
        self.assertEqual(st.to_picture()["config"]["gear"], "in transit")

    def test_freshness_stale_and_stop(self):
        clock = Clock()
        st = cp.CockpitState(clock=clock)
        self.assertFalse(st.to_picture()["available"])
        self.assertIn("7778", st.to_picture()["reason"])
        st.ingest(packet())
        self.assertTrue(st.fresh())
        clock.t += 6
        self.assertFalse(st.fresh())
        self.assertFalse(st.to_picture()["fresh"])
        self.assertIn("flight", st.to_picture())
        clock.t += 60
        self.assertNotIn("flight", st.to_picture())
        self.assertIn("silent", st.to_picture()["note"])
        st.ingest(packet())
        self.assertTrue(st.fresh())
        st.ingest({"v": 1, "seq": 9, "ev": "stop"})
        self.assertFalse(st.fresh())
        self.assertIsNone(st.ownship())

    def test_rwr_alerts_new_lock_launch_only(self):
        clock = Clock()
        st = cp.CockpitState(clock=clock)
        search = packet(rwr={"e": [emitter(7, "SA-11 Buk SR 9S18M1", "scan", 90)]})
        self.assertEqual(st.ingest(search)["alerts"], [])
        lock = packet(rwr={"e": [emitter(7, "SA-11 Buk SR 9S18M1", "lock", 90)]})
        alerts = st.ingest(lock)["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["call"], "Spike, SA-11 Buk SR 9S18M1 at your three o'clock.")
        clock.t += 0.2
        self.assertEqual(st.ingest(lock)["alerts"], [])  # same lock: no repeat
        launch = packet(rwr={"e": [emitter(7, "SA-11 Buk SR 9S18M1", "missle_radio_guided", 95)]})
        clock.t += 0.2
        alerts = st.ingest(launch)["alerts"]
        self.assertEqual(alerts[0]["call"], "Missile launch, SA-11 Buk SR 9S18M1 at your three o'clock.")
        clock.t += 1
        st.ingest(packet(rwr={"e": []}))  # off the scope
        clock.t += 1
        self.assertEqual(len(st.ingest(lock)["alerts"]), 1)  # locks again later: new alert
        self.assertEqual(len(st.pending_alerts(set())), 3)
        clock.t += cp.ALERT_ANNOUNCE_S + 1
        self.assertEqual(st.pending_alerts(set()), [])


# ----------------------------------------------------------------------------- eye merge
class EyeCockpitMergeTests(unittest.TestCase):
    def test_locked_target_matched_to_unit_with_call(self):
        eye, _ = scenario()
        lat, lon = fx.SU27_1
        eye.on_cockpit_packet(packet(lock=[{"id": 55, "type": "Su-27", "dist": 37040, "cv": 250, "mach": 0.9,
                                            "lat": lat, "lon": lon, "alt": 7620}]))
        pic = eye.picture()
        lt = pic["cockpit"]["locked_target"]
        self.assertEqual(lt["unit"]["name"], "Red CAP 1-1")
        self.assertEqual(lt["side"], "hostile")
        self.assertEqual(lt["range_nm"], 20.0)
        self.assertEqual(lt["closure_kt"], 486)
        self.assertEqual(lt["clock"], "11 o'clock, ahead left")  # bearing 060, nose 090
        self.assertTrue(lt["call"].startswith("Locked Su-27, zero six zero, twenty, angels twenty five, "),
                        lt["call"])
        self.assertIn("eleven o'clock", lt["call"])

    def test_nothing_locked(self):
        eye, _ = scenario()
        eye.on_cockpit_packet(packet(lock=[]))
        self.assertIsNone(eye.picture()["cockpit"]["locked_target"])

    def test_cockpit_unit_name_identifies_ownship(self):
        # Two players, neither name in the list: the cockpit's unit name picks the right one.
        me = fx.unit(100, "Prometheus-1", "F-16C_50", "blue", "Viper 1", "AIRPLANE", fx.LAT0, fx.LON0, 6096.0, 90.0,
                     250.0, player="Somebody")
        other = fx.unit(601, "Viper 2-1", "F-16C_50", "blue", "Viper 2", "AIRPLANE", *fx.offset(200, 40), 5000.0,
                        180.0, 220.0, player="Wingman")
        eye, _ = scenario(units=[me, other])
        self.assertIsNone(eye.ownship())
        eye.on_cockpit_packet(packet())
        self.assertEqual(eye.ownship()["id"], 100)

    def test_pseudo_ownship_when_grpc_has_none(self):
        eye, _ = scenario(units=[])
        self.assertIsNone(eye.picture()["ownship"])
        eye.on_cockpit_packet(packet())
        own = eye.picture()["ownship"]
        self.assertEqual(own["type"], "F-16C_50")
        self.assertEqual(own["coalition"], "blue")
        self.assertEqual(own["alt_ft"], 20000)
        self.assertIn("cockpit feed only", own["source"])
        self.assertEqual(own["loadout"]["source"], "cockpit (Export.lua payload)")

    def test_cockpit_payload_is_loadout_and_drop_is_a_shot_on_locked_target(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            eye, _ = scenario(clock, stores_dir=tmp)
            lat, lon = fx.SU27_1
            lock = [{"id": 55, "type": "Su-27", "dist": 37040, "lat": lat, "lon": lon, "alt": 7620}]
            eye.on_cockpit_packet(packet(lock=lock))
            lo = eye.picture()["ownship"]["loadout"]
            self.assertEqual(lo["source"], "cockpit (Export.lua payload)")
            self.assertEqual({w["weapon"]: w["count"] for w in lo["weapons"]}, {"AIM-120C": 2, "GBU-12": 2})
            self.assertEqual(lo["gun_rounds"], 510)
            clock.t += 1
            pay = packet()["pay"]
            pay["st"][0]["c"] = 0  # one AMRAAM gone
            eye.on_cockpit_packet(packet(seq=6, pay=pay, lock=lock))
            pic = eye.picture()
            shot = pic["my_shots_last_15_min"]["shots"][-1]
            self.assertEqual(shot["weapon"], "AIM-120C")
            self.assertEqual(shot["seen_by"], "loadout change only (no shot event)")
            self.assertEqual(shot["intended_target"]["name"], "Red CAP 1-1")
            self.assertEqual(shot["intended_target"]["method"], "locked target in the cockpit")
            self.assertEqual(pic["my_weapons_used_this_mission"]["by_weapon"], {"AIM-120C": 1})
            with open(os.path.join(tmp, os.listdir(tmp)[0]), encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            kinds = [r["kind"] for r in rows]
            self.assertIn("stores_snapshot", kinds)
            self.assertIn("shot", kinds)
            self.assertEqual([r for r in rows if r["kind"] == "stores_change"][0]["source"], "cockpit")

    def test_shot_event_uses_cockpit_lock_as_intended_target(self):
        eye, _ = scenario()
        lat, lon = fx.BUK_SR
        eye.on_cockpit_packet(packet(lock=[{"id": 9, "type": "SA-11", "dist": 18520, "lat": lat, "lon": lon,
                                            "alt": 300}]))
        eye.on_event(fx.shot(fx.PROMETHEUS, 777, "AGM_88C"))
        shot = eye.picture()["my_shots_last_15_min"]["shots"][-1]
        self.assertEqual(shot["intended_target"]["name"], "SAM North SR")
        self.assertEqual(shot["intended_target"]["range_nm_at_launch"], 10.0)

    def test_eval_ignored_while_cockpit_payload_fresh_and_source_switch_is_baseline(self):
        clock = Clock()
        eye, _ = scenario(clock)
        eval_snap = {"fuel": 0.6, "ammo": [{"type": "weapons.missiles.AIM_120C", "name": "AIM-120C", "count": 2,
                                            "category": 1}]}
        eye.on_cockpit_packet(packet())
        self.assertEqual(eye.on_stores(OWN, eval_snap), [])
        self.assertEqual(eye.stores[100]["source"], "cockpit")
        clock.t += cp.PAYLOAD_FRESH_S + 1  # cockpit payload goes stale: Eval takes over without a fake diff
        self.assertEqual(eye.on_stores(OWN, eval_snap), [])
        self.assertEqual(eye.stores[100]["source"], "eval")
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["total"], 0)
        self.assertEqual(eye.fuel, 0.6)

    def test_gun_burst_over_several_payload_packets_is_one_shot(self):
        clock = Clock()
        eye, _ = scenario(clock)
        eye.on_cockpit_packet(packet())
        for gun in (450, 380, 320):
            clock.t += 1
            pay = packet()["pay"]
            pay["gun"] = gun
            eye.on_cockpit_packet(packet(pay=pay))
        shots = eye.picture()["my_shots_last_15_min"]["shots"]
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["weapon"], "gun, 190 rounds")
        self.assertEqual(shots[0]["count"], 190)

    def test_feeder_polls_only_fuel_for_ownship_while_cockpit_fresh(self):
        """Round 4: the cockpit payload stays the stores source, but ownship fuel still comes from getFuel()."""
        eye, _ = scenario()
        eye.on_cockpit_packet(packet())
        calls = []

        class Stub:
            def Eval(self, request, timeout=None):
                calls.append(request.lua)
                return type("R", (), {"json": json.dumps({"fuel": 0.5, "ammo": []})})()
        f = GrpcFeeder(eye, "127.0.0.1", 1)
        self.assertTrue(f.poll_stores(Stub()))
        own_calls = [c for c in calls if '"Prometheus-1"' in c]
        self.assertEqual(len(own_calls), 1)
        self.assertNotIn("getAmmo", own_calls[0])
        self.assertIn("getFuel", own_calls[0])
        self.assertEqual(eye.fuel, 0.5)

    def test_rwr_alert_announced_once_and_logged(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            eye, _ = scenario(clock, stores_dir=tmp)
            eye.on_cockpit_packet(packet(rwr={"e": [emitter(7, "SA-11 Buk SR 9S18M1", "lock", 90)]}))
            pic = eye.picture()
            self.assertEqual([a for a in pic["rwr_alert_to_announce"] if not a.startswith("Bandit")], ["Spike, SA-11 Buk SR 9S18M1 at your three o'clock."])
            eye.mark_announced(pic["_rwr_keys"])
            self.assertFalse([a for a in eye.picture().get("rwr_alert_to_announce", []) if not a.startswith("Bandit")])
            with open(os.path.join(tmp, os.listdir(tmp)[0]), encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            alert = [r for r in rows if r["kind"] == "rwr_alert"][0]
            self.assertEqual((alert["signal"], alert["clock"]), ("lock", 3))

    def test_summary_has_cockpit(self):
        eye, _ = scenario()
        self.assertEqual(eye.summary()["cockpit"], "n/a")
        eye.on_cockpit_packet(packet(rwr={"e": [emitter(7, "SA-11", "lock", 90)]}))
        s = eye.summary()["cockpit"]
        self.assertEqual((s["fresh"], s["rwr"], s["rwr_highest"], s["gear"], s["fuel_lb"]),
                         (True, 1, "lock", "down", 8160))


# ----------------------------------------------------------------------------- prompt routing
class FakeTranscript:
    def __init__(self):
        self.records = []
        self.lock = threading.Lock()

    def log(self, **record):
        with self.lock:
            self.records.append(record)
        return True


def req(text, tid="t1", speaker="player"):
    return Request(tid, text, speaker, 133000000, "AM", "2026-11-07T00:00:00Z", "2026-11-07T00:00:00Z")


class PromptRoutingTests(unittest.TestCase):
    def test_focus_of_questions(self):
        cases = {
            "Athena, what's painting me?": ["rwr"],
            "Athena, RWR check": ["rwr"],
            "Am I locked?": ["rwr"],
            "Is anyone locking me": ["rwr"],
            "I'm spiked, where is it": ["rwr"],
            "Athena, what am I locked on?": ["lock"],
            "what's my target": ["lock"],
            "Athena, fuel state": ["fuel"],
            "am I bingo": ["fuel"],
            "is my gear down?": ["gear"],
            "Athena, what do I have left": ["loadout"],
            "what's my airspeed": ["flight"],
            "Athena, radio check": [],
            "Athena, picture": [],
        }
        for text, want in cases.items():
            self.assertEqual(focus_of(text), want, text)
        self.assertIn("cockpit.rwr.threats", focus_hint("what's painting me"))
        self.assertIsNone(focus_hint("radio check"))

    def test_prompt_covers_cockpit_questions(self):
        for needle in ("what's painting me", "What am I locked on", "Fuel state", "Gear", "rwr_alert_to_announce",
                       "cockpit.fresh"):
            self.assertIn(needle, PROMPT)

    def test_controller_sends_focus_alert_once_and_logs_cockpit(self):
        eye, _ = scenario()
        eye.on_cockpit_packet(packet(rwr={"e": [emitter(7, "SA-11 Buk SR 9S18M1", "missle_radio_guided", -90)]}))
        seen = []

        def llm(messages):
            seen.append(messages)
            return "Prometheus, Athena, missile launch, SA-11 at your nine o'clock."
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm)
        list(c.stream(req("Athena, what's painting me?")))
        list(c.stream(req("Athena, fuel state", tid="t2")))
        pic1 = json.loads([m for m in seen[0] if m["content"].startswith("PICTURE ")][0]["content"][8:])
        pic2 = json.loads([m for m in seen[1] if m["content"].startswith("PICTURE ")][0]["content"][8:])
        # phase 1: the controller ticks the Eye, so the SA-11 range alert is raised too (the launch goes first)
        self.assertEqual([a for a in pic1["rwr_alert_to_announce"] if not a.startswith("Bandit")][0],
                         "Missile launch, SA-11 Buk SR 9S18M1 at your nine o'clock.")
        self.assertNotIn("_rwr_keys", pic1)
        self.assertFalse([a for a in pic2.get("rwr_alert_to_announce", []) if not a.startswith("Bandit")])  # announced once
        self.assertEqual(pic1["cockpit"]["rwr"]["threats"][0]["signal"], "launch")
        focus = [m["content"] for m in seen[0] if m["content"].startswith("FOCUS:")]
        self.assertEqual(len(focus), 1)
        self.assertIn("cockpit.rwr", focus[0])
        self.assertEqual(seen[0][-1], {"role": "user", "content": "Athena, what's painting me?"})
        self.assertIn("ownship.fuel", [m["content"] for m in seen[1] if m["content"].startswith("FOCUS:")][0])
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        summary = tool["result"]["summary"]
        self.assertEqual(summary["cockpit"]["rwr_highest"], "launch")
        self.assertEqual([a for a in summary["rwr_alert"] if not a.startswith("Bandit")][0], "Missile launch, SA-11 Buk SR 9S18M1 at your nine o'clock.")


# ----------------------------------------------------------------------------- UDP listener
class ListenerTests(unittest.TestCase):
    def test_udp_packets_reach_the_eye(self):
        eye = Eye("Prometheus", source="fixture")
        listener = cp.CockpitListener(eye, host="127.0.0.1", port=0, allowed={"127.0.0.1"}).start()
        self.assertIsNone(listener.error)
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.sendto(b"garbage", ("127.0.0.1", listener.port))
            tx.sendto(json.dumps(packet()).encode(), ("127.0.0.1", listener.port))
            tx.close()
            deadline = time.time() + 3
            while time.time() < deadline and (eye.cockpit.packets < 1 or eye.cockpit.bad < 1):
                time.sleep(0.02)
            self.assertEqual(eye.cockpit.packets, 1)
            self.assertEqual(eye.cockpit.bad, 1)
            self.assertEqual(eye.cockpit.src, "127.0.0.1")
            self.assertEqual(eye.picture()["cockpit"]["aircraft"], "F-16C_50")
        finally:
            listener.close()

    def test_bind_failure_is_reported_not_raised(self):
        eye = Eye("Prometheus", source="fixture")
        first = cp.CockpitListener(eye, host="127.0.0.1", port=0, allowed={"127.0.0.1"}).start()
        try:
            second = cp.CockpitListener(eye, host="127.0.0.1", port=first.port, allowed={"127.0.0.1"}).start()  # port taken
            self.assertIsNotNone(second.error)
            self.assertIsNone(second.thread)
            self.assertIn("listener failed", eye.cockpit.status)
            self.assertIn("7778", eye.picture()["cockpit"]["reason"])  # picture still renders
            second.close()
        finally:
            first.close()


# ----------------------------------------------------------------------------- the Lua script itself
@unittest.skipUnless(LUA and os.path.exists(os.path.join(KIT, "AthenaExport.lua")),
                     "lua5.1 or infra/dcs-client/AthenaExport.lua not available")
class AthenaExportLuaTests(unittest.TestCase):
    def run_harness(self, *extra):
        out = subprocess.run([LUA, os.path.join(HERE, "athena_export_harness.lua"),
                              os.path.join(KIT, "AthenaExport.lua"), *extra],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        return out.splitlines()

    def test_packets_parse_and_merge_and_callbacks_chain(self):
        lines = self.run_harness()
        self.assertIn("ACTIVITY true", lines)              # ActivityNextEvent untouched
        self.assertIn("PEER 10.90.10.112:7778", lines)
        self.assertEqual(lines.count("PREV after"), 12)    # previous AfterNextFrame runs every frame, once
        self.assertEqual(lines.count("PREV start"), 1)
        self.assertEqual(lines.count("PREV stop"), 1)
        self.assertIn("ERRORS 0", lines)
        pkts = [cp.parse_packet(line[4:]) for line in lines if line.startswith("PKT ")]
        self.assertTrue(all(pkts))
        self.assertEqual(pkts[0]["ev"], "start")
        self.assertEqual(pkts[-1]["ev"], "stop")
        data = [p for p in pkts if "ev" not in p]
        self.assertEqual(len(data), 3)                     # 12 frames at 20 fps = 0.6 s at 5 Hz
        self.assertIn("pay", data[0])                      # slow parts on the first packet
        self.assertNotIn("pay", data[1])
        self.assertAlmostEqual(data[0]["lat"], 36.2364567, places=6)
        self.assertEqual(data[0]["aoa"], 5)                # radians -> degrees
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        for p in pkts[:2]:
            eye.on_cockpit_packet(p)
        pic = eye.picture()["cockpit"]
        self.assertEqual(pic["payload"]["by_weapon"], {"AIM-120C": 2, "GBU-12": 2})
        self.assertEqual(pic["payload"]["gun_rounds"], 510)
        self.assertEqual(pic["config"]["gear"], "down")
        self.assertEqual(pic["config"]["flaps_pct"], 50)
        self.assertEqual(pic["fuel"]["total_lb"], 8160)
        self.assertEqual([(t["type"], t["signal"], t["clock"]) for t in pic["rwr"]["threats"]],
                         [("SA-11 Buk SR 9S18M1", "lock", 3), ("Su-27", "search", 11)])
        self.assertEqual(pic["warning_lights"], ["FuelLow", "MasterWarning"])
        self.assertEqual(pic["locked_target"]["type"], "Su-27")
        self.assertEqual(pic["locked_target"]["range_nm"], 20.0)
        self.assertEqual(eye.picture()["rwr_alert_to_announce"],
                         ["Spike, SA-11 Buk SR 9S18M1 at your three o'clock."])

    def test_nothing_exported_never_errors(self):
        lines = self.run_harness("nil")
        self.assertIn("ERRORS 0", lines)
        self.assertEqual(lines.count("PREV after"), 12)
        pkts = [cp.parse_packet(line[4:]) for line in lines if line.startswith("PKT ")]
        self.assertEqual([sorted(p) for p in pkts if "ev" not in p], [["seq", "v"]] * 3)
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        for p in pkts:
            eye.on_cockpit_packet(p)
        self.assertTrue(eye.picture()["cockpit"]["available"])


if __name__ == "__main__":
    unittest.main()
