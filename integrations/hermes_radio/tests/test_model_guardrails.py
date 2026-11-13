"""Guardrails from the disconnected preflight exchange on 2026-11-11 18:10:55."""
import copy
import unittest
from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.tools.model_eval import _Eye, _Log, _request

class HistoryTests(unittest.TestCase):
    def make(self, pic):
        self.messages = []
        def model(messages):
            self.messages.append(messages)
            return "Prometheus, Athena, copy."
        self.eye = _Eye(pic)
        self.eye.reset_at = 1
        self.eye.session_id = None
        self.ctl = sc.SimpleController(None, _Log(), model="stub", api_key="", eye=self.eye, llm=model)
        return self.ctl

    def test_initial_session_clears_preflight_history(self):
        ctl = self.make({"connected": False, "ownship": None})
        ctl.exchange(_request("radio check"))
        self.eye.session_id = 123
        self.eye.pic = {"connected": True, "ownship": {"type": "F-4E", "id": 1}}
        ctl.exchange(_request("radio check"))
        self.assertEqual([m["role"] for m in self.messages[-1]].count("assistant"), 0)

    def test_disconnected_reply_is_not_stored(self):
        ctl = self.make({"connected": False, "ownship": None})
        ctl.exchange(_request("picture"))
        self.assertEqual(list(ctl.history), [])

    def test_spawn_and_slot_change_clear_chat_only(self):
        ctl = self.make({"connected": True, "ownship": None})
        ctl.exchange(_request("radio check"))
        ctl._confirmed_kills = [{"sentinel": True}]
        for own in ({"type": "F-4E", "id": 1}, {"type": "F-4E", "id": 2}):
            self.eye.pic["ownship"] = own
            ctl.exchange(_request("radio check"))
            self.assertFalse(any(m["role"] == "assistant" for m in self.messages[-1]))
            self.assertEqual(ctl._confirmed_kills, [{"sentinel": True}])

    def test_reconnect_clears_chat(self):
        ctl = self.make({"connected": False, "ownship": {"type": "F-4E"}})
        ctl.exchange(_request("radio check"))
        self.eye.pic["connected"] = True
        ctl.exchange(_request("radio check"))
        self.assertFalse(any(m["role"] == "assistant" for m in self.messages[-1]))

    def test_fixed_airfield_answer_supplies_followup_and_cap(self):
        pic = {"connected": True, "ownship": {"type": "F-4E"}, "airbases": {
            "nearest_friendly": [{"name": "Creech", "call": "Creech, bearing zero one zero, seven miles."}]}}
        ctl = self.make(pic)
        first = ctl.exchange(_request("what's the nearest airfield?"))
        self.assertEqual(first["route"], "fixed")
        ctl.exchange(_request("what's the name of the airfield?"))
        self.assertTrue(any("Creech" in m["content"] for m in self.messages[-1] if m["role"] == "assistant"))
        for _ in range(10): ctl.exchange(_request("radio check"))
        self.assertEqual(len(ctl.history), 8)

    def test_eye_catches_reconnect_between_radio_calls(self):
        from integrations.hermes_radio.eye import Eye
        eye = Eye()
        eye.set_connected(True)
        generation = getattr(eye, "chat_generation", 0)
        eye.set_connected(False)
        eye.set_connected(True)
        self.assertGreater(getattr(eye, "chat_generation", 0), generation)

    def test_eye_catches_same_type_same_group_new_unit(self):
        from integrations.hermes_radio.eye import Eye
        from integrations.hermes_radio.tests.eye_fixture import PROMETHEUS
        eye = Eye()
        eye.on_unit(copy.deepcopy(PROMETHEUS))
        eye.picture()
        generation = getattr(eye, "chat_generation", 0)
        eye.on_gone(PROMETHEUS["id"])
        u = copy.deepcopy(PROMETHEUS)
        u["id"] += 1
        eye.on_unit(u)
        eye.picture()
        self.assertGreater(getattr(eye, "chat_generation", 0), generation)

    def test_stale_pairs_filtered_but_factual_pairs_retained(self):
        ctl = self.make({"connected": True, "ownship": {"type": "F-4E"}})
        for answer in ("Fuel unknown.", "Tracking disconnected.", "Not connected.",
                       "No friendly aircraft showing.", "I don't have that field.",
                       "Fuel unavailable.", "Say again.", "Ask again for fuel."):
            ctl._llm = lambda messages, answer=answer: "Prometheus, Athena, " + answer
            ctl.exchange(_request("radio check"))
            self.assertEqual(list(ctl.history), [], answer)

