"""Phase 0 (2026-11-10 review): one reply budget, fixed replies skip the model (route label + latency),
'Athena, tally' = mission kill total, splash/threat/close-bandit calls marked said only when actually in
the fitted reply. Fixtures are real pictures from the 2026-11-09 transcript (debrief, ground picture, tally)."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from integrations.hermes_radio.contract import MAX_SPEECH_CHARS
from integrations.hermes_radio.simple_controller import (DEBRIEF_CHARS, FALLBACK, MAX_REPLY_CHARS, SimpleController,
                                                         call_class)
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req

LIVE = json.loads((Path(__file__).parent / "fixtures" / "r9_live_20261109.json").read_text())
BANDIT = {"key": "close-bandit-7", "call": "Bandit MiG-29S one-ship, bearing two seven zero, eight miles, "
                                           "angels twelve. At your nine o'clock, level."}


class Memory:
    generation = 1

    def schedule_debrief(self):
        return True

    def publish(self, *a, **k):
        pass

    def summary(self):
        return None


class FakeEye:
    def __init__(self, pic):
        self.pic = deepcopy(pic)
        self.memory = Memory()
        self.announced = []

    def picture(self):
        return deepcopy(self.pic)

    def mark_announced(self, keys):
        self.announced.extend(keys)


class Model:
    def __init__(self, answer="Prometheus, Athena, copy."):
        self.answer, self.calls = answer, 0

    def __call__(self, messages):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def run(pic, text, answer="Prometheus, Athena, copy."):
    eye, model, log = FakeEye(pic), Model(answer), FakeTranscript()
    c = SimpleController(None, log, model="fixture", api_key="unused", eye=eye, llm=model)
    frames = [f["speech"] for f in c.stream(req(text))]
    replies = [r for r in log.records if r["kind"] == "reply_generated"]
    return frames, replies, model, eye


class BudgetTests(unittest.TestCase):
    def test_one_budget(self):
        self.assertEqual(MAX_REPLY_CHARS, 230)  # ~13 chars/s Piper, radio cut at ~30 s; nothing >250 ever arrived
        self.assertEqual(DEBRIEF_CHARS, 150)
        self.assertLessEqual(MAX_REPLY_CHARS, MAX_SPEECH_CHARS)

    def test_every_live_picture_and_ask_fits_one_frame(self):
        asks = ("Athena, mission ended.", "Athena, ground picture", "Athena, tally.", "Athena, confirm kills",
                "Athena, what do I have left", "Athena, launch, launch", "Athena, picture", "Athena, running in",
                "Athena, how is it looking", "Athena, confirm kills, what remains")
        long_model = "Prometheus, Athena, " + " ".join("Group %d is out there, far away." % i for i in range(40))
        for name, pic in LIVE.items():
            for text in asks:
                with self.subTest(picture=name, ask=text):
                    frames, replies, _, _ = run(pic, text, long_model)
                    self.assertEqual(len(frames), 1, frames)
                    self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS, frames[0])
                    self.assertEqual(len(replies), 1)


class LiveReplayTests(unittest.TestCase):
    def test_debrief_2055_is_short_and_never_calls_the_model(self):
        frames, replies, model, _ = run(LIVE["debrief"], "Athena, mission ended.")
        self.assertEqual(frames, ["Prometheus, Athena, debrief written. Eight kills: five trucks, two SAM units "
                                  "and one triple-A. Four Mavericks fired. Guns: three kills."])
        self.assertLessEqual(len(frames[0]), DEBRIEF_CHARS)
        self.assertEqual(model.calls, 0)
        self.assertEqual(replies[0]["route"], "debrief")
        self.assertEqual(replies[0]["timings_ms"]["llm"], 0)

    def test_debrief_close_bandit_goes_first_within_the_budget(self):
        pic = dict(LIVE["debrief"], _close_bandits=[BANDIT])
        frames, _, _, eye = run(pic, "Athena, mission ended.")
        self.assertTrue(frames[0].startswith("Prometheus, Athena, " + BANDIT["call"] + " debrief written."), frames[0])
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
        self.assertEqual(eye.announced, [BANDIT["key"]])

    def test_ground_picture_1954_nearest_whole_rest_counted(self):
        rows = LIVE["ground_1954"]["ground_targets_within_20nm"]
        self.assertEqual(len(rows), 6)
        frames, replies, model, _ = run(LIVE["ground_1954"], "Athena, ground picture")
        self.assertEqual(frames, ["Prometheus, Athena, " + rows[0]["call"] + " Plus five more."])
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
        self.assertEqual(model.calls, 0)
        self.assertEqual(replies[0]["route"], "fixed")
        self.assertNotIn("llm", replies[0])

    def test_tally_2057_is_the_mission_kill_total(self):
        for text in ("Athena, tally.", "- Athena, I ask for a tally.", "Athena, tally, kills.", "Athena, confirm tally",
                     "Athena, tally kills"):
            with self.subTest(text=text):
                self.assertEqual(call_class(text), "kill_confirm")
                frames, replies, model, _ = run(LIVE["tally_2057"], text)
                self.assertTrue(frames[0].startswith("Prometheus, Athena, Mission total:"), frames[0])
                self.assertNotIn("Enemy armor", frames[0])  # live 20:57 answered with a ground picture
                self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
                self.assertEqual(model.calls, 0)
                self.assertEqual(replies[0]["route"], "fixed")

    def test_tally_with_a_contact_is_left_to_the_model(self):
        for text in ("Athena, tally two trucks", "tally the SAM site, north"):
            self.assertNotEqual(call_class(text), "kill_confirm", text)
            self.assertNotEqual(call_class(text), "ack", text)


class RouteTests(unittest.TestCase):
    def test_fixed_replies_skip_the_model(self):
        for name, text in (("loadout_2035", "Athena, what do I have left"), ("launch_2038", "Athena, launch, launch"),
                           ("confirm_2051", "Athena, confirm kills"), ("loadout_2035", "Athena, running in")):
            with self.subTest(text=text):
                frames, replies, model, _ = run(LIVE[name], text)
                self.assertEqual(model.calls, 0)
                self.assertEqual(replies[0]["route"], "fixed")
                self.assertEqual(replies[0]["timings_ms"]["llm"], 0)
                self.assertIn("total", replies[0]["timings_ms"])

    def test_free_questions_go_to_the_model_with_a_route_label(self):
        frames, replies, model, _ = run(LIVE["loadout_2035"], "Athena, how is it looking")
        self.assertEqual(model.calls, 1)
        self.assertEqual(frames, ["Prometheus, Athena, copy."])
        self.assertEqual(replies[0]["route"], "llm")

    def test_long_model_answer_is_trimmed_to_one_frame(self):
        long = "Prometheus, Athena, " + " ".join("Contact %d is somewhere out there." % i for i in range(40))
        frames, replies, _, _ = run(LIVE["loadout_2035"], "Athena, how is it looking", long)
        self.assertEqual(len(frames), 1)
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
        self.assertTrue(replies[0].get("trimmed"))


class SaidOnlyWhenSpokenTests(unittest.TestCase):
    def pic(self):
        return dict(LIVE["loadout_2035"], splash_to_announce=["Splash one Ural-375"], _splash_keys=["k-splash"],
                    rwr_alert_to_announce=["Spike, SA-2 at your six o'clock."], _rwr_keys=["k-rwr"])

    def test_model_failure_keeps_alerts_pending(self):
        frames, _, _, eye = run(self.pic(), "Athena, how is it looking", RuntimeError("down"))
        self.assertEqual(frames, [FALLBACK])
        self.assertEqual(eye.announced, [])

    def test_model_answer_marks_alerts(self):
        _, _, _, eye = run(self.pic(), "Athena, how is it looking",
                           "Prometheus, Athena, spike, SA-2 at your six o'clock. Splash one Ural-375.")
        self.assertEqual(sorted(eye.announced), ["k-rwr", "k-splash"])

    def test_fixed_reply_marks_only_alerts_that_fit(self):
        pic = self.pic()
        pic["splash_to_announce"] = ["Splash one " + "very long target name " * 12]
        frames, _, _, eye = run(pic, "Athena, running in")
        self.assertIn("Spike, SA-2", frames[0])
        self.assertNotIn("very long target", frames[0])
        self.assertEqual(eye.announced, ["k-rwr"])

    def test_close_bandit_first_on_a_model_reply_and_marked(self):
        long = "Prometheus, Athena, " + " ".join("Contact %d is somewhere out there." % i for i in range(10))
        pic = dict(LIVE["loadout_2035"], _close_bandits=[BANDIT])
        frames, _, _, eye = run(pic, "Athena, how is it looking", long)
        self.assertTrue(frames[0].startswith("Prometheus, Athena, " + BANDIT["call"]), frames[0])
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
        self.assertEqual(eye.announced, [BANDIT["key"]])

    def test_close_bandit_not_said_stays_pending(self):
        other = {"key": "close-bandit-8", "call": BANDIT["call"].replace("MiG-29S", "Su-27")}
        pic = dict(LIVE["loadout_2035"], _close_bandits=[BANDIT, other])
        _, _, _, eye = run(pic, "Athena, how is it looking")
        self.assertEqual(eye.announced, [BANDIT["key"]])  # one new bandit per reply; the other stays pending


if __name__ == "__main__":
    unittest.main()
