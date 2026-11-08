"""Round 6 radio talk: fresh-vs-old kill confirmations, whose kills, remaining counts, intent acks,
impact checks, compact loadout and end-of-mission debrief phrasing."""
from copy import deepcopy
import unittest

from integrations.hermes_radio.simple_controller import (SimpleController, call_class, loadout_calls,
                                                         spoken_target)
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req


def kill(target, age, category="armor", weapon="GBU_38", confidence="confirmed"):
    return {"target": target, "category": category, "weapon": weapon, "age_s": age, "confidence": confidence,
            "inferred": confidence != "confirmed", "how": "kill event"}


BASE = {
    "connected": True,
    "ownship": {"type": "A-10C_2", "coalition": "blue",
                "loadout": {"available": True, "gun_rounds": 992, "weapons": [
                    {"weapon": "AGM-65D", "count": 2}, {"weapon": "AGM-65H", "count": 2},
                    {"weapon": "AIM-9M", "count": 2}, {"weapon": "ALQ-184", "count": 1},
                    {"weapon": "AN/AAQ-28 LITENING", "count": 1}, {"weapon": "APKWS", "count": 7},
                    {"weapon": "CBU-97", "count": 1}, {"weapon": "GBU-12", "count": 1},
                    {"weapon": "GBU-38", "count": 1}, {"weapon": "GBU-54", "count": 1}]}},
    "my_kills_this_mission": [],
    "recent_kills_and_losses_last_10_min": [],
    "my_shots_last_15_min": {"shots": []},
    "alive_totals_by_coalition": {"red": {"armor": 2, "AAA": 0, "infantry": 3}},
    "data_age_s": {"units": 1},
}


class FakeEye:
    def __init__(self):
        self.pic = deepcopy(BASE)

    def picture(self):
        return deepcopy(self.pic)


def say(controller, text):
    return controller.exchange(req(text))["speech"]


class KillConfirmTests(unittest.TestCase):
    def setUp(self):
        self.eye = FakeEye()
        self.c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=self.eye,
                                  llm=lambda messages: "Prometheus, Athena, copy.")

    def test_second_confirmation_reports_only_the_new_kill_plus_remaining(self):
        self.eye.pic["my_kills_this_mission"] = [kill("Ural-375", 600, "ground vehicle"),
                                                 kill("Paratrooper AKS-74", 500, "infantry")]
        first = say(self.c, "Athena, confirm kills")
        self.assertIn("Ural-375", first)
        self.eye.pic["my_kills_this_mission"].append(kill("T-55", 10))
        second = say(self.c, "Confirm kill, tell me how many armor remain.")
        self.assertIn("splash one T-55", second)
        self.assertNotIn("Ural", second)
        self.assertIn("Two enemy armor remain", second)

    def test_no_new_kills_is_said_plainly_and_total_on_request(self):
        self.eye.pic["my_kills_this_mission"] = [kill("T-55", 300), kill("T-55", 200)]
        say(self.c, "confirm kills")
        again = say(self.c, "confirm kills")
        self.assertIn("No new kills since your last check", again)
        total = say(self.c, "confirm kills, mission total")
        self.assertIn("Mission total: two T-55", total)

    def test_wingman_kills_come_from_friendly_shooters_not_mine(self):
        self.eye.pic["my_kills_this_mission"] = [kill("T-55", 50)]
        self.eye.pic["recent_kills_and_losses_last_10_min"] = [
            {"age_s": 30, "event": "killed", "by_me": False, "friendly_loss": False, "weapon": "AGM_65H",
             "victim": {"type": "ZSU-23-4 Shilka", "category": "AAA", "coalition": "red"},
             "killer": {"type": "A-10C_2", "name": "Player Wingman", "coalition": "blue"}},
            {"age_s": 20, "event": "killed", "by_me": True, "weapon": "GBU_38",
             "victim": {"type": "T-55", "category": "armor", "coalition": "red"},
             "killer": {"type": "A-10C_2", "name": "- Player", "coalition": "blue"}}]
        answer = say(self.c, "Athena, confirm wingman kill")
        self.assertIn("Shilka", answer)
        self.assertNotIn("T-55", answer)

    def test_category_filter_for_air_defense(self):
        self.eye.pic["my_kills_this_mission"] = [kill("Ural-375", 90, "ground vehicle"),
                                                 kill("Strela-1 9P31", 40, "SAM")]
        answer = say(self.c, "confirm air defense kill")
        self.assertIn("Strela", answer)
        self.assertNotIn("Ural", answer)

    def test_bridge_code_is_spoken_as_bridge(self):
        self.assertEqual(spoken_target("MOST(ROAD)BIG-CRUSH", "scenery"), "bridge")
        self.assertEqual(spoken_target("- Player"), "Player")
        self.assertEqual(spoken_target("unknown", "scenery"), "structure")
        self.eye.pic["my_kills_this_mission"] = [kill("MOST(ROAD)BIG-CRUSH", 20, "scenery", weapon="16787200")]
        answer = say(self.c, "confirm kill")
        self.assertIn("bridge", answer)
        self.assertNotIn("MOST", answer)


