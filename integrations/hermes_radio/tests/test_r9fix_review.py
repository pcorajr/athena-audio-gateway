"""Round 9 review fixes: spoken names in impact calls, launch route vs own-weapon calls, SAM never merged into
vehicles, aircraft lead variants, designations kept, family names, fit_reply run-on.

Documented, deliberately NOT fixed (review MINORs): kill-lag 'checking' also fires for 'kill credited to another
shooter'; missile id equality shot-event vs Eval is unverified on live data; memory crash clocks mix mission/receipt time."""
from copy import deepcopy
import unittest

from integrations.hermes_radio import cockpit as cp
from integrations.hermes_radio.simple_controller import (MAX_REPLY_CHARS, call_class, debrief_summary, fit_reply,
                                                         impact_calls, loadout_calls)
from integrations.hermes_radio.tests.test_eye_round5 import contact, make_eye
from integrations.hermes_radio.tests.test_r9fix_radio import LIVE, controller, frames


def shot(result, hit_type=None, gone=None, intended=None, **extra):
    row = {"weapon": "AGM-65A", "kind": "missile", "age_s": 20, "result": result}
    if hit_type:
        row["hit"] = {"type": hit_type, "category": "SAM"}
    if gone:
        row["no_longer_showing"] = gone
    if intended:
        row["intended_target"] = {"type": intended}
    row.update(extra)
    return {"my_shots_last_15_min": {"shots": [row]}}


class ImpactSpokenNames(unittest.TestCase):
    def test_hit_and_kill_use_plain_names(self):
        self.assertEqual(impact_calls(shot("hit", "S_75M_Volhov")), ["Hit observed on SA-2, no kill yet."])
        self.assertEqual(impact_calls(shot("kill", "SNR_75V")), ["Good hit, splash one SA-2 radar."])
        self.assertEqual(impact_calls(shot("kill", gone=["Ural-4320T"])), ["Good hit, splash one Ural truck."])
        self.assertEqual(impact_calls(shot("likely kill", intended="S_75M_Volhov")),
                         ["Likely kill on SA-2, target no longer showing."])

    def test_live_fixture_has_no_raw_type_in_any_impact_call(self):
        for row in LIVE["confirm_2051"]["my_shots_last_15_min"]["shots"]:
            pic = {"my_shots_last_15_min": {"shots": [dict(row, age_s=5)]}}
            said = " ".join(impact_calls(pic))
            for raw in ("S_75M", "SNR_75V", "Ural-4320T", "ZU-23 Emplacement", "_Volhov"):
                self.assertNotIn(raw, said)

    def test_scenery_and_unknown_unchanged(self):
        self.assertEqual(impact_calls(shot("hit", "Unknown")), ["Hit observed on target, no kill yet."])
        self.assertEqual(impact_calls(shot("hit", "T-72B")), ["Hit observed on T-72B, no kill yet."])


