"""Phase 0 (2026-11-10 review): one reply budget, fixed replies skip the model (route label + latency),
'Athena, tally' = mission kill total, splash/threat/close-bandit calls marked said only when actually in
the fitted reply. Fixtures are real pictures from the 2026-11-09 transcript (debrief, ground picture, tally)."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from integrations.hermes_radio.contract import MAX_SPEECH_CHARS
from integrations.hermes_radio.eye import call_line
from integrations.hermes_radio.simple_controller import (DEBRIEF_CHARS, FALLBACK, MAX_REPLY_CHARS, SimpleController,
                                                         call_class)
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req

LIVE = json.loads((Path(__file__).parent / "fixtures" / "r9_live_20261109.json").read_text())
BANDIT = {"key": "close-bandit-7",
          "call": "Bandit MiG-29S, two seven zero, eight miles, angels twelve, nine o'clock level."}


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
        self.assertEqual(MAX_REPLY_CHARS, 280)  # 13.4 chars/s Piper median (10.6 slowest long reply), 30 s Go cap
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
        long = "Prometheus, Athena, " + " ".join("Copy, standing by for your next call." for i in range(40))
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


class CloseBanditKillConfirmTests(unittest.TestCase):
    """Review MAJOR-1/2: a close bandit never wipes out the kill confirmation, "what remains" is never dropped."""

    def test_bandit_first_then_kills_within_budget(self):
        for name in ("tally_2057", "confirm_2051"):
            for text, needle in (("Athena, confirm kills", "Confirmed"), ("Athena, tally.", "Mission total:"),
                                 ("Athena, confirm kills, what remains", "Confirmed")):
                with self.subTest(picture=name, ask=text):
                    frames, replies, model, eye = run(dict(LIVE[name], _close_bandits=[BANDIT]), text)
                    self.assertEqual(len(frames), 1)
                    self.assertTrue(frames[0].startswith("Prometheus, Athena, " + BANDIT["call"] + " "), frames[0])
                    self.assertIn(needle, frames[0])
                    self.assertNotIn("Ask again", frames[0])
                    if "remains" in text:
                        self.assertRegex(frames[0], r"(Remaining:|remaining\.)")
                    self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
                    self.assertEqual(eye.announced, [BANDIT["key"]])
                    self.assertEqual(model.calls, 0)

    def test_kills_said_with_a_bandit_are_acknowledged(self):
        eye, log = FakeEye(dict(LIVE["tally_2057"], _close_bandits=[BANDIT])), FakeTranscript()
        c = SimpleController(None, log, model="fixture", api_key="unused", eye=eye, llm=Model())
        first = [f["speech"] for f in c.stream(req("Athena, confirm kills"))][0]
        self.assertIn("Confirmed", first)
        again = [f["speech"] for f in c.stream(req("Athena, confirm kills"))][0]
        self.assertIn("No new kills", again)

    def test_remaining_is_counted_when_the_list_does_not_fit(self):
        long_bandit = dict(BANDIT, call="Bandit MiG-29S and Su-27 and MiG-21Bis and Su-25T four-ship, two seven zero, eight miles, "
                                        "angels twelve, nine o'clock level, closing fast from the west.")
        frames, _, _, _ = run(dict(LIVE["tally_2057"], _close_bandits=[long_bandit]), "Athena, confirm kills, what remains")
        self.assertIn("Confirmed: five trucks", frames[0])  # grouped kill wording
        self.assertRegex(frames[0], r"Remaining: [a-z ]+ enemy ground units\.$")
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)

    def test_no_bandit_confirm_and_remains_both_said(self):
        frames, _, _, _ = run(LIVE["tally_2057"], "Athena, confirm kills, what remains")
        self.assertIn("Confirmed", frames[0])
        self.assertIn("Remaining:", frames[0])
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)


# Real geometry: the 3-bandit picture of 2026-11-09 and the longest real air calls (MiG-15bis, 44 nm, angels 23).
REAL_THREE = [call_line("F-5E", "356", 33, 12, "air", 16000, "low"), call_line("F-5E", "360", 33, 12, "air", 8000, "low"),
              call_line("F-5E", "357", 37, 12, "air", 24000, "level")]
LONG_THREE = [call_line("MiG-15bis", "337", 44, 12, "air", 23000, "high"),
              call_line("MiG-15bis", "338", 47, 11, "air", 23000, "high"),
              call_line("MiG-15bis", "339", 49, 12, "air", 27000, "level")]


class ThreeBanditTests(unittest.TestCase):
    """Review MAJOR-3: an air picture says the three nearest bandits whole, in one transmission."""

    def test_three_compact_calls_fit(self):
        self.assertEqual(REAL_THREE[0], "F-5E, three five six, thirty three, angels sixteen, twelve o'clock low.")
        for three in (REAL_THREE, LONG_THREE):
            for klass, field in (("Athena, picture", "picture_top3"), ("Athena, long range picture", "picture_long_range_top3")):
                with self.subTest(calls=three[0], ask=klass):
                    pic = dict(LIVE["loadout_2035"], picture_top3=three, picture_long_range_top3=three,
                               air_picture_empty_call="x")
                    frames, replies, model, _ = run(pic, klass)
                    self.assertEqual(frames, ["Prometheus, Athena, " + " ".join(three)])
                    self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
                    self.assertEqual(model.calls, 0)

    def test_model_reply_over_budget_drops_whole_calls(self):
        answer = "Prometheus, Athena, " + " ".join(LONG_THREE) + " Two more groups further north, all cold."
        pic = dict(LIVE["loadout_2035"], _close_bandits=[BANDIT], picture_top3=LONG_THREE)
        frames, replies, _, _ = run(pic, "Athena, any bandits out there?", answer)
        self.assertLessEqual(len(frames[0]), MAX_REPLY_CHARS)
        self.assertTrue(frames[0].startswith("Prometheus, Athena, " + BANDIT["call"]), frames[0])
        body = frames[0][len("Prometheus, Athena, " + BANDIT["call"]):].strip()
        self.assertEqual(body, " ".join(LONG_THREE[:body.count("o'clock")]))  # whole calls only, never half
        self.assertIn(LONG_THREE[0], body)
        self.assertTrue(replies[0].get("trimmed"))


class MinorFixTests(unittest.TestCase):
    def test_wingman_or_visual_tally_is_not_the_kill_total(self):
        for text in ("Two, tally", "Athena, visual, tally.", "two, tally."):
            self.assertNotEqual(call_class(text), "kill_confirm", text)
        for text in ("Athena, tally", "tally.", "Athena tally"):
            self.assertEqual(call_class(text), "kill_confirm", text)

    def test_model_reply_without_the_alerts_keeps_them_pending(self):
        pic = dict(LIVE["loadout_2035"], splash_to_announce=["Splash one Ural-375"], _splash_keys=["k-splash"],
                   rwr_alert_to_announce=["Spike, SA-2 at your six o'clock."], _rwr_keys=["k-rwr"])
        _, _, _, eye = run(pic, "Athena, how is it looking", "Prometheus, Athena, quiet out here.")
        self.assertEqual(eye.announced, [])
        _, _, _, eye = run(pic, "Athena, how is it looking", "Prometheus, Athena, spike, SA-2 behind you.")
        self.assertEqual(eye.announced, ["k-rwr"])



class BanditWordingTests(unittest.TestCase):
    """Review P0b MAJOR: a hostile call outside a hostile-only picture says "Bandit"; under 10 nm keeps "miles"."""
    def _eye(self):
        from integrations.hermes_radio.tests.test_final_tiers import contact, make_eye
        return make_eye(contact(9, "MiG-29S", distance=8, bearing=270), contact(5, "A-10C", "blue", distance=12),
                        contact(6, "MiG-29S", distance=30, bearing=10), contact(7, "Su-27", distance=40, bearing=20))[0]

    def test_close_bandit_warning_says_bandit_and_type(self):
        eye = self._eye()
        close = eye.picture()["_close_bandits"]
        self.assertTrue(close and close[0]["call"].startswith("Bandit MiG-29S, "), close)
        self.assertIn(" eight miles, ", close[0]["call"])
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda m: "Prometheus, Athena, loud and clear.")
        said = " ".join(f["speech"] for f in c.stream(req("Athena, radio check")))
        self.assertTrue(said.startswith("Prometheus, Athena, Bandit MiG-29S, "), said)

    def test_traffic_list_hostile_row_says_bandit_friendly_says_friendly(self):
        traffic = self._eye().picture()["air_traffic"]
        self.assertTrue(all(g["call"].startswith("Bandit ") for g in traffic["red"]), traffic["red"])
        self.assertTrue(all(g["call"].startswith("Friendly ") for g in traffic["blue"]), traffic["blue"])

    def test_air_picture_stays_compact(self):
        pic = self._eye().picture()
        self.assertTrue(pic["picture_top3"] and not any(c.startswith("Bandit") for c in pic["picture_top3"]))

    def test_close_bandit_in_the_picture_reply_is_not_repeated(self):
        eye = self._eye()
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda m: "Prometheus, Athena, LLM.")
        said = " ".join(f["speech"] for f in c.stream(req("Athena, picture")))
        self.assertEqual(said.count(" eight miles, "), 1, said)
        self.assertTrue(any(k.startswith("close-bandit-") for k in eye.announced), eye.announced)

    def test_under_ten_miles_keeps_miles(self):
        self.assertEqual(call_line("MiG-29S", "270", 8, 9, "air", 12000, "level"),
                         "MiG-29S, two seven zero, eight miles, angels twelve, nine o'clock level.")
        self.assertIn(", nine miles, ", call_line("Su-27", "100", 9.4, 2, "air", 4000, "low"))
        self.assertIn(", ten, ", call_line("Su-27", "100", 9.6, 2, "air", 4000, "low"))

if __name__ == "__main__":
    unittest.main()
