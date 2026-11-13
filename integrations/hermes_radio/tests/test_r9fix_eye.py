"""Round 9 lane EYE (live session 7, 2026-11-09): plain spoken names, missile identity, merged ground calls,
fuel pounds for the F-100D."""
import unittest

from integrations.hermes_radio import cockpit as cp
from integrations.hermes_radio.eye import Eye, spoken_type
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye_round4 import F4E, SA11_LN, SA11_SR, f4e_eye, f4e_packet
from integrations.hermes_radio.tests.test_eye_round5 import contact, make_eye


class SpokenNameTests(unittest.TestCase):
    # every type string in the 2026-11-09 transcript tool records and debriefs that is not a plain name already
    TONIGHT = {
        "SA2V755": "SA-2", "S_75M_Volhov": "SA-2", "S 75M Volhov": "SA-2", "SNR_75V": "SA-2 radar",
        "SNR 75V": "SA-2 radar", "p-19 s-125 sr": "P-19 radar", "ZU-23 Emplacement Closed": "ZU-23",
        "Container 40Ft": "container", "Static Container 40ft-15-1_copy_35": "container",
        "Static Container 40ft-17-1_copy_37": "container", "Mk 4 FFAR Mk 5 HEAT": "Mk 5 rockets",
        "FFAR Mk5 HEAT": "Mk 5 rockets", "HB-ALE-40-30-60": "chaff/flare pod",
        "HB-ORD-Pave-Spike-Fast": "Pave Spike pod", "Ural-4320T": "Ural truck", "LAZ Bus": "bus",
        "ATMZ-5": "fuel truck", "F-4E-45MC": "F-4E", "ZIL-131 KUNG": "ZIL-131 truck",
    }

    def test_table_covers_tonights_names(self):
        for raw, spoken in self.TONIGHT.items():
            self.assertEqual(spoken_type(raw), spoken, raw)
            self.assertEqual(cp.spoken_name(raw), spoken, raw)

    def test_generic_fallback_strips_dcs_suffixes(self):
        self.assertEqual(cp.spoken_name("Static Widget-9-1_copy_7"), "Widget")
        self.assertEqual(cp.spoken_name("BMP_1"), "BMP")
        self.assertEqual(cp.spoken_name("weapons.missiles.AIM_9L"), "AIM 9L")
        self.assertEqual(cp.spoken_name(None), "contact")
        self.assertEqual(spoken_type("Su-27"), "Su-27")

    def test_weapon_name_keeps_old_designations_and_uses_table(self):
        self.assertEqual(cp.weapon_name("AGM-65A"), "AGM-65A")
        self.assertEqual(cp.weapon_name("weapons.missiles.AGM_65H"), "AGM-65H")
        self.assertEqual(cp.weapon_name("HB-ALE-40-30-60"), "chaff/flare pod")
        self.assertEqual(cp.weapon_name("SA2V755"), "SA-2")

    def test_picture_keeps_raw_and_adds_spoken(self):
        radar = fx.unit(901, "P19", "p-19 s-125 sr", "red", "Ground-9", "GROUND", *fx.offset(5, 12), 300.0)
        eye, _ = f4e_eye(extra=(radar,))
        pic = eye.picture()
        grp = [g for g in pic["hostile_groups"] if "p-19 s-125 sr" in g["types"]][0]
        self.assertEqual(grp["spoken_types"], {"P-19 radar": 1})
        self.assertIn("p-19 s-125 sr", grp["types"])  # raw kept for the debrief

    def test_loadout_rows_carry_raw_and_spoken(self):
        eye, _ = f4e_eye(extra=())
        eye.on_stores(F4E, {"ammo": [
            {"type": "HB-ORD-Pave-Spike-Fast", "name": "HB-ORD-Pave-Spike-Fast", "count": 1, "category": 1},
            {"type": "Mk 4 FFAR Mk 5 HEAT", "name": "Mk 4 FFAR Mk 5 HEAT", "count": 14, "category": 2}]})
        rows = {r["raw_type"]: r for r in eye.picture()["cockpit"].get("loadout", {}).get("weapons", [])} \
            or {r["raw_type"]: r for r in eye._loadout_json(eye._own_locked(), 1000.0)["weapons"]}
        self.assertEqual(rows["HB-ORD-Pave-Spike-Fast"]["spoken_name"], "Pave Spike pod")
        self.assertEqual(rows["Mk 4 FFAR Mk 5 HEAT"]["weapon"], "Mk 5 rockets")
        self.assertEqual(rows["Mk 4 FFAR Mk 5 HEAT"]["raw_weapon"], "Mk 4 FFAR Mk 5 HEAT")


