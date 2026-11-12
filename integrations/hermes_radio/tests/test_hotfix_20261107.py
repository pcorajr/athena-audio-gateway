"""2026-11-07 live hotfixes: multi-name pilot list, vertical (high/low/level/ground), BRAA-then-clock prompt."""
import json
import unittest

from integrations.hermes_radio.contract import ContractError, parse_request, speaker_matches
from integrations.hermes_radio.eye import PLAYER, Eye, _is_pilot
from integrations.hermes_radio.tests.bridge import bridge_request
from integrations.hermes_radio.simple_controller import PROMPT, SimpleController
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye import Clock, FakeTranscript, group, loaded, req

PILOTS = "Prometheus,Player,Client"


class PilotListTests(unittest.TestCase):
    def test_speaker_matches_list_case_insensitive(self):
        for name in ("Prometheus", "prometheus", "player", "PLAYER", " Client "):
            with self.subTest(name=name):
                self.assertTrue(speaker_matches(name, PILOTS))
                self.assertTrue(_is_pilot(name, PILOTS))
        for name in ("", " ", None, "Players", "Prometheus 2", "Athena", "Player,Client"):
            with self.subTest(name=name):
                self.assertFalse(speaker_matches(name, PILOTS))
                self.assertFalse(_is_pilot(name, PILOTS))
        self.assertFalse(speaker_matches("", "Prometheus,,Client"))
        self.assertTrue(speaker_matches("prometheus", "Prometheus"))

    def test_default_lists_agree(self):
        self.assertEqual(PLAYER, PILOTS)
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=loaded()[0], llm=lambda m: "x")
        self.assertEqual(c.speaker, PILOTS)

    def test_parse_request_accepts_any_listed_pilot(self):
        for name in ("Prometheus", "player", "Client"):
            raw = json.dumps(bridge_request() | {"speaker": name}).encode()
            self.assertEqual(parse_request(raw, speaker=PILOTS).speaker, name)
        with self.assertRaises(ContractError):
            parse_request(json.dumps(bridge_request() | {"speaker": "Someone"}).encode(), speaker=PILOTS)

    def test_controller_answers_single_player_name(self):
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=loaded()[0],
                             llm=lambda m: "Prometheus, Athena, loud and clear.")
        self.assertTrue(list(c.stream(req("radio check", speaker="player")))[0]["deliverable"])
        self.assertFalse(list(c.stream(req("radio check", tid="t2", speaker="Someone")))[0]["deliverable"])
        narrow = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=loaded()[0],
                                  llm=lambda m: "x", speaker="Prometheus")
        self.assertFalse(list(narrow.stream(req("radio check", speaker="player")))[0]["deliverable"])

    def test_eye_ownship_by_single_player_name(self):
        eye = Eye(PILOTS, clock=Clock(), source="fixture")
        eye.set_connected(True)
        me = dict(fx.PROMETHEUS, player_name="player")
        for u in [me] + fx.UNITS[1:]:
            eye.on_units_message(fx.units_message(u))
        self.assertIsNotNone(eye.ownship())
        pic = eye.picture()
        self.assertEqual(pic["ownship"]["type"], "F-16C_50")
        self.assertIsNotNone(group(pic, "Su-27"))
        self.assertIsNone(Eye("Prometheus", clock=Clock()).ownship())


class VerticalTests(unittest.TestCase):
    def test_air_high_low_level_and_ground_target(self):
        pic = loaded()[0].picture()
        self.assertEqual(group(pic, "Su-27")["vertical"], "high")             # 25k vs 20k ft
        self.assertEqual(group(pic, "Su-25T")["vertical"], "level")           # ~300 ft below
        self.assertEqual(group(pic, "F-16C_50", "friendly_groups")["vertical"], "low")  # ~16k vs 20k ft
        self.assertEqual(group(pic, "SA-11 Buk SR 9S18M1")["vertical"], "ground target")
        self.assertEqual(group(pic, "T-72B")["vertical"], "ground target")
        self.assertNotIn("angels", group(pic, "T-72B"))

    def test_vertical_threshold_is_1000_ft(self):
        eye = Eye(clock=Clock(), source="fixture")
        eye.set_connected(True)
        eye.on_units_message(fx.units_message(fx.PROMETHEUS))  # 6096 m = 20000 ft
        cases = {701: (6096 + 290, 30), 702: (6096 + 320, 150), 703: (6096 - 320, 300)}  # alt m, bearing
        for uid, (alt, brg) in cases.items():
            lat, lon = fx.offset(brg, 15)
            eye.on_units_message(fx.units_message(fx.unit(uid, "B%d" % uid, "Su-27", "red", "G%d" % uid,
                                                          "AIRPLANE", lat, lon, alt, 0.0, 200.0)))
        pic = eye.picture()
        got = {g["groups"][0]: g["vertical"] for g in pic["hostile_groups"]}
        self.assertEqual(got, {"G701": "level", "G702": "high", "G703": "low"})


class PromptTests(unittest.TestCase):
    def test_braa_then_clock_and_basic_atc(self):
        self.assertIn("BRA first then clock", PROMPT)
        air = PROMPT.index("Su-27 two-ship, zero three eight, twelve, angels fifteen, ten o'clock high.")
        ground = PROMPT.index("bearing zero three eight, three miles, ground target, eleven o'clock.")
        self.assertLess(air, ground)
        self.assertIn("Basic ATC", PROMPT)
        self.assertIn("never clears or approves anything: no clearance, runway, wind or altimeter", PROMPT)  # 11-11


if __name__ == "__main__":
    unittest.main()
