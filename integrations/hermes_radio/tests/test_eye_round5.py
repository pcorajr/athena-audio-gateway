"""Offline regressions for the Syria session: traffic, air calls, damage and attack follow-ups."""
from copy import deepcopy
import json
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from integrations.hermes_radio.contract import MAX_SPEECH_CHARS, encode_response
from integrations.hermes_radio.eye import Eye, GrpcFeeder, RAW_SAMPLE_CHARS, norm_unit
from integrations.hermes_radio.simple_controller import CLASS_HINTS, MAX_REPLY_CHARS, PROMPT, SimpleController, call_class
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye_round4 import A10, Clock, FakeTranscript, LUA, req, store_rows


def contact(uid, type_name="Su-27", coal="red", gcat="AIRPLANE", distance=19.0, bearing=90, alt=7000,
            group=None, speed=200):
    return fx.unit(uid, "unit-%d" % uid, type_name, coal, group or "group-%d" % uid, gcat,
                   *fx.offset(bearing, distance), alt, 90, speed)


def make_eye(*units, stores_dir=None):
    clock = Clock()
    eye = Eye(clock=clock, source="fixture", stores_dir=stores_dir)
    eye.set_connected(True)
    for u in (A10,) + units:
        eye.on_unit(deepcopy(u))
    return eye, clock


def exchange(eye, text, answer="Three bandits overhead. Plus forty more groups."):
    seen = []

    def llm(messages):
        seen.extend(messages)
        return answer

    log = FakeTranscript()
    controller = SimpleController(None, log, model="fixture", api_key="unused", eye=eye, llm=llm)
    result = controller.exchange(req(text))
    return result["speech"], seen, log


def cockpit_packet(**extra):
    return dict({"v": 1, "ac": "A-10C_2", "unit": A10["name"], "lat": fx.LAT0, "lon": fx.LON0,
                 "alt": 914, "hdg": 90, "coal": 2, "fu": 2, "fuel_int": 0.9, "warn": []}, **extra)


