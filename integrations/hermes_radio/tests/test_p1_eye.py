"""Phase 1 item 3 (#241) lane EYE: trustworthy DCS data.

Unit labels from DCS's own attributes (+ override table), one missile source with real ids, SAM warnings from
"is that radar tracking me", dead units cleared after a reconnect, per-stream health, a stalled/dead event feed
noticed, a side-effect-free picture, fuel/AoA numbers and the UDP 7778 allowlist. Everything is offline: the
read-only Lua chunks run in lua5.1 against a stub DCS API (when lua5.1 is installed); the chunks themselves still
need a live flight.
"""
import copy
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from integrations.hermes_radio import cockpit as cp
from integrations.hermes_radio import eye as eye_mod
from integrations.hermes_radio.eye import EVENTS_STALE_S, Eye, GrpcFeeder, classify, norm_unit, sam_profile, weapon_kind
from integrations.hermes_radio.memory_link import MemoryLink
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye_round4 import F4E, SA11_LN, SA11_SR, T55, Clock, f4e_eye, f4e_packet

LUA = shutil.which("lua5.1") or shutil.which("luajit")
GROUND = "GROUP_CATEGORY_GROUND"


class TypeInfoIsolation(unittest.TestCase):
    """TYPE_INFO / DCS_FUEL_MAX_KG are per-type DCS facts shared by every Eye: start and end each test empty."""

    def setUp(self):
        eye_mod.TYPE_INFO.clear()
        cp.DCS_FUEL_MAX_KG.clear()
        self.addCleanup(eye_mod.TYPE_INFO.clear)
        self.addCleanup(cp.DCS_FUEL_MAX_KG.clear)


def ground(uid, name, type_name, group, brg, rng, coalition="red"):
    return fx.unit(uid, name, type_name, coalition, group, "GROUND", *fx.offset(brg, rng), 300.0)


