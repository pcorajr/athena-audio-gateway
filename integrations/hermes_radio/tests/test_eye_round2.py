"""Athena Eye round 2 (2026-11-07 play session): event-stream liveness, vanish-as-kill, single-player
attribution, ready call lines, loadout/stores tracking, splash calls, dynamic whisper prompt."""
import json
import os
import stat
import tempfile
import threading
import unittest

from integrations.hermes_radio.contract import Request
from integrations.hermes_radio.eye import (Eye, GrpcFeeder, call_line, digit_words, range_words, words)
from integrations.hermes_radio.simple_controller import SimpleController
from integrations.hermes_radio.tests import eye_fixture as fx


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# Single player A-10C over the fixture origin, heading 090. DCS single player reports player_name "player".
A10 = fx.unit(100, "Pilot #001", "A-10C_2", "blue", "Hawg 1", "AIRPLANE", fx.LAT0, fx.LON0, 3000.0, 90.0, 150.0,
              player="player")
T72_A = fx.unit(801, "Armor-1", "T-72B", "red", "Red Armor East", "GROUND", *fx.offset(90, 3.0), 300.0)
T72_B = fx.unit(802, "Armor-2", "T-72B", "red", "Red Armor East", "GROUND", *fx.offset(91, 3.1), 300.0)
SA8 = fx.unit(803, "SAM-1", "Osa 9A33 ln", "red", "SA-8", "GROUND", *fx.offset(200, 30), 300.0)
WINGMAN = fx.unit(110, "Hawg 1-2", "A-10C_2", "blue", "Hawg 1", "AIRPLANE", *fx.offset(270, 2), 3000.0, 90.0, 150.0)


def scenario(clock=None, player="Prometheus,Player,Client", stores_dir=None):
    clock = clock or Clock()
    eye = Eye(player, clock=clock, source="fixture", stores_dir=stores_dir)
    eye.set_connected(True)
    for u in (A10, T72_A, T72_B, SA8, WINGMAN):
        eye.on_units_message(fx.units_message(u))
    eye.set_airbases(fx.AIRBASES)
    return eye, clock


