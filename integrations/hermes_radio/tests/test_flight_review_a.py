"""Review A of the 11-11 flight fixes (REVIEW-flight-A.md): threat calls are never pushed out by a fixed answer,
attack runs are not landings, friendly-fire questions never get the air-traffic list, a bandit at the field is
called as a bandit, a slip of a field name never gives the wrong field, and no reply clears or approves anything."""
from copy import deepcopy
import random
import re
import unittest

from integrations.hermes_radio.eye import Eye
from integrations.hermes_radio.simple_controller import (ADDRESS, FALLBACK, MAX_REPLY_CHARS, _field_key, call_class,
                                                         clearance_in, fuel_calls, named_field, strip_clearance)
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_flight_20261111 import MODEL, NEVADA, answer, live, nevada_eye
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say

BANDIT = {"key": "cb1", "call": "Bandit MiG-29, two seven zero, eight miles, angels ten, nine o'clock level."}
FRIENDLY = "Friendly A-10C, zero seven zero, five miles, angels two, three o'clock low."


def launch_picture(bandit=True, traffic=0):
    """18:30:45: two SA-2 launch alerts pending (the reviewer's reproduction), plus a close bandit and field traffic."""
    pic = live("18:30:45")
    pic["_rwr_keys"] = ["k0", "k1"]
    if bandit:
        pic["_close_bandits"] = [dict(BANDIT)]
    if traffic:
        for r in pic["airbases"]["nearest"]:
            r["traffic"] = {"count": traffic, "aircraft": [{"call": FRIENDLY, "side": "friendly"}] * traffic}
    return pic


class ThreatsFirstTests(unittest.TestCase):
    ASKS = ("Athena, fuel check", "Athena, vector to nearest air base", "Athena, request landing at Creech",
            "Athena, are there any friendlies in the air?", "Athena, vectors to Nelli's Air Force Base",
            "Athena, request landing at Fooville air force base", "Athena, divert", "Athena, how much fuel and weapons")

    def test_every_launch_alert_and_the_close_bandit_go_out_whole_with_any_fixed_answer(self):
        alerts = live("18:30:45")["rwr_alert_to_announce"]
        for bandit in (False, True):
            for traffic in (0, 4):
                for ask in self.ASKS:
                    with self.subTest(ask=ask, bandit=bandit, traffic=traffic):
                        spoken, reply, stub, eye, _ = answer(launch_picture(bandit, traffic), ask)
                        self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)
                        for alert in alerts:
                            self.assertIn(alert, spoken)
                        self.assertTrue({"k0", "k1"} <= eye.announced)
                        if bandit:
                            self.assertTrue(spoken.startswith(ADDRESS + BANDIT["call"]))
                            self.assertIn("cb1", eye.announced)
                        self.assertEqual(stub.calls, 0)

    def test_the_field_answer_is_deferred_not_the_missiles(self):
        spoken = answer(launch_picture(True, 4), "Athena, request landing at Creech")[0]
        self.assertEqual(spoken, ADDRESS + BANDIT["call"] + " Missile launch, SA-2, your three o'clock, thirteen miles. "
                         "Missile launch, SA-2, your three o'clock, fifteen miles. Ask again for the field.")
        spoken = answer(launch_picture(True, 0), "Athena, vectors to Nelli's Air Force Base")[0]
        self.assertTrue(spoken.endswith("fifteen miles. Ask again for the vector.") or "Nellis, bearing" in spoken)

    def test_a_list_answer_keeps_every_alert_too(self):
        pic = launch_picture(True, 0)
        pic["picture_top3"] = ["MiG-29 two-ship, two seven zero, twelve, angels ten, nine o'clock level."] * 3
        spoken = answer(pic, "Athena, picture")[0]
        self.assertIn("fifteen miles.", spoken)
        self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)


ATTACK_APPROACH = ("on approach to the target", "commencing approach to the target area",
                   "beginning approach on the SAM site", "on final to the target", "running in",
                   "Athena, on final, rifle", "starting my approach to the armor", "on approach to the bridge",
                   "commencing approach", "short final on the convoy")


