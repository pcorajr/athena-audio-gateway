"""Phase 1 integration (radio lane x Eye lane), end to end on recorded 2026-11-09 data.

Fixture tests/fixtures/p1_integration_20261109.json comes from the Memory DB (read-only): mission "F4-E - Nevada -
Range Reloaded", 20:36-20:37 local: the real SA-2 site (Ground-8), the pilot's F-4E track from the cockpit packets,
the real SA-2 shot event at 20:37:00 and the live Eval scan rows recorded for that missile (range, clock, speed; the
recording kept no weapon id or position, so the replay gives it one id and places it by heading + clock, as the
athena-eye-offline-replay method does). Pilot transmissions are the real transcript-20261109 texts.

No feeder thread runs here: nothing calls Eye.tick() except the controller (picture() is a pure read), so these tests
also prove the controller still raises SAM alerts and settles shots on its own.
"""
import json
import math
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from integrations.hermes_radio.eye import Eye, GrpcFeeder
from integrations.hermes_radio.simple_controller import SimpleController
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req
from integrations.hermes_radio.transcript import Transcript

FIX = json.loads((Path(__file__).parent / "fixtures" / "p1_integration_20261109.json").read_text())
LINES = FIX["pilot_lines"]  # real 11-09 transmissions -> time they were heard
PAINTED, LAUNCH, ANOTHER, LOADOUT, RADIO = ("Athena, I'm getting painted.", "Athena, missile launch.",
                                            "Another missile.", "Athena, what am I flying and what's my loadout?",
                                            "Athena, radio check.")
SHOT_T = FIX["shot"][0]
SHOT_HMS = 20 * 3600 + 37 * 60  # the SA-2 shot event was received at 20:37:00 local, the transcripts' clock
MISSILE_ID = 16780001  # replay id for the one SA-2 in the recorded scan


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def offset(lat, lon, brg, nm):
    d = nm / 60.0
    return lat + d * math.cos(math.radians(brg)), lon + d * math.sin(math.radians(brg)) / math.cos(math.radians(lat))


class Replay:
    """The recorded mission at 1 Hz: units, the pilot (unit + cockpit packet), the shot event, the scan rows."""

    def __init__(self):
        self.clock = Clock(FIX["cockpit"][0][0])
        self.eye = Eye("Prometheus,Player,Client", clock=self.clock, source="replay 2026-11-09")
        self.eye.set_connected(True)
        self.shot_done = False
        self.send_units()
        self.pilot()

    def send_units(self):
        for u in FIX["units"]:
            self.eye.on_units_message({"time": 0.0, "unit": u})

    def packet(self):
        rows = FIX["cockpit"]
        t = self.clock.t
        for (ta, a), (tb, b) in zip(rows, rows[1:]):
            if ta <= t <= tb:
                f = (t - ta) / (tb - ta)
                out = dict(a)
                for k in ("lat", "lon", "alt"):
                    out[k] = a[k] + f * (b[k] - a[k])
                out["hdg"] = (a["hdg"] + f * ((((b["hdg"] - a["hdg"]) + 180) % 360) - 180)) % 360
                return out
        return dict(rows[-1][1] if t > rows[-1][0] else rows[0][1])

    def pilot(self):
        p = self.packet()
        u = json.loads(json.dumps(FIX["pilot"]))
        u["position"].update(lat=p["lat"], lon=p["lon"], alt=p["alt"])
        u["velocity"] = {"heading": p["hdg"], "speed": p["gs"]}
        self.eye.on_units_message({"time": 0.0, "unit": u})
        self.eye.on_cockpit_packet(dict(p, t=self.clock.t))
        return p

    def scan_rows(self, p):
        rows = [m for t, m in FIX["scan"] if t <= self.clock.t < t + 1.5]
        out = []
        for m in rows[-1:]:
            brg = (p["hdg"] + 30 * m["clock"]) % 360
            lat, lon = offset(p["lat"], p["lon"], brg, m["range_nm"])
            out.append({"id": MISSILE_ID, "type": m["type"], "lat": lat, "lon": lon, "alt": 2000, "coal": 1,
                        "vn": -math.cos(math.radians(brg)) * 600, "ve": -math.sin(math.radians(brg)) * 600,
                        "target": FIX["pilot"]["name"] if m["targeting_me"] else None, "launcher": None,
                        "launcher_type": "SNR_75V"})
        return out

    def advance(self, to_t):
        while self.clock.t < to_t:
            self.clock.t = min(to_t, self.clock.t + 1.0)
            if not self.shot_done and SHOT_T <= self.clock.t:
                self.eye.on_event(FIX["shot"][1])
                self.shot_done = True
            p = self.pilot()
            self.eye.on_weapons(self.scan_rows(p))  # the 1 Hz Eval scan (no feeder: no tick() here)

    def at(self, line):
        """Advance to the second the pilot really said `line` on 11-09 (local clock of the recording)."""
        hh, mm, ss = (int(x) for x in LINES[line][11:19].split(":"))
        self.advance(SHOT_T + (hh * 3600 + mm * 60 + ss) - SHOT_HMS)