def ammo(gbu=2, mav=2, rounds=1150, rockets=7):
    return {"fuel": 0.61, "ammo": [
        {"type": "GBU_12", "name": "GBU-12", "count": gbu, "category": 3},
        {"type": "AGM_65D", "name": "AGM-65D", "count": mav, "category": 1},
        {"type": "HYDRA_70_M151", "name": "Hydra 70 M151", "count": rockets, "category": 2},
        {"type": "weapons.shells.GAU8_30_HE", "name": "30mm HE", "count": rounds // 2, "category": 0},
        {"type": "weapons.shells.GAU8_30_AP", "name": "30mm AP", "count": rounds - rounds // 2, "category": 0},
    ]}


class SpokenCallTests(unittest.TestCase):
    def test_words(self):
        self.assertEqual(words(0), "zero")
        self.assertEqual(words(15), "fifteen")
        self.assertEqual(words(21), "twenty one")
        self.assertEqual(words(120), "one hundred twenty")
        self.assertEqual(digit_words("038"), "zero three eight")
        self.assertEqual(range_words(0.3), "less than one mile")
        self.assertEqual(range_words(1.2), "one mile")
        self.assertEqual(range_words(11.6), "twelve miles")

    def test_required_order_air_and_ground(self):
        self.assertEqual(call_line("bandit", "038", 12.2, 10, "air", 15200, "high"),
                         "Bandit, zero three eight, twelve, angels fifteen, ten o'clock high.")
        self.assertIn(", one mile, ", call_line("bandit", "100", 1.2, 2, "air", 4000, "low"))
        self.assertEqual(call_line("enemy armor", "038", 3.0, 11, "ground"),
                         "Enemy armor, bearing zero three eight, three miles, ground target, eleven o'clock.")
        self.assertIn("surface target", call_line("enemy ship", "100", 20, 2, "sea"))
        self.assertIn("angels zero", call_line("bandit", "100", 5, 2, "air", 400, "low"))

    def test_picture_groups_and_airbases_have_calls(self):
        eye, _ = scenario()
        pic = eye.picture()
        armor = [g for g in pic["hostile_groups"] if "T-72B" in g["types"]][0]
        self.assertEqual(armor["call"], "Enemy armor, two contacts, bearing zero nine zero, three miles, ground "
                                        "target, twelve o'clock.")
        wing = [g for g in pic["friendly_groups"] if "A-10C_2" in g["types"]][0]
        self.assertTrue(wing["call"].startswith("Friendly A-10C, two seven zero, two miles, angels ten, "))
        ab = pic["airbases"]["nearest"][0]
        self.assertTrue(ab["call"].startswith("Kutaisi, bearing zero nine zero, one mile, at your twelve o'clock"))
        for g in pic["hostile_groups"] + pic["friendly_groups"]:
            self.assertRegex(g["call"], r"(bearing )?[a-z]+ [a-z]+ [a-z]+, .*o'clock")  # air calls: no 'bearing'


class SinglePlayerAttributionTests(unittest.TestCase):
    def test_player_name_from_list_owns_shots_and_kills(self):
        eye, clock = scenario()
        self.assertEqual(eye.ownship()["id"], 100)
        eye.on_event(fx.shot(A10, 9001, "AGM_65D"))
        clock.t += 8
        eye.on_event(fx.kill(A10, 9001, "AGM_65D", T72_A))
        pic = eye.picture()
        s = pic["my_shots_last_15_min"]["shots"][0]
        self.assertEqual((s["weapon"], s["result"]), ("AGM-65D", "kill"))
        self.assertEqual(pic["my_kills_this_mission"][0]["target"], "T-72B")
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-72B"])
        self.assertTrue(pic["recent_kills_and_losses_last_10_min"][0]["by_me"])

    def test_only_player_in_mission_is_ownship_even_with_unlisted_name(self):
        eye, clock = scenario(player="Prometheus")  # pilot list WITHOUT "player"
        self.assertEqual(eye.ownship()["id"], 100)
        eye.on_event(fx.shot(A10, 9002, "GBU_12"))
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["total"], 1)

    def test_initiator_matched_by_ownship_unit_id_without_player_name(self):
        eye, clock = scenario()
        anon = dict(A10)
        anon.pop("player_name")
        eye.on_event(fx.shot(anon, 9003, "AGM_65D"))
        self.assertEqual(eye.picture()["my_shots_last_15_min"]["total"], 1)


class VanishTests(unittest.TestCase):
    def test_vanish_without_death_event_is_inferred_loss(self):
        eye, clock = scenario()
        clock.t += 5
        eye.on_units_message(fx.gone_message(803, "SAM-1"))
        rows = eye.picture()["recent_kills_and_losses_last_10_min"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "no longer showing, likely destroyed")
        self.assertTrue(rows[0]["inferred"])
        self.assertEqual(rows[0]["victim"]["type"], "Osa 9A33 ln")
        self.assertIn("bearing_true", rows[0]["where"])

    def test_vanish_with_death_event_is_not_duplicated(self):
        eye, clock = scenario()
        eye.on_event(fx.dead(SA8))
        eye.on_units_message(fx.gone_message(803, "SAM-1"))
        rows = eye.picture()["recent_kills_and_losses_last_10_min"]
        self.assertEqual([r["event"] for r in rows], ["destroyed"])

    def test_player_leaving_is_not_a_loss(self):
        eye, clock = scenario()
        eye.on_units_message(fx.gone_message(100, "Pilot #001"))
        self.assertEqual(eye.picture()["recent_kills_and_losses_last_10_min"], [])

    def test_my_shot_then_target_vanishes_is_confirmed_kill_with_splash(self):
        """The 19:09 case: events silent, the armor just stopped showing 20 s after the pilot's bomb. Round 4:
        that is a CONFIRMED kill (still flagged inferred), splash without 'likely'."""
        eye, clock = scenario()
        eye.on_event(fx.shot(A10, 9004, "GBU_12"))
        clock.t += 20
        eye.on_units_message(fx.gone_message(801, "Armor-1"))
        pic = eye.picture()
        s = pic["my_shots_last_15_min"]["shots"][0]
        self.assertEqual(s["result"], "kill")
        self.assertEqual(s["no_longer_showing"], ["T-72B"])
        kill = pic["my_kills_this_mission"][0]
        self.assertTrue(kill["inferred"])
        self.assertEqual(kill["confidence"], "confirmed")
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-72B"])
        row = pic["recent_kills_and_losses_last_10_min"][0]
        self.assertTrue(row["by_me"] and row["inferred"])
        self.assertEqual(row["weapon"], "GBU-12")
        # A real kill event later clears the inferred flag.
        eye.on_event(fx.kill(A10, 9004, "GBU_12", T72_A))
        self.assertFalse(eye.picture()["my_kills_this_mission"][0]["inferred"])

    def test_vanish_long_after_the_shot_stays_likely(self):
        eye, clock = scenario()
        eye.on_event(fx.shot(A10, 9004, "GBU_12"))
        clock.t += 90
        eye.on_units_message(fx.gone_message(801, "Armor-1"))
        pic = eye.picture()
        self.assertTrue(pic["my_shots_last_15_min"]["shots"][0]["result"].startswith("likely kill"))
        self.assertEqual(pic["my_kills_this_mission"][0]["confidence"], "likely")
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-72B, likely"])
        # A real kill event upgrades it to confirmed.
        eye.on_event(fx.kill(A10, 9004, "GBU_12", T72_A))
        k = eye.picture()["my_kills_this_mission"][0]
        self.assertEqual((k["inferred"], k["confidence"]), (False, "confirmed"))


class StreamHealthTests(unittest.TestCase):
    def test_states_and_rate(self):
        eye, clock = scenario()
        self.assertEqual(eye.stream_health()["state"], "never received")
        for i in range(10):
            eye.on_event({"time": float(i), "simulation_fps": {"average": 60.0}})  # noise still proves liveness
        h = eye.stream_health()
        self.assertEqual((h["state"], h["events_total"], h["events_per_min"]), ("live", 10, 2.0))
        clock.t += 200
        eye.on_units_message(fx.units_message(A10))  # units keep flowing
        self.assertTrue(eye.stream_health()["state"].startswith("stale"))
        summary = eye.summary()
        self.assertTrue(summary["events"].startswith("stale"))
        self.assertEqual(summary["last_event_age_s"], 200.0)


class FakeCall:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


class FeederLivenessTests(unittest.TestCase):
    def feeder(self, eye):
        return GrpcFeeder(eye, "127.0.0.1", 1, events_stale_s=120)

    def test_watchdog_resubscribes_silent_event_stream_once_per_window(self):
        eye, clock = scenario()
        f = self.feeder(eye)
        call = FakeCall()
        f._events_call, f._events_subscribed = call, clock.t
        self.assertIsNone(f.check_liveness())  # fresh subscription
        clock.t += 130
        eye.on_units_message(fx.units_message(A10))
        self.assertEqual(f.check_liveness(), "events_stale")
        self.assertEqual(call.cancelled, 1)
        self.assertEqual(eye.stream_health()["resubscribes"], {"events_stale": 1})
        self.assertIsNone(f.check_liveness())  # not again right away
        clock.t += 130
        eye.on_units_message(fx.units_message(A10))
        eye.on_event({"time": 1.0, "simulation_fps": {"average": 60.0}})
        self.assertIsNone(f.check_liveness())  # events flowing again

    def test_no_resubscribe_when_units_are_stale_too(self):
        eye, clock = scenario()
        f = self.feeder(eye)
        f._events_call, f._events_subscribed = FakeCall(), clock.t
        clock.t += 500  # DCS gone: nothing flows, the reconnect loop handles it
        self.assertIsNone(f.check_liveness())

    def test_reconnect_of_any_stream_resubscribes_the_others(self):
        eye, clock = scenario()
        f = self.feeder(eye)
        air, ground, events = FakeCall(), FakeCall(), FakeCall()
        f._unit_calls = {air, ground}
        f._events_call = events
        f._live("units-air", air)  # first connect: nothing to do
        self.assertEqual((air.cancelled, ground.cancelled, events.cancelled), (0, 0, 0))
        f._errored.add("units-air")
        f._live("units-air", air)  # reconnect after a failure (e.g. DCS client reboot)
        self.assertEqual((air.cancelled, ground.cancelled, events.cancelled), (0, 1, 1))
        self.assertEqual(eye.stream_health()["resubscribes"], {"reconnect:units-air": 1})
        f._live("units-air", air)  # only once per failure
        self.assertEqual(events.cancelled, 1)


class LoadoutTests(unittest.TestCase):
    def test_snapshot_loadout_and_fuel(self):
        eye, clock = scenario()
        eye.on_stores(A10, ammo())
        own = eye.picture()["ownship"]
        self.assertEqual(own["fuel_fraction"], 0.61)
        lo = own["loadout"]
        self.assertTrue(lo["available"])
        self.assertEqual(lo["gun_rounds"], 1150)
        self.assertEqual({w["weapon"]: w["count"] for w in lo["weapons"]},
                         {"GBU-12": 2, "AGM-65D": 2, "Hydra 70 M151": 7})

    def test_unavailable_reason_reported(self):
        eye, _ = scenario()
        eye.set_stores_status(GrpcFeeder.EVAL_OFF)
        lo = eye.picture()["ownship"]["loadout"]
        self.assertFalse(lo["available"])
        self.assertIn("evalEnabled = true", lo["reason"])

    def test_diff_names_weapon_when_shot_event_lacks_it(self):
        eye, clock = scenario()
        eye.on_stores(A10, ammo())
        eye.on_event({"time": 100.0, "shot": {"initiator": {"unit": A10}}})  # no weapon in the event
        clock.t += 3
        used = eye.on_stores(A10, ammo(mav=1))
        self.assertEqual([(u["weapon"], u["count"]) for u in used], [("AGM-65D", 1)])
        pic = eye.picture()
        shots = pic["my_shots_last_15_min"]["shots"]
        self.assertEqual(len(shots), 1)
        self.assertEqual((shots[0]["weapon"], shots[0]["weapon_from"]), ("AGM-65D", "loadout change"))
        self.assertEqual(pic["my_weapons_used_this_mission"],
                         {"source": "stores (loadout diff)", "by_weapon": {"AGM-65D": 1}})

    def test_diff_without_any_shot_event_creates_the_shot(self):
        """Event stream down: the stores drop is the only evidence of the attack."""
        eye, clock = scenario()
        eye.on_stores(A10, ammo())
        clock.t += 3
        eye.on_stores(A10, ammo(gbu=0))
        pic = eye.picture()
        s = pic["my_shots_last_15_min"]["shots"][0]
        self.assertEqual((s["weapon"], s["kind"], s["count"]), ("GBU-12", "bomb", 2))
        self.assertEqual(s["seen_by"], "loadout change only (no shot event)")
        self.assertEqual(s["intended_target"]["type"], "T-72B")
        clock.t += 25
        eye.on_units_message(fx.gone_message(801, "Armor-1"))
        pic = eye.picture()
        self.assertEqual(pic["my_kills_this_mission"][0]["weapon"], "GBU-12")
        self.assertEqual(pic["splash_to_announce"], ["Splash one T-72B"])  # 25 s after a bomb: confirmed

    def test_shot_event_after_stores_drop_merges(self):
        eye, clock = scenario()
        eye.on_stores(A10, ammo())
        clock.t += 1
        eye.on_stores(A10, ammo(mav=1))
        clock.t += 1
        eye.on_event(fx.shot(A10, 9005, "AGM_65D"))
        shots = eye.picture()["my_shots_last_15_min"]["shots"]
        self.assertEqual(len(shots), 1)
        self.assertNotIn("seen_by", shots[0])

    def test_gun_burst_rounds_from_diff_and_rearm_is_not_use(self):
        eye, clock = scenario()
        eye.on_stores(A10, ammo())
        eye.on_event(fx.shooting("shooting_start", A10, "GAU8_30_HE"))
        clock.t += 2
        eye.on_event(fx.shooting("shooting_end", A10, "GAU8_30_HE"))
        clock.t += 1
        eye.on_stores(A10, ammo(rounds=1000))
        s = eye.picture()["my_shots_last_15_min"]["shots"][0]
        self.assertEqual((s["kind"], s["weapon"]), ("gun", "gun, 150 rounds"))
        clock.t += 60
        self.assertEqual(eye.on_stores(A10, ammo(rounds=1150)), [])  # rearmed
        self.assertEqual(eye.picture()["ownship"]["loadout"]["gun_rounds"], 1150)

    def test_player_shot_and_birth_wake_the_stores_poll(self):
        eye, clock = scenario()
        woke = []
        eye.on_player_shot = lambda: woke.append(1)
        eye.on_event(fx.shot(A10, 9006, "AGM_65D"))
        eye.on_event({"time": 1.0, "birth": {"initiator": {"unit": A10}}})
        eye.on_event(fx.shot(fx.UNITS[1], 9007, "R-73"))  # AI shot: no stores poll
        self.assertEqual(len(woke), 2)

    def test_durable_stores_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            eye, clock = scenario(stores_dir=tmp)
            eye.on_stores(A10, ammo())
            eye.on_event(fx.shot(A10, 9008, "AGM_65D"))
            clock.t += 3
            eye.on_stores(A10, ammo(mav=1))
            clock.t += 5
            eye.on_event(fx.kill(A10, 9008, "AGM_65D", T72_A))
            files = os.listdir(tmp)
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0], r"^stores-\d{8}\.jsonl$")
            path = os.path.join(tmp, files[0])
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            rows = [json.loads(line) for line in open(path, encoding="utf-8")]
            rows = [r for r in rows if r["kind"] != "raw_event_sample"]  # round 4 raw-event debug samples
            kinds = [r["kind"] for r in rows]
            self.assertEqual(kinds, ["stores_snapshot", "shot", "stores_change", "shot_weapon", "shot_result",
                                     "kill_by_me"])
            change = rows[2]["changes"][0]
            self.assertEqual((change["weapon"], change["from"], change["to"]), ("AGM-65D", 2, 1))
            self.assertEqual(rows[-1]["target"]["type"], "T-72B")
            self.assertEqual(rows[-1]["weapon"], "AGM_65D")