# ============================================================================= 1. unit labels
class LabelTests(TypeInfoIsolation):
    def test_review_proven_mislabels_fixed_without_dcs(self):
        cases = {"Soldier M4": "infantry", "KAMAZ Truck": "ground vehicle", "M-60": "armor", "PT_76": "armor",
                 "M1_37mm": "AAA", "generator_5i57": "ground vehicle", "RPC_5N62V": "SAM",
                 "Soldier M249": "infantry", "Soldier stinger": "SAM"}
        for t, want in cases.items():
            self.assertEqual(classify(t, GROUND), want, t)
        self.assertEqual(classify("RQ-1A Predator", "GROUP_CATEGORY_AIRPLANE"), "attack")
        self.assertEqual(sam_profile("RPC_5N62V")[:2], (80.0, "SA-5"))  # SA-5 radar now a SAM threat
        self.assertNotEqual(sam_profile("generator_5i57")[1], "SA-15")  # no bare "tor" match
        self.assertEqual(sam_profile("Tor 9A331")[1], "SA-15")

    def test_generic_fragments_no_longer_make_sams(self):
        for t in ("KAMAZ Truck", "Tractor", "Ural-375 Fuel Truck"):
            self.assertNotEqual(classify(t, GROUND), "SAM", t)
        for t in ("Hawk tr", "Hawk sr", "Hawk ln", "Kub 2P25 ln", "snr s-125 tr", "p-19 s-125 sr", "Hawk pcp"):
            self.assertEqual(classify(t, GROUND), "SAM", t)

    def test_weapon_kinds(self):
        self.assertEqual(weapon_kind("AGR_20A"), "rocket")  # APKWS, was "air-to-air missile"
        self.assertEqual(weapon_kind("AGR_20_M282"), "rocket")
        self.assertNotEqual(weapon_kind("P_9M117"), "bomb")  # gun-launched ATGM, was "bomb" via m117
        self.assertEqual(weapon_kind("M117"), "bomb")
        self.assertEqual(weapon_kind("AIM_9X"), "air-to-air missile")

    def test_dcs_attributes_win_over_patterns_and_relabel_live_units(self):
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        truck = ground(700, "T1", "Mystery tr", "Convoy", 90, 2)  # pattern says SAM (\btr\b)
        eye.on_units_message(fx.units_message(truck))
        self.assertEqual(eye.units[700]["category"], "SAM")
        self.assertEqual(eye.types_to_describe(), ["Mystery tr"])
        eye.note_type_info({"Mystery tr": {"attributes": ["Trucks", "Vehicles", "Ground Units"]}})
        # review P1 fail safe: attributes never downgrade a name the patterns call SAM (only TYPE_OVERRIDES can)
        self.assertEqual(eye.units[700]["category"], "SAM")
        self.assertEqual(eye.types_to_describe(), [])  # asked once per type
        truck2 = ground(703, "T2", "Mystery Hauler", "Convoy", 90, 2)  # no SAM pattern: DCS attributes decide
        eye.on_units_message(fx.units_message(truck2))
        self.assertEqual(eye.units[703]["category"], "ground vehicle")
        eye.note_type_info({"Mystery Hauler": {"attributes": ["Artillery"]}})
        self.assertEqual(eye.units[703]["category"], "artillery")  # existing unit relabelled
        eye.note_type_info({"Odd Thing": False})  # DCS gave nothing: patterns decide
        self.assertEqual(classify("Odd Thing", GROUND), "ground vehicle")
        eye.note_type_info({"Shilka-like": {"attributes": {"AA_flak": True, "SAM TR": True, "Mobile AAA": True}}})
        self.assertEqual(classify("Shilka-like", GROUND), "AAA")
        eye.note_type_info({"Tunguska-like": {"attributes": ["AA_flak", "AA_missile", "SAM TR"]}})
        self.assertEqual(classify("Tunguska-like", GROUND), "SAM")
        eye.note_type_info({"Tank-like": {"attributes": ["Tanks", "Armored vehicles"]},
                            "Grunt": {"attributes": ["Infantry"]}, "Gun": {"attributes": ["Artillery"]},
                            "Weird": {"attributes": ["Something DCS adds later"]}})
        self.assertEqual([classify(t, GROUND) for t in ("Tank-like", "Grunt", "Gun", "Weird")],
                         ["armor", "infantry", "artillery", "ground vehicle"])

    def test_overrides_beat_attributes(self):
        Eye("Prometheus", clock=Clock(), source="fixture").note_type_info(
            {"Soldier M4": {"attributes": ["Tanks"]}})
        self.assertEqual(classify("Soldier M4", GROUND), "infantry")

    def test_aircraft_attributes(self):
        Eye("Prometheus", clock=Clock(), source="fixture").note_type_info(
            {"Mystery Tanker": {"attributes": ["Planes", "Tankers"]}, "Some Jet": {"attributes": ["Planes"]}})
        self.assertEqual(classify("Mystery Tanker", "GROUP_CATEGORY_AIRPLANE"), "tanker")
        self.assertEqual(classify("Some Jet", "GROUP_CATEGORY_AIRPLANE"), "fighter")
        self.assertEqual(classify("A-10C", "GROUP_CATEGORY_AIRPLANE"), "attack")  # sets unchanged

    def test_no_false_sam_warning_for_a_truck_or_a_command_post(self):
        kamaz = ground(701, "K1", "KAMAZ Truck", "Convoy", 95, 1.5)
        comm = ground(702, "C1", "SA-18 Igla comm", "Ground-6", 100, 1.8)
        eye, clock = f4e_eye(extra=(kamaz, comm))
        eye.on_cockpit_packet(f4e_packet())
        eye.tick()
        self.assertEqual(eye.picture()["threats"]["sam_in_range"], [])  # by name, before DCS describes them
        self.assertEqual(list(eye.threat_alerts), [])
        eye.note_type_info({"SA-18 Igla comm": {"attributes": ["MANPADS AUX", "Infantry"]},
                            "KAMAZ Truck": {"attributes": ["Trucks"]}})
        pic = eye.picture()
        self.assertEqual(pic["threats"]["sam_in_range"], [])  # neither emits nor launches
        self.assertEqual(eye.units[702]["category"], "SAM")  # still air-defence for grouping/merging