class RoutingTests(unittest.TestCase):
    def test_attack_flow_intents_are_acks_not_kill_lists(self):
        for text, expected in (("Athena, weapons hot, running in, guns only, target two transport vehicles", "weapons hot"),
                               ("I'm running in to kill the last armor, confirm.", "running in"),
                               ("Athena, pickle.", "weapon away"),
                               ("Athena, rifle two AGM-65H", "weapon away"),
                               ("Athena, ten seconds till impact.", None),
                               ("Athena, abort run.", "abort")):
            self.assertEqual(call_class(text, {"last_attack_age_s": 30}), "intent", text)
            eye = FakeEye()
            eye.pic["my_kills_this_mission"] = [kill("T-55", 40)]
            c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                 llm=lambda messages: "LLM ANSWER")
            answer = say(c, text)
            self.assertNotIn("Confirmed", answer)
            if expected:
                self.assertIn(expected, answer)

    def test_bomb_impact_reports_latest_result(self):
        eye = FakeEye()
        eye.pic["my_shots_last_15_min"] = {"shots": [
            {"weapon": "GBU_12", "age_s": 700, "result": "kill", "no_longer_showing": ["Paratrooper"]},
            {"weapon": "GBU_38", "age_s": 8, "result": "kill", "hit": {"type": "T-55", "category": "armor"}}]}
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: "LLM ANSWER")
        self.assertEqual(call_class("Athena, bomb impact."), "impact")
        self.assertIn("splash one T-55", say(c, "Athena, bomb impact."))
        eye.pic["my_shots_last_15_min"] = {"shots": [{"weapon": "AGM_65D", "age_s": 5, "result": "pending"}]}
        self.assertIn("still tracking", say(c, "Athena, impact"))

    def test_loadout_is_compact_and_skips_pods(self):
        line = loadout_calls(BASE["ownship"])[0]
        self.assertNotIn("ALQ", line)
        self.assertNotIn("LITENING", line)
        self.assertIn("gun nine hundred ninety two rounds", line)
        self.assertLess(len(line), 220)
        eye = FakeEye()
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: "LLM ANSWER")
        self.assertIn("GBU-54 one", say(c, "Athena, what's my loadout?"))

    def test_end_of_mission_phrasings_request_a_debrief(self):
        class Memory:
            generation = 1
            scheduled = 0

            def schedule_debrief(self):
                self.scheduled += 1
                return True

            def publish(self, *args, **kwargs):
                pass

            def summary(self):
                return None

        for text in ("Athena, mission end. Please provide briefing.", "Okay, Athena, I'm going to end this mission.",
                     "Athena, debrief", "Athena, give me the debrief"):
            eye = FakeEye()
            eye.memory = Memory()
            c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                 llm=lambda messages: "LLM ANSWER")
            self.assertEqual(say(c, text), "debrief written", text)
            self.assertEqual(eye.memory.scheduled, 1)
        eye = FakeEye()
        eye.memory = Memory()
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: "LLM ANSWER")
        self.assertNotEqual(say(c, "Athena, mission status"), "debrief written")


if __name__ == "__main__":
    unittest.main()
