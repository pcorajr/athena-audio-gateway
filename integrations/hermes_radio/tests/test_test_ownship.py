"""Test-only stand-in pilot (ATHENA_TEST_OWNSHIP) and honest answers when Athena has no pilot position.
The no-ownship replay is the live adapter transcript of 2026-11-11 (DCS server, Syria, no human player)."""
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from integrations.hermes_radio import eye as eye_mod
from integrations.hermes_radio.eye import Eye
from integrations.hermes_radio.simple_controller import NO_POSITION, PROMPT
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, replies, say

LIVE = json.loads((Path(__file__).parent / "fixtures" / "no_ownship_live_20261111.json").read_text())
STAND_IN = "Viper 2-1"  # an AI blue F-16 in the fixture


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeMemory:
    generation, identity = 1, None

    def __init__(self):
        self.rows = []

    def publish(self, kind, payload, **_):
        self.rows.append((kind, payload))

    def invalidate(self):
        pass


def ai_eye(test_ownship=None, units=None):
    eye = Eye(clock=Clock(), test_ownship=test_ownship)
    for u in (fx.UNITS[1:] if units is None else units):  # everything but the human pilot
        eye.on_units_message(fx.units_message(u))
    return eye


class StandInTests(unittest.TestCase):
    def test_off_by_default_an_ai_only_mission_has_no_ownship(self):
        pic = ai_eye().picture()
        self.assertIsNone(pic["ownship"])
        self.assertNotIn("test_ownship", pic)

    def test_stand_in_is_ownship_and_the_picture_says_so_plainly(self):
        eye = ai_eye(STAND_IN)
        eye.tick()
        pic = eye.picture()
        own = pic["ownship"]
        self.assertEqual((own["type"], own["group"], own["player_name"]), ("F-16C_50", "Viper 2", None))
        self.assertEqual(own["source"], "TEST stand-in pilot: Viper 2-1 (an AI unit, NOT a human pilot)")
        self.assertEqual(pic["test_ownship"], own["source"])
        self.assertTrue(any("TEST stand-in pilot: Viper 2-1" in n for n in pic["notes"]))
        self.assertTrue(pic["hostile_groups"])
        self.assertIn("bearing_true", pic["hostile_groups"][0])
        self.assertIn("range_nm", pic["hostile_groups"][0])
        self.assertTrue(eye._is_me({"kind": "unit", "id": 601}))
        self.assertEqual(eye.player_units()[0]["id"], 601)  # fuel/loadout poll asks DCS about the stand-in

    def test_a_real_player_always_wins(self):
        eye = ai_eye(STAND_IN, fx.UNITS)
        pic = eye.picture()
        self.assertEqual(pic["ownship"]["group"], "Viper 1")
        self.assertEqual(pic["ownship"]["player_name"], "Prometheus")
        self.assertNotIn("source", pic["ownship"])
        self.assertNotIn("test_ownship", pic)
        self.assertFalse(any("TEST stand-in" in n for n in pic["notes"]))
        self.assertFalse(eye._is_me({"kind": "unit", "id": 601}))

    def test_missing_or_dead_stand_in_is_no_ownship_with_a_clear_note(self):
        eye = ai_eye("No Such Unit")
        pic = eye.picture()
        self.assertIsNone(pic["ownship"])
        self.assertIn("TEST stand-in pilot unit 'No Such Unit' is not in the mission", pic["ownship_note"])
        eye = ai_eye(STAND_IN)
        self.assertIsNotNone(eye.picture()["ownship"])
        eye.on_units_message(fx.gone_message(601, STAND_IN))
        pic = eye.picture()
        self.assertIsNone(pic["ownship"])
        self.assertIn("TEST stand-in pilot unit 'Viper 2-1' is not in the mission", pic["ownship_note"])
        self.assertNotIn("test_ownship", pic)

    def test_memory_records_are_tagged_so_the_debrief_cannot_pass_it_off_as_a_human(self):
        for name, units in ((STAND_IN, fx.UNITS[1:]), (None, fx.UNITS)):
            eye = ai_eye(name, units)
            eye.memory = FakeMemory()
            shooter = fx.UNITS[-1] if name else fx.PROMETHEUS
            eye.on_event(fx.shot(shooter, 9001, "AIM-120C"))
            eye._log_store({"kind": "kill_by_me"})
            events = [p for k, p in eye.memory.rows if k == "event"]
            self.assertTrue(events and events[0]["mine"])
            for kind, payload in eye.memory.rows:
                if kind in ("event", "kill_by_me"):
                    self.assertEqual(payload.get("test_ownship"), name, (kind, payload))

    def test_build_reads_the_env_setting_and_logs_one_loud_line(self):
        with mock.patch.object(eye_mod, "GrpcFeeder"), mock.patch.dict(os.environ, {"ATHENA_TEST_OWNSHIP": STAND_IN}):
            os.environ.pop("ATHENA_DCS_SOURCE", None)
            os.environ.pop("ATHENA_MEMORY_ENABLED", None)
            with self.assertLogs(eye_mod.LOG, logging.WARNING) as logs:
                eye, _ = eye_mod.build("127.0.0.1", 1)
            self.assertEqual(eye.test_ownship, STAND_IN)
            self.assertEqual(len(logs.output), 1)
            self.assertIn("TEST STAND-IN PILOT", logs.output[0])
            self.assertIn(STAND_IN, logs.output[0])
        with mock.patch.object(eye_mod, "GrpcFeeder"), mock.patch.dict(os.environ, {"ATHENA_TEST_OWNSHIP": ""}):
            os.environ.pop("ATHENA_DCS_SOURCE", None)
            os.environ.pop("ATHENA_MEMORY_ENABLED", None)
            with self.assertNoLogs(eye_mod.LOG, logging.WARNING):
                eye, _ = eye_mod.build("127.0.0.1", 1)
            self.assertIsNone(eye.test_ownship)

    def test_adapter_flag_reaches_the_eye(self):
        from integrations.hermes_radio import __main__ as adapter
        seen = {}

        def build(*args, **kwargs):
            seen.update(kwargs)
            raise ValueError("stop here")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(eye_mod, "build", build), \
                mock.patch.object(sys, "argv", ["adapter", "--eye-host", "127.0.0.1", "--transcript-dir", tmp,
                                                "--test-ownship", STAND_IN]), \
                mock.patch.object(sys, "stderr"):
            self.assertEqual(adapter.main(), 1)
        self.assertEqual(seen.get("test_ownship"), STAND_IN)