class TrafficTests(unittest.TestCase):
    def test_blue_aircraft_survive_nearer_ground_budget(self):
        ground = [contact(i, "T-55", "blue", "GROUND", i * 2, alt=100) for i in range(1, 10)]
        eye, _ = make_eye(*ground, contact(20, "F-16C_50", "blue", distance=25),
                          contact(21, "UH-1H", "blue", "HELICOPTER", distance=30))
        pic = eye.picture()
        self.assertTrue(all(g["domain"] == "ground" for g in pic["friendly_groups"]))
        self.assertEqual([list(g["types"])[0] for g in pic["air_traffic"]["blue"]], ["F-16C_50", "UH-1H"])
        for text in ("any blue traffic near me", "confirm only blue traffic", "blue only"):
            speech, _, _ = exchange(eye, text)
            self.assertIn("F-16C", speech)  # phase 0 budget: compact calls, both aircraft whole
            self.assertIn("UH-1H", speech)
            self.assertNotIn("armor", speech)
            self.assertEqual(speech.count("o'clock"), 2)
            self.assertLessEqual(len(speech), MAX_REPLY_CHARS)

    def test_ground_traffic_is_explicit_exception(self):
        self.assertNotEqual(call_class("confirm blue ground traffic", {"last_attack_age_s": 5}), "traffic")
        self.assertNotEqual(call_class("blue vehicles near me"), "traffic")

    def test_blue_is_coalition_not_ownship_side(self):
        eye, _ = make_eye(contact(20, "F-16C_50", "blue", distance=4), contact(21, distance=8))
        eye.units[A10["id"]]["coalition"] = "red"
        speech, _, _ = exchange(eye, "blue traffic")
        self.assertIn("F-16C", speech)
        self.assertNotIn("Su-27", speech)

    def test_field_traffic_includes_rolling_parked_helo_and_departing(self):
        # Field elevation 914 m: filtering by absolute 5,000 ft would miss the departure.
        eye, _ = make_eye(contact(1, "F-16C_50", "blue", distance=0.5, alt=914, speed=70),
                          contact(2, "F-16C_50", "blue", distance=1.9, alt=914, speed=0),
                          contact(3, "UH-1H", "blue", "HELICOPTER", distance=1, alt=914, speed=0),
                          contact(4, "F-16C_50", "blue", distance=4, alt=2000),
                          contact(5, "F-16C_50", "blue", distance=6, alt=1000),
                          contact(6, "F-16C_50", "blue", distance=2.2, alt=914, speed=0),
                          contact(7, "F-16C_50", "blue", distance=1, alt=6000),
                          contact(8, "T-55", "blue", "GROUND", distance=0.1, alt=914))
        eye.set_airbases([{"name": "Ramat David", "category": "AIRBASE_CATEGORY_AIRDROME",
                           "position": {"lat": fx.LAT0, "lon": fx.LON0, "alt": 914}}])
        field = eye.picture()["airbases"]["nearest"][0]
        self.assertEqual(field["traffic"]["count"], 4)
        self.assertEqual(sum(r["on_field"] for r in field["traffic"]["aircraft"]), 3)
        self.assertTrue(all(re.search(r"^Friendly [\w-]+, \w+ \w+ \w+, .*angels .*o'clock", r["call"])
                            for r in field["traffic"]["aircraft"]))
        said, messages, log = exchange(eye, "request take off at Ramat David", "Traffic at Ramat David.")
        self.assertEqual(messages, [])  # flight 11-11: a takeoff request is a fixed answer, the field and its traffic
        self.assertIn("Ramat David, bearing", said)
        self.assertIn("Traffic, four aircraft. Friendly F-16C", said)
        self.assertNotIn("clear", said.lower())
        tool = next(r for r in log.records if r["kind"] == "tool")
        self.assertEqual(tool["result"]["picture"]["airbases"]["nearest"][0]["traffic"]["count"], 4)

    def test_no_blue_aircraft_does_not_substitute_tanks(self):
        eye, _ = make_eye(contact(1, "T-55", "blue", "GROUND", distance=1))
        speech, _, _ = exchange(eye, "any blue traffic near me")
        self.assertIn("No blue aircraft showing within one hundred fifty miles", speech)
        self.assertNotIn("armor", speech)