# ============================================================================= 2. missiles
class MissileTests(TypeInfoIsolation):
    def shot(self, wid, heading=281.0, rng=10.2, brg=101, unit=SA11_LN):
        lat, lon = fx.offset(brg, rng)
        return {"time": 50.0, "shot": {"initiator": {"unit": unit}, "weapon": {
            "id": wid, "type": "SA2V755", "position": {"lat": lat, "lon": lon, "alt": 400.0},
            "velocity": {"heading": heading, "speed": 700.0}}}}

    def scan_row(self, wid, brg=100, rng=9.5, launcher="SAM SR", target="Aerial-3-2"):
        lat, lon = fx.offset(brg, rng)
        return {"id": wid, "type": "SA2V755", "lat": lat, "lon": lon, "alt": 1500, "vn": 0, "ve": -600, "coal": 1,
                "target": target, "launcher": launcher, "launcher_type": "SNR_75V"}

    def test_scan_on_bridge_expires_and_never_alerts_twice(self):
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_weapons([])
        eye.on_event(self.shot(4294967295))
        self.assertEqual(len(eye.threat_alerts), 1)  # v15: immediate shot alert; scan inherits its key
        clock.t += 3
        eye.on_weapons([])  # the scan never saw it (out of its sphere)
        eye.tick()
        self.assertEqual(eye.picture()["threats"]["missiles_in_flight"], [])
        self.assertEqual(eye.missile_shots, {})

    def test_two_missiles_from_one_site_are_two_rows_each_once(self):
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_weapons([])
        eye.on_event(self.shot(4294967290))
        eye.on_event(self.shot(4294967289))
        clock.t += 1
        eye.on_weapons([self.scan_row(16786001, rng=9.9), self.scan_row(16786002, rng=10.0)])
        flight = eye.picture()["threats"]["missiles_in_flight"]
        self.assertEqual(sorted(m["id"] for m in flight), ["16786001", "16786002"])
        self.assertEqual(len([a for a in eye.threat_alerts if a["kind"] == "missile launch"]), 2)
        for _ in range(5):  # the same two missiles, polled again: no new rows, no new alerts
            clock.t += 1
            eye.on_weapons([self.scan_row(16786001, rng=8.0), self.scan_row(16786002, rng=8.5)])
        self.assertEqual(len(eye.picture()["threats"]["missiles_in_flight"]), 2)
        self.assertEqual(len([a for a in eye.threat_alerts if a["kind"] == "missile launch"]), 2)

    def test_scan_off_shot_row_is_the_source_for_a_short_while(self):
        eye, clock = f4e_eye(extra=(SA11_LN,))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(self.shot(4294967291))
        pic = eye.picture()
        row = pic["threats"]["missiles_in_flight"][0]
        self.assertEqual((row["from"], row["scan"]), ("shot event", "no live scan (Eval off)"))
        self.assertEqual(len(eye.threat_alerts), 1)  # the only source: it alerts
        self.assertEqual(pic["rwr_alert_to_announce"], ["Missile launch, SA-11, your twelve o'clock, ten miles."])
        clock.t += eye_mod.EVAL_OFF_KEEP_S + 1
        eye.tick()
        pic = eye.picture()
        self.assertEqual(pic["threats"]["missiles_in_flight"], [])
        # no call about a missile we no longer track (the SA-11 range alert from tick() may be pending)
        self.assertFalse([c for c in pic.get("rwr_alert_to_announce", []) if c.startswith("Missile launch")])

    def test_shot_alert_follows_the_scan_when_it_comes_back(self):
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(self.shot(4294967288))  # scan not running yet: shot alert
        clock.t += 1
        eye.on_weapons([self.scan_row(16786100)])
        alerts = [a for a in eye.threat_alerts if a["kind"] == "missile launch"]
        self.assertEqual(len(alerts), 1)  # not a second alert for the same missile
        pic = eye.picture()
        self.assertEqual([m["id"] for m in pic["threats"]["missiles_in_flight"]], ["16786100"])
        self.assertEqual(pic["rwr_alert_to_announce"], ["Missile launch, SA-2, your twelve o'clock, ten miles."])


