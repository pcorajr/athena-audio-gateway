"""Athena Eye: picture geometry, clock positions, shot->hit/kill/miss resolution, airbases, queueing."""
import json
import threading
import time
import unittest

from integrations.hermes_radio.contract import Request
from integrations.hermes_radio.eye import (Eye, bearing_range, classify, clock_phrase, norm_unit, weapon_kind)
from integrations.hermes_radio.simple_controller import FALLBACK, SimpleController, split_parts
from integrations.hermes_radio.tests import eye_fixture as fx


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def loaded(clock=None):
    clock = clock or Clock()
    eye = Eye(clock=clock, source="fixture")
    eye.set_connected(True)
    for u in fx.UNITS:
        eye.on_units_message(fx.units_message(u))
    eye.set_airbases(fx.AIRBASES)
    return eye, clock


def group(pic, type_name, side="hostile_groups"):
    for g in pic[side]:
        if type_name in g["types"]:
            return g
    return None


class GeometryTests(unittest.TestCase):
    def test_bearing_range(self):
        brg, rng = bearing_range(fx.LAT0, fx.LON0, *fx.SU27_1)
        self.assertAlmostEqual(brg, 60, delta=0.5)
        self.assertAlmostEqual(rng, 20, delta=0.2)

    def test_clock_phrases(self):
        self.assertEqual(clock_phrase(0), "12 o'clock, on the nose")
        self.assertEqual(clock_phrase(-30), "11 o'clock, ahead left")
        self.assertEqual(clock_phrase(40), "1 o'clock, ahead right")
        self.assertEqual(clock_phrase(90), "3 o'clock, right")
        self.assertEqual(clock_phrase(180), "6 o'clock, behind")
        self.assertEqual(clock_phrase(225), "8 o'clock, behind left")
        self.assertEqual(clock_phrase(-90), "9 o'clock, left")
        self.assertEqual(clock_phrase(359), "12 o'clock, on the nose")

    def test_classification(self):
        self.assertEqual(classify("Su-27", "GROUP_CATEGORY_AIRPLANE"), "fighter")
        self.assertEqual(classify("KC-135", "GROUP_CATEGORY_AIRPLANE"), "tanker")
        self.assertEqual(classify("E-3A", "GROUP_CATEGORY_AIRPLANE"), "AWACS")
        self.assertEqual(classify("Su-25T", "GROUP_CATEGORY_AIRPLANE"), "attack")
        self.assertEqual(classify("Mi-8MT", "GROUP_CATEGORY_HELICOPTER"), "helicopter")
        self.assertEqual(classify("SA-11 Buk SR 9S18M1", "GROUP_CATEGORY_GROUND"), "SAM")
        self.assertEqual(classify("ZSU-23-4 Shilka", "GROUP_CATEGORY_GROUND"), "AAA")
        self.assertEqual(classify("T-72B", "GROUP_CATEGORY_GROUND"), "armor")
        self.assertEqual(classify("Ural-375", "GROUP_CATEGORY_GROUND"), "ground vehicle")
        self.assertEqual(classify("CVN_71", "GROUP_CATEGORY_SHIP"), "carrier")
        self.assertEqual(weapon_kind("AIM_120C"), "air-to-air missile")
        self.assertEqual(weapon_kind("GBU_12"), "bomb")
        self.assertEqual(weapon_kind("HYDRA_70_M151"), "rocket")
        self.assertEqual(weapon_kind("AGM_65D"), "missile")
        self.assertEqual(weapon_kind("M61_20_HE", burst=True), "gun")

    def test_fixture_matches_real_dcs_grpc_protobuf_shape(self):
        try:
            from google.protobuf.json_format import MessageToDict, ParseDict
            from dcs.common.v0 import common_pb2
            from dcs.mission.v0 import mission_pb2
        except ImportError:  # pragma: no cover
            self.skipTest("DCS-gRPC stubs not on PYTHONPATH")
        msgs = [fx.units_message(u) for u in fx.UNITS]
        events = [fx.shot(fx.PROMETHEUS, 5001, "AIM_120C"), fx.hit(fx.PROMETHEUS, 5001, "AIM_120C", fx.UNITS[1]),
                  fx.kill(fx.PROMETHEUS, 5001, "AIM_120C", fx.UNITS[1]), fx.gun_hit(fx.PROMETHEUS, "M61_20_HE", fx.UNITS[8]),
                  fx.shooting("shooting_start", fx.PROMETHEUS, "M61_20_HE"), fx.dead(fx.UNITS[8]),
                  fx.takeoff(fx.UNITS[1], fx.AIRBASES[1]), {"time": 1.0, "mission_start": {}}]
        for m in msgs:
            back = MessageToDict(ParseDict(m, mission_pb2.StreamUnitsResponse()), preserving_proto_field_name=True)
            self.assertEqual(norm_unit(back["unit"]), norm_unit(m["unit"]))
        for e in events:
            back = MessageToDict(ParseDict(e, mission_pb2.StreamEventsResponse()), preserving_proto_field_name=True)
            a, b = Eye(clock=Clock()), Eye(clock=Clock())
            ea, eb = a.on_event(e), b.on_event(back)
            for k in ("kind", "initiator", "target", "weapon", "weapon_id", "place"):
                self.assertEqual(ea[k], eb[k], (e, k))
        for ab in fx.AIRBASES:
            back = MessageToDict(ParseDict(ab, common_pb2.Airbase()), preserving_proto_field_name=True)
            a, b = Eye(clock=Clock()), Eye(clock=Clock())
            a.set_airbases([ab])
            b.set_airbases([back])
            self.assertEqual(a.airbases, b.airbases)


