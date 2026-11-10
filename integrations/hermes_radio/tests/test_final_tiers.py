"""Final round 2026-11-08: range tiers (20 nm ground + close bandits, 50 nm picture, 150 nm long range) and the
spoken debrief summary."""
import unittest

from integrations.hermes_radio.simple_controller import MAX_REPLY_CHARS, SimpleController, call_class, debrief_summary
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
        self.assertIn("Bandit F-5E-3 one-ship, bearing zero nine zero, thirty miles", said)  # nearest first
        self.assertIn("Plus two more.", said, said)  # phase 0 budget: the rest are counted, not read
        self.assertLessEqual(len(said), MAX_REPLY_CHARS)
        self.assertNotIn("one hundred sixty", said)

    def test_ground_targets_only_within_20(self):
        eye, _ = make_eye(contact(1, "T-55", "red", "GROUND", distance=12), contact(2, "BMP-1", "red", "GROUND", distance=35))
        pic = eye.picture()
        self.assertEqual(len(pic["ground_targets_within_20nm"]), 1)
        self.assertIn("twelve miles", pic["ground_targets_within_20nm"][0]["call"])
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
        self.assertIn("Three kills: three other targets.", text)  # round 9: broad categories, one short summary
        self.assertIn("Four Sidewinders and two Phoenixes fired.", text)
        self.assertEqual(debrief_summary({}), "No confirmed kills.")


class Final8ReviewTests(unittest.TestCase):
    def controller(self, eye, llm=None):
        return SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                llm=llm or (lambda messages: "Prometheus, Athena, loud and clear."))

    def test_close_bandit_survives_background_pictures_and_uses_current_geometry(self):
        eye, clock = make_eye(contact(1, "MiG-29S", distance=19))
        for _ in range(5):
            eye.picture()  # memory loop polls the picture every second before the pilot speaks
        for name in ("advance", "tick", "step"):
            if hasattr(clock, name):
                getattr(clock, name)(120)
                break
        else:
            clock.t = getattr(clock, "t", 0) + 120
        eye.on_unit(contact(1, "MiG-29S", distance=5))
        said = self.controller(eye).exchange(req("Athena, radio check"))["speech"]
        self.assertIn("MiG-29S", said)
        self.assertIn("five miles", said)
        self.assertNotIn("MiG-29S", self.controller(eye).exchange(req("Athena, radio check"))["speech"])

    def test_close_bandit_on_model_reply_and_on_model_failure(self):
        eye, _ = make_eye(contact(1, "MiG-29S", distance=10))

        def broken(messages):
            raise TimeoutError("model down")
        said = self.controller(eye, broken).exchange(req("Athena, radio check"))["speech"]
        self.assertIn("MiG-29S", said)

    def test_ground_picture_filters_category_before_cap(self):
        units = [contact(1, "Ural-375", "red", "GROUND", distance=5), contact(2, "T-55", "red", "GROUND", distance=7),
                 contact(3, "ZSU-23-4 Shilka", "red", "GROUND", distance=9),
                 contact(4, "Infantry AK", "red", "GROUND", distance=15)]
        eye, _ = make_eye(*units)
        said = speech(eye, "Athena, infantry picture")
        self.assertIn("fifteen miles", said)
        self.assertNotIn("five miles", said)
        none, _ = make_eye(contact(1, "Ural-375", "red", "GROUND", distance=5))
        self.assertIn("No enemy infantry within twenty miles", speech(none, "Athena, infantry picture"))

    def test_gun_kills_match_gun_usage(self):
        pic = {"my_kills_this_mission": [{"target": "T-72B", "weapon": "GAU-8", "confidence": "confirmed"}],
               "my_weapons_used_this_mission": {"by_weapon": {"gun rounds": 150}}}
        text = debrief_summary(pic)
        self.assertIn("Guns: one kill.", text)  # round 9: gun rounds are never 'fired' weapons
        self.assertNotIn("fired", text)

    def test_failed_stream_falls_back_even_with_done_text(self):
        import json
        from integrations.hermes_radio.codex_llm import parse_sse
        lines = ["data: " + json.dumps({"type": "response.output_text.done", "text": "partial"}),
                 "data: " + json.dumps({"type": "response.failed", "response": {}})]
        self.assertEqual(parse_sse(lines)[0], 502)


