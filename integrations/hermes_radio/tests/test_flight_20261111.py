"""Flight 2026-11-11 18:27-19:05 (F-4E, Nevada; transcript-20261111.jsonl lines 1627+). The model answered from the
right picture wrongly: 'no connected picture' to 'any friendlies in the air?', 'fuel unknown' with 3,640 lb, the
nearest airfield without its name, 'no friendly airfield showing' with Creech 7 nm away and Nellis listed, and
'Cleared to land' (Athena never clears anything). Those are fixed answers now. Also: every missile was said twice
in the launch reply, units-helo/ship read 'down' all flight while subscribed, and 'T-55' / 'T 55' were two types."""
from copy import deepcopy
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from integrations.hermes_radio.eye import NO_POSITION, Eye, GrpcFeeder, object_type, scenery_name
from integrations.hermes_radio.simple_controller import (FALLBACK, MAX_REPLY_CHARS, call_class, fuel_calls,
                                                         named_field)
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, replies, say

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = {e["time"]: e for e in json.loads((FIXTURES / "flight_20261111.json").read_text())["exchanges"]}
NO_OWN = json.loads((FIXTURES / "no_ownship_live_20261111.json").read_text())["picture"]
NEVADA = LIVE["19:00:12"]["picture"]  # Creech 7.5 nm, North Las Vegas, Nellis, McCarran, Henderson listed
MODEL = "Prometheus, Athena, MODEL ANSWER."
NO_TOWER = "no tower: I can't give clearances. "

# Each wrong live answer (and the double launch call and the split tally) -> the fixed reply now given.
EXPECTED = {
    "18:30:05": "Prometheus, Athena, SAM SA-2 tracking you, your eleven o'clock, nineteen miles. Friendly A-10C, two "
                "five one, five miles, angels eight, nine o'clock level. Friendly A-10C, two seven eight, thirteen, "
                "angels eight, ten o'clock level. Plus one more.",
    "18:30:45": "Prometheus, Athena, SA-2 in the air, your three o'clock, thirteen miles. Also SA-2, your three "
                "o'clock, fifteen miles.",
    "18:32:05": "Prometheus, Athena, Fuel 3,640 pounds.",
    "18:35:00": "Prometheus, Athena, Creech, bearing two zero nine, twenty seven miles, at your six o'clock.",
    "18:59:23": "Prometheus, Athena, Mission total: four T-55.",
    "18:59:55": "Prometheus, Athena, Nellis, bearing one three four, twenty nine miles, at your seven o'clock.",
    "19:00:12": "Prometheus, Athena, Creech, bearing three three seven, seven miles, at your two o'clock.",
    "19:00:43": "Prometheus, Athena, Creech, bearing zero zero nine, six miles, at your three o'clock. No traffic.",
    "19:01:46": "Prometheus, Athena, SAM SA-2 radar sees you, your twelve o'clock, eighteen miles. " + NO_TOWER + "Creech, bearing "
                "zero six nine, five miles, at your three o'clock. No traffic.",
}

FUEL_HITS = ("Athena, fuel check.", "Athena, say my fuel state", "fuel state", "Athena, how much gas do I have?",
             "what's my fuel", "Athena, bingo?", "Athena, how much fuel do I have left", "fuel remaining?",
             "Athena, what is my fuel state", "check my fuel", "Athena, fuel.", "am I bingo", "how's my fuel",
             "give me my fuel", "Athena, fuel status", "how much juice do I have", "what's my gas",
             "Athena, fuel chick", "Athena, Prometheus, fuel check please", "how close am I to bingo",
             "copy, what's my fuel?", "Athena, read me my fuel")
FUEL_MISSES = ("Athena, fuel truck destroyed", "fuel tanks hit", "splash the fuel depot", "bingo fuel, RTB",
               "Athena, I'm at joker", "dropping fuel tanks", "jettison fuel tanks", "where are the fuel trucks",
               "what's the fuel depot bearing", "how many fuel trucks are left", "the gas station is burning",
               "Athena, am I losing fuel", "feature request: call my bingo fuel", "bug report, fuel says zero",
               "Athena should be able to read my fuel", "fuel farm destroyed", "running in on the fuel storage")