class LaunchRoute(unittest.TestCase):
    OWN = ("Athena, Maverick launched.", "Maverick launched", "Athena, missile away.", "Athena, missiles away",
           "Athena, fox two.", "Athena, rifle.", "Rifle, launched", "Fox two launched", "Athena, pickle.",
           "Athena, ready to launch.", "Athena, cleared for launch?", "Athena, I'm launching a Maverick",
           "Athena, AGM-65 launched", "Athena, Sidewinder launched", "Athena, bomb launched",
           "Athena, I just launched", "Athena, request clear to launch", "Athena, we launched two")
    THREAT = ("Athena, stinger, launch.", "Athena, stinger launch", "Athena, missile launch.", "launch, launch",
              "Athena, SAM launch!", "Athena, SA-2 launch", "Athena, SA-6 launched", "Athena, SA 6 launch",
              "Athena, manpad launch", "Athena, manpads launch.", "Athena, missile inbound", "Athena, I'm being shot at",
              "Athena, I'm getting shot at", "Athena, another missile?", "Athena, any launches?",
              "Athena, was that a launch?", "Athena, launch at me")

    def test_own_weapon_calls_never_route_to_launch(self):
        for text in self.OWN:
            self.assertNotEqual(call_class(text), "launch", text)

    def test_threat_calls_route_to_launch(self):
        for text in self.THREAT:
            self.assertEqual(call_class(text), "launch", text)

    def test_own_release_keeps_previous_behaviour(self):
        c = controller(LIVE["launch_2038"], llm=lambda messages: "Prometheus, Athena, copy.")
        said = " ".join(frames(c, "Athena, Maverick launched."))
        self.assertNotIn("SA-2 in the air", said)
        self.assertNotIn("No missile in the air", said)
        self.assertEqual(" ".join(frames(controller(LIVE["launch_2038"]), "Athena, fox two.")), "Prometheus, Athena, copy, weapon away.")

    def test_threat_launch_still_answers_from_the_picture(self):
        said = " ".join(frames(controller(LIVE["launch_2038"]), "Athena, stinger, launch."))
        self.assertIn("SA-2", said)


class GroundMergeAirDefence(unittest.TestCase):
    def calls(self, *contacts):
        eye, _ = make_eye(*contacts)
        return eye.picture()["ground_targets_within_20nm"]

    def test_sam_not_merged_with_vehicles_or_armor(self):
        rows = self.calls(contact(1, "S_75M_Volhov", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "Ural-4320T", "red", "GROUND", distance=10.5, bearing=5),
                          contact(3, "T-55", "red", "GROUND", distance=11, bearing=5))
        self.assertEqual(len(rows), 2, [r["call"] for r in rows])
        self.assertTrue(any(r["category"] == "SAM" and "merged_groups" not in r for r in rows))
        self.assertFalse(any("Enemy vehicles" in r["call"] and "SAM" in r["member_categories"] for r in rows))

    def test_merge_step_keeps_sam_apart_too(self):
        # 1.8 nm apart: separate groups, inside the merge window; SAM must still stay its own call
        rows = self.calls(contact(1, "S_75M_Volhov", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "Ural-4320T", "red", "GROUND", distance=11.8, bearing=5),
                          contact(3, "T-55", "red", "GROUND", distance=11.9, bearing=5))
        self.assertEqual(len(rows), 2, [r["call"] for r in rows])
        self.assertTrue(rows[0]["call"].startswith("SAM site"), rows[0]["call"])
        self.assertTrue("two contacts" in rows[1]["call"] and "SAM" not in rows[1]["call"], rows[1]["call"])

    def test_vehicles_still_merge_but_without_sam(self):
        rows = self.calls(contact(1, "S_75M_Volhov", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "Ural-4320T", "red", "GROUND", distance=10.5, bearing=5),
                          contact(3, "T-55", "red", "GROUND", distance=11, bearing=5))
        for r in rows:
            if "merged_groups" in r:
                self.assertNotIn("SAM", r["member_categories"])

    def test_same_category_sam_merge_names_the_site(self):
        rows = self.calls(contact(1, "S_75M_Volhov", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "SNR_75V", "red", "GROUND", distance=10.4, bearing=5))
        self.assertEqual(len(rows), 1, [r["call"] for r in rows])
        self.assertTrue(rows[0]["call"].startswith("SAM site, two contacts"), rows[0]["call"])
        self.assertEqual(rows[0]["category"], "SAM")

    def test_any_sams_still_produces_sam_calls(self):
        from integrations.hermes_radio.simple_controller import requested_calls
        eye, _ = make_eye(contact(1, "S_75M_Volhov", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "Ural-4320T", "red", "GROUND", distance=10.5, bearing=5))
        pic = eye.picture()
        calls = requested_calls("ground_picture", "Athena, any SAMs?", pic)
        self.assertTrue(calls and all("SAM" in c or "SA-2" in c for c in calls), calls)
        self.assertFalse(any("Enemy vehicles" in c for c in calls))


