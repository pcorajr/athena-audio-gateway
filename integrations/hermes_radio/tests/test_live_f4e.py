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


class SpeechAliasTests(unittest.TestCase):
    def test_whisper_mishearings_of_kills(self):
        from integrations.hermes_radio.simple_controller import call_class
        for text in ("Athena, confirmed chaos.", "Athena, confirm chaos.", "Confirm kills."):
            self.assertEqual(call_class(text, {"last_attack_age_s": 30}), "kill_confirm", text)


class DebriefPhraseTests(unittest.TestCase):
    def test_generate_briefing_requests_debrief(self):
        from integrations.hermes_radio.simple_controller import DEBRIEF_ASK, PROMPT
        for text in ("Athena, Prometheus, generate briefing.", "Athena, create the debrief", "write my report",
                     "Athena, mission end. Please provide briefing."):
            self.assertTrue(DEBRIEF_ASK.search(text), text)
        self.assertFalse(DEBRIEF_ASK.search("Athena, mission status"))
        self.assertIn("OVERVIEW", PROMPT)


class Radio7ReviewTests(unittest.TestCase):
    """RV-1 radio7: mission status overview survives, scoped splash suppression, alert key groups."""

    def test_mission_status_keeps_the_model_overview(self):
        from integrations.hermes_radio.simple_controller import call_class
        self.assertIsNone(call_class("Athena, mission status", {"last_attack_age_s": None}))
        pic = base()
        pic["picture_top3"] = ["Bandit MiG-15bis, bearing three four zero, forty four miles, angels twenty three."]
        overview = "Prometheus, Athena, nine kills so far, seven enemy armor remain, fuel seven thousand pounds."
        eye = FakeEye(pic)
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: overview)
        self.assertIn("nine kills so far", c.exchange(req("Athena, what's the mission status?"))["speech"])

    def test_filtered_confirmation_leaves_unrelated_splash_pending(self):
        pic = base()
        pic["last_attack_age_s"] = 20
        pic["my_kills_this_mission"] = [{"target": "T-55", "category": "armor", "weapon": "GBU-38", "age_s": 5,
                                         "seq": 1, "confidence": "confirmed"}]
        pic["splash_to_announce"] = ["Splash one T-55."]
        pic["_splash_keys"] = ["k-t55"]
        for text in ("Athena, confirm wingman kill", "Athena, confirm air defense kill"):
            eye = FakeEye(dict(pic))
            c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                 llm=lambda messages: "LLM")
            speech = c.exchange(req(text))["speech"]
            # the T-55 is not part of this confirmation: it is announced as its own splash (or left pending)
            if "Splash one T-55" not in speech:
                self.assertNotIn("k-t55", eye.marked, text)
            else:
                self.assertIn("k-t55", eye.marked, text)
        eye = FakeEye(dict(pic))
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: "LLM")
        speech = c.exchange(req("Athena, confirm kill"))["speech"]
        self.assertEqual(speech.lower().count("splash one t-55"), 1)
        self.assertIn("k-t55", eye.marked)


class AlertGroupTests(unittest.TestCase):
    def test_duplicates_of_a_dropped_call_stay_pending(self):
        from integrations.hermes_radio.eye import group_alerts
        alerts = [{"call": "A.", "key": "a"}, {"call": "B.", "key": "b"}, {"call": "C.", "key": "c-old"},
                  {"call": "C.", "key": "c-new"}, {"call": "A.", "key": "a2"}]
        calls, keys = group_alerts(alerts)
        self.assertEqual(calls, ["A.", "B."])
        self.assertEqual(keys, [["a", "a2"], "b"])
        flat = [x for k in keys for x in (k if isinstance(k, list) else [k])]
        self.assertNotIn("c-new", flat)
        self.assertNotIn("c-old", flat)
        # next turn: A and B acknowledged, C-old expired -> C-new is still announced
        self.assertEqual(group_alerts([{"call": "C.", "key": "c-new"}]), (["C."], ["c-new"]))


if __name__ == "__main__":
    unittest.main()