AIRFIELD_HITS = ("Athena, what's the nearest airfield?", "Athena, vector to nearest air base.",
                 "vectors to the nearest airfield", "Athena, nearest friendly airfield", "closest air base",
                 "where's the closest field", "Athena, home plate", "vectors home plate", "RTB vector",
                 "give me a vector to home", "Athena, divert", "request divert field", "nearest airport",
                 "bearing to the nearest air base", "Athena, where's the nearest base", "vector nearest airbase",
                 "Athena, vectors to Nelli's Air Force Base.", "vector to Nellis", "bearing to Creech",
                 "where is McCarran", "steer to North Las Vegas", "vectors to Nellis AFB",
                 "how far to Henderson Executive airport", "Athena, nearest air field?")
AIRFIELD_MISSES = ("how many airfields are red", "Athena, red airfield captured", "Athena, nearest SAM",
                   "where is the nearest bandit", "vector to the target", "bearing to enemy armor",
                   "Athena, I'm over the field", "Good read, good read, returning to base.",
                   "where is the SAM near the airfield", "what's the name of the airfield?", "Athena, RTB",
                   "Athena, field of view is bad", "where's the enemy airfield", "Athena, any airfields captured?",
                   "Athena, what's the picture?", "vectors to the tanker", "where is my wingman")
LANDING_HITS = ("requesting permission to land", "Athena, requesting permission in land.", "request landing",
                "Athena, inbound for landing", "Roger, starting approach.", "request takeoff",
                "ready for takeoff", "request permission to take off", "Athena, request landing at Nellis",
                "permission to land at Creech", "request straight-in approach", "Athena, on final",
                "short final", "can I land", "requesting approach", "request clearance to land",
                "ready to depart", "commencing approach to Creech", "am I cleared to land?")
LANDING_MISSES = ("landing gear down?", "I landed", "Athena, touchdown.", "Athena, cleared hot?",
                  "approaching the target", "approaching IP", "missile inbound", "inbound to target",
                  "request bogey dope", "requesting picture", "request permission to engage",
                  "ready to launch Maverick", "departing target area", "what's the best approach to the SAM?",
                  "Athena, wheels up", "Athena, airborne", "is my gear down?")
FRIENDLY_HITS = ("Athena, are there any friendlies in the air?", "any friendlies", "where are the friendlies",
                 "friendly air traffic", "any friendlies airborne", "where are the friendlies at",
                 "any friendly aircraft", "friendlies airborne?", "show me the friendlies", "any blue aircraft",
                 "Athena, are there friendlies up", "where are the friendly aircraft", "where's the friendly flight",
                 "see any friendlies", "any blue traffic near me")
FRIENDLY_MISSES = ("tally one friendly", "friendly fire", "any friendly kills", "did any friendlies get killed",
                   "any friendlies destroyed?", "friendlies on the ground", "any friendly armor nearby",
                   "how many friendly aircraft are airborne", "is that a friendly?", "confirm friendly kills",
                   "any friendly ground units", "friendly trucks", "Athena, wingman splash",
                   "how many friendlies left", "friendly vehicles nearby",
                   # re-review AB2: a friendlies-near-a-place ask is never the air-traffic list
                   "friendly aircraft nearby", "Athena, any friendlies around?", "any friendly jets nearby",
                   "Athena, friendly air near me?", "any friendlies near the convoy")


def live(time_):
    return deepcopy(LIVE[time_]["picture"])


def answer(pic, text, llm=None):
    stub = llm or Stub(answer=MODEL)
    c, eye = controller(pic, stub)
    frames, _ = say(c, text)
    return frames[0]["speech"], replies(c)[0], stub, eye, c