class AttackRunTests(unittest.TestCase):
    def test_attack_run_approach_calls_are_acknowledged_not_landings(self):
        for text in ATTACK_APPROACH:
            with self.subTest(text=text):
                self.assertNotIn(call_class(text, NEVADA), ("landing", "airfield", "atc"))
                spoken = answer(deepcopy(NEVADA), text)[0]
                self.assertNotRegex(spoken, r"Creech|traffic|Nellis")

    def test_bare_starting_approach_needs_a_landing_context(self):
        far = live("18:35:00")  # Creech 27 miles: no landing context in the picture
        self.assertGreater(far["airbases"]["nearest_friendly"][0]["range_nm"], 15)
        self.assertEqual(call_class("Roger, starting approach.", far), "ack")
        self.assertEqual(call_class("Roger, starting approach.", far, ["Athena, vector to nearest air base."]),
                         "landing")
        self.assertEqual(call_class("Roger, starting approach.", NEVADA), "landing")  # Creech 7.5 nm
        # in sequence: an RTB vector, then the bare call, gets the field and its traffic
        c, _ = controller(far, Stub(answer=MODEL))
        say(c, "Athena, vector to nearest air base.", tid="a1")
        self.assertRegex(say(c, "Roger, starting approach.", tid="a2")[0][0]["speech"], r"Creech, bearing .*traffic")
        from integrations.hermes_radio.tests.test_flight_20261111 import NO_OWN
        from integrations.hermes_radio.eye import NO_POSITION
        for text in ("Athena, short final", "Roger, starting approach."):
            self.assertEqual(answer(deepcopy(NO_OWN), text)[0], ADDRESS + NO_POSITION)  # never a model guess


FRIENDLY_FIRE_ASKS = ("any friendlies hit", "did I hit friendlies", "any friendlies near the target",
                      "any friendlies in the area", "friendlies in the target area", "blue on blue?",
                      "Athena, any friendlies hit?", "are there any friendlies near the target area")


class FriendlyFireTests(unittest.TestCase):
    def test_friendly_fire_questions_never_get_the_air_traffic_answer(self):
        for text in FRIENDLY_FIRE_ASKS:
            with self.subTest(text=text):
                self.assertNotEqual(call_class(text, NEVADA), "traffic")
                spoken, reply, stub, _, _ = answer(deepcopy(NEVADA), text)
                self.assertNotRegex(spoken, r"No (blue |friendly )?aircraft showing")
                self.assertEqual(stub.calls, 1)  # as before the 11-11 change: the model answers


class FieldTrafficTests(unittest.TestCase):
    def test_a_bandit_at_the_field_is_called_first_and_as_a_bandit(self):
        eye = nevada_eye()
        f0 = eye.picture()["airbases"]["nearest_friendly"][0]
        ab = eye.airbases[f0["name"]]
        eye.on_units_message({"time": 0.0, "unit": fx.unit(77, "Bandit", "MiG-21Bis", "red", "Red-1", "AIRPLANE",
                                                           ab["lat"] + 0.01, ab["lon"], 1300.0, 180.0, 120.0)})
        eye.on_units_message({"time": 0.0, "unit": fx.unit(78, "Blue", "A-10C", "blue", "Blue-1", "AIRPLANE",
                                                           ab["lat"], ab["lon"] + 0.001, 900.0, 0.0, 0.0)})
        c, _ = controller(None, Stub(answer=MODEL), eye=eye)
        spoken = say(c, "Athena, request landing at %s" % f0["name"])[0][0]["speech"]
        self.assertRegex(spoken, r"Bandit MiG-21Bis, [^.]*, one mile from %s\. Other traffic, one aircraft\." % f0["name"])
        self.assertLess(spoken.index("Bandit MiG-21Bis"), spoken.index("Other traffic"))
        self.assertNotIn("Traffic, two aircraft", spoken)

    def test_synthetic_rows(self):
        pic = live("19:01:46")
        pic.pop("rwr_alert_to_announce", None)
        creech = next(r for r in pic["airbases"]["nearest"] if r["name"] == "Creech")
        mig = {"call": "MiG-21Bis, zero six eight, six miles, angels one, three o'clock low.", "side": "hostile",
               "field_range_nm": 0.6}
        creech["traffic"] = {"count": 2, "aircraft": [{"call": FRIENDLY, "side": "friendly"}, mig]}
        spoken = answer(pic, "Athena, requesting permission to land")[0]
        self.assertEqual(spoken, "Prometheus, Athena, Creech, bearing zero six nine, five miles, at your three o'clock. "
                                 "Bandit MiG-21Bis, zero six eight, six miles, angels one, three o'clock low, one mile "
                                 "from Creech. Other traffic, one aircraft. Ask again for the rest.")
        creech["traffic"] = {"count": 1, "aircraft": [mig]}
        self.assertTrue(answer(pic, "request landing")[0].endswith("three o'clock. Bandit MiG-21Bis, zero six eight, "
                                                                   "six miles, angels one, three o'clock low, one mile "
                                                                   "from Creech."))