class Final8bReviewTests(unittest.TestCase):
    def test_many_close_bandits_fit_two_frames_and_stay_pending(self):
        eye, _ = make_eye(*(contact(i, "MiG-29S", distance=5 + i * 2, bearing=i * 60) for i in range(1, 6)))
        for llm in (lambda m: "Prometheus, Athena, loud and clear.", None):
            c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                 llm=llm or (lambda m: (_ for _ in ()).throw(TimeoutError("down"))))
            frames = list(c.stream(req("Athena, radio check")))
            self.assertLessEqual(len(frames), 2)
        pending = eye.picture().get("_close_bandits") or []
        self.assertGreaterEqual(len(pending), 1)  # not all five were consumed in two replies

    def test_debrief_reply_carries_close_bandit(self):
        class Memory:
            generation = 1
            identity = "fixture"

            def schedule_debrief(self):
                return True

            def publish(self, *a, **k):
                pass

            def summary(self):
                return None
        eye, _ = make_eye(contact(1, "MiG-29S", distance=5))
        eye.memory = Memory()
        said = speech(eye, "Athena, generate briefing")
        self.assertIn("debrief written", said)
        self.assertIn("MiG-29S", said)

    def test_ground_selection_uncapped_mixed_groups_and_friendly(self):
        units = [contact(i, "Ural-375", "red", "GROUND", distance=2 + i, bearing=i * 25) for i in range(1, 14)]
        units.append(contact(40, "Infantry AK", "red", "GROUND", distance=18, bearing=200))
        eye, _ = make_eye(*units)
        self.assertIn("eighteen miles", speech(eye, "Athena, infantry picture"))
        mixed, _ = make_eye(contact(1, "Ural-375", "red", "GROUND", distance=6, group="g"),
                            contact(2, "Infantry AK", "red", "GROUND", distance=6.05, group="g"),
                            contact(3, "Infantry AK", "red", "GROUND", distance=6.1, group="g"))
        self.assertNotIn("No enemy infantry", speech(mixed, "Athena, infantry picture"))
        self.assertEqual(speech(mixed, "Athena, friendly ground picture"), "Prometheus, Athena, LLM.")

    def test_incomplete_response_falls_back(self):
        import json
        from integrations.hermes_radio.codex_llm import parse_sse
        lines = ["data: " + json.dumps({"type": "response.output_text.done", "text": "partial"}),
                 "data: " + json.dumps({"type": "response.incomplete", "response": {}})]
        self.assertEqual(parse_sse(lines)[0], 502)


class Final8cReviewTests(unittest.TestCase):
    def test_close_bandit_goes_first_and_the_ground_reply_fills_the_rest(self):
        units = [contact(i, "Ural-375", "red", "GROUND", distance=3 + i * 2, bearing=i * 40) for i in range(1, 4)]
        units.append(contact(9, "MiG-29S", distance=5, bearing=300))
        eye, _ = make_eye(*units)
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda m: "Prometheus, Athena, LLM.")
        frames = list(c.stream(req("Athena, ground picture")))
        said = " ".join(f["speech"] for f in frames)
        self.assertEqual(len(frames), 1)
        self.assertLessEqual(len(said), MAX_REPLY_CHARS)
        self.assertTrue(said.startswith("Prometheus, Athena, Bandit MiG-29S"), said)
        self.assertTrue(said.endswith("Ask again for the rest."), said)  # no ground call fits behind it
        again = " ".join(f["speech"] for f in c.stream(req("Athena, ground picture")))
        self.assertNotIn("MiG-29S", again)  # the bandit was said once
        self.assertIn("ground target", again)
        self.assertIn("Plus two more.", again)


if __name__ == "__main__":
    unittest.main()