class AircraftLead(unittest.TestCase):
    OWN = {"type": "F-4E-45MC", "loadout": {"available": True, "weapons": [{"weapon": "AGM-65A", "spoken_name": "AGM-65A", "count": 4}]}}

    def test_variants_get_the_lead(self):
        for text in ("what airplane am I in", "what aircraft am I flying", "what jet am I on", "which aircraft",
                     "what am I in", "what am I flying and what's my loadout", "what plane am I in?"):
            self.assertTrue(loadout_calls(self.OWN, text)[0].startswith("You're flying an F-4E."), text)
        self.assertFalse(loadout_calls(self.OWN, "what's my loadout")[0].startswith("You're"))

    def test_article_by_sound(self):
        for typ, art in (("Mirage-F1CE", "a"), ("A-10C_2", "an"), ("F-14B", "an"), ("MiG-29S", "a"), ("Su-27", "an")):
            own = dict(self.OWN, type=typ)
            self.assertTrue(loadout_calls(own, "what am I flying")[0].startswith("You're flying %s " % art), typ)


class DesignationsKept(unittest.TestCase):
    def test_designations_not_clipped(self):
        for raw, want in (("Shilka ZSU-23-4", "Shilka ZSU-23-4"), ("ZSU-23-4 Shilka", "ZSU-23-4 Shilka"),
                          ("SA-11 Buk", "SA-11 Buk"), ("2S6 Tunguska", "2S6 Tunguska"), ("T-72B", "T-72B"),
                          ("BTR-80", "BTR-80"), ("MiG-29S", "MiG-29S"), ("AIM-120C", "AIM-120C")):
            self.assertEqual(cp.spoken_name(raw), want, raw)

    def test_editor_suffix_still_stripped(self):
        self.assertEqual(cp.spoken_name("Static Widget-9-1_copy_7"), "Widget")
        self.assertEqual(cp.spoken_name("Widget-9-1"), "Widget")


class WeaponFamilies(unittest.TestCase):
    def test_loadout_and_debrief_agree(self):
        own = dict(AircraftLead.OWN)
        self.assertIn("Mavericks four", loadout_calls(own, "")[0])
        self.assertNotIn("AGM-65", loadout_calls(own, "")[0])
        pic = {"my_kills_this_mission": [], "my_weapons_used_this_mission": {"by_weapon": {"AGM-65A": 4, "AIM-9M": 1, "GBU-12": 2}}}
        text = debrief_summary(pic)
        self.assertIn("Four Mavericks", text)
        self.assertIn("one Sidewinder", text)
        self.assertIn("two GBU-12", text)

    def test_single_maverick_and_other_families(self):
        own = {"type": "F-4E", "loadout": {"available": True, "weapons": [
            {"weapon": "AGM-65D", "count": 1}, {"weapon": "AIM-7E", "count": 2}, {"weapon": "AIM-54C", "count": 6},
            {"weapon": "GBU-12", "count": 2}]}}
        said = loadout_calls(own, "")[0]
        for want in ("Maverick one", "Sparrows two", "Phoenixes six", "GBU-12 two"):
            self.assertIn(want, said)


class FitReplyRunOn(unittest.TestCase):
    def test_single_overlong_sentence_is_cut_at_a_word(self):
        words_ = " ".join("word%d" % i for i in range(400))
        text, trimmed = fit_reply("Prometheus, Athena, " + words_)
        self.assertTrue(trimmed)
        self.assertNotEqual(text, "Prometheus, Athena.")
        self.assertTrue(text.startswith("Prometheus, Athena, word0 word1"))
        self.assertTrue(text.endswith("."))
        self.assertLessEqual(len(text), MAX_REPLY_CHARS)
        self.assertRegex(text[:-1].split()[-1], r"^word\d+$")


if __name__ == "__main__":
    unittest.main()