class Stub:
    def __init__(self, answer="Prometheus, Athena, copy."):
        self.answer, self.seen = answer, []

    def __call__(self, messages):
        self.seen.append(messages)
        return self.answer

    def picture(self, n=-1):
        return json.loads([m for m in self.seen[n] if m["content"].startswith("PICTURE ")][0]["content"][8:])


def controller(eye, llm=None, transcript=None):
    return SimpleController(None, transcript or FakeTranscript(), model="test", api_key="x", eye=eye, llm=llm or Stub())


def say(c, text, tid):
    return list(c.stream(req(text, tid=tid)))[0]["speech"]


def outcome(transcript, tid, value):
    """What the Go gateway appends after a reply: here a say-again fallback instead of the reply."""
    transcript.append({"kind": "exchange_outcome", "transmission_id": tid, "outcome": value, "route": "fallback"})


class LaunchDeliveryTests(unittest.TestCase):
    def test_sa2_launch_one_call_unsent_reply_rearms_it_for_the_next_reply(self):
        r = Replay()
        r.advance(SHOT_T + 3)
        launches = [a for a in r.eye.threat_alerts if a["kind"] == "missile launch"]
        self.assertEqual(len(launches), 1)  # shot event + live scan = ONE missile, ONE launch call
        with tempfile.TemporaryDirectory() as root:
            transcript = Transcript(Path(root) / "radio")
            c = controller(r.eye, transcript=transcript)
            first = say(c, LAUNCH, "l-1")
            self.assertEqual(first.count("Missile launch, SA-2"), 1, first)
            self.assertIn(launches[0]["key"], r.eye.announced)
            outcome(transcript, "l-1", "say_again_fallback_sent:bridge_failed")  # the pilot heard "say again"
            r.advance(SHOT_T + 5)
            second = say(c, ANOTHER, "l-2")
            self.assertIn("Missile launch, SA-2", second)  # pending again: spoken on the next reply
            self.assertNotIn("Same missile", second)  # the unheard launch reply left no 'already called' trace
            self.assertTrue(any(rec.get("route") == "rearm" for rec in self._records(root)))
            r.advance(SHOT_T + 6)
            third = say(c, ANOTHER, "l-3")  # l-2 has no gateway record: delivered
            self.assertNotIn("Missile launch, SA-2", third)  # said once, not again
            self.assertIn("Same missile", third)
        self.assertEqual(len([a for a in r.eye.threat_alerts if a["kind"] == "missile launch"]), 1)

    @staticmethod
    def _records(root):
        return [json.loads(line) for f in (Path(root) / "radio").glob("*.jsonl") for line in f.read_text().splitlines()]


class SamWarningTests(unittest.TestCase):
    def kamaz_near_pilot(self, r):
        p = r.packet()
        lat, lon = offset(p["lat"], p["lon"], p["hdg"], 1.0)  # one mile off the nose
        r.eye.on_units_message({"time": 0.0, "unit": {
            "id": 99001, "name": "Convoy-1-1", "callsign": "1", "type": "KAMAZ Truck", "coalition": "COALITION_RED",
            "group": {"id": 99000, "name": "Convoy-1", "category": "GROUP_CATEGORY_GROUND",
                      "coalition": "COALITION_RED"}, "position": {"lat": lat, "lon": lon, "alt": 900.0}}})

    def test_kamaz_near_the_pilot_gives_no_sam_warning_and_range_rows_say_could_be(self):
        r = Replay()
        r.at(PAINTED)
        self.kamaz_near_pilot(r)
        self.assertNotIn("rwr_alert_to_announce", r.eye.picture())  # pure picture, no feeder: nothing raised yet
        stub = Stub()
        c = controller(r.eye, stub)
        say(c, PAINTED, "p-1")
        pic = stub.picture()
        rows = pic["threats"]["sam_in_range"]
        self.assertEqual([x["group"] for x in rows], ["Ground-8"])  # the real SA-2 site only, never the truck
        self.assertEqual(rows[0]["confidence"], "range only: could be tracking")
        alerts = pic.get("rwr_alert_to_announce") or []
        self.assertTrue(any(a.startswith("SAM SA-2 could be tracking you") for a in alerts), alerts)  # tick() ran
        self.assertFalse([a for a in alerts if "KAMAZ" in a or "Convoy" in a], alerts)
        system = " ".join(m["content"] for m in stub.seen[0] if m["role"] == "system")
        self.assertIn('"tracking" only when its confidence is DCS radar', system)

    def test_radar_reported_tracking_makes_the_warning_say_tracking(self):
        r = Replay()
        r.at(PAINTED)
        snr = next(u["name"] for u in FIX["units"] if u["type"] == "SNR_75V")
        r.eye.on_radars([{"name": snr, "has_radar": True, "on": True, "tracking_me": True, "detected": True}])
        c = controller(r.eye)
        speech = say(c, LOADOUT, "p-2")  # fixed route: the pending SAM warning goes first
        self.assertIn("SAM SA-2 tracking you", speech)
        self.assertNotIn("could be", speech)
        pic = r.eye.picture()
        self.assertEqual(pic["threats"]["sam_source"], "dcs_radar")
        self.assertEqual(pic["threats"]["sam_radar"]["tracking_you"][0]["group"], "Ground-8")
        self.assertEqual(pic["threats"]["sam_in_range"][0]["confidence"], "DCS radar: tracking you")


