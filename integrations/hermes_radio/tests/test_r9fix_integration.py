"""Round 9 integration: radio launch calls read the Eye's deduplicated threats.missiles_in_flight."""
from copy import deepcopy
import unittest
from integrations.hermes_radio.simple_controller import SimpleController, loadout_calls, debrief_summary
from integrations.hermes_radio.tests.test_r9fix_radio import LIVE, controller, frames


def row(mid, clock, rng, name="SA-2", raw="SA2V755", **kw):
    return dict({"id": mid, "type": name, "raw_type": raw, "spoken_name": name, "bearing": 40, "range_nm": rng,
                 "clock": clock, "call": "Missile launch, %s." % name, "first_seen": 100.0, "targeting_me": False,
                 "age_s": 5}, **kw)


def picture(flight, inbound=None):
    pic = deepcopy(LIVE["launch_2038"])
    pic["threats"]["missiles_in_flight"] = flight
    pic["threats"]["missiles_inbound"] = inbound if inbound is not None else [
        {"type": "SA-2", "range_nm": 8.2, "clock": 10, "launcher": "Other"},
        {"type": "SA2V755", "range_nm": 8.1, "clock": 10, "launcher": "Other"}]  # same missile twice, no id
    return pic


class LaunchInFlightTests(unittest.TestCase):
    def test_duplicate_rows_one_call_negative_stinger(self):
        pic = picture([row("777", 10, 8.2), row("777", 10, 8.1, raw="Eval")])
        said = " ".join(frames(controller(pic), "Athena, stinger launch."))
        self.assertEqual(said, "Prometheus, Athena, Negative Stinger; SA-2 in the air, your ten o'clock, eight miles.")
        self.assertNotIn("Also", said)

    def test_same_missile_then_new_launch_by_id(self):
        pic = picture([row("777", 10, 8.2)])
        c = controller(pic)
        self.assertIn("SA-2 in the air", " ".join(frames(c, "Athena, missile launch.")))
        self.assertIn("Same missile, SA-2 in the air, your ten o'clock, eight miles.",
                      " ".join(frames(c, "Athena, another missile?")))
        # same id, rows now at a different range: still the same missile
        pic["threats"]["missiles_in_flight"] = [row("777", 10, 20.0)]
        c.eye.pic = pic
        self.assertIn("Same missile", " ".join(frames(c, "Athena, another missile?")))
        pic["threats"]["missiles_in_flight"] = [row("777", 10, 6.0), row("778", 3, 14.0)]
        c.eye.pic = pic
        third = " ".join(frames(c, "Athena, another missile?"))
        self.assertNotIn("Same", third)
        self.assertIn("Two SA-2 missiles, ten and three o'clock.", third)

    def test_two_ids_both_called(self):
        said = " ".join(frames(controller(picture([row("1", 1, 6.0), row("2", 3, 9.0)])), "Athena, missile launch."))
        self.assertIn("Two SA-2 missiles, one and three o'clock.", said)

    def test_spoken_name_wins_over_raw_type(self):
        pic = picture([row("5", 2, 4.0, name="Stinger", raw="weapons.missiles.Stinger")])
        said = " ".join(frames(controller(pic), "Athena, stinger launch."))
        self.assertIn("Stinger in the air, your two o'clock, four miles.", said)
        self.assertNotIn("Negative", said)

    def test_empty_in_flight_beats_stale_inbound(self):
        said = " ".join(frames(controller(picture([])), "Athena, missile launch."))
        self.assertEqual(said, "Prometheus, Athena, No missile in the air that I can see.")

    def test_falls_back_to_inbound_without_in_flight(self):
        pic = deepcopy(LIVE["launch_2038"])
        pic["threats"].pop("missiles_in_flight", None)
        self.assertIn("Two SA-2 missiles", " ".join(frames(controller(pic), "Athena, missile launch.")))


class SpokenNamesTests(unittest.TestCase):
    OWN = {"type": "F-14B", "loadout": {"available": True, "gun_rounds": 0, "weapons": [
        {"weapon": "Mk 5 rockets", "spoken_name": "Mk 5 rockets", "count": 14},
        {"weapon": "chaff/flare pod", "spoken_name": "chaff/flare pod", "count": 1},
        {"weapon": "LANTIRN pod", "spoken_name": "LANTIRN pod", "count": 1},
        {"weapon": "HB-ALE40-30/60", "count": 1}]}}

    def test_loadout_lists_pods_but_not_as_weapons(self):
        line = loadout_calls(self.OWN, "what's my loadout?")[0]
        self.assertTrue(line.startswith("You have Mk 5 rockets fourteen."), line)
        self.assertIn("Pods: chaff/flare pod and LANTIRN pod.", line)
        self.assertNotIn("HB-", line)
        self.assertNotIn("pod one", line)

    def test_pods_only_is_winchester_with_pods(self):
        own = deepcopy(self.OWN)
        own["loadout"]["weapons"] = own["loadout"]["weapons"][1:3]
        self.assertTrue(loadout_calls(own, "loadout")[0].startswith("Winchester, no weapons left."))

    def test_debrief_never_counts_pods_as_fired(self):
        pic = {"my_kills_this_mission": [], "my_weapons_used_this_mission": {"by_weapon": {
            "chaff/flare pod": 2, "Pave Spike pod": 1, "Mk 5 rockets": 14}}}
        text = debrief_summary(pic)
        self.assertIn("Mk 5 rockets", text)
        self.assertNotIn("pod", text)

    def test_kill_confirm_uses_eye_spoken_target(self):
        pic = deepcopy(LIVE["launch_2038"])
        pic["my_kills_this_mission"] = [{"target": "SA-2 radar", "category": "SAM", "weapon": "AGM-65A",
                                         "seq": 1, "age_s": 5, "confidence": "confirmed"}]
        said = " ".join(frames(controller(pic), "Athena, confirm kills"))
        self.assertIn("one SA-2 radar", said)


if __name__ == "__main__":
    unittest.main()
