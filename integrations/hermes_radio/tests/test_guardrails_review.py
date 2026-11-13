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
                    self.assertEqual(r["speech"].count(call), 1)
                    self.assertIn("Stay sharp.", r["speech"])
                    self.assertIn("alert", ctl.eye.announced)

    def test_subject_checks_keep_other_sentences(self):
        pic = {"connected": True, "ownship": {"id": 1, "type": "F-4E", "fuel": {"total_lb": 3640}},
               "threats": {"sam_in_range": [{"call": "SAM SA-2 could be tracking you."}]},
               "my_kills_this_mission": [{"target": "Ural-375", "confidence": "confirmed"}]}
        for bad in ("Fuel unknown, radar unavailable.", "Fuel unknown, cockpit unavailable.", "No fuel data.",
                    "No SAM threats.", "Splash nine kills."):
            ctl = self.make(pic, sc.ADDRESS + bad + " Stay sharp.")
            r = ctl.exchange(_request("give me your assessment"))
            self.assertNotIn(bad, r["speech"])
            self.assertIn("Stay sharp.", r["speech"])
            self.assertNotEqual(r["speech"], sc.FALLBACK)
        for good in ("F-4E Phantom.", "Ural three seven fives.", "Eighteen friendly aircraft.", "Running in, copy."):
            self.assertIsNone(sc.reply_check(good, pic, "assessment"))
        for absent in ("Fuel unknown, radar unavailable.", "No fuel data.", "No SAM threats.", "No kills showing."):
            self.assertIsNone(sc.reply_check(absent, {}, "assessment"))

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

    def test_unknown_subject_and_named_clearance_boundaries(self):
        pic = {"connected": True, "ownship": {"type": "F-4E", "fuel": {"total_lb": 3640}},
               "threats": {"sam_in_range": [{"call": "SAM SA-2 could be tracking you."}]}}
        for reply in ("Fuel is fine but radar unavailable.", "Fuel is fine and cockpit unavailable.",
                      "You are clear of the SA-2.", "Enemy position unknown."):
            self.assertIsNone(sc.reply_check(reply, pic, "assessment"), reply)
        for reply in ("Position unavailable.", "I don't know.", "I do not know."):
            self.assertIsNotNone(sc.reply_check(reply, pic, "fuel state"), reply)
        for reply in ("No tanker is showing in the traffic picture.", "No missile track is available in this picture."):
            scoped = dict(pic, air_traffic={"blue": [{"call": "Friendly A-10C."}]},
                          threats={"missiles_inbound": [{"call": "Missile launch, SA-2."}]})
            self.assertIsNone(sc.reply_check(reply, scoped, "tracking my weapon?"), reply)
        self.assertIsNone(sc.reply_check("I don't know.", {}, "fuel state"))

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

    def test_all_clear_scope_does_not_borrow_another_subject(self):
        pic = {"ownship": {"type": "F-4E"}, "threats": {
            "sam_in_range": [{"call": "Early warning radar could be watching you.", "range_nm": 39},
                             {"call": "SAM SA-2 could be tracking you.", "range_nm": 18}],
            "missiles_inbound": [{"call": "Missile launch, SA-2."}]}}
        self.assertIsNone(sc.reply_check("No SAM within ten miles.", pic, "assessment"))
        self.assertIsNone(sc.reply_check("No missile tracking data available.", pic, "tracking my weapon?"))
        self.assertIsNotNone(sc.reply_check("No SAM threats.", pic, "assessment"))
        self.assertIsNotNone(sc.reply_check("No missiles.", pic, "assessment"))
        pic["threats"]["sam_in_range"].pop()
        self.assertIsNone(sc.reply_check("No SAM in range.", pic, "assessment"))

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
                self.assertEqual(r["speech"].count(alert), 1)
                self.assertIn("Stay sharp.", r["speech"])
                self.assertLessEqual(len(r["speech"]), 280)
                self.assertTrue(ctl.eye.announced)

    def test_real_sa2_false_clear_and_kill_number(self):
        import json
        from pathlib import Path
        pic = json.loads((Path(__file__).parent / "fixtures/guardrails-review.json").read_text())["w03"]
        for prose in ("No SAM threats.", "No SAMs in range.", "Clear.", "No threats."):
            ctl = self.make(pic, prose + " Stay sharp.")
            r = ctl.exchange(_request("give me your assessment"))
            self.assertIn(pic["threats"]["sam_in_range"][0]["call"], r["speech"])
            self.assertNotIn(prose, r["speech"])
            self.assertIn("Stay sharp.", r["speech"])
        ctl = self.make(pic, "Splash nine kills. Stay sharp.")
        r = ctl.exchange(_request("give me your assessment"))
        self.assertIn("No kills yet.", r["speech"])
        self.assertIn("Stay sharp.", r["speech"])

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

    def test_all_clear_collections_and_kill_scope(self):
        cases = [
            ("Nothing tracking you.", {"threats": {"sam_radar": {"tracking_you": [{"type": "SA-2"}]}}}, "Radar tracking you."),
            ("No threats.", {"threats": {"sam_radar": {"tracking_you": [{"type": "SA-2"}]}}}, "Radar tracking you."),
            ("No hostiles within fifty miles.", {"hostile_groups": [{"category": "fighter", "range_nm": 40, "call": "Bandit F-5, forty miles."}]}, "Bandit F-5, forty miles."),
            ("No ground targets.", {"ground_targets_within_20nm": [{"call": "Enemy armor, ten miles."}]}, "Enemy armor, ten miles."),
            ("No kills.", {"recent_kills_and_losses_last_10_min": [{"by_me": True, "event": "killed", "victim": {"type": "Ural-375", "category": "vehicles"}}]}, "Ural-375"),
        ]
        for prose, pic, expected in cases:
            ctl = self.make(dict(pic, ownship={"type": "F-4E"}), prose + " Stay sharp.")
            r = ctl.exchange(_request("give me your assessment"))
            self.assertIn(expected, r["speech"])
            self.assertNotIn(prose, r["speech"])
        self.assertIsNone(sc.reply_check("No bandits within twenty miles.", cases[2][1], "assessment"))
        pic = {"ownship": {"type": "F-4E"}, "my_kills_this_mission": [
            {"seq": 1, "target": "Ural-375", "age_s": 10, "confidence": "confirmed"},
            {"seq": 2, "target": "T-55", "age_s": 5, "confidence": "confirmed"}]}
        ctl = self.make(pic, "Two new kills.")
        ctl._remember("me", ctl._kill_rows(pic, False, 1)[:1], [])
        r = ctl.exchange(_request("give me your assessment"))
        self.assertNotIn("Two new kills.", r["speech"])
        self.assertIn("splash one T-55", r["speech"])
        ctl._llm = lambda m: "Two total kills."
        r = ctl.exchange(_request("give me your assessment"))
        self.assertIn("Two total kills.", r["speech"])

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