class NoPositionEyeTests(unittest.TestCase):
    def test_no_ownship_picture_has_no_empty_all_clear_lists(self):
        pic = ai_eye().picture()
        threats = pic["threats"]
        self.assertIsNone(threats["sam_in_range"])
        self.assertIsNone(threats["missiles_inbound"])
        self.assertIn("no position", threats["note"])
        self.assertEqual(pic["no_position_call"], NO_POSITION)
        self.assertEqual(pic["alive_totals_by_coalition"]["red"]["fighter"], 2)  # coalition-wide facts stay


class NoPositionReplyTests(unittest.TestCase):
    """Live 2026-11-11 10:22: with no pilot unit Athena answered three questions with a false all-clear."""
    SAID = "Prometheus, Athena, " + NO_POSITION
    FALSE_ALL_CLEARS = ("Athena, Prometheus, picture", "Athena, Prometheus, what enemy ground units are out there",
                        "Athena, Prometheus, any SAM threats")

    def test_fixture_is_the_live_miss(self):
        live = {a["text"]: a["live_reply"] for a in LIVE["asks"]}
        self.assertIsNone(LIVE["picture"]["ownship"])
        self.assertEqual(live["Athena, Prometheus, picture"], "Prometheus, Athena, no hostile air groups within fifty miles.")
        self.assertEqual(live["Athena, Prometheus, what enemy ground units are out there"],
                         "Prometheus, Athena, no ground targets within twenty miles.")
        self.assertIn("no SAMs in range", live["Athena, Prometheus, any SAM threats"])

    def test_range_and_bearing_questions_say_no_position_without_the_model(self):
        asks = self.FALSE_ALL_CLEARS + ("Athena, bogey dope", "Athena, long range picture", "Athena, any traffic",
                                        "Athena, bearing to the nearest tanker", "Athena, where is the nearest airfield",
                                        "Athena, launch, launch", "Athena, what's painting me",
                                        "Athena, request landing at Incirlik",
                                        "Athena, any threats", "Athena, nearest friendly airfield",
                                        "Athena, what's the closest bandit", "Athena, am I clear", "Athena, RTB",
                                        "Athena, vector to home plate", "Athena, anything near me",
                                        "Athena, defending SA-2")
        for text in asks:
            with self.subTest(text=text):
                llm = Stub(answer="Prometheus, Athena, no hostile air groups within fifty miles.")
                c, _ = controller(LIVE["picture"], llm)
                frames, _ = say(c, text)
                expected = self.SAID.replace("Athena, ", "Athena, no tower: I can't give clearances. ") if "request landing" in text else self.SAID
                self.assertEqual(frames[0]["speech"], expected)
                self.assertEqual(replies(c)[0]["route"], "fixed")
                self.assertEqual(llm.calls, 0)

    def test_questions_that_need_no_position_still_go_their_normal_way(self):
        for text in ("Athena, Prometheus, radio check", "Athena, Prometheus, how many friendly aircraft are airborne",
                     "Athena, Prometheus, confirm kills", "Athena, Prometheus, say my fuel state",
                     "Athena, splash two tanks", "Athena, copy",
                     "Athena, did I kill that tank", "Athena, how many tanks are left",
                     "Athena, how many enemy ground units are alive", "Athena, how many vehicles has red lost",
                     "Athena, how's the ground war going"):
            with self.subTest(text=text):
                llm = Stub(answer="Prometheus, Athena, copy.")
                c, _ = controller(LIVE["picture"], llm)
                frames, _ = say(c, text)
                self.assertNotIn("position", frames[0]["speech"])
                self.assertEqual(llm.calls, 1)

    def test_with_an_ownship_nothing_changes(self):
        eye = ai_eye(None, fx.UNITS)
        eye.tick()
        pic = eye.picture()
        self.assertNotIn("no_position_call", pic)
        self.assertIsInstance(pic["threats"]["sam_in_range"], list)
        c, _ = controller({k: v for k, v in pic.items() if not k.startswith("_")}, Stub())
        frames, _ = say(c, "Athena, picture")
        self.assertNotIn("position", frames[0]["speech"])
        self.assertIn("Su-27 two-ship, zero six zero, twenty", frames[0]["speech"])

    def test_prompt_tells_the_model_the_same(self):
        self.assertIn("no position", PROMPT)