# ============================================================================= 3. SAM radar truth
class RadarTests(TypeInfoIsolation):
    def eye(self):
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        return eye, clock

    def test_candidates_bounded_radars_first(self):
        eye, _ = self.eye()
        self.assertEqual(eye.radar_candidates(), ["SAM SR", "SAM LN"])
        self.assertEqual(eye.radar_candidates(limit=1), ["SAM SR"])

    def test_tracking_you_from_dcs_beats_range_table(self):
        eye, clock = self.eye()
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": True, "tracking_me": True, "detected": True},
                       {"name": "SAM LN", "has_radar": False, "on": False}])
        eye.tick()
        pic = eye.picture()
        th = pic["threats"]
        self.assertEqual(th["sam_source"], "dcs_radar")
        self.assertTrue(th["sam_radar"]["available"])
        self.assertEqual(th["sam_radar"]["tracking_you"][0]["group"], "SA-11 East")
        row = th["sam_in_range"][0]
        self.assertEqual(row["confidence"], "DCS radar: tracking you")
        self.assertTrue(row["call"].startswith("SAM SA-11 tracking you, your twelve o'clock"), row["call"])
        self.assertEqual(pic["rwr_alert_to_announce"], [row["call"]])

    def test_radar_off_or_searching_keeps_the_range_warning(self):
        # review P1 (MAJOR): a dark or searching radar can ambush; it keeps the range-table warning, once
        eye, clock = self.eye()
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": False}])
        eye.tick()
        pic = eye.picture()
        row = pic["threats"]["sam_in_range"][0]
        self.assertEqual((row["confidence"], row["radar"]), ("range only: could be tracking", "off"))
        self.assertEqual(pic["rwr_alert_to_announce"], [row["call"]])
        eye.mark_announced([a["key"] for a in eye.threat_alerts])
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": True, "detected": False}])
        eye.tick()
        pic = eye.picture()
        self.assertEqual(pic["threats"]["sam_in_range"][0]["radar"], "searching")
        self.assertNotIn("rwr_alert_to_announce", pic)  # same group, same warning: not again
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": True, "tracking_me": True}])
        eye.tick()
        self.assertEqual(eye.picture()["rwr_alert_to_announce"][0][:24], "SAM SA-11 tracking you, ")

    def test_missing_api_or_stale_radar_data_falls_back_to_range_table(self):
        eye, clock = self.eye()
        eye.on_radars([{"name": "SAM SR"}, {"name": "SAM LN"}])  # older DCS: only names came back
        eye.tick()
        row = eye.picture()["threats"]["sam_in_range"][0]
        self.assertEqual(row["confidence"], "range only: could be tracking")
        self.assertTrue(row["call"].startswith("SAM SA-11 could be tracking you"))
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": False}])
        clock.t += eye_mod.RADAR_FRESH_S + 1  # the poll stopped answering
        th = eye.picture()["threats"]
        self.assertEqual(th["sam_source"], "range_table")
        self.assertEqual(th["sam_in_range"][0]["confidence"], "range only: could be tracking")

    def test_range_table_hysteresis_no_edge_flicker(self):
        sr = ground(801, "SA2 SR", "SNR_75V", "Ground-8", 90, 22.5)  # SA-2: 23 nm nominal
        eye, clock = f4e_eye(extra=(sr,))
        eye.on_cockpit_packet(f4e_packet(alt=300.0))
        listed = []
        for rng in (22.5, 23.4, 24.6, 23.2, 25.5, 24.0, 22.8):
            sr["position"]["lat"], sr["position"]["lon"] = fx.offset(90, rng)
            eye.on_units_message(fx.units_message(sr))
            eye.tick()
            listed.append(bool(eye.picture()["threats"]["sam_in_range"]))
            clock.t += 1
        # in at 22.5; held to 23+2 nm; out past 25; back in only inside 23 nm again
        self.assertEqual(listed, [True, True, True, True, False, False, True])
        self.assertEqual(len([a for a in eye.threat_alerts if a["kind"] == "SAM in range"]), 1)

    def test_sam_alert_rearms_after_a_minute_away(self):
        sr = ground(802, "SA2 SR", "SNR_75V", "Ground-8", 90, 20)
        eye, clock = f4e_eye(extra=(sr,))
        eye.on_cockpit_packet(f4e_packet(alt=300.0))
        eye.tick()
        self.assertEqual(len(eye.threat_alerts), 1)
        sr["position"]["lat"], sr["position"]["lon"] = fx.offset(90, 40)
        eye.on_units_message(fx.units_message(sr))
        for _ in range(7):
            clock.t += 10
            eye.tick()
        sr["position"]["lat"], sr["position"]["lon"] = fx.offset(90, 20)
        eye.on_units_message(fx.units_message(sr))
        eye.tick()
        self.assertEqual([a["key"] for a in eye.threat_alerts], ["sam-Ground-8-0", "sam-Ground-8-1"])


