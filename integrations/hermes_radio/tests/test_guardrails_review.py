import unittest
from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.tools.model_eval_guardrails import Eye
from integrations.hermes_radio.tools.model_eval import _Log, _request

class ReviewTests(unittest.TestCase):
    def make(self, pic, reply):
        self.log = _Log()
        return sc.SimpleController(None, self.log, model="stub", api_key="", eye=Eye(pic), llm=lambda m: reply)

    def test_controller_owns_pending_calls(self):
        for field, key, call in (("rwr_alert_to_announce", "_rwr_keys", "SAM SA-2 radar sees you, your twelve o'clock, eighteen miles."),
                                 ("splash_to_announce", "_splash_keys", "Splash one Ural-375.")):
            with self.subTest(field=field):
                pic = {"connected": True, "ownship": {"type": "F-4E"}, field: [call], key: ["alert"]}
                for prose in ("Stay sharp.", call + " Stay sharp.", "Fuel unknown. Stay sharp."):
                    ctl = self.make(pic, sc.ADDRESS + prose)
                    r = ctl.exchange(_request("give me your assessment"))
                    self.assertEqual(r["speech"].count(call), 2 if call in prose else 1)
                    self.assertIn("Stay sharp.", r["speech"])
                    self.assertIn("alert", ctl.eye.announced)


    def test_identity_null_and_pseudo_flaps_do_not_clear(self):
        from integrations.hermes_radio.eye import Eye as LiveEye
        real = {"id": 101, "name": "pilot", "type": "F-4E", "group": "flight"}
        eye = LiveEye()
        ctl = self.make({"connected": True, "ownship": real}, "Copy.")
        for own in (real, None, real, dict(real, id=-1), real, None, real):
            eye._resolve_own = lambda own=own: own
            eye._own_locked()
            ctl.eye.pic["ownship"] = own
            ctl.exchange(_request("radio check"))
        self.assertEqual(eye.chat_generation, 1)
        self.assertEqual(len(ctl.history), 7)
        for own in (dict(real, id=102), dict(real, id=102, type="F-16"), dict(real, id=102, name="other")):
            eye._resolve_own = lambda own=own: own
            eye._own_locked()
            ctl.eye.pic["ownship"] = own
            ctl.exchange(_request("radio check"))
            self.assertEqual(len(ctl.history), 1)
        self.assertEqual(eye.chat_generation, 4)

    def test_sentence_history_and_fixed_repeat(self):
        pic = {"connected": True, "ownship": {"id": 1, "type": "F-4E"}}
        ctl = self.make(pic, "Fuel unknown; Creech is seven miles.")
        first = ctl.exchange(_request("tell me something"))
        self.assertEqual(ctl.history[-1][1], sc.ADDRESS + "Creech is seven miles.")
        ctl._llm = lambda m: self.fail("repeat must not call model")
        for ask in ("say again", "repeat", "say that again", "repeat last"):
            r = ctl.exchange(_request(ask))
            self.assertEqual(r["route"], "fixed")
            self.assertEqual(r["speech"], first["speech"])
        for bad in ("I don't know.", "I do not know.", "You are not in an aircraft."):
            self.assertEqual(sc.factual_history(bad), "")
        for good in ("No kills showing.", "No traffic."):
            self.assertEqual(sc.factual_history(good), sc.ADDRESS + good)

    def test_second_identical_splash_is_a_new_event(self):
        call = "Splash one Ural-375."
        from integrations.hermes_radio.eye import Eye as LiveEye
        live = LiveEye()
        keys = [live._record_kill(live.my_kills, {"id": ident, "type": "Ural-375"},
                                 "AGM-65A", "kill event", 100.0)["key"] for ident in (11, 12)]
        self.assertNotEqual(*keys)
        self.assertEqual(live.my_kills[0]["splash_call"], live.my_kills[1]["splash_call"])
        ctl = self.make({"ownship": {"type": "F-4E"}}, "Stay sharp.")
        for key in keys:
            ctl.eye.pic.update(splash_to_announce=[call], _splash_keys=[key])
            r = ctl.exchange(_request("give me your assessment"))
            self.assertEqual(r["speech"].count(call), 1)
            self.assertIn(key, ctl.eye.announced)

    def test_pending_calls_rearm_after_unsent_model_reply(self):
        call = "SAM SA-2 tracking you."
        ctl = self.make({"ownship": {"type": "F-4E"}, "rwr_alert_to_announce": [call],
                         "_rwr_keys": ["sam"]}, "Stay sharp.")
        ctl.transcript.outcome_of = lambda tid: "failed"
        for _ in range(2):
            r = ctl.exchange(_request("give me your assessment"))
            self.assertIn(call, r["speech"])


    def test_repeat_fallback_reanswers_previous_question(self):
        ctl = self.make({"ownship": {"type": "F-4E"}}, "")
        r = ctl.exchange(_request("give me your assessment"))
        self.assertEqual(r["speech"], sc.FALLBACK)
        seen = []
        ctl._llm = lambda m: seen.append(m[-1]["content"]) or "Stay sharp."
        r = ctl.exchange(_request("repeat last"))
        self.assertEqual(seen, ["give me your assessment"])
        self.assertIn("Stay sharp.", r["speech"])

    def test_cache_checks_complete_ordered_messages(self):
        from integrations.hermes_radio.tools.model_eval_guardrails import Model, CachedModel
        model = Model(None, stub=True)
        messages = [{"role": "user", "content": "fuel"}]
        model.answer_by(messages, 0)
        cached = CachedModel(model.rows[-1])
        cached.answer_by(messages, 0)
        with self.assertRaises(AssertionError):
            cached.answer_by([{"role": "user", "content": "kill"}], 0)


    def test_exact_recorded_sam_and_splash_survive_harmless_tail(self):
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parent / "fixtures/guardrails-review.json").read_text())
        for key, field in (("1890", "rwr_alert_to_announce"), ("1834", "splash_to_announce")):
            pic = fixture[key]
            alert = pic[field][0].rstrip(". ") + "."
            for prose in ("Stay sharp.", alert + " Stay sharp.", "Cleared to land. Stay sharp."):
                ctl = self.make(pic, sc.ADDRESS + prose)
                r = ctl.exchange(_request("give me your assessment"))
                self.assertIn(alert, r["speech"])
                self.assertEqual(r["speech"].count(alert), 2 if alert in prose else 1)
                self.assertIn("Stay sharp.", r["speech"])
                self.assertLessEqual(len(r["speech"]), 280)
                self.assertTrue(ctl.eye.announced)


    def test_close_bandit_and_alerts_are_whole_under_budget(self):
        close = "Bandit MiG-29, zero nine zero, eight miles, angels ten, three o'clock."
        alerts = ["SAM SA-2 tracking you, your twelve o'clock, eighteen miles.",
                  "Missile launch, SA-2, your twelve o'clock, eighteen miles.",
                  "Missile launch, SA-2, your eleven o'clock, seventeen miles."]
        pic = {"ownship": {"type": "F-4E"}, "_close_bandits": [{"call": close, "key": "close"}],
               "rwr_alert_to_announce": alerts, "_rwr_keys": ["a", "b", "c"]}
        ctl = self.make(pic, "Stay sharp. " * 20)
        r = ctl.exchange(_request("give me your assessment"))
        self.assertLessEqual(len(r["speech"]), 280)
        self.assertIn(close, r["speech"])
        for alert, key in zip(alerts, ["a", "b", "c"]):
            self.assertEqual(key in ctl.eye.announced, alert in r["speech"])
        self.assertNotIn("seventeen", r["speech"].replace(alerts[2], ""))


    def test_repeat_skips_failed_delivery_and_reanswers_expired(self):
        import time
        ctl = self.make({"connected": True, "ownship": {"type": "F-4E"}}, "Creech.")
        req = _request("tell me something"); req.transmission_id = "sent"
        ctl.exchange(req)
        ctl.transcript.outcome_of = lambda tid: "sent" if tid == "sent" else "failed"
        ctl._llm = lambda m: "Nellis."
        req = _request("what else"); req.transmission_id = "failed"
        ctl.exchange(req)
        r = ctl.exchange(_request("repeat last"))
        self.assertEqual(r["speech"], sc.ADDRESS + "Creech.")
        ctl._last_exchange = None
        ctl._last_delivered = ("tell me something", sc.ADDRESS + "Creech.", time.monotonic()-121)
        seen=[]
        ctl._llm = lambda messages: seen.append(messages[-1]["content"]) or "Updated answer."
        r = ctl.exchange(_request("repeat last"))
        self.assertEqual(seen, ["tell me something"])
        self.assertIn("Updated answer.", r["speech"])