class FakeEval:
    """CustomService stub stand-in."""
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def Eval(self, request, timeout=None):
        self.calls.append(request.lua)
        if self.error:
            raise self.error
        return type("R", (), {"json": json.dumps(self.payload)})()


class FakeRpcError(Exception):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code


class PollStoresTests(unittest.TestCase):
    def test_poll_feeds_eye_with_lua_for_ownship(self):
        eye, _ = scenario()
        f = GrpcFeeder(eye, "127.0.0.1", 1)
        stub = FakeEval(ammo())
        self.assertTrue(f.poll_stores(stub))
        self.assertIn('Unit.getByName("Pilot #001")', stub.calls[0])
        self.assertIn("getAmmo", stub.calls[0])
        self.assertTrue(eye.picture()["ownship"]["loadout"]["available"])

    def test_eval_disabled_is_reported_clearly(self):
        eye, _ = scenario()
        f = GrpcFeeder(eye, "127.0.0.1", 1)
        self.assertFalse(f.poll_stores(FakeEval(error=FakeRpcError("StatusCode.PERMISSION_DENIED"))))
        self.assertFalse(f.eval_ok)
        self.assertIn("evalEnabled", eye.picture()["ownship"]["loadout"]["reason"])


class WhisperPromptTests(unittest.TestCase):
    def test_vocabulary_has_callsign_and_nearest_airfields(self):
        eye, _ = scenario()
        text = eye.whisper_vocabulary(max_airbases=3)
        self.assertTrue(text.startswith("Prometheus, Athena."))
        self.assertIn("Airfields: Kutaisi, Kobuleti, Senaki-Kolkhi.", text)  # nearest first
        self.assertNotIn("FARP", text)
        self.assertIn("splash", text)

    def test_feeder_writes_private_file_only_on_change(self):
        eye, _ = scenario()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "whisper-prompt-dynamic.txt")
            f = GrpcFeeder(eye, "127.0.0.1", 1, prompt_path=path)
            self.assertTrue(f.write_prompt())
            self.assertFalse(f.write_prompt())
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path, encoding="utf-8") as fh:
                self.assertIn("Kutaisi", fh.read())