NEV = ('Beatty', 'Boulder City', 'Creech', 'Echo Bay', 'Groom Lake', 'Henderson Executive', 'Jean', 'Laughlin',
       'Lincoln County', 'McCarran International', 'Mesquite', 'Mina', 'Nellis', 'North Las Vegas', 'Pahute Mesa',
       'Tonopah', 'Tonopah Test Range')
SYRIA = ('Abu al-Duhur', 'Adana Sakirpasa', 'Akrotiri', 'Al Qusayr', 'Al-Dumayr', 'Aleppo', 'An Nasiriyah', 'At Tanf',
         'Bassel Al-Assad', 'Beirut-Rafic Hariri', 'Ben Gurion', 'Damascus', 'Deir ez-Zor', 'Ercan', 'Eyn Shemer',
         'Gaziantep', 'Gazipasa', 'Gecitkale', 'H', 'H3', 'H3 Northwest', 'H3 Southwest', 'H4', 'Haifa', 'Hama',
         'Hatay', 'Hatzor', 'Herzliya', 'Incirlik', 'Jirah', 'Khalkhalah', 'Kharab Ishk', 'King Abdullah II',
         'King Hussein Air College', 'Kingsfield', 'Kiryat Shmona', 'Kuweires', 'Lakatamia', 'Larnaca',
         'Marj Ruhayyil', 'Marj as Sultan North', 'Marj as Sultan South', 'Marka', 'Megiddo', 'Mezzeh', 'Minakh',
         'Muwaffaq Salti', 'Naqoura', 'Nicosia', 'Palmachim', 'Palmyra', 'Paphos', 'Pinarbashi', 'Prince Hassan',
         'Qabr as Sitt', 'Ramat David', 'Rayak', 'Rene Mouawad', 'Rosh Pina', 'Ruwayshid', 'Sanliurfa', 'Sayqal',
         'Shayrat', 'Tabqa', 'Taftanaz', 'Tal Siman', 'Tel Nof', "Tha'lah", 'Tiyas', 'Wujah Al Hajar',
         "HI01", "HI03", "HI05", "HI08", "HI12", "HI13", "HI14", "HI15", "HI17", "HI21", "HI24", "HI26", "HMed18",
         "HMed20", "FARP", "Invisible FARP", "0302 | CVN-72 Abraham Lincoln")


def fields_picture(names):
    pic = deepcopy(NEVADA)
    pic["_airfields"] = [{"name": n, "owner": "blue", "category": "airdrome", "range_nm": 10 + i,
                          "call": "%s, bearing zero one zero, ten miles, at your twelve o'clock." % n}
                         for i, n in enumerate(names)]
    return pic


def slips(names, seed, per_name):
    """The reviewer's one-letter speech slips: delete, substitute, insert or swap one letter of each name."""
    rnd, letters = random.Random(seed), "abcdefghijklmnopqrstuvwxyz"
    for n in names:
        for _ in range(per_name):
            s = list(n)
            i, op = rnd.randrange(len(s)), rnd.choice("dsit")
            if op == "d" and len(s) > 1:
                del s[i]
            elif op == "s":
                s[i] = rnd.choice(letters)
            elif op == "i":
                s.insert(i, rnd.choice(letters))
            elif op == "t" and i < len(s) - 1:
                s[i], s[i + 1] = s[i + 1], s[i]
            yield n, "".join(s)


