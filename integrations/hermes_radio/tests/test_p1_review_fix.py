"""Phase 1 review fix (REVIEW-P1): never hide a threat.

Owner's rule: a missed real threat is worse than a false alarm. New DCS data may only ADD confidence, or remove a
warning when DCS positively says so in a way that cannot hide an ambush. Each test reproduces a reviewer finding
and fails on 29b34da:
1. silent SAMs: a group whose radars DCS reports 'off' (EMCON / alarm state) was dropped from sam_in_range;
2. uncalled launch: with the scan 'on' at the shot, a scan that then stalls left the launch unannounced;
3. cockpit allowlist: in server mode the client PC (where AthenaExport.lua runs) was blocked, silently;
minors: Codex byte trickle past its 6 s limit, API body trickle past the deadline, attributes downgrading a SAM.
"""
import http.client
import http.server
import json
import math
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

from integrations.hermes_radio import cockpit as cp
from integrations.hermes_radio import codex_llm
from integrations.hermes_radio import eye as eye_mod
from integrations.hermes_radio.eye import Eye, classify
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye_round4 import SA10_FAR, SA11_LN, SA11_SR, f4e_eye, f4e_packet

GROUND = "GROUP_CATEGORY_GROUND"


class Isolated(unittest.TestCase):
    def setUp(self):
        eye_mod.TYPE_INFO.clear()
        self.addCleanup(eye_mod.TYPE_INFO.clear)


def launch_alerts(eye):
    return [a for a in eye.threat_alerts if a["kind"] == "missile launch"]


def announce(eye):
    return eye.picture().get("rwr_alert_to_announce", [])