class StandInSafetyTests(unittest.TestCase):
    """Review 2026-11-11: the stand-in must never pass for the pilot (a test aid is not a person)."""
    def player(self, uid, name):
        return fx.unit(uid, name + "-1", "F-16C_50", "blue", name, "AIRPLANE", *fx.offset(300, 30), 5000.0, player=name)
    def test_any_player_unit_in_the_mission_disables_the_stand_in(self):
        eye = ai_eye(STAND_IN)
        self.assertIsNotNone(eye.picture()["ownship"])
        for other in (self.player(701, "Alice"), self.player(702, "Bob")):
            eye.on_units_message(fx.units_message(other))
        pic = eye.picture()  # two unknown players: r12 had no ownship, the stand-in must not step in
        self.assertIsNone(pic["ownship"])
        self.assertNotIn("test_ownship", pic)
    def test_a_single_unknown_player_is_the_pilot_not_the_stand_in(self):
        eye = ai_eye(STAND_IN)
        eye.on_units_message(fx.units_message(self.player(701, "Alice")))
        pic = eye.picture()
        self.assertEqual(pic["ownship"]["player_name"], "Alice")
        self.assertNotIn("test_ownship", pic)
    def test_once_a_real_pilot_was_ownship_the_stand_in_never_comes_back_until_a_new_mission(self):
        eye = ai_eye(STAND_IN, fx.UNITS)
        self.assertEqual(eye.picture()["ownship"]["player_name"], "Prometheus")
        eye.on_units_message(fx.gone_message(100, "Prometheus-1"))  # crash / slot change
        pic = eye.picture()
        self.assertIsNone(pic["ownship"])
        self.assertNotIn("test_ownship", pic)
        eye.reset("mission_start")
        for u in fx.UNITS[1:]:
            eye.on_units_message(fx.units_message(u))
        self.assertIn("test_ownship", eye.picture())  # a new mission: the stand-in is available again
    def test_a_fresh_cockpit_feed_disables_the_stand_in(self):
        eye = ai_eye(STAND_IN)
        eye.on_cockpit_packet({"v": 1, "ac": "A-10C_2", "unit": "Not in gRPC", "lat": fx.LAT0, "lon": fx.LON0,
                               "alt": 914, "hdg": 90, "coal": 2, "fu": 2, "fuel_int": 0.9, "warn": []})
        pic = eye.picture()
        self.assertEqual(pic["ownship"]["source"], "cockpit feed only (DCS-gRPC has no unit for Prometheus)")
        self.assertNotIn("test_ownship", pic)
    def test_the_stand_ins_kills_are_never_the_humans_after_he_appears(self):
        eye = ai_eye(STAND_IN)
        eye.tick()
        eye.on_event(fx.shot(fx.UNITS[-1], 9001, "AIM-120C", t=100.0))
        eye.on_event(fx.kill(fx.UNITS[-1], 9001, "AIM-120C", fx.UNITS[1], t=111.0))
        pic = eye.picture()
        self.assertEqual(len(pic["my_kills_this_mission"]), 1)  # while it is ownship it is labelled as the test
        self.assertIn("test_ownship", pic)
        eye.on_units_message(fx.units_message(fx.PROMETHEUS))
        pic = eye.picture()
        self.assertEqual(pic["ownship"]["player_name"], "Prometheus")
        self.assertEqual(pic["my_kills_this_mission"], [])
        self.assertEqual(pic["my_kills_recent"], [])
        c, _ = controller(None, Stub(answer="Prometheus, Athena, copy."), eye=eye)
        frames, _ = say(c, "Athena, confirm kills")
        self.assertNotIn("Su-27", frames[0]["speech"])
        self.assertNotIn("Test pilot", frames[0]["speech"])
