"""Offline regressions from Session 4, 2026-11-08 (no live DCS/SRS).

Reduced event shapes transcribed from stores-20261108.jsonl raw_event_sample and kill_by_me:
12:57:16 numeric gun weapon.type plus GAU8_30_AP weapon_name; 13:12:53 bridge scenery;
13:28:59 power pole hit followed by unknown scenery kill. BDG 2 is infantry id 18.
The mission target sequence below preserves all 19 distinct target ids/types from the stores log.
Additional mutations exercise missing descriptors, duplicate/replayed events and long missions.
"""
from copy import deepcopy
import json
import unittest

from integrations.hermes_radio.cockpit import CockpitState
from integrations.hermes_radio.eye import Eye, norm_object, weapon_name
from integrations.hermes_radio.tests import eye_fixture as fx


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


PILOT = fx.unit(1, "- Player", "A-10C_2", "blue", "- A-10C (Player)", "AIRPLANE",
                42.10836, 43.76773, 1097, 169, 180, player="Prometheus")
WINGMAN = dict(deepcopy(PILOT), id=2, name="Player Wingman", callsign="Hawg12", number_in_group=2)
WINGMAN.pop("player_name")

# Order and identities from the 19 kill_by_me rows; unknown scenery really was unknown in those rows.
MISSION_TARGETS = [
    (4, "Ural-375", "unit"), (3, "Ural-375", "unit"), (8, "Ural-375", "unit"),
    (12, "Paratrooper AKS-74", "unit"), (10, "Paratrooper AKS-74", "unit"),
    (13, "Paratrooper AKS-74", "unit"), (11, "Paratrooper AKS-74", "unit"),
    (9, "Ural-375", "unit"), (70270976, "MOST(ROAD)BIG-CRUSH", "scenery"),
    (17, "Paratrooper AKS-74", "unit"), (18, "Paratrooper AKS-74", "unit"),
    (22, "T-55", "unit"), (171507742, "unknown", "scenery"), (21, "T-55", "unit"),
    (171507774, "unknown", "scenery"), (171507779, "unknown", "scenery"),
    (171507773, "unknown", "scenery"), (277430537, "VAZ_NEW", "scenery"), (23, "T-55", "unit"),
]


def target(uid, type_name, kind="unit", name=""):
    # Kill descriptors in the actual log often omit group/callsign, unlike hit descriptors.
    d = {"id": uid, "type": type_name, "position": {"lat": 42.09, "lon": 43.77, "alt": 680}}
    if kind == "unit":
        d.update(name=name or "Unit #%s" % uid, coalition="COALITION_RED")
    return {kind: d}


def event(victim, shooter=PILOT, kind="kill", wid=16787200, wtype="16787200", name="GBU_12", t=1200.301):
    return {"time": t, kind: {"initiator": {"unit": deepcopy(shooter)}, "target": deepcopy(victim),
                             "weapon": {"id": wid, "type": wtype, "position": {}, "velocity": {"velocity": {}}},
                             "weapon_name": name}}


def scenario():
    clock = Clock()
    eye = Eye(clock=clock, source="offline-session4")
    eye.on_unit(deepcopy(PILOT))
    eye.on_unit(deepcopy(WINGMAN))
    return eye, clock