class AirPictureTests(unittest.TestCase):
    def test_stream_preserves_three_mixed_groups_and_budgets_pending_alerts(self):
        units = [contact(i * 2 + j, type_name, distance=19 + i * 10 + j * 0.1)
                 for i in range(3) for j, type_name in enumerate(("Su-27", "MiG-29S"), 1)]
        eye, _ = make_eye(*units)
        base = eye.picture()
        self.assertEqual(len(base["picture_top3"]), 3)
        for count in (0, 4, 30):
            with self.subTest(alerts=count):
                pic = deepcopy(base)
                pic["rwr_alert_to_announce"] = ["Spike, SA-11 at your three o'clock, contact number %d." % i for i in range(count)]
                pic["_rwr_keys"] = ["rwr-%d" % i for i in range(count)]
                pic["splash_to_announce"] = ["Splash one T-55, target number %d." % i for i in range(count)]
                pic["_splash_keys"] = ["splash-%d" % i for i in range(count)]
                rwr_keys, splash_keys = pic["_rwr_keys"], pic["_splash_keys"]
                controller = SimpleController(None, FakeTranscript(), model="fixture", api_key="unused",
                                              eye=eye, llm=lambda _: "Three bandits overhead.")
                with patch.object(eye, "picture", return_value=pic), patch.object(eye, "mark_announced") as mark:
                    frames = list(controller.stream(req("air picture")))
                self.assertEqual(len(frames), 1)  # phase 0: one frame within the one reply budget
                for frame in frames:
                    encode_response(frame)
                speech = frames[0]["speech"]
                self.assertLessEqual(len(speech), MAX_REPLY_CHARS)
                kept = [call for call in base["picture_top3"] if call in speech]
                self.assertEqual(kept, base["picture_top3"][:len(kept)])  # nearest first, whole calls
                # review A (11-11): pending threat alerts go out whole first; the picture gets the room left
                self.assertGreaterEqual(len(kept), 0 if count else 1)
                for call in base["picture_top3"][len(kept):]:
                    self.assertNotIn(call.split(", angels")[0], speech)  # dropped whole, not half
                if not kept:
                    self.assertTrue(speech.endswith("Ask again for the picture."))
                elif len(kept) < 3:
                    self.assertIn("Plus %s more." % ("two" if len(kept) == 1 else "one"), speech)
                rwr_said = [k for i, k in enumerate(rwr_keys) if "contact number %d." % i in speech]
                splash_said = [k for i, k in enumerate(splash_keys) if "target number %d." % i in speech]
                marked = [k for c in mark.call_args_list for k in c.args[0] if not str(k).startswith('close-bandit')]
                self.assertEqual(marked, rwr_said + splash_said)  # only what was actually said is announced
                if count:
                    self.assertTrue(rwr_said)  # alerts first; the picture rides in the room they leave
                    self.assertLess(len(rwr_said) + len(splash_said), count * 2)  # the rest stay pending

    def test_oversize_requested_calls_are_reported_not_silently_truncated(self):
        eye, _ = make_eye(contact(1))
        pic = eye.picture()
        pic["picture_top3"] = ["Bandit " + "long type " * 100 + ". At your twelve o'clock, high."]
        pic["rwr_alert_to_announce"] = ["Spike, SA-11."]
        pic["_rwr_keys"] = ["pending-spike"]
        pic["_close_bandits"] = []  # (a close bandit would be said first, then "Ask again for the rest.")
        log = FakeTranscript()
        controller = SimpleController(None, log, model="fixture", api_key="unused", eye=eye,
                                      llm=lambda _: "Three bandits overhead.")
        with patch.object(eye, "picture", return_value=pic), patch.object(eye, "mark_announced") as mark:
            frames = list(controller.stream(req("air picture")))
        speech = " ".join(f["speech"] for f in frames)
        self.assertIn("requested calls are too long", speech)
        self.assertLessEqual(len(frames), 2)
        self.assertLessEqual(len(speech), MAX_SPEECH_CHARS)
        self.assertTrue(any(r.get("outcome") == "fixed_response_overflow" for r in log.records))
        self.assertFalse([k for c in mark.call_args_list for k in c.args[0] if not str(k).startswith('close-bandit')])

    def test_closest_with_full_format_and_no_character_truncation(self):
        eye, _ = make_eye(*(contact(i, distance=d) for i, d in enumerate((19, 29, 39, 45, 51), 1)))
        pic = eye.picture()
        self.assertEqual(len(pic["picture_top3"]), 3)
        for text in ("air picture", "bogey dope", "give me the picture"):  # mission status = overview
            speech, _, _ = exchange(eye, text)
            self.assertLessEqual(len(speech), MAX_REPLY_CHARS)
            kept = [line for line in pic["picture_top3"] if line in speech]
            self.assertEqual(kept, pic["picture_top3"][:len(kept)])  # nearest first, whole calls only
            self.assertGreaterEqual(len(kept), 1)
            self.assertEqual(len(kept), 3)  # phase 0: three compact calls fit one transmission
            for word in ("Su-27, ", "angels", "o'clock"):
                self.assertEqual(speech.count(word), len(kept), word)
            self.assertNotIn("Plus", speech)
            self.assertNotIn("overhead", speech)
            self.assertNotIn("more groups", speech)
            self.assertNotIn("fifty one", speech)

    def test_fifty_mile_boundary_uses_unrounded_range(self):
        eye, _ = make_eye(contact(1))
        own, enemy = eye.ownship(), eye.units[1]
        for distance, count in ((50, 1), (50.001, 0), (49.999, 1)):
            with patch.object(eye, "_cluster", return_value=[("air", [(distance, 90, enemy)])]):
                pic = {}
                eye._contacts(own, pic)
                self.assertEqual(len(pic["picture_top3"]), count)

    def test_empty_radius_does_not_fall_back_to_ground_or_distant_air(self):
        eye, _ = make_eye(contact(1, distance=51), contact(2, "T-55", "red", "GROUND", distance=1))
        speech, _, _ = exchange(eye, "bogey dope")
        self.assertIn("Nothing inside fifty miles", speech)  # 150 nm tier gives the 51 nm bandit
        self.assertNotIn("T-55", speech)  # never falls back to ground

    def test_overhead_flag_exact_boundary(self):
        eye, _ = make_eye(contact(1))
        for distance, allowed in ((2.9, True), (3.0, True), (3.01, False), (19, False)):
            row = eye._group_json(eye.ownship(), "air", [(distance, 90, eye.units[1])])
            self.assertEqual(row["overhead_allowed"], allowed)
        self.assertIn("within three nautical miles", PROMPT)

    def test_two_ship_type_and_low_altitude_format(self):
        eye, _ = make_eye(contact(1, alt=100), contact(2, distance=19.2, alt=100))
        line = eye.picture()["picture_top3"][0]
        self.assertTrue(line.startswith("Su-27 two-ship, zero nine zero, "), line)
        self.assertIn("angels zero, twelve o'clock low.", line)

    def test_ground_picture_still_routes_to_ground(self):
        self.assertEqual(call_class("give me the ground picture"), "ground_picture")
        self.assertIsNone(call_class("bearing to enemy armor"))  # phase 0: bearings are the model's job

    def test_stale_air_picture_is_labeled_not_discarded(self):
        eye, clock = make_eye(contact(1))
        clock.t += 80
        eye.set_connected(False)
        speech, _, _ = exchange(eye, "picture")
        self.assertIn("Tracking disconnected", speech)
        self.assertIn("eighty seconds old", speech)
        self.assertIn("Su-27, zero nine zero", speech)