# ============================================================================= 4. reconnect / health
class HealthTests(TypeInfoIsolation):
    def test_units_not_resent_after_resubscribe_are_swept(self):
        a, b = ground(901, "A", "T-55", "Armor", 90, 5), ground(902, "B", "T-55", "Armor", 90, 5.2)
        eye, clock = f4e_eye(extra=(a, b))
        eye.set_connected(True, stream="units-ground")
        clock.t += 30  # stream outage; B dies meanwhile, its "gone" is lost
        eye.begin_unit_sync(GROUND, clock.t, 10.0, stream="units-ground")
        clock.t += 0.5
        eye.on_units_message(fx.units_message(a))  # DCS re-sends what is alive
        eye.on_units_message(fx.units_message(T55))
        eye.on_units_message(fx.units_message(F4E))
        eye.tick()
        self.assertIn(902, eye.units)  # inside the window: not yet
        clock.t += 10
        eye.tick()
        self.assertNotIn(902, eye.units)
        self.assertIn(901, eye.units)
        pic = eye.picture()
        row = [r for r in pic["recent_kills_and_losses_last_10_min"] if r["victim"]["name"] == "B"][0]
        self.assertEqual(row["event"], "no longer showing (missed during a data outage)")
        self.assertNotIn("by_me", row)  # no kill credit
        self.assertEqual(pic["my_kills_this_mission"], [])
        self.assertEqual(pic["data_health"]["unit_sync"]["removed"], 1)

    def test_no_sweep_when_the_stream_failed_again(self):
        b = ground(903, "B", "T-55", "Armor", 90, 5.2)
        eye, clock = f4e_eye(extra=(b,))
        clock.t += 30
        eye.begin_unit_sync(GROUND, clock.t, 10.0, stream="units-ground")
        eye.set_connected(False, "units-ground: RpcError UNAVAILABLE", stream="units-ground")
        clock.t += 11
        eye.tick()
        self.assertIn(903, eye.units)

    def test_one_bad_stream_is_not_dcs_disconnected(self):
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        for s in ("units-air", "units-ground", "events", "world"):
            eye.set_connected(True, stream=s)
        eye.set_connected(False, "units-ground: RpcError UNAVAILABLE", stream="units-ground")
        pic = eye.picture()
        self.assertTrue(pic["connected"])
        self.assertTrue(pic["data_health"]["streams"]["units-ground"].startswith("down"))
        self.assertEqual(pic["data_health"]["streams"]["events"], "up")
        for s in ("units-air", "events", "world"):
            eye.set_connected(False, "%s: RpcError UNAVAILABLE" % s, stream=s)
        self.assertFalse(eye.picture()["connected"])  # every stream down = DCS gone

    def test_memory_does_not_end_the_mission_on_one_bad_stream(self):
        now = [0.0]
        link = MemoryLink(start=False, capacity=100, clock=lambda: now[0])
        self.addCleanup(link.close)
        link.set_identity(1, "fixture.miz", "fixture:1")
        link.publish("shot", {})  # pilot activity
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        eye.set_connected(True, stream="events")
        eye.set_connected(False, "units-ground: x", stream="units-ground")
        for now[0] in (0.0, 30.0, 61.0, 120.0):
            link.capture_picture(eye.picture())
        self.assertEqual(link._pending_debriefs, {})

    def test_heartbeat_stall_announced_and_resubscribed_within_60s(self):
        eye, clock = f4e_eye(extra=())
        feeder = GrpcFeeder(eye, "127.0.0.1", 1, events_stale_s=EVENTS_STALE_S)

        class Call:
            cancelled = 0

            def cancel(self):
                Call.cancelled += 1
        feeder._events_call, feeder._events_subscribed = Call(), clock.t
        for _ in range(40):  # 1 Hz heartbeat flowing
            clock.t += 1
            eye.on_event({"time": 1.0, "simulation_fps": {"average": 60.0}})
            eye.on_units_message(fx.units_message(F4E))
        self.assertEqual(eye.stream_health()["state"], "live")
        stalled_at = clock.t
        while clock.t - stalled_at < 60 and not Call.cancelled:
            clock.t += 5  # watchdog period
            eye.on_units_message(fx.units_message(F4E))
            feeder.check_liveness()
        self.assertEqual(Call.cancelled, 1)
        self.assertLessEqual(clock.t - stalled_at, 35)
        self.assertTrue(eye.stream_health()["state"].startswith("stale (heartbeat stopped"))
        self.assertIn("kills may be unconfirmed", eye.picture()["data_health"]["note"])

    def test_dead_combat_feed_noticed_although_heartbeat_flows(self):
        eye, clock = f4e_eye(extra=())
        eye.on_event({"time": 1.0, "simulation_fps": {"average": 60.0}})
        stores = {"ammo": [{"type": "AGM_65D", "name": "AGM-65D", "count": 4, "category": 1}]}
        eye.on_stores(norm_unit(F4E), stores)
        for n in (3, 2, 1):
            clock.t += 20
            eye.on_event({"time": 1.0, "simulation_fps": {"average": 60.0}})
            eye.on_stores(norm_unit(F4E), {"ammo": [dict(stores["ammo"][0], count=n)]})
        es = eye.picture()["event_stream"]
        self.assertEqual(es["state"], "combat events missing (kills may be unconfirmed)")
        self.assertEqual(es["shots_without_event_10_min"], 3)
        dh = eye.picture()["data_health"]
        self.assertTrue(dh["combat_events"].startswith("missing"))

    def test_shot_events_flowing_is_not_missing(self):
        eye, clock = f4e_eye(extra=())
        eye.on_event(fx.shot(F4E, 5, "AGM_65D"))
        for n in (3, 2, 1):
            clock.t += 20
            eye._attribute_use(eye._own_locked(), {"t": clock.t, "kind": "missile", "weapon": "AGM-65D",
                                                   "type": "AGM_65D", "count": 1})
        self.assertEqual(eye.stream_health()["state"], "live")