# ============================================================================= 1. silent SAMs
class SilentSamTests(Isolated):
    def eye(self, extra=(SA11_SR, SA11_LN)):
        eye, clock = f4e_eye(extra=extra)
        eye.on_cockpit_packet(f4e_packet())
        return eye, clock

    def test_radar_off_inside_range_still_warns_could_be_tracking(self):
        """Reviewer repro: every polled radar of the SA-11 group 'off', Prometheus 10 nm inside its 19 nm ring."""
        eye, clock = self.eye()
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": False},
                       {"name": "SAM LN", "has_radar": True, "on": False}])
        eye.tick()
        rows = eye.picture()["threats"]["sam_in_range"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confidence"], "range only: could be tracking")
        self.assertTrue(rows[0]["call"].startswith("SAM SA-11 could be tracking you"), rows[0]["call"])
        self.assertEqual(rows[0]["radar"], "off")  # DCS's radar state is shown, never used to hide the site
        self.assertEqual(announce(eye), [rows[0]["call"]])
        # the radar comes on and locks: DCS upgrades it to a fresh 'tracking you' call
        clock.t += 1
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": True, "tracking_me": True}])
        eye.tick()
        self.assertTrue(announce(eye)[0].startswith("SAM SA-11 tracking you"))

    def test_radar_searching_inside_range_still_warns(self):
        eye, _ = self.eye()
        eye.on_radars([{"name": "SAM SR", "has_radar": True, "on": True, "detected": False}])
        eye.tick()
        row = eye.picture()["threats"]["sam_in_range"][0]
        self.assertEqual(row["confidence"], "range only: could be tracking")
        self.assertEqual(row["radar"], "searching")
        self.assertEqual(announce(eye), [row["call"]])

    def test_radar_off_outside_range_and_dead_groups_are_not_listed(self):
        eye, _ = self.eye(extra=(SA10_FAR, SA11_SR, SA11_LN))
        eye.on_radars([{"name": "S300 SR", "has_radar": True, "on": False}])
        eye.tick()
        self.assertEqual([r["group"] for r in eye.picture()["threats"]["sam_in_range"]], ["SA-11 East"])
        eye.on_gone(SA11_SR["id"])
        eye.on_gone(SA11_LN["id"])
        eye.tick()
        self.assertEqual(eye.picture()["threats"]["sam_in_range"], [])

    def test_attributes_never_downgrade_a_named_sam(self):
        """Minor (d): DCS attributes without the listed AD names must not turn an SA-11 into a truck."""
        eye, _ = self.eye()
        eye.note_type_info({"SA-11 Buk SR 9S18M1": {"attributes": ["Ground vehicles", "Air Defence"]},
                            "SA-11 Buk LN 9A310M1": {"attributes": ["Vehicles"]},
                            "Mystery sam": {"attributes": ["Trucks"]}})
        self.assertEqual(eye.units[SA11_SR["id"]]["category"], "SAM")
        self.assertEqual(classify("SA-11 Buk LN 9A310M1", GROUND), "SAM")
        self.assertEqual(classify("Mystery sam", GROUND), "SAM")
        self.assertEqual(classify("KAMAZ Truck", GROUND), "ground vehicle")  # explicit override still wins
        eye.tick()
        self.assertEqual([r["group"] for r in eye.picture()["threats"]["sam_in_range"]], ["SA-11 East"])


# ============================================================================= 2. launch without a scan
class LaunchWithoutScanTests(Isolated):
    def shot(self, wid, t=50.0, heading=281.0, rng=10.2, brg=101):
        lat, lon = fx.offset(brg, rng)
        return {"time": t, "shot": {"initiator": {"unit": SA11_LN}, "weapon": {
            "id": wid, "type": "SA2V755", "position": {"lat": lat, "lon": lon, "alt": 400.0},
            "velocity": {"heading": heading, "speed": 700.0}}}}

    def scan_row(self, wid, rng=9.5):
        lat, lon = fx.offset(100, rng)
        return {"id": wid, "type": "SA2V755", "lat": lat, "lon": lon, "alt": 1500, "vn": 0, "ve": -600, "coal": 1,
                "target": "Aerial-3-2", "launcher": "SAM SR", "launcher_type": "SNR_75V"}

    def eye(self):
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        eye.on_cockpit_packet(f4e_packet())
        return eye, clock

    def test_scan_stalls_after_the_shot_launch_is_still_called_once(self):
        """Reviewer repro (stall.py): one scan before the shot, none after. Base 29b34da: no launch call at all."""
        eye, clock = self.eye()
        eye.on_weapons([])  # the scan answered: 'on'
        clock.t += 0.5
        eye.on_event(self.shot(4294967001))
        self.assertEqual(len(launch_alerts(eye)), 1)  # v15: do not wait for the scan to warn
        for _ in range(3):
            clock.t += 1
            eye.tick()
        alerts = launch_alerts(eye)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["source"], "shot event")
        pic = eye.picture()
        self.assertEqual([m["id"] for m in pic["threats"]["missiles_in_flight"]], ["4294967001"])
        self.assertIn("Missile launch, SA-11, your twelve o'clock, ten miles.", pic["rwr_alert_to_announce"])
        for _ in range(40):
            clock.t += 1
            eye.tick()
        self.assertEqual(len(launch_alerts(eye)), 1)  # once
        self.assertEqual(eye.picture()["threats"]["missiles_in_flight"], [])  # old shot-event timeout passed

    def test_a_scan_asked_before_the_shot_does_not_count_as_newer(self):
        eye, clock = self.eye()
        eye.on_weapons([])
        clock.t += 0.5
        shot_t = clock.t
        eye.on_event(self.shot(4294967002))
        clock.t += 1
        eye.on_weapons([], asked_at=shot_t - 0.2)  # this Eval was sent before the shot, answered after
        clock.t += 2
        eye.tick()
        self.assertEqual(len(launch_alerts(eye)), 1)

    def test_late_scan_takes_over_the_shot_alert_no_second_call(self):
        eye, clock = self.eye()
        eye.on_weapons([])
        eye.on_event(self.shot(4294967003))
        clock.t += 3
        eye.tick()
        self.assertEqual(len(launch_alerts(eye)), 1)
        clock.t += 6  # the scan comes back 9 s after the shot and sees the missile
        eye.on_weapons([self.scan_row(16786500)])
        eye.tick()
        alerts = launch_alerts(eye)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["id"], "16786500")
        self.assertEqual([m["id"] for m in eye.picture()["threats"]["missiles_in_flight"]], ["16786500"])

    def test_working_scan_still_one_alert_per_missile_and_no_ghost_row(self):
        eye, clock = self.eye()
        eye.on_weapons([])
        eye.on_event(self.shot(4294967004))
        clock.t += 1
        eye.on_weapons([self.scan_row(16786600)])
        for _ in range(10):
            clock.t += 1
            eye.on_weapons([self.scan_row(16786600, rng=8.0)])
            eye.tick()
        self.assertEqual(len(launch_alerts(eye)), 1)
        self.assertEqual([m["id"] for m in eye.picture()["threats"]["missiles_in_flight"]], ["16786600"])

    def test_scan_newer_than_the_shot_without_the_missile_drops_the_bridge(self):
        eye, clock = self.eye()
        eye.on_weapons([])
        eye.on_event(self.shot(4294967005))
        clock.t += 1
        eye.on_weapons([])  # a scan after the shot ran and did not see it
        clock.t += 2
        eye.tick()
        self.assertEqual(len(launch_alerts(eye)), 1)  # historical immediate alert remains in the audit
        self.assertFalse([a for a in eye.picture().get("rwr_alert_to_announce", []) if a.startswith("Missile launch")])
        self.assertEqual(eye.picture()["threats"]["missiles_in_flight"], [])
        self.assertEqual(eye.missile_shots, {})

    def test_scan_down_every_launch_called_at_once(self):
        eye, clock = self.eye()
        eye.on_event(self.shot(4294967006))  # no scan at all (Eval off): r11 behaviour
        self.assertEqual(len(launch_alerts(eye)), 1)
        eye.on_weapons([])
        clock.t += eye_mod.WEAPONS_FRESH_S + 1  # scan went stale
        eye.on_event(self.shot(4294967007))
        self.assertEqual(len(launch_alerts(eye)), 2)

    # ------------------------------------------------------------------------- re-review P1b: never worse than r11
    def heard_calls(self, eye):
        """One radio exchange: the pending launch calls, marked announced (the pilot heard them)."""
        pic = eye.picture()
        calls = [c for c in pic.get("rwr_alert_to_announce", []) if c.startswith("Missile")]
        if calls:
            eye.mark_announced(pic.get("_rwr_keys") or [])
        return calls

    def test_salvo_second_missile_after_a_stall_is_called(self):
        """REVIEW-P1b MAJOR (salvo.py): A fired while the scan stalls, called, then gone; B fired from the same site;
        the scan comes back 3 s after B. B's scan row must not inherit A's announced alert. r11 called both."""
        eye, clock = self.eye()
        t0 = clock.t
        heard = []
        for s in range(30):
            clock.t = t0 + s
            if s == 5:
                eye.on_event(self.shot(4294967101))  # missile A at the pilot
            if s == 9:
                eye.on_event(self.shot(4294967102))  # missile B, same launcher (A has hit/missed)
            clock.t = t0 + s + 0.5
            if s < 5 or s >= 12:  # scan stalls 5..11 s
                rows = [self.scan_row(16786002, rng=10.0 - 0.3 * (s - 9))] if 9 <= s <= 39 else []
                eye.on_weapons(rows, asked_at=clock.t - 0.3)
            eye.tick()
            heard += [(s, c) for c in self.heard_calls(eye)]
        self.assertEqual(len(launch_alerts(eye)), 2, launch_alerts(eye))
        self.assertEqual(len(heard), 2, heard)  # both immediate shot alerts, no second scan call
        self.assertEqual(heard[1][0], 9)

    def test_matching_prefers_the_shot_whose_flight_time_fits(self):
        """Two shot rows from the same group: the scan row goes to the one whose distance flown fits its age."""
        eye, clock = self.eye()
        eye.on_event(self.shot(4294967111))  # no scan: alert at once (A)
        clock.t += 7
        eye.on_event(self.shot(4294967112))  # B, also called at once (scan off)
        clock.t += 1
        eye.on_weapons([self.scan_row(16786010, rng=9.9)])  # B just after launch, near the launch point
        alerts = {a["key"]: a.get("id") for a in launch_alerts(eye)}
        self.assertEqual(alerts["launch-4294967112"], "16786010")
        self.assertNotEqual(alerts["launch-4294967111"], "16786010")

    def test_nil_scan_reply_is_not_an_empty_scan(self):
        """REVIEW-P1b MINOR: DCS cannot find the pilot's unit -> WEAPONS_LUA returns nil. That must not count as a
        complete scan that saw nothing (it dropped every shot row unannounced); the shot event alerts."""
        eye, clock = self.eye()
        eye.on_weapons([])  # the scan answered once: 'on'
        feeder = eye_mod.GrpcFeeder.__new__(eye_mod.GrpcFeeder)
        feeder.eye = eye
        feeder._eval_json = lambda custom, lua: None
        own = eye.ownship()
        with patch.object(eye, "ownship", return_value=dict(own, name="Aerial-3-2", source="dcs")):
            clock.t += 0.5
            eye.on_event(self.shot(4294967121))
            for _ in range(5):
                clock.t += 1
                self.assertEqual(feeder.poll_weapons(custom=object()), [])
                eye.tick()
        self.assertEqual(len(launch_alerts(eye)), 1)
        self.assertEqual(launch_alerts(eye)[0]["source"], "shot event")

    def untargeted_scan_row(self, wid, rng, hdg):
        r = self.scan_row(wid, rng=rng)
        r["target"] = None
        r["vn"], r["ve"] = 600 * math.cos(math.radians(hdg)), 600 * math.sin(math.radians(hdg))
        return r

    def test_untargeted_missile_off_the_line_is_called_like_r11(self):
        """REVIEW-P1b MINOR: no DCS target and 40 deg off the line to the pilot. The scan's rule says 'not at you',
        the shot event's rule (r11: within 45 deg) said launch: it is called once."""
        eye, clock = self.eye()
        eye.on_weapons([])
        eye.on_event(self.shot(4294967131))  # heading 281: straight at the pilot by the shot rule
        for s in range(10):
            clock.t += 1
            eye.on_weapons([self.untargeted_scan_row(16786020, 9.5 - 0.3 * s, hdg=320.0)])
            eye.tick()
        alerts = launch_alerts(eye)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0]["id"], "16786020")
        self.assertIn("16786020", [m["id"] for m in eye.picture()["threats"]["missiles_in_flight"]])

    def test_untargeted_missile_flying_away_by_the_shot_rule_stays_silent(self):
        """No more calls than r11: a shot flying away (r11: no call) whose scan row is not at the pilot."""
        eye, clock = self.eye()
        eye.on_weapons([])
        eye.on_event(self.shot(4294967141, heading=101.0))
        for s in range(10):
            clock.t += 1
            eye.on_weapons([self.untargeted_scan_row(16786030, 10.5 + 0.3 * s, hdg=100.0)])
            eye.tick()
        self.assertEqual(launch_alerts(eye), [])