class DamageTests(unittest.TestCase):
    def test_target_damage_question_keeps_model_answer_despite_ownship_damage(self):
        enemy = contact(1, "T-55", "red", "GROUND", distance=1)
        eye, _ = make_eye(enemy)
        eye.on_life(eye.ownship(), {"life": 100, "life0": 100})
        eye.on_life(eye.ownship(), {"life": 70, "life0": 100})
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        eye.on_event(fx.hit(A10, 1, "GAU_8", enemy))
        answer = "Prometheus, Athena, hit on the T-55, no confirmed kill."
        for text in ("What damage did I do to the enemy tank?", "Confirm damage to the target",
                     "Enemy damage report", "Damage report on my target"):
            with self.subTest(text=text):
                self.assertIsNone(call_class(text, eye.picture()))
                speech, messages, _ = exchange(eye, text, answer)
                self.assertEqual(speech, answer)
                self.assertNotIn(CLASS_HINTS["damage"], [m["content"] for m in messages])
                self.assertNotIn("life down", speech)

    def test_ownship_damage_intents_keep_primary_evidence(self):
        eye, _ = make_eye()
        eye.on_life(eye.ownship(), {"life": 100, "life0": 100})
        eye.on_life(eye.ownship(), {"life": 70, "life0": 100})
        for text in ("I'm hit", "I got hit", "taking hits", "damage report", "my damage",
                     "I took damage", "damage to my aircraft", "am I damaged?"):
            with self.subTest(text=text):
                self.assertEqual(call_class(text), "damage")
                speech, _, _ = exchange(eye, text, "No damage showing.")
                self.assertIn("You took damage, life down to seventy percent", speech)

    def test_weapon_initiator_hit_logged_without_cockpit_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            eye, _ = make_eye(stores_dir=tmp)
            eye.on_cockpit_packet(cockpit_packet())
            eye.on_event({"hit": {"initiator": {"weapon": fx.weapon(900, "23mm shell")},
                                  "target": {"unit": A10}}})
            damage = eye.picture()["ownship"]["damage"]
            self.assertEqual(damage["events"][0]["source"], "hit event")
            self.assertEqual(damage["secondary"]["warning_lights"], [])
            self.assertTrue(any(e["kind"] == "own_damage" for e in eye.events))
            self.assertEqual(len([r for r in store_rows(tmp) if r["kind"] == "own_damage"]), 1)
            speech, _, _ = exchange(eye, "I'm hit", "No hit report in the cockpit data.")
            self.assertIn("You were hit by 23mm shell", speech)
            self.assertNotIn("No hit", speech)

    def test_hit_on_someone_else_is_not_own_damage(self):
        enemy = contact(1)
        eye, _ = make_eye(enemy)
        eye.on_event(fx.hit(A10, 1, "GAU_8", enemy))
        self.assertEqual(eye.picture()["ownship"]["damage"]["events"], [])

    def test_life_drop_percentage_repeated_hits_and_durable_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            eye, clock = make_eye(stores_dir=tmp)
            for life in (1000, 800, 800, 600):
                eye.on_life(eye.ownship(), {"life": life, "life0": 1000})
                clock.t += 3
            damage = eye.picture()["ownship"]["damage"]
            self.assertEqual(damage["life_percent"], 60)
            self.assertEqual(len(damage["events"]), 2)
            self.assertEqual(damage["life_age_s"], 3)
            self.assertEqual(len([r for r in store_rows(tmp) if r["kind"] == "own_damage"]), 2)
            speech, _, _ = exchange(eye, "damage report")
            self.assertIn("You took damage, life down to sixty percent", speech)

    def test_zero_life_is_valid_missing_nan_or_bad_max_is_not(self):
        eye, _ = make_eye()
        for data in ({}, {"life": 0, "life0": 0}, {"life": float("nan"), "life0": 10}):
            eye.on_life(eye.ownship(), data)
        self.assertFalse(eye.picture()["ownship"]["damage"]["life_available"])
        eye.on_life(eye.ownship(), {"life": 10, "life0": 10})
        eye.on_life(eye.ownship(), {"life": 0, "life0": 10})
        self.assertEqual(eye.picture()["ownship"]["damage"]["life_percent"], 0)

    def test_new_airframe_and_mission_clear_damage_baseline(self):
        eye, _ = make_eye()
        eye.on_life(eye.ownship(), {"life": 100, "life0": 100})
        eye.on_life(eye.ownship(), {"life": 50, "life0": 100})
        eye.on_event({"birth": {"initiator": {"unit": A10}}})
        self.assertIsNone(eye.life)
        self.assertFalse(eye.damage_events)
        eye.on_life(eye.ownship(), {"life": 20, "life0": 100})
        self.assertFalse(eye.damage_events)  # baseline, not a new hit
        eye.on_event({"mission_start": {}})
        self.assertIsNone(eye.life)

    def test_feeder_polls_life_even_with_fresh_cockpit_payload(self):
        for fresh in (False, True):
            eye, clock = make_eye()
            if fresh:
                eye.on_cockpit_packet(cockpit_packet(pay={"gun": 500, "st": []}))
            calls = []

            class Stub:
                def Eval(self, request, timeout=None):
                    calls.append(request.lua)
                    return type("Reply", (), {"json": json.dumps({"life": 100 if len(calls) == 1 else 75,
                                                                  "life0": 100, "fuel": 0.9, "ammo": []})})()

            feeder = GrpcFeeder(eye, "127.0.0.1", 1)
            feeder.poll_stores(Stub())
            clock.t += 3
            feeder.poll_stores(Stub())
            self.assertIn("u:getLife()", calls[0])
            self.assertIn("u:getLife0()", calls[0])
            self.assertEqual("getAmmo" in calls[0], not fresh)
            self.assertEqual(eye.picture()["ownship"]["damage"]["life_percent"], 75)
            self.assertEqual(len(eye.damage_events), 1)
            self.assertEqual(feeder.stores_poll, 3)

    @unittest.skipUnless(LUA, "Lua interpreter unavailable")
    def test_eval_lua_executes_with_life_and_missing_life_api(self):
        assert LUA is not None
        for template in (GrpcFeeder.FUEL_LUA, GrpcFeeder.STORES_LUA):
            for available in (True, False):
                lua = "local u = {isExist=function() return true end, getFuel=function() return .5 end, "
                lua += "getAmmo=function() return {} end} "
                if available:
                    lua += "u.getLife=function() return 75 end u.getLife0=function() return 100 end "
                lua += "Unit={getByName=function() return u end} local function poll() "
                lua += template % '"test"'
                lua += " end local r=poll() assert(r.fuel == .5) "
                lua += "assert(r.life == 75 and r.life0 == 100)" if available else "assert(r.life == nil)"
                result = subprocess.run([LUA, "-"], input=lua, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_secondary_rpm_fuel_loss_and_mcp_are_not_confirmed_hits(self):
        eye, clock = make_eye()
        eye.on_cockpit_packet(cockpit_packet(rpm=[90, 90], fuel_int=0.9))
        clock.t += 30
        eye.on_cockpit_packet(cockpit_packet(rpm=[50, 90], fuel_int=0.8, warn=["LeftEngineFire"]))
        damage = eye.picture()["ownship"]["damage"]
        self.assertEqual(damage["events"], [])
        secondary = damage["secondary"]
        self.assertEqual(secondary["engine"]["rpm_change"], [-40, 0])
        self.assertGreater(secondary["fuel_trend"]["loss_lb_per_min"], 0)
        self.assertIn("not a confirmed leak", secondary["fuel_trend"]["note"])
        self.assertEqual(secondary["warning_lights"], ["LeftEngineFire"])
        clock.t += 10
        self.assertNotIn("secondary", eye.picture()["ownship"]["damage"])

    def test_damage_request_without_evidence_gets_non_denial_hint(self):
        eye, _ = make_eye()
        _, messages, _ = exchange(eye, "I'm hit", "Copy, damage telemetry unavailable.")
        self.assertIn(CLASS_HINTS["damage"], [m["content"] for m in messages])
        self.assertIn("No warnings NEVER means no damage", PROMPT)


class AttackFollowupTests(unittest.TestCase):
    def test_ambiguous_confirm_within_five_minutes_only(self):
        for age, expected in ((0, "kill_confirm"), (299.9, "kill_confirm"), (300, None), (None, None)):
            self.assertEqual(call_class("Confirm chaos", {"last_attack_age_s": age}), expected)
        self.assertEqual(call_class("Athena, confirm, kills."), "kill_confirm")

    def test_explicit_other_requests_override_recent_attack(self):
        pic = {"last_attack_age_s": 5}
        for text, expected in (("confirm only blue traffic", "traffic"), ("confirm fuel", None),
                               ("confirm gear down", None), ("confirm picture", "picture"),
                               ("confirm bearing to enemy armor", None)):
            self.assertEqual(call_class(text, pic), expected)

    def test_confirm_chaos_answers_kills_without_upgrading_likely(self):
        target = contact(1, "Infantry AK", "red", "GROUND", distance=1)
        eye, clock = make_eye(target)
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 50
        eye.on_gone(1)
        speech, messages, _ = exchange(eye, "Confirm chaos")
        self.assertIn("Likely: one Infantry AK", speech)
        self.assertNotIn("Bandit", speech)
        self.assertTrue(eye.my_kills[0]["likely"])
        self.assertEqual(messages, [])  # phase 0: the fixed kill answer never calls the model

    def test_last_attack_age_uses_end_of_burst(self):
        eye, clock = make_eye()
        eye.on_event(fx.shooting("shooting_start", A10, "GAU_8"))
        clock.t += 20
        eye.on_event(fx.shooting("shooting_end", A10, "GAU_8"))
        clock.t += 290
        self.assertEqual(eye.picture()["last_attack_age_s"], 290)

    def test_soft_targets_23_33_and_45_seconds_strong_46_weak(self):
        for type_name in ("Infantry AK", "Ural-375"):
            for delay, likely in ((23, False), (33, False), (45, False), (46, True)):
                with self.subTest(type_name=type_name, delay=delay):
                    target = contact(1, type_name, "red", "GROUND", distance=1)
                    eye, clock = make_eye(target)
                    eye.on_event(fx.shot(A10, 0, "GAU_8"))
                    clock.t += delay
                    eye.on_gone(1)
                    self.assertEqual(eye.my_kills[0]["likely"], likely)
                    self.assertTrue(eye.my_kills[0]["inferred"])

    def test_soft_group_count_drop_attributed_to_my_burst(self):
        a = contact(1, "Infantry AK", "red", "GROUND", distance=1, group="squad")
        b = contact(2, "Infantry AK", "red", "GROUND", distance=1.1, group="squad")
        eye, clock = make_eye(a, b)
        eye.on_event(fx.shooting("shooting_start", A10, "GAU_8"))
        clock.t += 2
        eye.on_event(fx.shooting("shooting_end", A10, "GAU_8"))
        clock.t += 33
        eye.on_gone(2)
        self.assertFalse(eye.my_kills[0]["likely"])
        self.assertIn("group count 2 to 1", eye.my_kills[0]["how"])

    def test_unrelated_soft_target_not_credited_and_armor_window_unchanged(self):
        a = contact(1, "T-55", "red", "GROUND", distance=1)
        b = contact(2, "Infantry AK", "red", "GROUND", distance=1.1, bearing=270)
        eye, clock = make_eye(a, b)
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 33
        eye.on_gone(2)
        self.assertEqual(eye.my_kills, [])
        eye.on_gone(1)
        self.assertTrue(eye.my_kills[0]["likely"])

    def test_stores_only_shot_preserves_group_count_baseline(self):
        target = contact(1, "Infantry AK", "red", "GROUND", distance=1)
        eye, clock = make_eye(target)
        for count in (100, 90):
            eye.on_stores(eye.ownship(), {"ammo": [{"type": "gun", "count": count, "category": 0}]})
        clock.t += 33
        eye.on_gone(1)
        self.assertFalse(eye.my_kills[0]["likely"])
        self.assertIn("group count 1 to 0", eye.my_kills[0]["how"])


class RawEventTests(unittest.TestCase):
    def test_long_sample_preserves_target_weapon_strips_vectors_and_throttles(self):
        with tempfile.TemporaryDirectory() as tmp:
            eye, clock = make_eye(stores_dir=tmp)
            attacker = deepcopy(A10)
            attacker["name"] = "attacker-" + "x" * 800
            attacker["orientation"] = {"forward": {"x": "x" * 9000}}
            msg = fx.hit(attacker, 90, "GAU_8", contact(1, "Infantry AK", "red", "GROUND"))
            for delay in (0, 1, 58, 1):
                clock.t += delay
                eye.on_event(msg)
            rows = [r for r in store_rows(tmp) if r["kind"] == "raw_event_sample"]
            self.assertEqual(len(rows), 2)
            sample = rows[0]["sample"]
            self.assertGreater(len(sample), 700)
            self.assertLessEqual(len(sample), RAW_SAMPLE_CHARS)
            data = json.loads(sample)
            self.assertEqual(data["hit"]["target"]["unit"]["type"], "Infantry AK")
            self.assertEqual(data["hit"]["weapon"]["type"], "GAU_8")
            self.assertNotIn("orientation", sample)
            self.assertIn("orientation", msg["hit"]["initiator"]["unit"])  # source untouched


if __name__ == "__main__":
    unittest.main()