class LiveMissTests(unittest.TestCase):
    def test_every_wrong_live_answer_is_now_a_correct_fixed_answer(self):
        for t, want in EXPECTED.items():
            with self.subTest(time=t, said=LIVE[t]["text"]):
                pic = live(t)
                if pic.get("rwr_alert_to_announce"):
                    pic["_rwr_keys"] = ["alert-%d" % i for i, _ in enumerate(pic["rwr_alert_to_announce"])]
                spoken, reply, stub, _, _ = answer(pic, LIVE[t]["text"])
                self.assertEqual(spoken, want)
                self.assertEqual(reply["route"], "fixed")
                self.assertEqual(stub.calls, 0)
                self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)
                self.assertNotRegex(spoken.replace(NO_TOWER, "").lower(), r"clear|runway|wind|altimeter|permission")

    def test_launch_reply_says_each_missile_once_and_marks_both_alerts_delivered(self):
        pic = live("18:30:45")
        self.assertEqual(len(pic["rwr_alert_to_announce"]), 2)  # live: both alerts AND both launch lines spoken
        pic["_rwr_keys"] = ["missile-16865792", "missile-16935936"]
        spoken, _, _, eye, _ = answer(pic, "Launch, launch, launch.")
        self.assertEqual(spoken.count("SA-2"), 2)
        self.assertNotIn("Missile launch", spoken)
        self.assertEqual(eye.announced, {"missile-16865792", "missile-16935936"})

    def test_opaque_missile_names_cannot_expand_the_reply_budget(self):
        pic = live("18:30:45")
        pic["_rwr_keys"] = ["missile-16865792", "missile-16935936"]
        pic["threats"]["missiles_in_flight"][1]["spoken_name"] = "Stinger " + "x" * 260
        spoken, _, _, eye, _ = answer(pic, "Launch, launch, launch.")
        self.assertIn("SA-2 in the air, your three o'clock, thirteen miles.", spoken)
        self.assertNotIn("xxx", spoken)
        self.assertEqual(eye.announced, {"missile-16865792", "missile-16935936"})

    def test_no_position_still_wins(self):
        for text in ("Athena, vector to nearest air base.", "Athena, requesting permission in land.",
                     "vectors to Nellis", "Athena, home plate", "any friendlies in the air"):
            with self.subTest(text=text):
                spoken, reply, stub, _, _ = answer(NO_OWN, text)
                self.assertEqual(spoken, "Prometheus, Athena, " + (NO_TOWER if "permission" in text else "") + NO_POSITION)
                self.assertEqual(stub.calls, 0)


class PhraseTests(unittest.TestCase):
    def check(self, hits, misses, klass):
        for text in hits:
            with self.subTest(hit=text):
                self.assertEqual(call_class(text, NEVADA), klass)
        for text in misses:
            with self.subTest(miss=text):
                self.assertNotEqual(call_class(text, NEVADA), klass)
        self.assertGreaterEqual(min(len(hits), len(misses)), 15)

    def test_fuel(self):
        self.check(FUEL_HITS, FUEL_MISSES, "fuel")

    def test_airfield(self):
        self.check(AIRFIELD_HITS, AIRFIELD_MISSES, "airfield")

    def test_landing(self):
        self.check(LANDING_HITS, LANDING_MISSES, "landing")

    def test_friendly_traffic(self):
        self.check(FRIENDLY_HITS, FRIENDLY_MISSES, "traffic")


class FuelTests(unittest.TestCase):
    def test_fuel_wording_from_the_picture(self):
        cases = (
            ({"total_lb": 3640, "spoken": "3,640 pounds", "estimated": False}, "Fuel 3,640 pounds."),
            ({"total_lb": 5200, "spoken": "about 5,200 pounds", "estimated": True}, "Fuel about 5,200 pounds, estimated."),
            ({"internal_pct": 100, "total_pct": 157, "includes_external": True, "total_lb": 9000,
              "spoken": "9,000 pounds", "estimated": False}, "Fuel 9,000 pounds, including external tanks."),
            ({"internal_lb": 6000, "external_lb": 2400, "total_lb": 8400, "total_kg": 3810},
             "Fuel 8,400 pounds, including external tanks."),
            ({"internal_pct": 42, "source": "fraction (pounds unknown for this airframe: give percent)"},
             "Fuel forty two percent."),
            ({"known": False, "reason": "no cockpit or DCS fuel reading", "note": "..."},
             "Fuel unknown, no cockpit or DCS fuel reading."),
            ({"known": False, "note": "fuel unknown"}, "Fuel unknown."),
        )
        for fuel, want in cases:
            with self.subTest(fuel=fuel):
                self.assertEqual(fuel_calls(fuel), [want])

    def test_eye_says_why_fuel_is_unknown(self):
        eye = Eye("Prometheus", clock=lambda: 1000.0)
        self.assertEqual(eye._fuel_json({"type": "F-4E-45MC"}, {}, 1000.0)["reason"], "no cockpit or DCS fuel reading")