class MissionTallyTests(unittest.TestCase):
    def test_session4_all_19_survive_cap_and_time_window(self):
        eye, clock = scenario()
        previous = set()
        for uid, type_name, kind in MISSION_TARGETS:
            clock.t += 120
            msg = event(target(uid, type_name, kind))
            eye.on_event(msg)
            eye.on_event(msg)  # stream replay never adds a second target
            pic = eye.picture()
            ids = {k["target_id"] for k in pic["my_kills_this_mission"]}
            self.assertTrue(previous <= ids)
            previous = ids
        clock.t += 3601
        pic = eye.picture()  # prunes recent events and shots, not mission facts
        self.assertEqual(len(pic["my_kills_this_mission"]), 19)
        self.assertEqual(pic["my_kills_recent"], [])
        self.assertEqual(pic["recent_kills_and_losses_last_10_min"], [])
        tally = pic["kill_tallies"]["me"]
        self.assertEqual(tally["total"], 19)
        self.assertEqual(tally["by_type"]["Ural-375"], 4)
        self.assertEqual(tally["by_category"], {"vehicles": 4, "infantry": 6, "scenery": 6,
                                                "armor": 3, "AAA": 0, "SAM": 0})
        eye.on_event({"mission_end": {}})
        eye.on_gone(1)
        self.assertEqual(eye.picture()["kill_tallies"]["me"]["total"], 19)

    def test_recent_cursor_is_nonconsuming_and_duplicates_keep_seq(self):
        eye, clock = scenario()
        msg = event(target(4, "Ural-375"))
        eye.on_event(msg)
        first = eye.picture()["my_kills_recent"][0]
        cursor = eye.picture()["kill_seq"]
        self.assertEqual(first["mission_time"], 1200.301)
        self.assertEqual(first["t"], clock.t)
        eye.on_event(msg)
        self.assertEqual(eye.kills_since(cursor), [])
        clock.t += 601
        eye.on_event(event(target(23, "T-55")))
        pic = eye.picture()
        self.assertEqual(len(pic["my_kills_this_mission"]), 2)
        self.assertEqual([k["target"] for k in pic["my_kills_recent"]], ["T-55"])
        self.assertEqual(eye.kills_since(cursor), pic["my_kills_recent"])
        self.assertEqual(eye.kills_since(0)[0]["seq"], first["seq"])
        self.assertEqual(eye.kills_since(cursor), eye.kills_since(cursor))

    def test_per_shooter_and_category_tallies_survive_pruning(self):
        eye, clock = scenario()
        other = dict(deepcopy(WINGMAN), id=30, name="Other Flight", group={"name": "Other"})
        enemy = dict(deepcopy(WINGMAN), id=99, coalition="COALITION_RED")
        eye.on_event(event(target(4, "Ural-375")))
        for uid, typ in ((19, "ZSU-23-4 Shilka"), (47, "Strela-1 9P31")):
            msg = event(target(uid, typ), WINGMAN, wtype="AGR_20A", name="AGR_20A")
            eye.on_event(msg)
            eye.on_event(msg)
            eye.on_event({"dead": {"initiator": target(uid, typ)}})
        eye.on_event(event(target(50, "T-55"), other))
        eye.on_event(event(target(60, "T-55"), enemy))
        clock.t += 3601
        eye.on_gone(2)
        tally = eye.picture()["kill_tallies"]
        self.assertEqual(tally["me"]["total"], 1)
        self.assertEqual(tally["all_friendly"]["total"], 4)
        self.assertEqual(tally["by_shooter"]["2"]["total"], 2)
        self.assertEqual(tally["by_shooter"]["2"]["by_category"]["AAA"], 1)
        self.assertEqual(tally["by_shooter"]["2"]["by_category"]["SAM"], 1)
        self.assertTrue(tally["by_shooter"]["2"]["flight_member"])
        self.assertFalse(tally["by_shooter"]["30"]["flight_member"])
        self.assertNotIn("99", tally["by_shooter"])
        self.assertEqual(len(eye.kills_since(0, "2")), 2)
        self.assertEqual(len(eye.kills_since(0, "all_friendly")), 4)

    def test_zero_kill_wingman_present(self):
        eye, _ = scenario()
        self.assertEqual(eye.picture()["kill_tallies"]["by_shooter"]["2"]["total"], 0)

    def test_wingman_only_tally_survives_pilot_leaving(self):
        eye, clock = scenario()
        eye.on_event(event(target(19, "ZSU-23-4 Shilka"), WINGMAN))
        eye.on_gone(1)
        eye.on_gone(2)
        clock.t += 3601
        pic = eye.picture()
        self.assertEqual(pic["kill_tallies"]["all_friendly"]["total"], 1)
        self.assertTrue(pic["kill_tallies"]["by_shooter"]["2"]["flight_member"])
        self.assertEqual(len(eye.kills_since(0, "2")), 1)

    def test_reset_clears_tallies_and_correlation_without_rewinding_cursor(self):
        eye, _ = scenario()
        eye.on_event(event(target(4, "Ural-375")))
        cursor = eye.picture()["kill_seq"]
        eye.on_event({"mission_start": {}})
        self.assertEqual(eye.picture()["my_kills_this_mission"], [])
        self.assertEqual(eye.picture()["friendly_kills_this_mission"], [])
        self.assertEqual(eye.weapon_types, {})
        self.assertEqual(eye.known_objects, {})
        eye.on_event(event(target(4, "Ural-375")))
        self.assertEqual(len(eye.kills_since(cursor)), 1)
        eye.reset("test new mission")
        self.assertEqual(eye.picture()["kill_tallies"]["all_friendly"]["total"], 0)

    def test_inferred_to_confirmed_and_death_replay_keep_one_target(self):
        eye, clock = scenario()
        unit = fx.unit(4, "Unit #2", "Ural-375", "red", "SUPPLY", "GROUND", 42.09, 43.77, 680)
        eye.on_unit(unit)
        eye._record_my_kill(eye.units[4], "APKWS", "vanish", clock.t, inferred=True, likely=True)
        cursor = eye.picture()["kill_seq"]
        eye.on_event(event({"unit": unit}))
        eye.on_event(fx.dead(unit))
        eye.on_event({"unit_lost": {"initiator": {"unit": unit}}})
        pic = eye.picture()
        self.assertEqual(pic["kill_tallies"]["me"]["total"], 1)
        self.assertEqual(pic["kill_tallies"]["me"]["confirmed"], 1)
        self.assertFalse(pic["my_kills_this_mission"][0]["inferred"])
        self.assertEqual(eye.kills_since(cursor), [])

    def test_same_numeric_id_different_object_kinds_do_not_collapse(self):
        eye, _ = scenario()
        eye.on_event(event(target(4, "Ural-375")))
        eye.on_event(event(target(4, "MOST(ROAD)BIG-CRUSH", "scenery")))
        self.assertEqual(eye.picture()["kill_tallies"]["me"]["total"], 2)