class PictureTests(unittest.TestCase):
    def test_ownship_and_contacts(self):
        eye, _ = loaded()
        pic = eye.picture()
        json.dumps(pic)  # compact JSON-serialisable
        own = pic["ownship"]
        self.assertEqual(own["type"], "F-16C_50")
        self.assertAlmostEqual(own["alt_ft"], 20000, delta=10)
        self.assertEqual(own["speed_kt"], 486)
        self.assertEqual(own["heading_true"], "090")
        self.assertTrue(any("ALIVE" in n for n in pic["notes"]))
        self.assertIn("TRUE", pic["bearings"])
        self.assertTrue(pic["connected"])
        self.assertIsNotNone(pic["data_age_s"]["units"])

        flankers = group(pic, "Su-27")
        self.assertEqual(flankers["count"], 2)
        self.assertEqual(flankers["label"], "bandit")
        # Round 2: ready call line, BRAA first then clock (user's required order).
        self.assertEqual(flankers["call"], "Bandit Su-27 two-ship, bearing zero six zero, twenty miles, angels "
                                           "twenty five. At your eleven o'clock, high.")
        self.assertEqual(flankers["bearing_true"], "060")
        self.assertAlmostEqual(flankers["range_nm"], 20, delta=0.3)
        self.assertEqual(flankers["clock"], "11 o'clock, ahead left")
        self.assertEqual(flankers["aspect"], "hot")
        self.assertEqual(flankers["angels"], 25)
        self.assertTrue(850 < flankers["closure_kt"] < 950)

        frogfoot = pic["hostile_groups"][0]  # sorted by range: Su-25T at 1.2 nm
        self.assertEqual(frogfoot["types"], {"Su-25T": 1})
        self.assertEqual(frogfoot["clock"], "12 o'clock, on the nose")
        self.assertEqual(frogfoot["category"], "attack")

        sam = group(pic, "SA-11 Buk SR 9S18M1")
        self.assertEqual(sam["count"], 2)
        self.assertEqual(sam["category"], "SAM")
        self.assertEqual(sam["label"], "SAM site")
        self.assertEqual(sam["clock"], "3 o'clock, right")
        self.assertNotIn("alt_ft", sam)

        armor = group(pic, "T-72B")
        self.assertEqual(armor["clock"], "6 o'clock, behind")
        self.assertEqual(armor["label"], "enemy armor")
        self.assertEqual(armor["call"], "Enemy armor, bearing two seven zero, five miles, ground target. "
                                        "Target is at your six o'clock.")

        tanker = group(pic, "KC-135", "friendly_groups")
        self.assertEqual(tanker["clock"], "9 o'clock, left")
        self.assertEqual(tanker["category"], "tanker")
        self.assertIsNone(group(pic, "E-3A", "friendly_groups"))  # 150 nm: outside the 80 nm picture
        self.assertEqual(pic["alive_totals_by_coalition"]["blue"]["AWACS"], 1)
        self.assertEqual(pic["alive_totals_by_coalition"]["red"]["fighter"], 2)
        ranges = [g["range_nm"] for g in pic["hostile_groups"]]
        self.assertEqual(ranges, sorted(ranges))

    def test_magnetic_when_declination_available(self):
        eye, _ = loaded()
        eye.set_declination(6.0)
        pic = eye.picture()
        flankers = group(pic, "Su-27")
        self.assertEqual(flankers["bearing_mag"], "054")
        self.assertNotIn("bearing_true", flankers)
        self.assertIn("magnetic", pic["bearings"])
        self.assertEqual(pic["ownship"]["heading_mag"], "084")

    def test_airbases_nearest_and_at(self):
        eye, _ = loaded()
        bases = eye.picture()["airbases"]
        self.assertEqual(bases["count"], 7)
        names = [b["name"] for b in bases["nearest"]]
        self.assertEqual(names, ["Kutaisi", "Kobuleti", "FARP London", "Senaki-Kolkhi", "Batumi"])
        self.assertEqual(bases["nearest"][0]["owner"], "blue")
        self.assertEqual(bases["nearest"][2]["category"], "FARP/helipad")
        self.assertEqual(bases["nearest"][1]["owner"], "red")
        self.assertIn("clock", bases["nearest"][1])
        self.assertEqual(bases["at_or_near"]["name"], "Kutaisi")
        self.assertFalse(bases["at_or_near"]["on_ground"])

    def test_no_ownship_still_reports_world(self):
        clock = Clock()
        eye = Eye(clock=clock)
        eye.on_units_message(fx.units_message(fx.UNITS[1]))
        eye.set_airbases(fx.AIRBASES)
        pic = eye.picture()
        self.assertIsNone(pic["ownship"])
        self.assertIn("not currently in an aircraft", pic["ownship_note"])
        self.assertEqual(pic["airbases"]["count"], 7)
        self.assertEqual(pic["alive_totals_by_coalition"]["red"]["fighter"], 1)

    def test_unit_gone_and_mission_start_reset(self):
        eye, clock = loaded()
        eye.on_units_message(fx.gone_message(401, "Armor 1"))
        self.assertIsNone(group(eye.picture(), "T-72B"))
        eye.on_event(fx.shot(fx.PROMETHEUS, 5001, "AIM_120C"))
        resubscribed = []
        eye.on_mission_start = lambda: resubscribed.append(1)
        eye.on_event({"time": 0.0, "mission_start": {}})
        pic = eye.picture()
        self.assertEqual(resubscribed, [1])
        self.assertEqual(pic["my_shots_last_15_min"]["total"], 0)
        self.assertEqual(len(eye.events), 1)  # only the mission_start itself
        eye.reset("session_changed")  # what the feeder does on a new DCS-gRPC session id
        pic = eye.picture()
        self.assertIsNone(pic["ownship"])
        self.assertEqual(pic["alive_totals_by_coalition"], {})

    def test_events_kept_30_minutes(self):
        eye, clock = loaded()
        eye.on_event(fx.takeoff(fx.UNITS[1], fx.AIRBASES[1]))
        self.assertTrue(any("took off from Senaki-Kolkhi" in line for line in eye.picture()["recent_events_last_10_min"]))
        clock.t += 11 * 60
        self.assertEqual(eye.picture()["recent_events_last_10_min"], [])
        self.assertEqual(len(eye.events), 1)
        clock.t += 20 * 60
        eye.picture()
        self.assertEqual(len(eye.events), 0)


