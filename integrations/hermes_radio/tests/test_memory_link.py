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

    def test_lost_identity_does_not_relabel_new_claims(self):
        link = self.link()
        link.publish("old_fact", {})
        link.set_identity(None, None, None)
        self.assertFalse(link.publish("new_unknown_mission", {}))
        self.assertEqual(link.queue[0][0], (1, "fixture.miz", "fixture:1"))
        self.assertIsNone(link.mission_key)

    def test_unavailable_filename_hook_does_not_break_eye_session_check(self):
        from types import SimpleNamespace
        from integrations.hermes_radio.eye import GrpcFeeder
        eye = Eye()
        eye.memory = self.link()
        feeder = GrpcFeeder(eye, "fixture.invalid")
        stub = SimpleNamespace(GetSessionId=lambda *args, **kwargs: SimpleNamespace(session_id=2))
        with patch.object(feeder, "_chan", return_value=None), patch(
                "dcs.hook.v0.hook_pb2_grpc.HookServiceStub", side_effect=OSError):
            feeder._check_session(stub)
            feeder._metadata_thread.join(1)
        self.assertEqual(eye.session_id, 2)
        self.assertIsNone(eye.memory.identity)

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
        kill = next(r["payload"] for r in rows if r["type"] == "kill_by_me")
        self.assertEqual(kill["shooter"]["id"], fx.PROMETHEUS["id"])
        self.assertEqual(kill["target"]["id"], fx.UNITS[1]["id"])
        self.assertEqual(kill["mission_time"], 111.)

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

    def test_debrief_is_queued_without_blocking_next_turn(self):
        from integrations.hermes_radio.tests.test_eye import req
        controller = self.controller()
        memory = controller.eye.memory
        entered, release = threading.Event(), threading.Event()
        def slow(payload, **kwargs):
            if payload["operation"] == "debrief":
                entered.set()
                release.wait(3)
                return {"path": "/fixture/report.md"}
            return {"memory": {}}
        with patch("integrations.hermes_radio.memory_link.request", side_effect=slow):
            try:
                started = time.monotonic()
                self.assertIn("debrief written", controller.exchange(req("Athena, debrief"))["speech"])
                self.assertTrue(entered.wait(1))
                self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(memory.debrief_status["state"], "queued")
                self.assertTrue(controller.exchange(req("Athena, radio check"))["speech"])
                self.assertFalse(release.is_set())
                self.assertEqual(memory.debrief_status["state"], "queued")
            finally:
                release.set()
                memory.debrief_thread.join(1)
        self.assertEqual(memory.debrief_status["state"], "written")
        with patch("integrations.hermes_radio.memory_link.request", side_effect=OSError):
            self.assertIn("debrief written", controller.exchange(req("debrief"))["speech"])
            memory.debrief_thread.join(1)
        self.assertEqual(memory.debrief_status["state"], "failed")
        memory.invalidate()
        self.assertIn("debrief unavailable", controller.exchange(req("debrief"))["speech"].lower())

    def test_slow_metadata_does_not_serialize_concurrent_subscriptions_or_reset(self):
        from types import SimpleNamespace
        from integrations.hermes_radio.eye import GrpcFeeder
        eye = Eye()
        eye.memory = self.link()
        eye.session_id = 1
        feeder = GrpcFeeder(eye, "fixture.invalid")
        entered, release = threading.Event(), threading.Event()
        subscriptions = []
        def filename(*args, **kwargs):
            entered.set()
            release.wait(5)
            return SimpleNamespace(name="new.miz")
        def stream(*args):
            subscriptions.append(threading.current_thread().name)
            return iter(())
        stub = SimpleNamespace(GetSessionId=lambda *a, **k: SimpleNamespace(session_id=2), StreamUnits=stream, StreamEvents=stream)
        with patch.object(feeder, "_chan", return_value=None), patch(
                "dcs.mission.v0.mission_pb2_grpc.MissionServiceStub", return_value=stub), patch(
                "dcs.hook.v0.hook_pb2_grpc.HookServiceStub", return_value=SimpleNamespace(GetMissionFilename=filename)):
            workers = [threading.Thread(target=feeder._units, args=(str(i), "GROUP_CATEGORY_AIRPLANE", 1, 1)) for i in range(3)]
            workers.append(threading.Thread(target=feeder._events))
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(entered.wait(1))
                for worker in workers:
                    worker.join(.3)
                self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertEqual(len(subscriptions), 4)
                self.assertEqual(eye.session_id, 2)
                self.assertEqual(eye.reset_reason, "session_changed")
                self.assertIsNone(eye.memory.identity)
                self.assertTrue(feeder._session_lock.acquire(blocking=False))
                feeder._session_lock.release()
            finally:
                release.set()
                for worker in workers:
                    worker.join(1)
                feeder._metadata_thread.join(1)
        self.assertEqual(eye.memory.identity, (2, "new.miz", "fixture.invalid:50051"))

    def test_mission_start_and_stale_picture_are_fenced_during_metadata(self):
        from types import SimpleNamespace
        from integrations.hermes_radio.eye import GrpcFeeder
        eye = Eye()
        link = eye.memory = self.link(100)
        eye.session_id = 1
        eye.on_unit(fx.PROMETHEUS)
        eye.on_event(fx.shot(fx.PROMETHEUS, 55, "old-weapon"))
        old_picture = eye.picture()
        old_count = len(link.queue)
        feeder = GrpcFeeder(eye, "fixture.invalid")
        entered, release = threading.Event(), threading.Event()
        def filename(*args, **kwargs):
            entered.set()
            release.wait(3)
            return SimpleNamespace(name="new.miz")
        stub = SimpleNamespace(GetSessionId=lambda *a, **k: SimpleNamespace(session_id=2))
        with patch.object(feeder, "_chan", return_value=None), patch(
                "dcs.hook.v0.hook_pb2_grpc.HookServiceStub", return_value=SimpleNamespace(GetMissionFilename=filename)):
            try:
                eye.on_event({"time": 0., "mission_start": {}})
                self.assertIsNone(link.identity)
                self.assertFalse(eye.units)
                feeder._check_session(stub)
                self.assertTrue(entered.wait(1))
                link.capture_picture(old_picture)
                self.assertFalse(link.publish("transition_fact", {}))
                self.assertEqual(len(link.queue), old_count)
                self.assertGreater(link.dropped, 0)
            finally:
                release.set()
                feeder._metadata_thread.join(1)
        link.capture_picture(old_picture)  # captured before reset, published after new identity
        self.assertEqual(len(link.queue), old_count)
        eye.on_unit(fx.PROMETHEUS)
        eye.on_event(fx.shot(fx.PROMETHEUS, 56, "new-weapon"))
        link.capture_picture(eye.picture())
        self.assertTrue(all(identity[0] == 1 for identity, _ in list(link.queue)[:old_count]))
        self.assertTrue(all(identity[0] == 2 for identity, _ in list(link.queue)[old_count:]))
        self.assertNotIn("old-weapon", json.dumps(list(link.queue)[old_count:]))
        self.assertNotIn("new-weapon", json.dumps(list(link.queue)[:old_count]))

    def test_metadata_completion_from_before_boundary_cannot_install(self):
        from types import SimpleNamespace
        from integrations.hermes_radio.eye import GrpcFeeder
        eye = Eye()
        link = eye.memory = self.link()
        feeder = GrpcFeeder(eye, "fixture.invalid")
        entered, release = threading.Event(), threading.Event()
        def filename(*args, **kwargs):
            entered.set()
            release.wait(3)
            return SimpleNamespace(name="old.miz")
        stub = SimpleNamespace(GetSessionId=lambda *a, **k: SimpleNamespace(session_id=1))
        with patch.object(feeder, "_chan", return_value=None), patch(
                "dcs.hook.v0.hook_pb2_grpc.HookServiceStub", return_value=SimpleNamespace(GetMissionFilename=filename)):
            try:
                feeder._check_session(stub)
                self.assertTrue(entered.wait(1))
                eye.on_event({"time": 0., "mission_start": {}})
            finally:
                release.set()
                feeder._metadata_thread.join(1)
        self.assertIsNone(link.identity)


if __name__ == "__main__":
    unittest.main()
