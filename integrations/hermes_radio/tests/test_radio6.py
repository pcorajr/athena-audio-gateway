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
        self.assertIn("no result yet", say(c, "Athena, impact"))

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
            self.assertIn("debrief written", say(c, text), text)
            self.assertEqual(eye.memory.scheduled, 1)
        eye = FakeEye()
        eye.memory = Memory()
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: "LLM ANSWER")
        self.assertNotEqual(say(c, "Athena, mission status"), "debrief written")



class ReviewFixTests(unittest.TestCase):
    """RV-1 round-6 findings: identity, generic remaining, likely impact, '?' tags."""

    def controller(self, eye, llm=None):
        return SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                llm=llm or (lambda messages: "LLM ANSWER"))

    def test_same_type_kills_one_second_apart_are_both_new(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [kill("T-55", 30.0)]
        c = self.controller(eye)
        self.assertIn("splash one T-55", say(c, "confirm kill"))
        eye.pic["my_kills_this_mission"] = [kill("T-55", 31.0), kill("T-55", 30.0)]
        self.assertIn("splash one T-55", say(c, "confirm kill"))

    def test_identical_rows_are_distinct_kills(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [kill("Paratrooper AKS-74", 100, "infantry"),
                                            kill("Paratrooper AKS-74", 100, "infantry")]
        c = self.controller(eye)
        self.assertIn("two Paratrooper AKS-74", say(c, "confirm kills"))
        self.assertIn("No new kills", say(c, "confirm kills"))

    def test_stable_ids_win_and_llm_latency_does_not_replay(self):
        import time as _time
        eye = FakeEye()
        k = kill("T-55", 10)
        k["seq"] = 7
        eye.pic["my_kills_this_mission"] = [k]

        def slow(messages):
            _time.sleep(0.05)
            return "LLM ANSWER"
        c = self.controller(eye, slow)
        say(c, "confirm kill")
        k2 = dict(k, age_s=400)  # same seq, very different age: still the same kill
        eye.pic["my_kills_this_mission"] = [k2]
        self.assertIn("No new kills", say(c, "confirm kill"))

    def test_pilot_and_wingman_same_type_are_separate(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [kill("T-55", 20)]
        eye.pic["recent_kills_and_losses_last_10_min"] = [
            {"age_s": 20, "event": "killed", "by_me": False, "friendly_loss": False, "weapon": "AGM_65H",
             "victim": {"type": "T-55", "category": "armor", "coalition": "red"},
             "killer": {"type": "A-10C_2", "name": "Wingman", "coalition": "blue"}}]
        c = self.controller(eye)
        self.assertIn("splash one T-55", say(c, "confirm kill"))
        self.assertIn("T-55", say(c, "confirm wingman kill"))

    def test_generic_remaining_reports_surviving_armor_not_scenery(self):
        eye = FakeEye()
        eye.pic["alive_totals_by_coalition"] = {"red": {"armor": 2}}
        eye.pic["my_kills_this_mission"] = [kill("T-55", 40), kill("MOST(ROAD)BIG-CRUSH", 20, "scenery")]
        answer = say(self.controller(eye), "- Confirm kill, tell me what remains.")
        self.assertIn("two enemy armor", answer)
        self.assertNotIn("structures remaining", answer)

    def test_impact_likely_and_pending_without_flight_evidence(self):
        eye = FakeEye()
        eye.pic["my_shots_last_15_min"] = {"shots": [
            {"weapon": "GBU_38", "age_s": 9, "result": "likely kill (inferred: target no longer showing)",
             "intended_target": {"type": "T-55"}}]}
        c = self.controller(eye)
        self.assertIn("Likely kill on T-55", say(c, "Athena, bomb impact"))
        eye.pic["my_shots_last_15_min"] = {"shots": [{"weapon": "GBU_38", "age_s": 4, "result": "pending"}]}
        answer = say(c, "Athena, impact")
        self.assertIn("no result yet", answer)
        self.assertNotIn("tracking", answer)

    def test_confirm_tag_with_question_mark_is_still_an_ack(self):
        for text in ("I'm running in to kill the last armor, confirm?", "Athena, weapons hot, confirm?"):
            self.assertEqual(call_class(text, {"last_attack_age_s": 20}), "intent", text)
        self.assertNotEqual(call_class("Athena, what's my time to impact?"), "intent")


class ReviewFix2Tests(unittest.TestCase):
    """RV-1 radio6b: filtered remembering, likely->confirmed upgrade, eye6 category spelling."""

    def controller(self, eye):
        return SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                llm=lambda messages: "LLM ANSWER")

    @staticmethod
    def seqkill(seq, target, age, category, confidence="confirmed"):
        row = kill(target, age, category, confidence=confidence)
        row["seq"] = seq
        return row

    def test_category_total_does_not_consume_other_categories(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [self.seqkill(1, "T-55", 40, "armor"),
                                            self.seqkill(2, "Strela-1 9P31", 30, "SAM")]
        c = self.controller(eye)
        total = say(c, "confirm air defense kills, mission total")
        self.assertIn("Strela", total)
        self.assertNotIn("T-55", total)
        self.assertIn("splash one T-55", say(c, "confirm armor kill"))

    def test_empty_category_total_consumes_nothing(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [self.seqkill(1, "T-55", 40, "armor")]
        c = self.controller(eye)
        self.assertIn("No kills yet", say(c, "confirm infantry kills, mission total"))
        self.assertIn("splash one T-55", say(c, "confirm kill"))

    def test_likely_then_confirmed_is_reported_once_as_upgrade(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [self.seqkill(7, "T-55", 20, "armor", "likely")]
        c = self.controller(eye)
        self.assertIn("Likely: one T-55", say(c, "confirm kill"))
        eye.pic["my_kills_this_mission"] = [self.seqkill(7, "T-55", 25, "armor", "confirmed")]
        upgrade = say(c, "confirm kill")
        self.assertIn("Now confirmed, earlier likely: one T-55", upgrade)
        self.assertNotIn("splash one", upgrade)
        self.assertIn("No new kills", say(c, "confirm kill"))

    def test_eye6_vehicles_category_matches_truck_requests(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = [self.seqkill(3, "Ural-375", 50, "vehicles")]
        c = self.controller(eye)
        self.assertIn("splash one Ural-375", say(c, "confirm truck kill"))
        self.assertIn("Mission total: one Ural-375", say(c, "confirm vehicle kills, mission total"))
        old = FakeEye()
        old.pic["my_kills_this_mission"] = [kill("Ural-375", 50, "ground vehicle")]
        self.assertIn("Ural-375", say(self.controller(old), "confirm truck kill"))

    def test_wingman_uses_cumulative_eye6_friendly_rows(self):
        eye = FakeEye()
        mine = self.seqkill(1, "T-55", 900, "armor")
        wing = dict(self.seqkill(2, "ZSU-23-4 Shilka", 1500, "AAA"), shooter="Wingman 1-2")
        eye.pic["my_kills_this_mission"] = [mine]
        eye.pic["friendly_kills_this_mission"] = [mine, wing]
        eye.pic["recent_kills_and_losses_last_10_min"] = []
        total = say(self.controller(eye), "confirm wingman kills, mission total")
        self.assertIn("Shilka", total)
        self.assertNotIn("T-55", total)


class ReviewFix3Tests(unittest.TestCase):
    """RV-1 radio6c: artillery in remains; overflowed answers do not acknowledge kills."""

    def controller(self, eye):
        return SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                                llm=lambda messages: "LLM ANSWER")

    def test_artillery_only_survivors_are_reported(self):
        eye = FakeEye()
        eye.pic["alive_totals_by_coalition"] = {"red": {"artillery": 2}}
        eye.pic["my_kills_this_mission"] = [kill("T-55", 30)]
        answer = say(self.controller(eye), "confirm kill, tell me what remains")
        self.assertIn("two enemy artillery", answer)
        self.assertNotIn("No enemy ground units", answer)

    def test_mixed_armor_artillery_and_scenery_exclusion(self):
        eye = FakeEye()
        eye.pic["alive_totals_by_coalition"] = {"red": {"armor": 3, "artillery": 1, "scenery": 9, "fighter": 2}}
        answer = say(self.controller(eye), "confirm kill, what's left")
        self.assertIn("three enemy armor", answer)
        self.assertIn("one enemy artillery", answer)
        self.assertNotIn("structures", answer)
        self.assertNotIn("fighter", answer)
        self.assertIn("One enemy artillery remain", say(self.controller(eye), "confirm kill, how much artillery remains"))

    def many_kills(self, confidence="confirmed"):
        rows = []
        for i in range(60):
            row = kill("Target type number %02d long name" % i, 100 + i, "armor", confidence=confidence)
            row["seq"] = i + 1
            rows.append(row)
        return rows

    BANDIT = [{"key": "close-bandit-9", "call": "Bandit MiG-29S " + "very long call " * 14 + "one mile."}]

    def mixed_kills(self):
        rows = self.many_kills()
        for i, row in enumerate(rows):
            row["category"] = ("armor", "SAM", "AAA", "infantry")[i % 4]
        return rows

    def test_long_confirmation_names_kill_groups_within_budget(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = self.many_kills()
        c = self.controller(eye)
        said = say(c, "confirm kills")
        self.assertEqual(said, "Prometheus, Athena, Confirmed: sixty armor.")  # grouped, not 60 type names
        self.assertIn("No new kills", say(c, "confirm kills"))  # it was said, so it is acknowledged

    def test_overflowed_confirmation_does_not_acknowledge(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = self.mixed_kills()
        eye.pic["_close_bandits"] = self.BANDIT  # the bandit goes first; the kill line no longer fits
        c = self.controller(eye)
        said = say(c, "confirm kills")
        self.assertIn("Ask again for the rest.", said)
        self.assertNotIn("Confirmed", said)
        del eye.pic["_close_bandits"]
        eye.pic["my_kills_this_mission"] = self.many_kills()[:1]
        self.assertIn("splash one Target type number 00", say(c, "confirm kill"))

    def test_overflowed_upgrade_is_retried(self):
        eye = FakeEye()
        eye.pic["my_kills_this_mission"] = self.many_kills("likely")[:1]
        c = self.controller(eye)
        self.assertIn("Likely", say(c, "confirm kill"))
        eye.pic["my_kills_this_mission"] = self.mixed_kills()
        eye.pic["_close_bandits"] = self.BANDIT
        self.assertIn("Ask again for the rest.", say(c, "confirm kills"))
        del eye.pic["_close_bandits"]
        eye.pic["my_kills_this_mission"] = self.many_kills()[:1]
        self.assertIn("Now confirmed, earlier likely", say(c, "confirm kill"))


class ReviewFix4Tests(unittest.TestCase):
    """RV-1 radio6d: every category Eye.domain() calls air or sea is excluded from ground survivors."""

    AIR_SEA = ("fighter", "attack", "bomber", "AWACS", "tanker", "transport", "helicopter", "attack helo",
               "ship", "carrier")

    def answer(self, red):
        eye = FakeEye()
        eye.pic["alive_totals_by_coalition"] = {"red": red}
        c = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused", eye=eye,
                             llm=lambda messages: "LLM ANSWER")
        return say(c, "confirm kill, tell me what remains")

    def test_air_and_sea_alone_are_not_ground(self):
        for cat in self.AIR_SEA:
            with self.subTest(cat=cat):
                answer = self.answer({cat: 2})
                self.assertIn("No enemy ground units remaining", answer)
                self.assertNotIn(cat, answer.split("Athena, ", 1)[1].replace("No kills showing.", ""))

    def test_air_and_sea_mixed_with_ground(self):
        red = {cat: 1 for cat in self.AIR_SEA}
        red.update({"armor": 2, "artillery": 1, "train": 1})
        answer = self.answer(red)
        self.assertIn("two enemy armor", answer)
        self.assertIn("one enemy artillery", answer)
        self.assertIn("one trains", answer)
        for cat in ("AWACS", "attack helo", "carrier", "fighter", "tanker"):
            self.assertNotIn(cat, answer)


if __name__ == "__main__":
    unittest.main()