class FakeTranscript:
    def __init__(self):
        self.records = []
        self.lock = threading.Lock()

    def log(self, **record):
        with self.lock:
            self.records.append(record)
        return True


def req(text, tid="t1", speaker="player"):
    return Request(tid, text, speaker, 133000000, "AM", "2026-11-07T00:00:00Z", "2026-11-07T00:00:00Z")


class ControllerRound2Tests(unittest.TestCase):
    def test_summary_splash_once_and_empty_count(self):
        eye, clock = scenario()
        eye.on_stores(A10, ammo())
        eye.on_event(fx.shot(A10, 9009, "AGM_65D"))
        eye.on_event(fx.kill(A10, 9009, "AGM_65D", T72_A))
        seen = []

        def llm(messages):
            seen.append(json.loads([m for m in messages if m["content"].startswith("PICTURE ")][0]["content"][8:]))
            return "Prometheus, Athena, copy, splash one T-72B."  # marked said only when the model says it
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm)
        list(c.stream(req("Athena, acknowledge my last transmission.")))
        list(c.stream(req("-")))
        list(c.stream(req("how is it looking", tid="t3")))  # phase 0: fixed replies (loadout) skip the model
        self.assertEqual(seen[0]["splash_to_announce"], ["Splash one T-72B"])
        self.assertNotIn("_splash_keys", seen[0])
        self.assertNotIn("splash_to_announce", seen[1])  # announced once
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        summary = tool["result"]["summary"]
        self.assertEqual(summary["events"], "live")
        self.assertIn("events_per_min", summary)
        self.assertEqual(summary["loadout"], {"GBU-12": 2, "AGM-65D": 2, "Hydra 70 M151": 7})
        self.assertEqual(summary["gun_rounds"], 1150)
        self.assertEqual(summary["my_kills"], 1)
        empty = [r for r in c.transcript.records if r.get("outcome") == "empty_transcript"][0]
        self.assertEqual(empty["counts"], {"empty_transcripts": 1, "transmissions": 2})


if __name__ == "__main__":
    unittest.main()
