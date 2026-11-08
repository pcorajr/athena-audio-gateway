"""F-4E live session 2026-11-08 15:17: S-60 57 mm AAA shells (53-UOR-281U) were read as six 'Missile launch'
calls in one transmission (speech_duration_exceeded); 'confirm hit' read the splash twice."""
import unittest

from integrations.hermes_radio.eye import weapon_kind
from integrations.hermes_radio.simple_controller import SimpleController
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req


class ShellTests(unittest.TestCase):
    def test_aaa_shells_are_guns(self):
        for name in ("53-UOR-281U", "weapons.shells.53-UOR-281U", "53-UOF-412", "weapons.shells.ZU_23_HEI_T",
                     "57mm HE", "KS-19 100mm", "weapons.shells.GAU8-30-AP", "OFZ-23", "M61_20_HE"):
            self.assertEqual(weapon_kind(name), "gun", name)
        for name in ("SA-2 V-750", "AIM-9M", "R-60", "9M33", "weapons.missiles.SA2V755"):
            self.assertNotEqual(weapon_kind(name), "gun", name)


class FakeEye:
    def __init__(self, pic):
        self.pic, self.marked = pic, []

    def picture(self):
        return dict(self.pic)

    def mark_announced(self, keys):
        self.marked.extend(keys)


def base():
    return {"connected": True, "ownship": {"type": "F-4E-45MC", "coalition": "blue"}, "data_age_s": {"units": 1},
            "my_kills_this_mission": [], "alive_totals_by_coalition": {}}


class AlertTests(unittest.TestCase):
    def controller(self, eye):
        return SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                llm=lambda messages: "Prometheus, Athena, no blue traffic near you.")

    def test_repeated_alert_text_is_read_once(self):
        pic = base()
        call = "Missile launch, SA-2, your two o'clock, three miles."
        pic["rwr_alert_to_announce"] = [call] * 2
        pic["_rwr_keys"] = ["a", "b"]
        eye = FakeEye(pic)
        speech = self.controller(eye).exchange(req("Athena, confirm kill"))["speech"]
        self.assertEqual(speech.count("Missile launch"), 1)

    def test_confirm_does_not_repeat_the_splash(self):
        pic = base()
        pic["my_kills_this_mission"] = [{"target": "Ural-375 ZU-23", "category": "AAA", "weapon": "AGM-65D",
                                         "age_s": 5, "seq": 1, "confidence": "confirmed"}]
        pic["last_attack_age_s"] = 20
        pic["splash_to_announce"] = ["Splash one Ural-375 ZU-23"]
        pic["_splash_keys"] = ["k1"]
        eye = FakeEye(pic)
        speech = self.controller(eye).exchange(req("Athena, confirm head."))["speech"]
        self.assertEqual(speech.lower().count("splash one ural-375 zu-23"), 1, speech)
        self.assertIn("k1", eye.marked)


if __name__ == "__main__":
    unittest.main()