class MissileIdentityTests(unittest.TestCase):
    def shot(self, wid, heading, rng=10.2, brg=101):
        lat, lon = fx.offset(brg, rng)
        return {"time": 50.0, "shot": {"initiator": {"unit": SA11_LN}, "weapon": {
            "id": wid, "type": "SA2V755", "position": {"lat": lat, "lon": lon, "alt": 400.0},
            "velocity": {"heading": heading, "speed": 700.0}}}}

    def test_shot_event_has_id_and_spoken_name(self):
        eye, _ = f4e_eye(extra=(SA11_LN,))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(self.shot(777, 281.0))
        pic = eye.picture()
        flight = pic["threats"]["missiles_in_flight"]
        self.assertEqual(len(flight), 1)
        m = flight[0]
        self.assertEqual((m["id"], m["spoken_name"], m["raw_type"]), ("777", "SA-11", "SA2V755"))
        self.assertEqual(m["call"], "Missile launch, SA-11, your twelve o'clock, ten miles.")
        for k in ("bearing", "range_nm", "clock", "first_seen"):
            self.assertIn(k, m)
        self.assertEqual(pic["threats"]["missiles_inbound"][0]["id"], "777")

    def test_missile_not_aimed_at_pilot_is_listed_but_not_alerted(self):
        eye, _ = f4e_eye(extra=(SA11_LN,))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(self.shot(778, 101.0))  # flying away
        pic = eye.picture()
        self.assertEqual(pic["threats"]["missiles_inbound"], [])
        self.assertEqual([m["id"] for m in pic["threats"]["missiles_in_flight"]], ["778"])

    def test_same_missile_once_shot_event_plus_eval(self):
        # Phase 1 (R3 T1): live, the shot-event id is invented by the overlay and NEVER equals the scan's real id;
        # DCS also names the SA-2/SA-11 radar as the scan's launcher. Use different ids and launchers, as live.
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_weapons([])  # the 1 Hz scan is running
        eye.on_event(self.shot(4294967294, 281.0))
        first = eye.picture()["threats"]["missiles_in_flight"]
        self.assertEqual([m["id"] for m in first], ["4294967294"])  # bridge row before the scan sees it
        first_seen = first[0]["first_seen"]
        clock.t += 1
        lat, lon = fx.offset(95, 8)
        eye.on_weapons([{"id": 16786433, "type": "SA2V755", "lat": lat, "lon": lon, "alt": 2000, "vn": 0, "ve": -600,
                         "coal": 1, "target": "Aerial-3-2", "launcher": "SAM SR",
                         "launcher_type": "SA-11 Buk SR 9S18M1"}])
        pic = eye.picture()
        flight = pic["threats"]["missiles_in_flight"]
        self.assertEqual([m["id"] for m in flight], ["16786433"])  # one missile, not two
        self.assertEqual(flight[0]["first_seen"], first_seen)
        self.assertEqual(flight[0]["range_nm"], 8.0)
        self.assertEqual([m["id"] for m in pic["threats"]["missiles_inbound"]], ["16786433"])
        self.assertEqual([a["kind"] for a in eye.threat_alerts], ["missile launch"])  # ONE launch alert
        clock.t += 1
        eye.on_weapons([])  # the missile left the scan: it is gone
        eye.tick()
        pic = eye.picture()
        self.assertEqual(pic["threats"]["missiles_in_flight"], [])
        self.assertFalse([c for c in pic.get("rwr_alert_to_announce", []) if c.startswith("Missile launch")])

    def test_new_launch_gets_new_id(self):
        eye, _ = f4e_eye(extra=(SA11_LN,))
        eye.on_cockpit_packet(f4e_packet())
        eye.on_event(self.shot(780, 281.0))
        eye.on_event(self.shot(781, 281.0))
        ids = sorted(m["id"] for m in eye.picture()["threats"]["missiles_in_flight"])
        self.assertEqual(ids, ["780", "781"])

    def test_eval_missile_within_20nm_not_aimed_at_me_is_listed(self):
        eye, _ = f4e_eye(extra=())
        lat, lon = fx.offset(180, 15)
        eye.on_weapons([{"id": 5100, "type": "SA2V755", "lat": lat, "lon": lon, "alt": 2000, "vn": 600, "ve": 0,
                         "coal": 1, "target": "Someone else"},
                        {"id": 5101, "type": "SA2V755", "lat": fx.offset(180, 30)[0], "lon": lon, "alt": 2000,
                         "vn": 0, "ve": 0, "coal": 1, "target": "Someone else"}])  # beyond 20 nm: not listed
        flight = eye.picture()["threats"]["missiles_in_flight"]
        self.assertEqual([m["id"] for m in flight], ["5100"])
        self.assertFalse(flight[0]["targeting_me"])
        self.assertEqual(flight[0]["spoken_name"], "missile")  # unknown shooter, opaque weapon ID