# Seventeen Nevada fields (names as DCS has them); positions are synthetic around the fixture origin.
NEVADA_FIELDS = ("Creech", "North Las Vegas", "Nellis", "McCarran International", "Henderson Executive", "Groom Lake",
                 "Beatty", "Boulder City", "Echo Bay", "Jean", "Laughlin", "Lincoln County", "Mesquite", "Mina",
                 "Pahute Mesa", "Tonopah", "Tonopah Test Range")


def nevada_eye():
    eye = Eye("Prometheus", source="fixture", clock=lambda: 1000.0)
    eye.on_units_message({"time": 0.0, "unit": fx.unit(1, "Prom", "F-4E-45MC", "blue", "Aerial-3", "AIRPLANE",
                                                       fx.LAT0, fx.LON0, 2000.0, 0.0, 200.0, player="Prometheus")})
    rows = []
    for i, name in enumerate(NEVADA_FIELDS):
        lat, lon = fx.offset(i * 21 % 360, 5 + 4 * i)
        coalition = "COALITION_NEUTRAL" if name == "Groom Lake" else "COALITION_RED" if name == "Mina" else \
            "COALITION_BLUE"
        rows.append({"name": name, "category": "AIRBASE_CATEGORY_AIRDROME", "coalition": coalition,
                     "position": {"lat": lat, "lon": lon, "alt": 900.0}})
    eye.set_airbases(rows)
    eye.set_connected(True)
    return eye