class ShotTests(unittest.TestCase):
    def test_missile_shot_hit_then_kill(self):
        eye, clock = loaded()
        eye.on_units_message(fx.gone_message(205, "Frogfoot"))  # leave the Flankers as the only air targets
        eye.on_event(fx.shot(fx.PROMETHEUS, 5001, "AIM_120C"))
        shots = eye.picture()["my_shots_last_15_min"]
        self.assertEqual(shots["total"], 1)
        s = shots["shots"][0]
        self.assertEqual(s["result"], "pending")
        self.assertEqual(s["kind"], "air-to-air missile")
        self.assertEqual(s["intended_target"]["id"], 201)
        self.assertEqual(s["intended_target"]["type"], "Su-27")
        clock.t += 10
        eye.on_event(fx.hit(fx.PROMETHEUS, 5001, "AIM_120C", fx.UNITS[1]))
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["shots"][0]["result"], "hit")
        clock.t += 1
        eye.on_event(fx.kill(fx.PROMETHEUS, 5001, "AIM_120C", fx.UNITS[1]))
        pic = eye.picture()
        s = pic["my_shots_last_15_min"]["shots"][0]
        self.assertEqual(s["result"], "kill")
        self.assertEqual(s["hit"]["type"], "Su-27")
        self.assertEqual(group(pic, "Su-27")["count"], 1)  # the dead one is gone from the alive picture
        kills = pic["recent_kills_and_losses_last_10_min"]
        self.assertEqual(kills[0]["event"], "killed")
        self.assertTrue(kills[0]["by_me"])
        self.assertEqual(kills[0]["victim"]["type"], "Su-27")
        self.assertEqual(kills[0]["where"]["clock"], "11 o'clock, ahead left")
        self.assertFalse(kills[0]["friendly_loss"])
        # DCS follows the kill with dead/unit_lost for the same unit: still one row, killer kept
        eye.on_event(fx.dead(fx.UNITS[1]))
        eye.on_event({"time": 112.0, "unit_lost": {"initiator": {"unit": fx.UNITS[1]}}})
        kills = eye.picture()["recent_kills_and_losses_last_10_min"]
        # Round 2: the Frogfoot left the unit stream without a death event -> listed as inferred.
        inferred = [k for k in kills if k.get("inferred")]
        self.assertEqual([k["victim"]["type"] for k in inferred], ["Su-25T"])
        self.assertEqual(inferred[0]["event"], "no longer showing, likely destroyed")
        kills = [k for k in kills if not k.get("inferred")]
        self.assertEqual(len(kills), 1)
        self.assertEqual(kills[0]["event"], "killed")
        self.assertEqual(kills[0]["killer"]["player"], "Prometheus")
        # stays a kill after the miss window passes
        clock.t += 300
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["shots"][0]["result"], "kill")

    def test_missile_miss_after_window(self):
        eye, clock = loaded()
        eye.on_event(fx.shot(fx.PROMETHEUS, 5002, "AIM_9X"))
        clock.t += 60
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["shots"][0]["result"], "pending")
        clock.t += 61
        pic = eye.picture()
        self.assertEqual(pic["my_shots_last_15_min"]["shots"][0]["result"], "miss")
        self.assertEqual(pic["my_shots_last_15_min"]["by_result"], {"miss": 1})
        clock.t += 15 * 60
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["total"], 0)

    def test_weapon_name_match_without_id(self):
        eye, clock = loaded()
        eye.on_event(fx.shot(fx.PROMETHEUS, 7001, "AGM_65D"))
        eye.on_event(fx.gun_hit(fx.PROMETHEUS, "AGM_65D", fx.UNITS[5]))  # hit with weapon_name only
        s = eye.picture()["my_shots_last_15_min"]["shots"][0]
        self.assertEqual(s["result"], "hit")
        self.assertEqual(s["hit"]["type"], "T-72B")

    def test_gun_burst_hit_then_dead_credits_kill(self):
        eye, clock = loaded()
        eye.on_event(fx.shooting("shooting_start", fx.PROMETHEUS, "M61_20_HE"))
        s = eye.picture()["my_shots_last_15_min"]["shots"][0]
        self.assertEqual(s["kind"], "gun")
        self.assertEqual(s["intended_target"]["type"], "Su-25T")
        clock.t += 1
        eye.on_event(fx.gun_hit(fx.PROMETHEUS, "M61_20_HE", fx.UNITS[8]))
        eye.on_event(fx.gun_hit(fx.PROMETHEUS, "M61_20_HE", fx.UNITS[8]))
        eye.on_event(fx.shooting("shooting_end", fx.PROMETHEUS, "M61_20_HE"))
        clock.t += 3
        eye.on_event(fx.dead(fx.UNITS[8]))
        pic = eye.picture()
        s = pic["my_shots_last_15_min"]["shots"][0]
        self.assertEqual(s["result"], "kill")
        self.assertEqual(s["hits"], 2)
        self.assertIsNone(group(pic, "Su-25T"))
        self.assertEqual(pic["recent_kills_and_losses_last_10_min"][0]["event"], "destroyed")

    def test_gun_burst_without_hits_misses(self):
        eye, clock = loaded()
        eye.on_event(fx.shooting("shooting_start", fx.PROMETHEUS, "M61_20_HE"))
        clock.t += 2
        eye.on_event(fx.shooting("shooting_end", fx.PROMETHEUS, "M61_20_HE"))
        clock.t += 13
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["shots"][0]["result"], "miss")

    def test_global_kills_and_hits_on_me(self):
        eye, clock = loaded()
        viper = fx.UNITS[9]
        eye.on_event(fx.kill(viper, 9001, "AGM_88C", fx.UNITS[4]))
        eye.on_event(fx.hit(fx.UNITS[8], 9100, "R-60M", fx.PROMETHEUS))
        eye.on_event(fx.shot(viper, 9002, "AIM_120C"))  # other people's shots do not become my shots
        pic = eye.picture()
        self.assertEqual(pic["my_shots_last_15_min"]["total"], 0)
        k = pic["recent_kills_and_losses_last_10_min"][0]
        self.assertEqual(k["victim"]["type"], "SA-11 Buk LN 9A310M1")
        self.assertEqual(k["killer"]["name"], "Viper 2-1")
        self.assertFalse(k["by_me"])
        self.assertEqual(k["weapon"], "AGM_88C")
        self.assertEqual(group(pic, "SA-11 Buk SR 9S18M1")["count"], 1)
        self.assertTrue(any("PROMETHEUS WAS HIT" in line for line in pic["recent_events_last_10_min"]))


