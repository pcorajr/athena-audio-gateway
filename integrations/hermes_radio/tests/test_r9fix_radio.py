"""Round 9 (live session 7) radio fixes: short debrief, 900-char/2-frame safety net, kill-confirm lag,
launch-call coverage, aircraft + loadout. Fixtures are real pictures from the 2026-11-09 transcript."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from integrations.hermes_radio.contract import MAX_SPEECH_CHARS
from integrations.hermes_radio.simple_controller import (PART_CHARS, SimpleController, call_class, debrief_summary,
                                                         fit_reply, loadout_calls)
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req

LIVE = json.loads((Path(__file__).parent / "fixtures" / "r9_live_20261109.json").read_text())


class Memory:
    generation = 1
    identity = "fixture"

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

    def picture(self):
        return deepcopy(self.pic)


def no_llm(messages):
    raise AssertionError("deterministic route must not call the LLM")


def controller(pic, llm=no_llm, log=None):
    return SimpleController(None, log or FakeTranscript(), model="fixture", api_key="unused", eye=FakeEye(pic), llm=llm)


def frames(c, text):
    return [f["speech"] for f in c.stream(req(text))]


class DebriefTests(unittest.TestCase):
    def test_live_2055_picture_is_one_short_summary(self):
        text = debrief_summary(LIVE["debrief"])
        self.assertTrue(text.startswith("Eight kills: five trucks, two SAM units and one triple-A."), text)
        self.assertIn("Four Mavericks fired.", text)
        self.assertIn("Guns: three kills.", text)
        self.assertLess(len(text), 250)

    def test_no_gun_shells_or_rounds_as_weapons(self):
        text = debrief_summary(LIVE["debrief"])
        for bad in ("M53", "M56", "639", "six hundred", "rounds", "HEI", "gun rounds"):
            self.assertNotIn(bad, text)

    def test_gun_line_only_when_gun_kills(self):
        pic = {"my_kills_this_mission": [{"target": "T-72B", "category": "armor", "weapon": "AGM-65D", "confidence": "confirmed"}],
               "my_weapons_used_this_mission": {"by_weapon": {"AGM-65D": 1, "gun rounds": 300}}}
        self.assertEqual(debrief_summary(pic), "One kill: one armor. One Maverick fired.")

    def test_groups_capped_with_and_n_more(self):
        kills = [{"target": t, "category": c, "weapon": "GBU_12", "confidence": "confirmed"} for t, c in (
            ("a", "armor"), ("b", "armor"), ("c", "SAM"), ("d", "AAA"), ("e", "infantry"), ("f", "fighter"), ("g", "scenery"))]
        text = debrief_summary({"my_kills_this_mission": kills,
                                "my_weapons_used_this_mission": {"by_weapon": {"GBU_12": 7}}})
        self.assertIn("Seven kills: two armor, one SAM unit, one triple-A, one aircraft and two more.", text)
        self.assertIn("Seven GBU_12 fired.", text)

    def test_no_kills_no_weapons(self):
        self.assertEqual(debrief_summary({}), "No confirmed kills.")

    def test_spoken_debrief_fits_one_frame_without_llm(self):
        for text in ("Athena, give me briefing.", "Athena, provide debriefing.", "Athena, mission ended."):
            c = controller(LIVE["debrief"])
            out = frames(c, text)
            self.assertEqual(len(out), 1, out)
            self.assertTrue(out[0].startswith("Prometheus, Athena, debrief written. Eight kills"), out[0])
            self.assertLessEqual(len(out[0]), PART_CHARS)
            self.assertLess(len(out[0]), 300)


class SafetyNetTests(unittest.TestCase):
    def test_fit_reply_trims_whole_sentences_from_the_end(self):
        sentences = ["Prometheus, Athena, bandit one two three."] + ["Group number %d, twelve o'clock, ten miles." % i
                                                                     for i in range(60)]
        text, trimmed = fit_reply(" ".join(sentences), MAX_SPEECH_CHARS)
        self.assertTrue(trimmed)
        self.assertLessEqual(len(text), MAX_SPEECH_CHARS)
        self.assertTrue(text.startswith("Prometheus, Athena, bandit one two three."))
        self.assertTrue(text.endswith("miles."))
        self.assertEqual(fit_reply("Prometheus, Athena, copy.", PART_CHARS), ("Prometheus, Athena, copy.", False))

    def test_llm_answer_over_budget_is_trimmed_and_logged(self):
        long = "Prometheus, Athena, " + " ".join("Contact %d is somewhere out there." % i for i in range(40))
        log = FakeTranscript()
        c = controller(LIVE["loadout_2035"], llm=lambda m: long, log=log)
        out = frames(c, "Athena, how is it looking out there?")
        self.assertLessEqual(len(out), 2)
        self.assertTrue(all(len(p) <= PART_CHARS for p in out), [len(p) for p in out])

    def ground_pic(self, n):
        rows = [{"call": "Enemy armor, %d contacts, bearing one zero two, %d miles, ground target." % (i + 1, 5 + i),
                 "range_nm": 5 + i, "category": "armor"} for i in range(n)]
        return {"connected": True, "ownship": {"type": "A-10C_2", "coalition": "blue"},
                "ground_targets_within_20nm": rows[::-1], "ground_empty_call": "none"}

    def test_ground_picture_capped_at_three_closest_first_plus_n_more(self):
        out = " ".join(frames(controller(self.ground_pic(7)), "Athena, ground picture"))
        self.assertLess(out.index("1 contacts"), out.index("2 contacts"))
        self.assertIn("3 contacts", out)
        self.assertNotIn("4 contacts", out)
        self.assertTrue(out.endswith("Plus four more."), out)
        three = " ".join(frames(controller(self.ground_pic(3)), "Athena, ground picture"))
        self.assertNotIn("Plus", three)

    def test_oversize_fixed_calls_drop_whole_contacts_and_log(self):
        pic = self.ground_pic(3)
        for r in pic["ground_targets_within_20nm"]:
            r["call"] = "Enemy armor " + "heavy column " * 25 + "ground target."
        log = FakeTranscript()
        out = " ".join(frames(controller(pic, log=log), "Athena, ground picture"))
        self.assertLessEqual(len(out), MAX_SPEECH_CHARS)
        self.assertTrue(out.endswith("ground target."))
        self.assertTrue(any(r.get("trimmed") for r in log.records))


class KillConfirmLagTests(unittest.TestCase):
    def pic(self, shots, kills=()):
        return {"connected": True, "ownship": {"type": "F-4E-45MC", "coalition": "blue"},
                "my_kills_this_mission": list(kills), "my_shots_last_15_min": {"shots": shots},
                "alive_totals_by_coalition": {}}

    def test_live_2051_recent_gun_hit_is_not_confirmed_yet(self):
        pic = deepcopy(LIVE["confirm_2051"])
        pic["my_kills_this_mission"] = []
        said = " ".join(frames(controller(pic), "Athena, confirm, hit."))
        self.assertIn("Not confirmed yet, checking.", said)
        self.assertNotIn("No new kills", said)

    def test_no_recent_release_keeps_no_new_kills(self):
        kill = {"target": "Ural-4320T", "category": "vehicles", "weapon": "AGM-65A", "seq": 1, "age_s": 400,
                "confidence": "confirmed"}
        c = controller(self.pic([{"weapon": "AGM-65A", "age_s": 400, "result": "kill"}], [kill]))
        frames(c, "Athena, confirm kill")
        self.assertIn("No new kills since your last check", " ".join(frames(c, "Athena, confirm kill")))

    def test_old_pending_shot_is_not_recent(self):
        kill = {"target": "T", "category": "armor", "weapon": "x", "seq": 1, "age_s": 500, "confidence": "confirmed"}
        c = controller(self.pic([{"weapon": "AGM-65A", "age_s": 200, "result": "pending"}], [kill]))
        frames(c, "Athena, confirm kill")
        self.assertIn("No new kills", " ".join(frames(c, "Athena, confirm kill")))

    def test_recent_miss_is_not_pending(self):
        c = controller(self.pic([{"weapon": "AGM-65A", "age_s": 20, "result": "miss"}]))
        self.assertIn("No kills showing", " ".join(frames(c, "Athena, confirm kill")))

    def test_confirmed_kill_still_confirmed(self):
        kill = {"target": "T-55", "category": "armor", "weapon": "GBU_38", "seq": 9, "age_s": 5, "confidence": "confirmed"}
        said = " ".join(frames(controller(self.pic([{"weapon": "GBU_38", "age_s": 8, "result": "kill"}], [kill])),
                               "Athena, confirm kill"))
        self.assertIn("splash one T-55", said)


class LaunchTests(unittest.TestCase):
    def test_class(self):
        for text in ("Athena, stinger, launch.", "Athena, missile launch.", "Athena, another missile?", "launch, launch"):
            self.assertEqual(call_class(text), "launch", text)
        for text in ("Athena, how many missiles do I have left?", "Athena, I'm launching a Maverick", "Athena, missiles away",
                     "Athena, what's my loadout"):
            self.assertNotEqual(call_class(text), "launch", text)

    def test_named_type_not_present_reports_the_real_missile(self):
        pic = deepcopy(LIVE["launch_2038"])
        pic["threats"]["missiles_inbound"] = pic["threats"]["missiles_inbound"][:1]
        pic["threats"]["missiles_inbound"][0].update(clock=10, range_nm=8.2)
        said = " ".join(frames(controller(pic), "Athena, stinger, launch."))
        self.assertEqual(said, "Prometheus, Athena, Negative Stinger; SA-2 in the air, your ten o'clock, eight miles.")

    def test_reports_every_missile(self):
        said = " ".join(frames(controller(LIVE["launch_2038"]), "Athena, missile launch."))
        self.assertIn("SA-2 in the air, your one o'clock, six miles.", said)
        self.assertIn("Also SA-2, your one o'clock, nine miles.", said)
        self.assertNotIn("Negative", said)

    def test_named_type_present(self):
        said = " ".join(frames(controller(LIVE["launch_2038"]), "Athena, SA-2 launch."))
        self.assertNotIn("Negative", said)
        self.assertIn("SA-2 in the air", said)

    def test_none_in_flight(self):
        pic = deepcopy(LIVE["launch_2038"])
        pic["threats"]["missiles_inbound"] = []
        said = " ".join(frames(controller(pic), "Athena, stinger, launch."))
        self.assertEqual(said, "Prometheus, Athena, No missile in the air that I can see.")

    def test_same_missile_on_another_ask(self):
        pic = deepcopy(LIVE["launch_2038"])
        pic["threats"]["missiles_inbound"] = pic["threats"]["missiles_inbound"][:1]
        c = controller(pic)
        self.assertNotIn("Same", " ".join(frames(c, "Athena, missile launch.")))
        self.assertIn("Same missile, SA-2 in the air", " ".join(frames(c, "Athena, another missile?")))
        pic["threats"]["missiles_inbound"].append({"type": "SA2V755", "range_nm": 14.0, "clock": 3, "launcher": "Other"})
        c.eye.pic = pic
        third = " ".join(frames(c, "Athena, another missile?"))
        self.assertNotIn("Same", third)
        self.assertIn("your three o'clock", third)

    def test_followup_do_you_see_it(self):
        c = controller(LIVE["launch_2038"])
        frames(c, "Athena, stinger, launch.")
        self.assertIn("SA-2 in the air", " ".join(frames(c, "Do you see it?")))


class AircraftLoadoutTests(unittest.TestCase):
    def test_flying_and_loadout_answers_both(self):
        own = LIVE["loadout_2035"]["ownship"]
        line = loadout_calls(own, "what am I flying and what's my loadout?")[0]
        self.assertTrue(line.startswith("You're flying an F-4E. You have Mavericks four"), line)
        self.assertNotIn("HB-", line)
        self.assertIn("gun six hundred thirty nine rounds", line)
        self.assertNotIn("flying", loadout_calls(own, "what's my loadout?")[0])
        said = " ".join(frames(controller(LIVE["loadout_2035"]), "Athena, what am I flying and what's my loadout?"))
        self.assertIn("You're flying an F-4E.", said)

    def test_winchester_when_only_pods_remain(self):
        self.assertEqual(loadout_calls(LIVE["debrief"]["ownship"], "winchester"), ["Winchester, no weapons left."])


if __name__ == "__main__":
    unittest.main()
