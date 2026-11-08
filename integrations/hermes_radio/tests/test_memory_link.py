"""memory6 offline publisher tests (also run through unittest discovery)."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from integrations.hermes_radio.eye import Eye
from integrations.hermes_radio.memory_link import MemoryLink, request
from integrations.hermes_radio.tests import eye_fixture as fx


class MemoryLinkTests(unittest.TestCase):
    def link(self, capacity=16):
        link = MemoryLink(capacity=capacity, start=False)
        link.set_identity(1, "fixture.miz", "fixture:1")
        return link

    def test_drop_oldest_never_calls_socket_on_producer(self):
        link = self.link(2)
        with patch("integrations.hermes_radio.memory_link.request", side_effect=AssertionError("producer I/O")):
            for n in range(5):
                self.assertTrue(link.publish("future_feature", {"n": n}, at=n))
        self.assertEqual(link.dropped, 3)
        self.assertEqual([r[1]["payload"]["n"] for r in link.queue], [3, 4])

    def test_unknown_identity_counts_loss(self):
        link = MemoryLink(start=False)
        self.assertFalse(link.publish("shot", {}))
        self.assertEqual(link.dropped, 1)

    def test_down_keeps_bounded_queue(self):
        link = self.link(2)
        for n in range(6):
            link.publish("shot", {"n": n})
            with patch("integrations.hermes_radio.memory_link.request", side_effect=OSError):
                self.assertFalse(link._send_one())
        self.assertEqual(len(link.queue), 2)
        self.assertEqual(link.dropped, 4)
        self.assertEqual(link.failures, 6)

    def test_context_is_captured_not_relabelled_on_mission_change(self):
        link = self.link()
        link.publish("shot", {"weapon": "AIM-9"})
        link.set_identity(2, "same.miz", "fixture:1")
        calls = []
        def rpc(row, **kwargs):
            calls.append(row)
            return {"ok": True, "mission": {"mission_key": "old-key"}}
        with patch("integrations.hermes_radio.memory_link.request", side_effect=rpc):
            self.assertTrue(link._send_one())
        self.assertEqual(calls[0]["session_id"], 1)
        self.assertEqual(calls[1]["observation"]["mission_key"], "old-key")
        self.assertIsNone(link.mission_key)

    def test_eye_shot_kill_and_takeoff_hooks_without_file_logging(self):
        eye = Eye(clock=lambda: 1000.)
        eye.memory = self.link(50)
        eye.on_unit(fx.PROMETHEUS)
        eye.on_unit(fx.UNITS[1])
        eye.on_event(fx.takeoff(fx.PROMETHEUS, fx.AIRBASES[0]))
        eye.on_event(fx.shot(fx.PROMETHEUS, 55, "AIM_120C"))
        eye.on_event(fx.kill(fx.PROMETHEUS, 55, "AIM_120C", fx.UNITS[1]))
        eye.memory.capture_picture(eye.picture())
        rows = [r for _, r in eye.memory.queue]
        kinds = {r["type"] for r in rows}
        self.assertTrue({"shot", "kill_by_me", "event", "loadout", "fuel", "cockpit", "sam_threat", "weapons_used"} <= kinds)
        takeoff = next(r for r in rows if r["type"] == "event" and r["payload"]["kind"] == "takeoff")
        self.assertEqual(takeoff["payload"]["place"], "Kutaisi")
        self.assertTrue(takeoff["payload"]["mine"])

    def test_request_deadline_includes_dribbling_response(self):
        with tempfile.TemporaryDirectory(prefix="ml") as tmp:
            path = str(Path(tmp) / "s")
            ready = threading.Event()
            def slow():
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.bind(path)
                    listener.listen(1)
                    ready.set()
                    conn, _ = listener.accept()
                    with conn:
                        conn.recv(4096)
                        try:
                            for _ in range(10):
                                conn.sendall(b" ")
                                time.sleep(.03)
                        except OSError:
                            pass
            thread = threading.Thread(target=slow)
            thread.start()
            self.assertTrue(ready.wait(1))
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                request({"operation": "health"}, path=path, timeout=.10)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, .15)
            thread.join(1)

    def test_payload_copy(self):
        link = self.link()
        payload = {"nested": [1]}
        link.publish("fact", payload)
        payload["nested"].append(2)
        self.assertEqual(link.queue[0][1]["payload"], {"nested": [1]})

    def controller(self):
        from integrations.hermes_radio.simple_controller import SimpleController
        from integrations.hermes_radio.tests.test_eye import FakeTranscript
        eye = Eye()
        eye.memory = self.link()
        with eye.memory.lock:
            eye.memory.bindings[eye.memory.identity] = "mission-fixture"
        return SimpleController(None, FakeTranscript(), model="fixture", api_key="fixture", eye=eye,
                                llm=lambda messages: "Prometheus, copy.")

    def test_optional_picture_memory_and_silent_outage(self):
        controller = self.controller()
        with patch("integrations.hermes_radio.memory_link.request", return_value={"memory": {"takeoff_airfield": "Kutaisi"}}):
            self.assertEqual(controller.picture()["memory"]["takeoff_airfield"], "Kutaisi")
        with patch("integrations.hermes_radio.memory_link.request", side_effect=OSError):
            self.assertNotIn("memory", controller.picture())

    def test_debrief_reply_only_after_receipt(self):
        from integrations.hermes_radio.tests.test_eye import req
        controller = self.controller()
        with patch("integrations.hermes_radio.memory_link.request", return_value={"path": "/fixture/report.md"}):
            self.assertEqual(controller.exchange(req("Athena, debrief"))["speech"], "debrief written")
        with patch("integrations.hermes_radio.memory_link.request", side_effect=OSError):
            self.assertEqual(controller.exchange(req("debrief"))["speech"], "Debrief unavailable.")


if __name__ == "__main__":
    unittest.main()