class NamedFieldTests(unittest.TestCase):
    def test_spoken_names_match_tolerantly(self):
        pic = nevada_eye().picture()
        self.assertEqual(len(pic["_airfields"]), 17)
        self.assertEqual(len(pic["airbases"]["nearest"]), 5)
        cases = {"vectors to Nelli's Air Force Base": "Nellis", "vector to nellis afb": "Nellis",
                 "vector to NELLIS": "Nellis", "steer to Creach": "Creech", "vector to McCarran airport": "McCarran International",
                 "vectors to north las vegas": "North Las Vegas", "vector to Tonopah": "Tonopah",
                 "vector to Tonopah Test Range": "Tonopah Test Range", "where is Lincoln county": "Lincoln County",
                 "bearing to Henderson": "Henderson Executive", "vector to Jean": "Jean"}
        for text, name in cases.items():
            with self.subTest(text=text):
                row, named = named_field(text, pic)
                self.assertTrue(named)
                self.assertEqual(row and row["name"], name)
        for text in ("vector to Fooville Air Force Base", "vectors to Springfield"):
            self.assertEqual(named_field(text, pic), (None, True))
        self.assertEqual(named_field("vector to nearest air base", pic), (None, False))
        self.assertEqual(named_field("vector to the jet", pic)[0], None)  # short names match exactly only

    def test_any_mission_field_resolves_by_name_but_is_never_shown_to_the_model(self):
        eye = nevada_eye()
        far = [r["name"] for r in eye.picture()["_airfields"][5:]]
        self.assertIn("Tonopah Test Range", far)
        c, _ = controller(None, Stub(answer=MODEL), eye=eye)
        frames, _ = say(c, "Athena, vectors to Tonopah Test Range")
        self.assertRegex(frames[0]["speech"], r"^Prometheus, Athena, Tonopah Test Range, bearing [a-z ]+, \w+ \w+ "
                                              r"miles, at your \w+ o'clock\.$")
        seen = []
        c2, _ = controller(None, lambda m: seen.append(m) or MODEL, eye=eye)
        say(c2, "Athena, radio check", tid="t2")
        prompt = [m["content"] for m in seen[0] if m["content"].startswith("PICTURE ")][0]
        self.assertNotIn("Tonopah Test Range", prompt)
        self.assertNotIn("_airfields", prompt)
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        self.assertNotIn("_airfields", tool["result"]["picture"])

    def test_owner_is_said_for_a_field_that_is_not_friendly_and_an_unknown_field_gets_the_nearest(self):
        eye = nevada_eye()
        c, _ = controller(None, Stub(answer=MODEL), eye=eye)
        self.assertRegex(say(c, "vector to Groom Lake")[0][0]["speech"], r"Groom Lake, neutral field, bearing")
        self.assertRegex(say(c, "vector to Mina", tid="t2")[0][0]["speech"], r"Mina, enemy field, red, bearing")
        spoken = say(c, "vectors to Fooville Air Force Base", tid="t3")[0][0]["speech"]
        self.assertRegex(spoken, r"^Prometheus, Athena, I don't have that field\. Nearest friendly, Creech, bearing")
        landing = say(c, "request landing at Creech", tid="t4")[0][0]["speech"]
        self.assertRegex(landing, r"^Prometheus, Athena, no tower: I can't give clearances\. Creech, bearing .*\. No traffic\.$")

    def test_no_friendly_field_known(self):
        pic = deepcopy(NEVADA)
        pic["airbases"] = {"count": 1, "nearest": [], "nearest_friendly": [],
                           "nearest_friendly_note": "no friendly airfield known; do not offer one"}
        self.assertEqual(answer(pic, "Athena, vector to nearest air base.")[0],
                         "Prometheus, Athena, No friendly airfield known.")
        self.assertEqual(answer(pic, "request landing")[0], "Prometheus, Athena, " + NO_TOWER + "No friendly airfield known.")

    def test_landing_calls_the_field_traffic(self):
        pic = live("19:01:46")
        pic.pop("rwr_alert_to_announce", None)
        creech = next(r for r in pic["airbases"]["nearest"] if r["name"] == "Creech")
        creech["traffic"] = {"count": 2, "aircraft": [
            {"call": "Friendly A-10C, zero seven zero, five miles, angels two, three o'clock low."},
            {"call": "Friendly A-10C, zero six eight, six miles, angels one, three o'clock low."}]}
        spoken = answer(pic, "Athena, requesting permission to land")[0]
        self.assertEqual(spoken, "Prometheus, Athena, " + NO_TOWER + "Creech, bearing zero six nine, five miles, at your three o'clock. "
                                 "Traffic, two aircraft. Friendly A-10C, zero seven zero, five miles, angels two, "
                                 "three o'clock low.")

    def test_256_fields_resolve_quickly(self):
        pic = deepcopy(NEVADA)
        pic["_airfields"] = [{"name": "Field %s %03d" % (chr(65 + i % 26) * 3, i), "owner": "blue",
                              "call": "x."} for i in range(250)] + [
            {"name": n, "owner": "blue", "call": n + ", bearing one."} for n in
            ("Abu al-Duhur", "Al-Dumayr", "King Hussein Air College", "Ramat David", "Incirlik", "Gaziantep")]
        t0 = time.perf_counter()
        for text in ("vectors to Incirlik", "request landing at Ramat David", "vector to Abu al Duhur air base",
                     "vector to al dumayr", "vectors to King Hussein air college"):
            self.assertIsNotNone(named_field(text, pic)[0], text)
        self.assertLess(time.perf_counter() - t0, 0.5)