def wrong_field_rate(names, seed, per_name):
    pic = fields_picture(names)
    tried = wrong = 0
    for meant, slip in slips(names, seed, per_name):
        row, _ = named_field("vectors to " + slip, pic)
        tried += 1
        if row is not None and row["name"] != meant and _field_key(row["name"]) != _field_key(slip):
            wrong += 1
    return wrong, tried


class NamedFieldTests(unittest.TestCase):
    def test_exact_names_are_themselves(self):
        for names in (NEV, SYRIA):
            pic = fields_picture(names)
            for n in names:
                for frame in ("vectors to %s", "request landing at %s", "how far to %s"):
                    with self.subTest(said=frame % n):
                        self.assertEqual((named_field(frame % n, pic)[0] or {}).get("name"), n)

    def test_one_letter_slips_never_give_the_wrong_field(self):
        for names, seed, per in ((NEV, 7, 20), (SYRIA, 7, 20), (SYRIA, 11, 40)):
            wrong, tried = wrong_field_rate(names, seed, per)
            self.assertEqual(wrong, 0, "%d wrong of %d" % (wrong, tried))

    def test_reviewer_wrong_field_cases(self):
        nev, syr = fields_picture(NEV), fields_picture(SYRIA)
        for text, pic, want in (("vector to Tonopah test", nev, None), ("vector to Tonopah Test Range", nev,
                                                                         "Tonopah Test Range"),
                                ("vector to Tonopah", nev, "Tonopah"), ("vectors to H3 Southwet", syr, "H3 Southwest"),
                                ("vectors to H four", syr, "H4"), ("vectors to H three", syr, "H3"),
                                ("vectors to H", syr, "H"), ("vectors to H3 south west", syr, "H3 Southwest"),
                                ("vector to Al-qumayr", syr, None), ("vectors to Haima", syr, None),
                                ("vectors to Hama", syr, "Hama"), ("vectors to Haifa", syr, "Haifa"),
                                ("vector to Al Dumayr", syr, "Al-Dumayr"), ("vector to Al Qusayr", syr, "Al Qusayr"),
                                ("vectors to Nelli's Air Force Base", nev, "Nellis"), ("steer to Creach", nev, "Creech"),
                                ("vector to HI one", syr, None), ("vector to HI zero one", syr, "HI01"),
                                ("vectors to H Northwest", syr, None), ("vectors to pInvisible FARP", syr, None)):
            with self.subTest(text=text):
                self.assertEqual((named_field(text, pic)[0] or {}).get("name"), want)
        pic = fields_picture(NEV[:16])  # no Tonopah Test Range on this map: 'H four' style rule for digits
        self.assertEqual(named_field("vectors to Tonopah", pic)[0]["name"], "Tonopah")
        syr_no_h4 = fields_picture([n for n in SYRIA if n != "H4"])
        self.assertEqual(named_field("vectors to H four", syr_no_h4), (None, "ambiguous"))

    def test_an_unsure_name_gets_i_dont_have_that_field_and_the_nearest_friendly(self):
        pic = fields_picture(NEV)
        pic["airbases"]["nearest_friendly"] = [{"name": "Creech", "owner": "blue", "range_nm": 7.5,
                                                "call": "Creech, bearing three three seven, seven miles, at your two "
                                                        "o'clock."}]
        self.assertEqual(call_class("vector to Tonopah test", pic), "airfield")
        spoken = answer(pic, "vector to Tonopah test")[0]
        self.assertEqual(spoken, "Prometheus, Athena, I don't have that field. Nearest friendly, Creech, bearing three "
                                 "three seven, seven miles, at your two o'clock.")