class StandInMarkerTests(unittest.TestCase):
    START = "Prometheus, Athena, Test pilot. "
    def eye_and_controller(self, answer="Prometheus, Athena, copy.", name=STAND_IN, units=None):
        eye = ai_eye(name, units)
        eye.tick()
        llm = Stub(answer=answer)
        c, _ = controller(None, llm, eye=eye)
        return c, llm
    def test_fixed_replies_start_with_the_marker_and_still_fit(self):
        c, llm = self.eye_and_controller()
        for text in ("Athena, picture", "Athena, bogey dope", "Athena, any SAM threats", "Athena, nearest tanker",
                     "Athena, what ground targets are near me", "Athena, long range picture", "Athena, any traffic"):
            with self.subTest(text=text):
                frames, _ = say(c, text)
                speech = frames[0]["speech"]
                self.assertTrue(speech.startswith(self.START), speech)
                self.assertLessEqual(len(speech), 280)
    def test_model_replies_get_the_marker_and_keep_the_limit_and_the_first_sentence(self):
        long = "Prometheus, Athena, " + "bandits at one two zero for thirty miles, " * 12 + "end."
        c, llm = self.eye_and_controller(long)
        frames, _ = say(c, "Athena, tell me a story about the fight")
        speech = frames[0]["speech"]
        self.assertEqual(llm.calls, 1)
        self.assertTrue(speech.startswith(self.START), speech)
        self.assertLessEqual(len(speech), 280)
        c, llm = self.eye_and_controller("Prometheus, copy.")
        speech = say(c, "Athena, radio check")[0][0]["speech"]
        self.assertTrue(speech.startswith(self.START), speech)
    def test_a_close_threat_line_is_never_cut_for_the_marker(self):
        eye = ai_eye(STAND_IN)
        eye.tick()
        close = fx.unit(800, "Fox", "MiG-29S", "red", "Fox", "AIRPLANE", *fx.offset(30, 8, *fx.offset(200, 40)), 7000.0, 210.0, 250.0)
        eye.on_units_message(fx.units_message(close))
        eye.tick()
        long = "Prometheus, Athena, " + "bandits at one two zero for thirty miles, " * 12 + "end."
        c, _ = controller(None, Stub(answer=long), eye=eye)
        speech = say(c, "Athena, tell me a story about the fight")[0][0]["speech"]
        self.assertTrue(speech.startswith(self.START), speech)
        self.assertRegex(speech, r"(?i)MiG-29")
        self.assertLessEqual(len(speech), 280)
    def test_no_marker_for_a_real_pilot(self):
        c, _ = self.eye_and_controller(name=STAND_IN, units=fx.UNITS)
        for text in ("Athena, picture", "Athena, radio check"):
            self.assertNotIn("Test pilot", say(c, text)[0][0]["speech"])
if __name__ == "__main__":
    unittest.main()