class MissionMemoryTests(unittest.TestCase):
    def test_new_mission_clears_history_reconnect_keeps_it(self):
        r = Replay()
        feeder = GrpcFeeder(r.eye, "fixture.invalid")
        session = lambda sid: SimpleNamespace(GetSessionId=lambda *a, **k: SimpleNamespace(session_id=sid))  # noqa: E731
        c = controller(r.eye, Stub())
        say(c, RADIO, "m-1")
        feeder._check_session(session(111))  # DCS connects after the radio is up: first session id, same mission
        r.advance(r.clock.t + 1)
        say(c, RADIO, "m-2")
        self.assertEqual(len(c.history), 2)
        # reconnect: the unit stream drops and resubscribes; units are re-sent; the sweep runs; same session id
        r.eye.set_connected(False, "UNAVAILABLE", stream="units_ground")
        r.advance(r.clock.t + 3)
        r.eye.set_connected(True, stream="units_ground")
        r.eye.begin_unit_sync("GROUP_CATEGORY_GROUND", r.clock.t, 2.0, stream="units_ground")
        r.advance(r.clock.t + 1)
        r.send_units()
        r.advance(r.clock.t + 3)
        r.eye.tick()
        self.assertIsNotNone(r.eye.last_sweep)
        feeder._check_session(session(111))
        say(c, RADIO, "m-3")
        self.assertEqual(len(c.history), 3)
        self.assertFalse(any(rec.get("route") == "reset" for rec in c.transcript.records))
        # new mission: mission_start event (no Memory attached), then a new DCS session id
        r.advance(r.clock.t + 1)
        r.eye.on_event({"time": 0.0, "mission_start": {}})
        say(c, RADIO, "m-4")
        self.assertEqual(len(c.history), 1)
        r.advance(r.clock.t + 1)
        say(c, RADIO, "m-5")
        with mock.patch.object(feeder, "resubscribe_units"):
            feeder._check_session(session(222))
        say(c, RADIO, "m-6")
        self.assertEqual(len(c.history), 1)
        self.assertEqual(sum(1 for rec in c.transcript.records if rec.get("route") == "reset"), 2)


class KillReportTests(unittest.TestCase):
    def test_kill_confirm_says_unconfirmed_when_combat_events_are_missing(self):
        r = Replay()
        r.at(PAINTED)
        pic = r.eye.picture()
        pic["data_health"] = dict(pic["data_health"], combat_events="missing: 2 of my shots seen only as loadout "
                                                                     "drops, no shot event")

        class Fixed:
            memory = None

            def picture(self):
                return json.loads(json.dumps(pic))
        c = controller(Fixed())
        speech = say(c, "Athena, confirm kills", "k-1")
        self.assertIn("kills may be unconfirmed", speech)
        self.assertNotIn("No kills showing", speech)
        pic["data_health"]["combat_events"] = "ok"
        self.assertIn("No kills showing", say(controller(Fixed()), "Athena, confirm kills", "k-2"))

    def test_pilot_kill_report_never_creates_a_kill_the_eye_decides(self):
        r = Replay()
        r.at(PAINTED)
        c = controller(r.eye)
        say(c, "Athena, splash one SA-2", "k-3")
        self.assertEqual(r.eye.picture()["my_kills_this_mission"], [])
        tool = [rec for rec in c.transcript.records if rec["kind"] == "tool"][0]
        self.assertEqual(tool["result"]["pilot_report"], {"confirmed": None, "how": "not recorded: no DCS kill to confirm"})


if __name__ == "__main__":
    unittest.main()
