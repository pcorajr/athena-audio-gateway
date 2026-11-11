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
                                        "Athena, request landing at Incirlik")
        for text in asks:
            with self.subTest(text=text):
                llm = Stub(answer="Prometheus, Athena, no hostile air groups within fifty miles.")
                c, _ = controller(LIVE["picture"], llm)
                frames, _ = say(c, text)
                self.assertEqual(frames[0]["speech"], self.SAID)
                self.assertEqual(replies(c)[0]["route"], "fixed")
                self.assertEqual(llm.calls, 0)

    def test_questions_that_need_no_position_still_go_their_normal_way(self):
        for text in ("Athena, Prometheus, radio check", "Athena, Prometheus, how many friendly aircraft are airborne",
                     "Athena, Prometheus, confirm kills", "Athena, Prometheus, say my fuel state",
                     "Athena, splash two tanks", "Athena, copy"):
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
        self.assertIn("no_position_call", PROMPT)


if __name__ == "__main__":
    unittest.main()