class PromptTests(unittest.TestCase):
    def test_study_grounding_is_first_without_growing_prompt(self):
        self.assertTrue(sc.PROMPT.startswith("GROUNDING (most important):\n"))
        self.assertEqual(sum(l.startswith("- ") for l in sc.PROMPT.split("\n\n")[0].splitlines()), 7)
        self.assertNotIn("your discretion", sc.PROMPT)
        self.assertLess(len(sc.PROMPT), 10124)
        self.assertLessEqual(sum(l.startswith("- ") for l in sc.PROMPT.splitlines()), 35)
        for instruction in ("likely destroyed", "grpc_inferred", "close-bandit", "DEV NOTE", "Follow-up"):
            self.assertIn(instruction, sc.PROMPT)

class ReplyCheckTests(unittest.TestCase):
    def check(self, reply, pic, ask="radio check"):
        check = getattr(sc, "reply_check", lambda *args: None)
        return check(reply, pic, ask)


    def test_grounded_spoken_numbers_and_rounding(self):
        pic = {"fuel": 3640, "range": 46.2, "bearing": "039", "call": "SA-2, your three o'clock."}
        self.assertIsNone(self.check("Three thousand six hundred forty pounds, zero three nine and forty six miles.", pic))
        self.assertIsNone(self.check("SA-2 at your three o'clock.", pic))

    def test_false_unknown_even_with_unrelated_number(self):
        pic = {"connected": True, "ownship": {"type": "F-4E", "fuel": {"total_lb": 3640}}, "distance": 20}
        for reply in ("Fuel unknown.", "Fuel unknown. Twenty miles.", "No connected picture."):
            self.assertIsNotNone(self.check(reply, pic, "fuel please"), reply)

    def test_study_false_rejects_are_allowed(self):
        pic = {"connected": True, "ownship": {"coalition": "blue", "loadout": {"available": True,
               "weapons": [{"weapon": "AIM-9", "count": 2}]}}, "airbases": {"nearest_friendly": []},
               "air_traffic": {"blue": [{"call": "Friendly F-4E."}]}}
        self.assertIsNone(self.check("No friendly airfield showing.", pic, "nearest friendly airfield"))
        self.assertIsNone(self.check("No Mavericks are showing in your loadout.", pic, "any Mavericks left?"))


    def test_live_rejection_is_logged_one_call_and_not_remembered(self):
        log = _Log(); calls = []
        def model(messages):
            calls.append(messages)
            return "Prometheus, Athena, fuel unknown."
        ctl = sc.SimpleController(None, log, model="stub", api_key="", eye=_Eye({
            "connected": True, "ownship": {"type": "F-4E", "fuel": {"total_lb": 3640}}}), llm=model)
        reply = ctl.exchange(_request("tell me something"))
        self.assertEqual(reply["route"], "fallback")
        self.assertEqual(reply["speech"], "Prometheus, Athena, Fuel 3,640 pounds.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctl.history[-1][1], reply["speech"])
        self.assertTrue(any("reply_check" in str(r.get("llm", {}).get("fallback_reason", "")) for r in log.records))

class GuardEdgeTests(unittest.TestCase):
    def test_internal_key_names_rejected_by_existing_guard(self):
        for answer in ("ownship is null", "JSON says no", "picture_top3 empty"):
            self.assertTrue(sc.parroted(answer), answer)



    def test_no_fuel_reading_is_rejected_when_fuel_present(self):
        pic = {"connected": True, "ownship": {"fuel": {"total_lb": 3640}}}
        self.assertIsNotNone(sc.reply_check("No fuel reading.", pic, "how are things?"))

    def test_article_plus_number_is_not_an_aircraft_type(self):
        self.assertIsNone(sc.reply_check("A two ship.", {"count": 2}, "radio check"))


    def test_real_model_spoken_forms_are_not_names(self):
        self.assertIsNone(sc.reply_check("Thirty-five enemy armor contacts remain.", {"count": 35}, "how many tanks?"))
        self.assertIsNone(sc.reply_check("Missile tracking is limited.", {}, "radio check"))
        self.assertIsNone(sc.reply_check("Three AGM-sixty-five Alphas remaining.",
                                        {"type": "AGM-65A", "count": 3}, "how many Mavericks?"))
        self.assertIsNone(sc.reply_check("Three AGM-sixty-five Bravos remaining.",
                                           {"type": "AGM-65A", "count": 3}, "how many Mavericks?"))




    def test_all_five_original_study_false_rejections_are_removed(self):
        import json
        from pathlib import Path
        from integrations.hermes_radio.tools import model_eval as me
        docs = Path(__file__).resolve().parents[3] / "docs/model-study"
        items = {i["id"]: i for i in me.load_set(docs / "eval-data.json.gz")[0]}
        rows = [json.loads(line) for line in (docs / "results.jsonl").read_text().splitlines()]
        cases = [r for r in rows if (r["id"] == "20261111:1415" and r["variant"] in ("baseline", "slim", "facts", "medium"))
                 or (r["id"] == "w17" and r["variant"] == "diet")]
        self.assertEqual(len(cases), 5)
        for row in cases:
            item = items[row["id"]]
            self.assertIsNone(sc.reply_check(row["reply"], item["picture"], item["ask"]), row)