# ============================================================================= 3. cockpit allowlist
class CockpitAllowlistTests(unittest.TestCase):
    def test_server_mode_still_allows_the_client_pc(self):
        server_eye = type("E", (), {"source": "10.90.10.78:50051"})()
        with patch.dict(os.environ, {"ATHENA_COCKPIT_ALLOWED": "", "ATHENA_DCS_SOURCE": "server",
                                     "ATHENA_DCS_CLIENT_HOST": ""}):
            self.assertIn("10.90.10.113", cp.allowed_sources(server_eye))
        with patch.dict(os.environ, {"ATHENA_COCKPIT_ALLOWED": "", "ATHENA_DCS_SOURCE": "server",
                                     "ATHENA_DCS_CLIENT_HOST": "10.90.10.120"}):
            self.assertIn("10.90.10.120", cp.allowed_sources(server_eye))
        with patch.dict(os.environ, {"ATHENA_COCKPIT_ALLOWED": "10.0.0.5", "ATHENA_DCS_SOURCE": "server"}):
            self.assertEqual(cp.allowed_sources(server_eye), {"10.0.0.5"})

    def test_rejected_senders_are_named_in_the_picture(self):
        eye = Eye("Prometheus", source="10.90.10.78:50051")
        listener = cp.CockpitListener(eye, host="127.0.0.1", port=0, allowed={"10.90.10.113"}).start()
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(2):
                tx.sendto(json.dumps(f4e_packet()).encode(), ("127.0.0.1", listener.port))
            end = time.time() + 3
            while time.time() < end and eye.cockpit.rejected < 2:
                time.sleep(0.02)
            tx.close()
        finally:
            listener.close()
        ck = eye.picture()["cockpit"]
        self.assertEqual(ck["rejected_senders"], {"127.0.0.1": 2})
        self.assertIn("dropping cockpit packets from 127.0.0.1", ck["reason"])
        self.assertIn("ATHENA_COCKPIT_ALLOWED", ck["reason"])


