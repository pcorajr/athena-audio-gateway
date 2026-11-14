"""r12.10: fixes from the 2026-11-13 live flight (r12.9). No model or network access."""
import unittest
from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say
from integrations.hermes_radio.tests.test_threat_early import ApproachTests


class GunOnlyTests(unittest.TestCase):
    OWN = {"type": "A-10C_2", "loadout": {"available": True, "gun_rounds": 241, "weapons": [
        {"weapon": "AGM-65D", "spoken_name": "Maverick", "count": 4}]}}

    def test_gun_question_gets_the_gun_only(self):
        # 22:50:49 "how much gun ammo do I have left" got the whole loadout, twice
        for ask in ("Athena, how much gun ammo do I have left?", "how many rounds left",
                    "the ammo is 250 for the gun, but I only asked you about guns"):
            self.assertEqual(sc.loadout_calls(self.OWN, ask), ["Gun, two hundred forty one rounds."], ask)

    def test_loadout_questions_unchanged(self):
        for ask in ("Athena, what's my loadout?", "weapons and gun rounds", "what bombs and gun ammo do I have"):
            line = sc.loadout_calls(self.OWN, ask)[0]
            self.assertIn("Maverick", line, ask)
            self.assertIn("gun two hundred forty one rounds", line, ask)

    def test_empty_gun(self):
        own = {"loadout": {"available": True, "gun_rounds": 0, "weapons": []}}
        self.assertEqual(sc.loadout_calls(own, "gun ammo?"), ["Gun empty."])


class PastLaunchTests(unittest.TestCase):
    def threats(self, launches):
        return {"threats": {"missile_scan": "on", "missiles_in_flight": [], "missiles_inbound": [], "sam_in_range": [],
                            "launches_last_2_min": launches}}

    def test_recent_launches_are_reported_when_none_is_in_the_air(self):
        # 22:34:09 "was there SAM launch?" 20 s after two SA-13 shots: reply was only "No missile in the air"
        c, _ = controller({}, Stub(answer="x"))
        two = [{"type": "SA9M333", "launcher": "Strela-10M3", "age_s": 22}, {"type": "SA9M333", "launcher": "Strela-10M3", "age_s": 16}]
        self.assertEqual(c.launch_calls("was there SAM launch?", self.threats(two)),
                         ["Two SA-13 launches in the last two minutes; none in the air now."])
        self.assertEqual(c.launch_calls("was there SAM launch?", self.threats(two[:1])),
                         ["One SA-13 launch in the last two minutes; none in the air now."])

    def test_no_recent_launch_keeps_the_plain_answer(self):
        c, _ = controller({}, Stub(answer="x"))
        self.assertEqual(c.launch_calls("SAM launch?", self.threats([])), ["No missile in the air that I can see."])

    def test_raw_weapon_id_is_never_spoken(self):
        c, _ = controller({}, Stub(answer="x"))
        line = c.launch_calls("launch?", self.threats([{"type": "SA9M333", "launcher": None, "age_s": 5}]))[0]
        self.assertNotIn("SA9M333", line)



class _Site(unittest.TestCase):
    def setUp(self):
        ApproachTests.setUp(self)

    def site(self, *a, **k):
        return ApproachTests.site(self, *a, **k)


class EyeLaunchHistoryTests(_Site):
    def test_only_launch_alerts_of_the_last_two_minutes(self):
        self.site()
        now = self.now
        for key, age, kind in (("launch-1", 30, "missile launch"), ("launch-2", 300, "missile launch"), ("sam-1", 10, "SAM")):
            self.eye.threat_alerts.append({"key": key, "t": now - age, "call": "x", "kind": kind,
                                           "type": "SA9M333", "launcher": "Strela-10M3"})
        rows = self.eye.picture()["threats"]["launches_last_2_min"]
        self.assertEqual([(r["type"], r["launcher"]) for r in rows], [("SA9M333", "Strela-10M3")])
        self.assertTrue(28 <= rows[0]["age_s"] <= 33)


class SamStatementWordingTests(_Site):
    def test_negative_only_contradicts_a_dormant_claim(self):
        self.site()
        c, _ = controller({}, Stub(answer="copy."), eye=self.eye)
        frames, _ = say(c, "The SAM looks dormant")
        self.assertIn("Negative, SA-13 is alive", frames[0]["speech"])

    def test_attack_or_closing_statement_gets_a_caution_once(self):
        # 22:39:10 / 22:39:25: "running in with guns" got "Negative, SA-13 is alive" twice
        self.site()
        c, _ = controller({}, Stub(answer="copy."), eye=self.eye)
        frames, _ = say(c, "Athena, the SAM is out of ammunition, I'm running in with guns", "a")
        self.assertIn("Caution, SA-13 is alive", frames[0]["speech"])
        self.assertNotIn("Negative", frames[0]["speech"])
        frames, _ = say(c, "Good call, I'm still running in on the SAM", "b")
        self.assertNotIn("is alive", frames[0]["speech"])

    def test_a_dormant_claim_is_always_contradicted(self):
        self.site()
        c, _ = controller({}, Stub(answer="copy."), eye=self.eye)
        say(c, "I'm running in on the SAM", "a")
        frames, _ = say(c, "The SAM looks dead", "b")
        self.assertIn("Negative, SA-13 is alive", frames[0]["speech"])


class TrafficCapTests(unittest.TestCase):
    def test_two_nearest_and_a_count(self):
        # 22:41:52: three full friendly calls took 21 s on the radio
        calls = ["Friendly A-10C, one.", "Friendly IL-76MD, two.", "Friendly IL-76MD, three.", "Friendly F-16, four."]
        kept, _ = sc._fit_calls("traffic", calls, 280)
        self.assertEqual(kept, calls[:2] + ["Plus two more."])


if __name__ == "__main__":
    unittest.main()