class FakeTranscript:
    def __init__(self):
        self.records = []
        self.lock = threading.Lock()

    def log(self, **record):
        with self.lock:
            self.records.append(record)
        return True


def req(text, tid="t1", speaker="Prometheus"):
    return Request(tid, text, speaker, 133000000, "AM", "2026-11-07T00:00:00Z", "2026-11-07T00:00:00Z")


class ControllerTests(unittest.TestCase):
    def controller(self, llm, eye=None):
        eye = eye or loaded()[0]
        return SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm)

    def test_answers_every_pilot_transmission_with_picture(self):
        seen = []

        def llm(messages):
            seen.append(messages)
            return "Prometheus, Athena, two bandits, bearing zero six zero, twenty miles, angels twenty five, your eleven o'clock, hot."
        c = self.controller(llm)
        parts = list(c.stream(req("picture")))  # no "Athena" prefix needed
        self.assertTrue(parts[0]["deliverable"])
        self.assertIn("bearing zero six zero", " ".join(p["speech"] for p in parts))
        picture = [m for m in seen[0] if m["content"].startswith("PICTURE ")][0]["content"]
        data = json.loads(picture[len("PICTURE "):])
        self.assertEqual(data["ownship"]["type"], "F-16C_50")
        self.assertIn("hostile_groups", data)
        kinds = [r["kind"] for r in c.transcript.records]
        self.assertEqual(kinds[0], "adapter_ingress")
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        self.assertEqual(tool["result"]["summary"]["ownship"], "F-16C_50")
        self.assertIn("reply_generated", kinds)

    def test_other_speakers_and_empty_ignored(self):
        c = self.controller(lambda m: "x")
        self.assertFalse(list(c.stream(req("picture", speaker="Someone")))[0]["deliverable"])
        self.assertFalse(list(c.stream(req("...")))[0]["deliverable"])

    def test_llm_retry_then_fallback(self):
        calls = []

        def flaky(messages):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "Prometheus, Athena, copy."
        c = self.controller(flaky)
        self.assertEqual(c.exchange(req("radio check"))["speech"], "Prometheus, Athena, copy.")

        def dead(messages):
            raise RuntimeError("down")
        c = self.controller(dead)
        self.assertEqual(c.exchange(req("radio check"))["speech"], FALLBACK)

    def test_picture_retried_once(self):
        eye, _ = loaded()
        real = eye.picture
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return real()
        eye.picture = flaky
        seen = []
        c = self.controller(lambda m: seen.append(m) or "Prometheus, Athena, copy.", eye)
        c.exchange(req("picture"))
        self.assertEqual(len(calls), 2)
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        self.assertEqual(tool["outcome"], "ok")

    def test_queue_while_busy_answers_next(self):
        release = threading.Event()
        order = []

        def llm(messages):
            text = messages[-1]["content"]
            order.append(("start", text))
            if text == "first":
                release.wait(2)
            order.append(("end", text))
            return "Prometheus, Athena, answer to %s." % text
        c = self.controller(llm)
        results = {}

        def run(text, tid):
            results[text] = c.exchange(req(text, tid))
        a = threading.Thread(target=run, args=("first", "a"))
        a.start()
        time.sleep(0.1)
        b = threading.Thread(target=run, args=("second", "b"))
        b.start()
        time.sleep(0.1)
        self.assertNotIn(("start", "second"), order)  # waiting, not dropped
        release.set()
        a.join(3)
        b.join(3)
        self.assertEqual(results["first"]["speech"], "Prometheus, Athena, answer to first.")
        self.assertEqual(results["second"]["speech"], "Prometheus, Athena, answer to second.")
        self.assertEqual([o for o in order if o[0] == "start"], [("start", "first"), ("start", "second")])
        self.assertTrue(any(r.get("outcome") == "queued_behind_previous" for r in c.transcript.records))

    def test_server_queues_instead_of_503(self):
        from integrations.hermes_radio.fixture_data import bridge_request
        from integrations.hermes_radio.server import RadioServer
        from integrations.hermes_radio.tests.support import post
        release = threading.Event()

        def llm(messages):
            if messages[-1]["content"] == "first":
                release.wait(2)
            return "Prometheus, Athena, copy %s." % messages[-1]["content"]
        server = RadioServer(self.controller(llm), port=0, request_timeout=1.0)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        port = server.server_address[1]
        results = {}
        try:
            def send(text):
                body = dict(bridge_request(text), transmission_id="id-" + text)
                results[text] = post(port, json.dumps(body).encode())
            a = threading.Thread(target=send, args=("first",))
            a.start()
            time.sleep(0.15)
            b = threading.Thread(target=send, args=("second",))
            b.start()
            time.sleep(0.15)
            release.set()
            a.join(4)
            b.join(4)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
        self.assertEqual(results["first"][0], 200)
        self.assertEqual(results["second"][0], 200)
        self.assertIn("copy second", json.loads(results["second"][1])["speech"])

    def test_split_parts(self):
        answer = "Prometheus, Athena, picture. " + " ".join("Group %d, bearing zero four zero, ten miles." % i
                                                         for i in range(30))
        parts = split_parts(answer)
        self.assertEqual(parts[0], "Prometheus, Athena, picture.")
        self.assertTrue(all(len(p) <= 420 for p in parts))
        self.assertEqual(" ".join(parts), answer)
        self.assertEqual(split_parts("Prometheus, Athena, loud and clear."), ["Prometheus, Athena, loud and clear."])


if __name__ == "__main__":
    unittest.main()