# ============================================================================= minors (a)/(b): trickling peers
class _Trickle(http.server.BaseHTTPRequestHandler):
    mode = "sse"
    protocol_version = "HTTP/1.1"  # chunked SSE like the real endpoint (HTTP/1.0 makes http.client drop conn.sock)

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            self.send_response(200)
            if self.mode == "sse":
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for _ in range(100):  # one byte every 0.1 s, never a newline: 10 s
                    self.wfile.write(b"1\r\nx\r\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            else:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
        except OSError:
            pass  # the client hung up: that is the point


def _serve(mode):
    handler = type("H", (_Trickle,), {"mode": mode})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TrickleTests(unittest.TestCase):
    def test_codex_byte_trickle_is_cut_at_its_wall_clock_limit(self):
        srv = _serve("sse")
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d/responses" % srv.server_address[1]
        start = time.monotonic()
        with patch.object(http.client, "HTTPSConnection", http.client.HTTPConnection):
            with self.assertRaises(TimeoutError):
                codex_llm._http_post(url, {}, b"{}", start + 1.0)
        self.assertLess(time.monotonic() - start, 1.6)

    def test_api_body_trickle_is_cut_at_the_exchange_deadline(self):
        from integrations.hermes_radio.tests.test_p1_radio import LIVE, controller
        srv = _serve("json")
        self.addCleanup(srv.shutdown)
        c, _ = controller(LIVE["loadout_2035"])
        c._host, c._path = "127.0.0.1:%d" % srv.server_address[1], "/v1/chat/completions"
        start = time.monotonic()
        with patch.object(http.client, "HTTPSConnection", http.client.HTTPConnection):
            with self.assertRaises((TimeoutError, socket.timeout)):
                c.ask([], deadline=start + 1.0)
        self.assertLess(time.monotonic() - start, 1.6)  # the thread and its socket end at the deadline


if __name__ == "__main__":
    unittest.main()