class GroundMergeTests(unittest.TestCase):
    def test_same_clock_and_bearing_merge_into_one_call(self):
        eye, _ = make_eye(
            *(contact(i, "T-55", "red", "GROUND", distance=10, bearing=5) for i in (1, 2, 3)),
            *(contact(i, "Ural-4320T", "red", "GROUND", distance=11.8, bearing=5) for i in (4, 5)),
            contact(9, "T-55", "red", "GROUND", distance=14, bearing=200))
        # three T-55 in one cluster, two trucks 1.8 nm behind in another, one far away on another bearing
        rows = eye.picture()["ground_targets_within_20nm"]
        self.assertEqual(len(rows), 2, [r["call"] for r in rows])
        merged = rows[0]
        self.assertEqual(merged["contacts"], 5)
        self.assertEqual(merged["merged_groups"], 2)
        self.assertTrue(merged["call"].startswith("Enemy vehicles, five contacts, bearing"), merged["call"])
        self.assertEqual(merged["call"].count("o'clock"), 1)
        self.assertIn("ground target", merged["call"])
        self.assertIn("ten miles", merged["call"])  # closest-first anchor
        self.assertNotIn("merged_groups", rows[1])

    def test_different_range_or_bearing_stays_separate(self):
        eye, _ = make_eye(contact(1, "T-55", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "T-55", "red", "GROUND", distance=14, bearing=5),
                          contact(3, "T-55", "red", "GROUND", distance=10, bearing=45))
        rows = eye.picture()["ground_targets_within_20nm"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["call"].split(", bearing")[0] for r in rows], ["Enemy armor"] * 3)

    def test_same_category_keeps_its_label(self):
        eye, _ = make_eye(contact(1, "T-55", "red", "GROUND", distance=10, bearing=5),
                          contact(2, "T-55", "red", "GROUND", distance=11.8, bearing=5))
        rows = eye.picture()["ground_targets_within_20nm"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["call"].startswith("Enemy armor, two contacts"), rows[0]["call"])


class FuelPoundsTests(unittest.TestCase):
    def test_f100d_gets_estimated_pounds(self):
        out = cp.fuel_lb_from_fraction(0.4, "F-100D")
        self.assertEqual(out["internal_pct"], 40)
        self.assertEqual(out["total_lb"], 3090)
        self.assertTrue(out["estimated"])
        self.assertEqual(out["spoken"], "about 3,090 pounds")
        self.assertIn("approximate", out["source"])

    def test_airframes_covered(self):
        for ac in ("F-100D", "F-4E-45MC", "A-10C_2", "A-10C", "F-14B"):
            out = cp.fuel_lb_from_fraction(0.5, ac)
            self.assertIn("total_lb", out, ac)
            self.assertTrue(out["estimated"], ac)

    def test_unknown_airframe_still_percent_only(self):
        out = cp.fuel_lb_from_fraction(0.5, "Nonesuch")
        self.assertNotIn("total_lb", out)
        self.assertNotIn("estimated", out)

    def test_picture_fuel_for_f100d(self):
        f100 = fx.unit(300, "Aerial-1-1", "F-100D", "blue", "Aerial-1", "AIRPLANE", fx.LAT0, fx.LON0, 3000.0, 90.0,
                       200.0, player="Prometheus")
        eye = Eye("Prometheus", clock=lambda: 1000.0, source="fixture")
        eye.set_connected(True)
        eye.on_units_message(fx.units_message(f100))
        eye.set_fuel(0.4)
        out = eye._fuel_json(eye._own_locked(), {}, 1000.0)
        self.assertEqual((out["total_lb"], out["internal_pct"], out["estimated"]), (3090, 40, True))
        self.assertEqual(out["spoken"], "about 3,090 pounds")


if __name__ == "__main__":
    unittest.main()
