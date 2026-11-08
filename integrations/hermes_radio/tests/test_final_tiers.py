"""Final round 2026-11-08: range tiers (20 nm ground + close bandits, 50 nm picture, 150 nm long range) and the
spoken debrief summary."""
import unittest

from integrations.hermes_radio.simple_controller import SimpleController, call_class, debrief_summary
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req
from integrations.hermes_radio.tests.test_eye_round5 import contact, make_eye


def speech(eye, text):
    c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                         llm=lambda messages: "Prometheus, Athena, LLM.")
    return c.exchange(req(text))["speech"]


class TierTests(unittest.TestCase):
    def test_bandit_beyond_fifty_is_reported_out_to_150(self):
        eye, _ = make_eye(contact(1, "F-5E-3", distance=60))
        pic = eye.picture()
        self.assertEqual(pic["picture_top3"], [])
        self.assertTrue(pic["air_picture_empty_call"].startswith("Nothing inside fifty miles."))
        self.assertIn("F-5E-3", pic["air_picture_empty_call"])
        said = speech(eye, "Athena, bogey dope")
        self.assertIn("Nothing inside fifty miles", said)
        self.assertIn("sixty", said)

    def test_nothing_within_150(self):
        eye, _ = make_eye(contact(1, "F-5E-3", distance=170))
        self.assertIn("No bandits within one hundred fifty miles", speech(eye, "Athena, bogey dope"))

    def test_long_range_picture(self):
        eye, _ = make_eye(*(contact(i, "F-5E-3", distance=d) for i, d in enumerate((30, 90, 140, 160), 1)))
        self.assertEqual(call_class("Athena, long range picture"), "long_picture")
        said = speech(eye, "Athena, long range picture")
        self.assertEqual(said.count("Bandit"), 3, said)
        self.assertNotIn("one hundred sixty", said)

    def test_ground_targets_only_within_20(self):
        eye, _ = make_eye(contact(1, "T-55", "red", "GROUND", distance=12), contact(2, "BMP-1", "red", "GROUND", distance=35))
        pic = eye.picture()
        self.assertEqual(len(pic["ground_targets_within_20nm"]), 1)
        self.assertIn("twelve miles", pic["ground_targets_within_20nm"][0])
        far, _ = make_eye(contact(2, "BMP-1", "red", "GROUND", distance=35))
        self.assertIn("No ground targets within twenty miles", speech(far, "Athena, ground picture"))

    def test_close_bandit_is_announced_once(self):
        eye, _ = make_eye(contact(1, "MiG-29S", distance=15))
        first = speech(eye, "Athena, rifle")
        self.assertIn("MiG-29S", first)
        second = speech(eye, "Athena, rifle")
        self.assertNotIn("MiG-29S", second)


class DebriefSummaryTests(unittest.TestCase):
    def test_summary_from_tally_and_weapons(self):
        pic = {"my_kills_this_mission": [{"target": "F-5E-3", "weapon": "AIM-9", "confidence": "confirmed"}] * 3,
               "my_weapons_used_this_mission": {"by_weapon": {"AIM-9": 4, "AIM-54C-Mk47": 2}}}
        text = debrief_summary(pic)
        self.assertIn("Three kills: three F-5E-3 with AIM-9.", text)
        self.assertIn("AIM-9 four fired, three kills", text)
        self.assertIn("AIM-54C-Mk47 two fired, no kills", text)
        self.assertEqual(debrief_summary({}), "No confirmed kills.")


if __name__ == "__main__":
    unittest.main()
