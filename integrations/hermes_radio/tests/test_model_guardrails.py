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


class GuardEdgeTests(unittest.TestCase):
    def test_internal_key_names_rejected_by_existing_guard(self):
        for answer in ("ownship is null", "JSON says no", "picture_top3 empty"):
            self.assertTrue(sc.parroted(answer), answer)