class ClearanceGuardTests(unittest.TestCase):
    def test_a_model_clearance_is_never_spoken(self):
        pic = live("19:01:46")
        pic.pop("rwr_alert_to_announce", None)
        # re-review AB2: an ATC ask never reaches the model; it gets the fixed field + traffic answer
        fixed = ("Prometheus, Athena, Creech, bearing zero six nine, five miles, at your three o'clock. No traffic. "
                 "I have no runway, wind or altimeter data.")
        cases = {
            "Prometheus, Athena, Creech, bearing zero six nine, five miles, at your three o'clock. Cleared to land, "
            "no traffic.": fixed,
            "Prometheus, Athena, no traffic, cleared for takeoff runway two seven.": fixed,
            "Prometheus, Athena, permission granted, cleared to land.": fixed,
            "Prometheus, Athena, you're cleared for the approach, no traffic.": fixed,
            "Prometheus, Athena, cleared hot.": fixed,
        }
        for said, want in cases.items():
            with self.subTest(said=said):
                stub = Stub(answer=said)
                spoken = answer(pic, "Athena, which runway is in use?", stub)[0]
                self.assertEqual(spoken, want)
                self.assertEqual(stub.calls, 0)


class StreamHealthTests(unittest.TestCase):
    def test_a_subscribed_stream_with_nothing_to_send_is_up(self):
        eye = Eye("Prometheus", clock=lambda: 1000.0)
        eye.set_connected(False, "units-helo: _InactiveRpcError StatusCode.UNAVAILABLE", stream="units-helo")
        eye.set_connected(False, "units-ship: _InactiveRpcError StatusCode.UNAVAILABLE", stream="units-ship")
        eye.set_connected(True, stream="units-air")
        eye.set_connected(True, stream="units-helo", idle=True)
        streams = eye.picture()["data_health"]["streams"]
        self.assertEqual(streams["units-helo"], "up, no units for 0s")
        self.assertTrue(streams["units-ship"].startswith("down 0s (units-ship"))  # truly failing: still down
        self.assertEqual(streams["units-air"], "up")
        eye.set_connected(True, stream="units-helo")  # a message arrived
        self.assertEqual(eye.picture()["data_health"]["streams"]["units-helo"], "up")

    def feed(self, done):
        eye = Eye("Prometheus", clock=lambda: 1000.0)
        f = GrpcFeeder(eye, "127.0.0.1", 1)
        f._chan, f._check_session = (lambda: None), (lambda stub: None)
        seen = {}

        class Call:
            def initial_metadata(self):
                return ()

            def done(self):
                return done

            def cancel(self):
                pass

            def __iter__(self):
                seen["during"] = eye.streams_json(1000.0).get("units-helo")
                return iter(())

        class Stub_:
            def __init__(self, channel):
                pass

            def StreamUnits(self, req):
                return Call()

        from dcs.mission.v0 import mission_pb2_grpc
        with mock.patch.object(mission_pb2_grpc, "MissionServiceStub", Stub_):
            f._units("units-helo", "GROUP_CATEGORY_HELICOPTER", 1, 5)
        return seen["during"]

    def test_the_feeder_marks_an_open_subscription_up_before_any_unit_arrives(self):
        self.assertEqual(self.feed(done=False), "up, no units for 0s")
        self.assertIsNone(self.feed(done=True))  # the call already ended (failed): nothing marked up


class TallyTests(unittest.TestCase):
    def test_static_and_unit_t55_are_one_type_and_keep_their_category(self):
        self.assertEqual(scenery_name("T-55"), "T-55")
        self.assertEqual(object_type({"kind": "static", "type": "T-55"}), "T-55")
        self.assertEqual(scenery_name("Wire_Fence_1"), "Wire Fence 1")
        pic = live("18:59:23")
        kills = pic["my_kills_this_mission"]
        self.assertEqual([k["target"] for k in kills], ["T-55", "T 55", "T 55", "T 55"])
        self.assertEqual(answer(pic, "What's the tally now?")[0], "Prometheus, Athena, Mission total: four T-55.")
        # the unit/scenery category is kept: armor alone is the one live T-55
        self.assertEqual(answer(pic, "Athena, confirm tally armor")[0], "Prometheus, Athena, Mission total: one T-55.")
        pic["my_kills_this_mission"] = kills + [dict(kills[0], target="T-72", seq=9001),
                                                dict(kills[0], target="BMP 2", seq=9002)]
        self.assertEqual(answer(pic, "What's the tally now?")[0],
                         "Prometheus, Athena, Mission total: four T-55, one T-72, one BMP-2.")


if __name__ == "__main__":
    unittest.main()