BLOCKED = ("Prometheus, Athena, you're clear to land.", "Prometheus, Athena, clear for the approach.",
           "Prometheus, Athena, approved.", "Prometheus, Athena, you may land.",
           "Prometheus, Athena, runway two one, no traffic.", "Prometheus, Athena, landing approved, no traffic.",
           "Prometheus, Athena, cleared to land runway two seven left, wind calm.",
           "Prometheus, Athena, you are cleared for takeoff.", "Prometheus, Athena, cleared for departure.",
           "Prometheus, Athena, cleared ILS approach runway eight.", "Prometheus, Athena, cleared visual approach.",
           "Prometheus, Athena, approach approved, altimeter two niner niner two.",
           "Prometheus, Athena, permission granted.", "Prometheus, Athena, takeoff approved.",
           "Prometheus, Athena, Creech, no traffic, land at will.", "Prometheus, Athena, Cleared to land.",
           "Prometheus, Athena, CLEARED FOR THE OPTION.", "Prometheus, Athena, cleared straight-in approach.",
           "Prometheus, Athena, cleared to taxi.", "Prometheus, Athena, good to land.",
           "Prometheus, Athena, clear of SAMs, cleared to land.", "Prometheus, Athena, permission approved, runway three four.",
           "Prometheus, Athena, wind two seven zero at ten.", "Prometheus, Athena, QNH one zero one three.",
           "Prometheus, Athena, squawk seven seven zero zero.", "Prometheus, Athena, contact tower.",
           "Prometheus, Athena, hold short of the runway.", "Prometheus, Athena, line up and wait.",
           "Prometheus, Athena, runway 27 in use.", "Prometheus, Athena, you can take off now.",
           "Prometheus, Athena, cleared for touch and go.", "Prometheus, Athena, report base.",
           "Prometheus, Athena, number one for landing.", "Prometheus, Athena, winds calm, no traffic.",
           "Prometheus, Athena, cleared to land.", "Prometheus, Athena, the altimeter is two niner niner two.",
           "Prometheus, Athena, enter left downwind.", "Prometheus, Athena, ok to land, no traffic.",
           "Prometheus, Athena, cleared hot.")  # re-review AB2: Athena never clears weapons either
PASSED = ("Prometheus, Athena, you are clear of the SA-2.", "Prometheus, Athena, clear to the north.",
          "Prometheus, Athena, no traffic.", "Prometheus, Athena, copy, runway attack complete.",
          "Prometheus, Athena, loud and clear.",
          "Prometheus, Athena, Creech, bearing zero six nine, five miles, at your three o'clock. No traffic.",
          "Prometheus, Athena, copy, landing gear down.", "Prometheus, Athena, copy, wheels up.",
          "Prometheus, Athena, Fuel 3,640 pounds.", "Prometheus, Athena, enemy armor on the runway at Mina.",
          "Prometheus, Athena, SAM SA-2 tracking you, your twelve o'clock, eighteen miles.",
          "Prometheus, Athena, the bomb landed short.", "Prometheus, Athena, copy, approaching the target.",
          "Prometheus, Athena, copy, on approach.", "Prometheus, Athena, Bandit MiG-29, two seven zero, eight miles, "
          "angels ten, nine o'clock level.", "Prometheus, Athena, you're clear of threats to the west.",
          "Prometheus, Athena, Traffic, two aircraft. Friendly A-10C, zero seven zero, five miles.",
          "Prometheus, Athena, copy, noted for development: call the wind.", "Prometheus, Athena, copy, RTB.",
          "Prometheus, Athena, Nellis, bearing one three four, twenty nine miles, at your seven o'clock.",
          "Prometheus, Athena, splash one T-55.", "Prometheus, Athena, the target area is clear of friendlies I can see.",
          "Prometheus, Athena, copy, weapons hot.", "Prometheus, Athena, no friendly airfield showing.",
          "Prometheus, Athena, you have two Mavericks left.", "Prometheus, Athena, copy, departing the target area.",
          "Prometheus, Athena, copy, landing at Creech.", "Prometheus, Athena, I don't have that field.",
          "Prometheus, Athena, copy, Sidewinder away.", "Prometheus, Athena, copy, abort.",
          "Prometheus, Athena, enemy trucks clear of the bridge.")