class ReadableFactsTests(unittest.TestCase):
    def test_real_numeric_weapon_types_prefer_valid_name(self):
        eye, _ = scenario()
        for uid, wid, name, expected in ((4, 33573376, "GAU8_30_AP", "GAU-8"),
                                         (12, 16785664, "AGR_20A", "AGR-20A"),
                                         (18, 16787200, "GBU_12", "GBU-12")):
            eye.on_event(event(target(uid, "Ural-375"), wid=wid, wtype=str(wid), name=name))
            self.assertEqual(eye.picture()["my_kills_this_mission"][-1]["weapon"], expected)
            self.assertEqual(eye.picture()["recent_kills_and_losses_last_10_min"][0]["weapon"], expected)

    def test_numeric_weapon_resolves_from_shot_id_after_shot_pruned(self):
        eye, clock = scenario()
        eye.on_event(fx.shot(PILOT, 16785664, "AGR_20A"))
        clock.t += 3601
        eye.picture()
        self.assertFalse(eye.shots)
        eye.on_event(event(target(12, "Paratrooper AKS-74"), wid=16785664, wtype="16785664", name=""))
        self.assertEqual(eye.picture()["my_kills_this_mission"][0]["weapon"], "AGR-20A")

    def test_shells_are_gun_names_and_unknown_ids_are_not_spoken(self):
        for name in ("weapons.shells.GAU8-30-AP", "weapons.shells.GAU8_30_AP", "GAU_8"):
            self.assertEqual(weapon_name(name), "GAU-8")
        self.assertEqual(weapon_name("weapons.shells.M61_20_HE"), "M61")
        self.assertEqual(weapon_name("33573376"), "unknown weapon")
        self.assertEqual(weapon_name("APKWS"), "APKWS")
        self.assertEqual(weapon_name("weapons.missiles.AGM_65H"), "AGM-65H")

    def test_scenery_hit_descriptor_survives_unknown_kill(self):
        eye, _ = scenario()
        eye.on_event(event(target(171507773, "POWERTRANSPOLE_ROAD_01", "scenery"), kind="hit",
                           wtype="AGM_65D", name="AGM_65D"))
        eye.on_event(event(target(171507773, "unknown", "scenery"), wtype="AGM_65D", name="AGM_65D"))
        pic = eye.picture()
        self.assertEqual(pic["my_kills_this_mission"][0]["target"], "power pole")
        self.assertEqual(pic["recent_kills_and_losses_last_10_min"][0]["victim"]["type"], "power pole")
        self.assertIn("power pole", " ".join(pic["recent_events_last_10_min"]))
        self.assertNotIn("POWERTRANSPOLE", json.dumps(pic))

    def test_common_scenery_mapping_and_title_case_fallback(self):
        for kind in ("static", "scenery"):
            for typ, label in (("MOST(ROAD)BIG-CRUSH", "bridge"), ("BDG 2", "building"),
                               ("HANGAR_A", "hangar"), ("BUNKER", "bunker"), ("OIL_TANK", "storage tank"),
                               ("TV_TOWER", "tower"), ("CUSTOM_MAP_OBJECT", "Custom Map Object")):
                with self.subTest(kind=kind, typ=typ):
                    eye, _ = scenario()
                    eye.on_event(event(target(88, typ, kind)))
                    self.assertEqual(eye.picture()["my_kills_this_mission"][0]["target"], label)
                    self.assertEqual(eye.picture()["kill_tallies"]["me"]["by_category"]["scenery"], 1)

    def test_bdg2_is_infantry_not_a_building(self):
        eye, _ = scenario()
        eye.on_event(event(target(18, "Paratrooper AKS-74", name="BDG 2")))
        row = eye.picture()["my_kills_this_mission"][0]
        self.assertEqual(row["target"], "Paratrooper AKS-74")
        self.assertEqual(row["category"], "infantry")

    def test_unknown_unit_recovers_last_known_type_even_after_gone(self):
        eye, _ = scenario()
        eye.on_unit(fx.unit(18, "BDG 2", "Paratrooper AKS-74", "red", "BDG", "GROUND", 42.09, 43.77, 680))
        eye.on_gone(18)
        eye.on_event(event(target(18, "unknown", name="BDG 2")))
        self.assertEqual(eye.picture()["my_kills_this_mission"][0]["target"], "Paratrooper AKS-74")
        self.assertEqual(eye.picture()["my_kills_this_mission"][0]["category"], "infantry")

    def test_descriptor_variants_preserve_type(self):
        for kind in ("unit", "static", "scenery", "weapon"):
            for desc in ({"type": "T-55"}, {"type_name": "T-55"}, {"desc": {"typeName": "T-55"}},
                         {"type": "unknown", "descriptor": {"type_name": "T-55"}}):
                with self.subTest(kind=kind, desc=desc):
                    self.assertEqual(norm_object({kind: dict(desc, id=23)})["type"], "T-55")

    def test_player_prefix_cleaned_without_changing_simulator_unit_identity(self):
        eye, clock = scenario()
        pilot = dict(PILOT, player_name="- Player")
        eye.on_unit(pilot)
        eye.on_event(event(target(4, "Ural-375"), shooter=pilot))
        pic = eye.picture()
        self.assertEqual(pic["ownship"]["player_name"], "Player")
        self.assertEqual(pic["recent_kills_and_losses_last_10_min"][0]["killer"]["name"], "Player")
        self.assertEqual(pic["kill_tallies"]["me"]["total"], 1)
        own = eye.ownship()
        assert own is not None
        self.assertEqual(own["name"], "- Player")  # Eval lookup must retain the real unit name
        cp = CockpitState(clock=clock)
        cp.ingest({"v": 1, "pilot": "- Player", "unit": "- Player", "ac": "A-10C_2", "lat": 42, "lon": 43})
        self.assertEqual(cp.to_picture()["pilot"], "Player")
        own = cp.ownship()
        assert own is not None
        self.assertEqual(own["player"], "Player")

    def test_cockpit_payload_and_eye_shots_share_readable_names(self):
        eye, _ = scenario()
        packet = {"v": 1, "unit": "- Player", "pilot": "- Player", "ac": "A-10C_2",
                  "lat": 42.1, "lon": 43.77, "pay": {"st": [
                      {"i": 1, "n": "AGR_20A", "c": 2, "l": [4, 7, 1, 1]},
                      {"i": 2, "n": "AGR-20A", "c": 3, "l": [4, 7, 1, 1]}]}}
        eye.on_cockpit_packet(packet)
        eye.on_event(fx.shot(PILOT, 900, "AGR_20A"))
        eye.on_event(event(target(4, "Ural-375"), wid=900, wtype="900", name=""))
        eye.on_event(event(target(12, "Paratrooper AKS-74"), wid=900, wtype="900", name=""))
        pic = eye.picture()
        self.assertEqual(pic["cockpit"]["payload"]["by_weapon"], {"AGR-20A": 5})
        self.assertTrue(all(w["weapon"] == "AGR-20A" for w in pic["cockpit"]["payload"]["stations"]))
        self.assertEqual(pic["my_shots_last_15_min"]["shots"][0]["weapon"], "AGR-20A")
        self.assertEqual(pic["my_weapons_used_this_mission"]["by_weapon"], {"AGR-20A": 1})
        self.assertTrue(all(k["weapon"] == "AGR-20A" for k in pic["my_kills_this_mission"]))


if __name__ == "__main__":
    unittest.main()