# ============================================================================= 5. picture is a pure read
class PurePictureTests(TypeInfoIsolation):
    STATE = ("units", "shots", "events", "threat_alerts", "missile_shots", "missiles_near", "_sam_alerted",
             "_sam_inside", "_sam_listed", "announced", "my_kills", "other_kills", "vanished", "known_objects")

    def snapshot(self, eye):
        return repr([(k, copy.deepcopy(getattr(eye, k))) for k in self.STATE])

    def test_picture_twice_same_result_marks_nothing(self):
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(fx.shot(F4E, 6001, "AGM_65D"))  # will be past its miss window
        eye.on_event(MissileTests().shot(4294967200))  # scan off: shot row + alert
        other = fx.unit(650, "Wingman", "F-4E-45MC", "blue", "Aerial-9", "AIRPLANE", *fx.offset(0, 5), 3000.0)
        eye.on_event({"time": 60.0, "kill": {"initiator": {"unit": other}, "target": {"unit": SA11_LN},
                                             "weapon_name": "Mk-82"}})
        clock.t += 400  # SAM in range not yet alerted, shot unsettled, missile row expired
        before = self.snapshot(eye)
        first = eye.picture()
        second = eye.picture()
        self.assertEqual(json.dumps(first, sort_keys=True, default=str),
                         json.dumps(second, sort_keys=True, default=str))
        self.assertEqual(self.snapshot(eye), before)  # nothing marked, settled, pruned or alerted
        self.assertEqual(first["my_shots_last_15_min"]["shots"][0]["result"], "miss")  # shown settled
        self.assertEqual(eye.shots[0]["result"], "pending")  # but not written
        eye.tick()
        self.assertEqual(eye.shots[0]["result"], "miss")
        self.assertTrue(any(a["kind"] == "SAM in range" for a in eye.threat_alerts))

    def test_build_time_busy_map(self):
        eye = Eye("Prometheus", clock=Clock(), source="fixture")
        eye.set_connected(True)
        eye.on_units_message(fx.units_message(fx.PROMETHEUS))
        for i in range(1500):
            u = ground(10000 + i, "U%d" % i, ("T-55", "SNR_75V", "ZSU_57_2", "Ural-375")[i % 4], "G%d" % (i // 4),
                       (i * 7) % 360, 2 + (i % 140))
            eye.on_units_message(fx.units_message(u))
        eye.on_radars([{"name": "U%d" % i, "has_radar": True, "on": i % 2 == 0} for i in range(1, 120, 4)])
        times = []
        for _ in range(7):
            t0 = time.perf_counter()
            eye.picture()
            times.append(time.perf_counter() - t0)
        times.sort()
        print("p1 busy map: 1500 units, picture p50 %.1f ms" % (times[3] * 1000))
        self.assertLess(times[3], 0.25)


# ============================================================================= 6. numbers
class NumberTests(TypeInfoIsolation):
    def test_aoa_undoes_the_double_degree_conversion(self):
        self.assertEqual(cp.aoa_deg({"aoa": 237.5}), 4.1)  # A-10C II live median
        self.assertEqual(cp.aoa_deg({"aoa": -62.4}), -1.1)
        self.assertEqual(cp.aoa_deg({"aoa_deg": 5.0, "aoa": 286.5}), 5.0)
        self.assertIsNone(cp.aoa_deg({}))

    def test_fuel_from_dcs_max_and_fractions_above_one(self):
        f100 = cp.cockpit_fuel({"ac": "F-100D", "fu": 2, "fuel_int": 1.5737, "fuel_ext": 0})
        self.assertGreater(f100["total_lb"], 10000)  # was 0 lb (read as kg)
        self.assertTrue(f100["includes_external"])
        self.assertEqual(f100["internal_pct"], 100)
        f16 = cp.fuel_lb_from_fraction(1.28, "F-16C_50")
        self.assertEqual((f16["internal_pct"], f16["total_pct"]), (100, 128))
        self.assertTrue(cp.fuel_lb_from_fraction(0.75, "F-14BU")["total_lb"])  # was percent only
        Eye("Prometheus", clock=Clock(), source="fixture").note_type_info(
            {"F-16C_50": {"attributes": ["Planes"], "fuel_max_kg": 3249.0}})
        out = cp.fuel_lb_from_fraction(0.5, "F-16C_50")
        self.assertEqual(out["total_lb"], int(round(0.5 * 3249.0 * cp.KG_TO_LB, -1)))
        self.assertFalse(out["estimated"])
        self.assertIn("fuelMassMax", out["source"])

    def test_stores_eval_brings_dcs_max_fuel(self):
        eye, clock = f4e_eye(extra=())
        eye.on_stores(norm_unit(F4E), {"fuel": 0.5, "fuel_max_kg": 5510.0})
        fuel = eye.picture()["ownship"]["fuel"]
        self.assertEqual(fuel["total_lb"], int(round(0.5 * 5510.0 * cp.KG_TO_LB, -1)))
        self.assertFalse(fuel["estimated"])


# ============================================================================= 7. UDP allowlist
class AllowlistTests(unittest.TestCase):
    def test_sources(self):
        with patch.dict(os.environ, {"ATHENA_COCKPIT_ALLOWED": "", "ATHENA_DCS_SOURCE": "", "ATHENA_DCS_CLIENT_HOST": ""}):
            self.assertEqual(cp.allowed_sources(None), {"10.90.10.113"})
            # review P1: the client PC (where the export runs) stays allowed whatever the Eye's DCS host
            self.assertEqual(cp.allowed_sources(type("E", (), {"source": "10.1.2.3:50051"})()),
                             {"10.90.10.113", "10.1.2.3"})
        with patch.dict(os.environ, {"ATHENA_COCKPIT_ALLOWED": "10.0.0.5, 10.0.0.6"}):
            self.assertEqual(cp.allowed_sources(None), {"10.0.0.5", "10.0.0.6"})
        with patch.dict(os.environ, {"ATHENA_COCKPIT_ALLOWED": "*"}):
            self.assertIsNone(cp.allowed_sources(None))

    def test_foreign_packets_dropped_and_counted(self):
        eye = Eye("Prometheus", source="10.90.10.113:50051")
        listener = cp.CockpitListener(eye, host="127.0.0.1", port=0).start()  # default: only the DCS PC
        self.assertEqual(listener.allowed, {"10.90.10.113"})
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.sendto(json.dumps(f4e_packet()).encode(), ("127.0.0.1", listener.port))
            deadline = time.time() + 3
            while time.time() < deadline and eye.cockpit.rejected == 0:
                time.sleep(0.02)
            tx.close()
        finally:
            listener.close()
        self.assertEqual(eye.cockpit.rejected, 1)
        self.assertEqual(eye.cockpit.packets, 0)
        self.assertEqual(eye.picture()["cockpit"]["rejected_packets_other_source"], 1)


# ============================================================================= feeder with canned Eval replies
class FakeCustom:
    def __init__(self, replies):
        self.replies, self.lua = replies, []

    def Eval(self, req, timeout=None):  # noqa: N802 - grpc stub name
        self.lua.append(req.lua)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return type("R", (), {"json": reply if isinstance(reply, str) else json.dumps(reply)})()


@unittest.skipUnless(__import__("importlib").util.find_spec("dcs"), "DCS-gRPC python stubs not installed")
class FeederEvalTests(TypeInfoIsolation):
    def feeder(self, eye):
        return GrpcFeeder(eye, "127.0.0.1", 1)

    def test_types_poll_caches_once_and_never_caches_a_failed_call(self):
        eye, _ = f4e_eye(extra=(SA11_SR,))
        f = self.feeder(eye)
        custom = FakeCustom([RuntimeError("eval failed"), "", {"types": {
            "SA-11 Buk SR 9S18M1": {"attributes": ["SAM SR", "SAM elements"], "fuel_max_kg": None},
            "F-4E-45MC": {"attributes": ["Planes", "Battleplanes"], "fuel_max_kg": 5510},
            "T-55": False}}])
        with self.assertRaises(RuntimeError):
            f.poll_types(custom)
        self.assertEqual(f.poll_types(custom), 0)  # empty reply: ask again
        self.assertEqual(eye_mod.TYPE_INFO, {})
        self.assertEqual(f.poll_types(custom), 3)
        self.assertIn('"SA-11 Buk SR 9S18M1"', custom.lua[-1])
        self.assertEqual(eye_mod.type_attributes("SA-11 Buk SR 9S18M1"), frozenset({"SAM SR", "SAM elements"}))
        self.assertEqual(cp.DCS_FUEL_MAX_KG["F-4E-45MC"], 5510)
        self.assertEqual(f.poll_types(FakeCustom([])), 0)  # nothing left to ask

    def test_radar_poll(self):
        eye, _ = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        f = self.feeder(eye)
        custom = FakeCustom([{"radars": [{"name": "SAM SR", "has_radar": True, "on": True, "tracking_me": True}]},
                             "null"])
        f.poll_radars(custom)
        self.assertIn('Unit.getByName("Aerial-3-2")', custom.lua[0])
        self.assertIn('{"SAM SR","SAM LN"}', custom.lua[0])
        self.assertEqual(eye.picture()["threats"]["sam_in_range"][0]["confidence"], "DCS radar: tracking you")
        self.assertIsNone(f.poll_radars(custom))  # nil reply (own unit gone): keep last, goes stale


# ============================================================================= the Lua chunks against a stub DCS API
STUB = r"""
local scenario, chunk_path = arg[1], arg[2]
local function enc(v)
  local t = type(v)
  if t == "nil" then return "null" elseif t == "boolean" then return tostring(v)
  elseif t == "number" then return string.format("%.14g", v)
  elseif t == "string" then return '"' .. v:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end) .. '"'
  elseif t == "table" then
    local parts = {}
    if #v > 0 then for _, x in ipairs(v) do parts[#parts + 1] = enc(x) end return "[" .. table.concat(parts, ",") .. "]" end
    for k, x in pairs(v) do parts[#parts + 1] = enc(tostring(k)) .. ":" .. enc(x) end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  return "null"
end
local full = scenario == "full"
Unit = {SensorType = {RADAR = 1}}
Controller = {Detection = {RADAR = 4}}
Weapon = {Category = {MISSILE = 1}}
Object = {Category = {WEAPON = 2}}
coord = {LOtoLL = function(p) return 36.0 + p.x / 1e5, -115.0 + p.z / 1e5, p.y end}
local function unit(name, coal, opts)
  local u = {}
  function u:isExist() return true end
  function u:getName() return name end
  function u:getCoalition() return coal end
  function u:getPoint() return {x = 0, y = 1000, z = 0} end
  if full then
    function u:hasSensors(kind) return opts.radar end
    function u:getRadar() if opts.radar then return opts.on, opts.tgt end return false, nil end
    function u:getGroup() return {getController = function() return {isTargetDetected = function(_, o, d) return opts.det end} end} end
  else
    function u:getGroup() return nil end
  end
  return u
end
local own = unit("Aerial-3-2", 2, {})
local units = {["Aerial-3-2"] = own,
  ["SAM SR"] = unit("SAM SR", 1, {radar = true, on = true, tgt = own, det = true}),
  ["SAM LN"] = unit("SAM LN", 1, {radar = false, on = false, det = false})}
function Unit.getByName(n) return units[n] end
if full then
  function Unit.getDescByName(t)
    if t == "KAMAZ Truck" then return {attributes = {Trucks = true, Vehicles = true}} end
    if t == "F-16C_50" then return {attributes = {Planes = true}, fuelMassMax = 3249} end
    return nil
  end
end
local function weapon(id, coal)
  return {id_ = id, getDesc = function() return {category = 1} end, getCoalition = function() return coal end,
          getPoint = function() return {x = 100, y = 900, z = 100} end, getVelocity = function() return {x = 1, y = 0, z = 2} end,
          getTarget = function() return own end, getTypeName = function() return "SA2V755" end,
          getLauncher = function() return units["SAM SR"] end}
end
world = {VolumeType = {SPHERE = 1}, searchObjects = function(cat, vol, fn)
  fn(weapon(16786433, 1)) fn(weapon(16786434, 2)) end}
local f = assert(loadfile(chunk_path))
print(enc(f()))
"""


@unittest.skipUnless(LUA, "lua5.1/luajit not installed")
class LuaChunkTests(unittest.TestCase):
    def run_chunk(self, lua, scenario):
        with tempfile.TemporaryDirectory() as d:
            stub, chunk = os.path.join(d, "stub.lua"), os.path.join(d, "chunk.lua")
            with open(stub, "w") as fh:
                fh.write(STUB)
            with open(chunk, "w") as fh:
                fh.write(lua)
            out = subprocess.run([LUA, stub, scenario, chunk], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_radars_chunk_full_and_old_api(self):
        lua = GrpcFeeder.RADARS_LUA % (json.dumps("Aerial-3-2"), GrpcFeeder._lua_list(["SAM SR", "SAM LN", "Gone"]))
        full = self.run_chunk(lua, "full")["radars"]
        self.assertEqual(full[0], {"name": "SAM SR", "has_radar": True, "on": True, "tracking_me": True,
                                   "detected": True})
        self.assertEqual(full[1], {"name": "SAM LN", "has_radar": False, "on": False, "detected": False})
        self.assertEqual(len(full), 2)  # a unit that no longer exists is skipped
        old = self.run_chunk(lua, "old")["radars"]  # no hasSensors/getRadar/controller: names only
        self.assertEqual(old, [{"name": "SAM SR"}, {"name": "SAM LN"}])

    def test_types_chunk_full_and_old_api(self):
        lua = GrpcFeeder.TYPES_LUA % GrpcFeeder._lua_list(["KAMAZ Truck", "F-16C_50", "Nope"])
        full = self.run_chunk(lua, "full")["types"]
        self.assertEqual(sorted(full["KAMAZ Truck"]["attributes"]), ["Trucks", "Vehicles"])
        self.assertEqual(full["F-16C_50"]["fuel_max_kg"], 3249)
        self.assertFalse(full["Nope"])
        old = self.run_chunk(lua, "old")["types"]  # no Unit.getDescByName: every type false
        self.assertEqual(old, {"KAMAZ Truck": False, "F-16C_50": False, "Nope": False})

    def test_weapons_chunk_skips_own_coalition(self):
        rows = self.run_chunk(GrpcFeeder.WEAPONS_LUA % json.dumps("Aerial-3-2"), "full")["weapons"]
        self.assertEqual([r["id"] for r in rows], [16786433])
        self.assertEqual((rows[0]["launcher"], rows[0]["target"]), ("SAM SR", "Aerial-3-2"))


if __name__ == "__main__":
    unittest.main()