class ClearanceGuardTests(unittest.TestCase):
    def test_the_whole_clearance_family_is_blocked_and_plain_replies_pass(self):
        self.assertGreaterEqual(len(BLOCKED), 30)
        self.assertGreaterEqual(len(PASSED), 30)
        for text in BLOCKED:
            with self.subTest(blocked=text):
                self.assertTrue(clearance_in(text))
                self.assertFalse(clearance_in(strip_clearance(text)))
        for text in PASSED:
            with self.subTest(passed=text):
                self.assertFalse(clearance_in(text))
                self.assertEqual(strip_clearance(text), text)

    def test_an_atc_ask_gets_the_fixed_field_answer_and_others_lose_the_sentence(self):
        pic = live("19:01:46")
        pic.pop("rwr_alert_to_announce", None)
        fixed = ("Prometheus, Athena, Creech, bearing zero six nine, five miles, at your three o'clock. No traffic. "
                 "I have no runway, wind or altimeter data.")  # re-review AB2: an ATC ask never calls the model
        for said in BLOCKED + ("Prometheus, Athena, you're good to go, no traffic.",):
            with self.subTest(said=said):
                stub = Stub(answer=said)
                spoken, reply, _, _, _ = answer(deepcopy(pic), "Athena, which runway is in use?", stub)
                self.assertEqual(spoken, fixed)
                self.assertEqual(reply["route"], "fixed")
                self.assertEqual(stub.calls, 0)
        for said, want in (("Prometheus, Athena, SAM SA-2 tracking you, your twelve o'clock. Cleared to land.",
                            FALLBACK),  # no SA-2 in this picture: clearance-free is not necessarily grounded
                           ("Prometheus, Athena, permission granted.", FALLBACK),
                           ("Prometheus, Athena, radio check, loud and clear, good to go.",
                            "Prometheus, Athena, radio check, loud and clear, good to go.")):
            with self.subTest(said=said):
                self.assertEqual(answer(deepcopy(pic), "Athena, radio check", Stub(answer=said))[0], want)


class MinorTests(unittest.TestCase):
    def test_a_real_zero_is_zero(self):
        self.assertEqual(fuel_calls({"total_lb": 0, "internal_pct": 0}), ["Fuel zero."])
        self.assertEqual(fuel_calls({"internal_pct": 0}), ["Fuel zero."])
        self.assertEqual(fuel_calls({}), ["Fuel unknown."])
        self.assertEqual(fuel_calls({"known": False, "total_lb": 0}), ["Fuel unknown."])

    def test_fuel_and_weapons_and_bingo_rtb(self):
        pic = live("18:32:05")
        pic["ownship"]["loadout"] = {"available": True, "weapons": [{"weapon": "AGM-65D", "count": 2}],
                                     "gun_rounds": 0}
        self.assertEqual(answer(deepcopy(pic), "how much fuel and weapons do I have")[0],
                         "Prometheus, Athena, Fuel 3,640 pounds. You have Mavericks two.")
        spoken = answer(deepcopy(pic), "Bingo fuel, RTB")[0]
        self.assertRegex(spoken, r"^Prometheus, Athena, Fuel 3,640 pounds\. \w+, bearing ")

    def test_a_question_about_the_field_is_not_a_vector(self):
        for text in ("home plate is under attack?", "is the base under attack", "is home plate safe?"):
            self.assertNotEqual(call_class(text, NEVADA), "airfield", text)

    def test_an_idle_stream_shows_its_age_and_goes_down_when_the_call_ends(self):
        now = [1000.0]
        eye = Eye("Prometheus", clock=lambda: now[0])
        open_ = [True]
        eye.set_connected(True, stream="units-helo", idle=True, alive=lambda: open_[0])
        now[0] = 1600.0
        self.assertEqual(eye.streams_json(now[0])["units-helo"], "up, no units for 600s")
        open_[0] = False
        self.assertEqual(eye.streams_json(now[0])["units-helo"], "down 600s (subscription ended)")
        eye.set_connected(True, stream="units-helo")
        self.assertEqual(eye.streams_json(now[0])["units-helo"], "up")


if __name__ == "__main__":
    unittest.main()
