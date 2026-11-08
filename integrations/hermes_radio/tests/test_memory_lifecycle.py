"""Offline sampling/lifecycle regressions; no live DCS, SRS or service config."""
import unittest
import threading
from unittest.mock import patch

from integrations.hermes_radio.eye import Eye
from integrations.hermes_radio.memory_link import MemoryLink, SNAPSHOT_TYPES
from integrations.hermes_radio.tests import eye_fixture as fx


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.link = MemoryLink(start=False, capacity=1000, clock=lambda: self.now)
        self.link.set_identity(1, "fixture.miz", "fixture:1")
        self.calls = []
        self.rpc = patch("integrations.hermes_radio.memory_link.request", side_effect=self.request)
        self.rpc.start()
        self.addCleanup(self.rpc.stop)
        self.addCleanup(self.link.close)

    def request(self, payload, **kwargs):
        self.calls.append(payload)
        if payload["operation"] == "resolve":
            return {"mission": {"mission_key": "mission-%s" % payload["session_id"]}}
        return {"ok": True, "path": "/fixture/report.md"}

    def drain(self):
        while self.link.queue:
            self.assertTrue(self.link._send_one())
        scheduled = self.link._auto_debrief_one()
        if self.link.debrief_thread:
            self.link.debrief_thread.join(1)
            self.assertFalse(self.link.debrief_thread.is_alive())
        return scheduled

    def reports(self):
        return [c["mission"] for c in self.calls if c["operation"] == "debrief"]

    def test_all_snapshot_types_on_change_at_ten_seconds_not_age_only(self):
        for kind in SNAPSHOT_TYPES:
            with self.subTest(kind=kind):
                self.now = 0
                self.assertTrue(self.link.publish(kind, {"value": {"reading": 1, "age_s": 0}}))
                self.now = 9.99
                self.assertFalse(self.link.publish(kind, {"value": {"reading": 2, "age_s": 1}}))
                self.now = 10
                self.assertTrue(self.link.publish(kind, {"value": {"reading": 2, "age_s": 2}}))
                self.now = 100
                self.assertFalse(self.link.publish(kind, {"value": {"reading": 2, "age_s": 99}}))
        self.assertEqual(self.link.sampled_out, 10)
        self.assertEqual(self.link.dropped, 0)
        self.assertEqual(self.link.queue[1][1]["payload"]["value"]["age_s"], 2)

    def test_life_drops_and_gear_stores_changes_bypass_interval(self):
        cases = [
            ("life_damage", {"value": {"life": 100}}, {"value": {"life": 99}}),
            ("life_damage", {"value": {"damage": {"life_percent": 100}}},
             {"value": {"damage": {"life_percent": 99}}}),
            ("cockpit", {"value": {"config": {"gear": "up"}}},
             {"value": {"config": {"gear": "down"}}}),
            ("cockpit", {"value": {"payload": {"gun_rounds": 100}}},
             {"value": {"payload": {"gun_rounds": 99}}}),
            ("loadout", {"value": {"weapons": [{"weapon": "AIM-9", "count": 2}]}},
             {"value": {"weapons": [{"weapon": "AIM-9", "count": 1}]}}),
            ("cockpit_input", {"packet": {"pay": {"gun": 100}, "mech": {"gear": 0}}},
             {"packet": {"pay": {"gun": 99}, "mech": {"gear": 1}}}),
        ]
        for kind, before, after in cases:
            with self.subTest(kind=kind):
                self.link.invalidate()
                self.link.set_identity(1, "fixture.miz", "fixture:1")
                self.assertTrue(self.link.publish(kind, before))
                self.assertTrue(self.link.publish(kind, after))
                self.assertFalse(self.link.publish(kind, after))

    def test_sparse_slow_fields_do_not_make_every_packet_urgent(self):
        initial = {"packet": {"t": 0, "pay": {"gun": 100}, "mech": {"gear": 0}}}
        self.assertTrue(self.link.publish("cockpit_input", initial))
        self.assertFalse(self.link.publish("cockpit_input", {"packet": {"t": 1}}))
        self.assertFalse(self.link.publish("cockpit_input", initial))
        self.assertTrue(self.link.publish("cockpit_input", {"packet": {"pay": {"gun": 99}}}))

    def test_first_slow_field_evidence_is_retained_within_window(self):
        self.assertTrue(self.link.publish("cockpit_input", {"packet": {"t": 0}}))
        self.assertTrue(self.link.publish("cockpit_input", {"packet": {"pay": {"gun": 100}}}))
        self.assertTrue(self.link.publish("cockpit_input", {"packet": {"mech": {"gear": 0}}}))

    def test_null_derived_payload_between_updates_is_not_a_stores_change(self):
        full = {"derived": {"payload": {"ammo": [{"count": 2, "name": "APKWS"}]}}}
        self.assertTrue(self.link.publish("cockpit_input", full))
        self.assertFalse(self.link.publish("cockpit_input", {"derived": {"payload": None}}))
        self.assertFalse(self.link.publish("cockpit_input", full))
        full["derived"]["payload"]["ammo"][0]["count"] = 1
        self.assertTrue(self.link.publish("cockpit_input", full))

    def test_tally_is_bounded_and_stale_generation_is_not_returned(self):
        self.link.bindings[self.link.identity] = "mission-1"
        with patch("integrations.hermes_radio.memory_link.request", return_value={"tally": {"count": 19}}) as rpc:
            self.assertEqual(self.link.mission_tally("me", "armor"), {"count": 19})
            self.assertEqual(rpc.call_args.kwargs["timeout"], .15)
            self.assertEqual(rpc.call_args.args[0]["operation"], "mission_tally")
        def changed(*args, **kwargs):
            self.link.invalidate()
            return {"tally": {"count": 19}}
        with patch("integrations.hermes_radio.memory_link.request", side_effect=changed):
            self.assertIsNone(self.link.mission_tally())

    def test_recovered_life_then_drop_is_retained_inside_window(self):
        self.assertTrue(self.link.publish("life_damage", {"life": 90}))
        self.assertFalse(self.link.publish("life_damage", {"life": 100}))
        self.assertTrue(self.link.publish("life_damage", {"life": 99}))

    def test_identical_events_are_never_sampled_and_new_identity_resets_sampling(self):
        for _ in range(100):
            self.assertTrue(self.link.publish("event", {"kind": "hit"}))
            self.assertTrue(self.link.publish("shot", {"weapon": "AIM-9"}))
        self.assertEqual(len(self.link.queue), 200)
        self.assertTrue(self.link.publish("fuel", {"value": 10}))
        old_generation = self.link.generation
        self.link.set_identity(2, "fixture.miz", "fixture:1")
        self.assertFalse(self.link.publish("fuel", {"value": 20}, generation=old_generation))
        self.assertTrue(self.link.publish("fuel", {"value": 10}))

    def test_capture_picture_uses_same_sampler_and_preserves_damage(self):
        eye = Eye(clock=lambda: self.now)
        eye.memory = self.link
        eye.on_unit(fx.PROMETHEUS)
        eye.on_life(fx.PROMETHEUS, {"life": 100, "life0": 100})
        self.link.capture_picture(eye.picture())
        first = sum(row["type"] == "life_damage" for _, row in self.link.queue)
        self.now = 1
        eye.on_life(fx.PROMETHEUS, {"life": 99, "life0": 100})
        self.link.capture_picture(eye.picture())
        self.assertEqual(sum(row["type"] == "life_damage" for _, row in self.link.queue), first + 1)
        self.assertTrue(any(row["type"] == "own_damage" for _, row in self.link.queue))

    def test_mission_end_flushes_event_then_reports_once_across_unload(self):
        self.link.publish("event", {"kind": "mission_end"})
        self.assertFalse(self.link._auto_debrief_one())
        self.assertTrue(self.drain())
        self.assertEqual([c["operation"] for c in self.calls], ["resolve", "observe", "debrief"])
        self.assertEqual(self.link.debrief_status["reason"], "mission_end")
        self.link.publish("event", {"kind": "mission_end"})
        self.link.invalidate()
        self.assertFalse(self.drain())
        self.assertEqual(self.reports(), ["mission-1"])

    def test_identity_change_reports_old_mission_and_unload_reports_new(self):
        self.link.publish("shot", {})
        self.link.set_identity(2, "fixture.miz", "fixture:1")
        self.link.publish("shot", {})
        self.assertTrue(self.drain())
        self.assertEqual(self.reports(), ["mission-1"])
        self.link.set_identity(None, None, None)
        self.assertTrue(self.drain())
        self.assertEqual(self.reports(), ["mission-1", "mission-2"])
        self.link.invalidate()
        self.assertFalse(self.drain())

    def test_disconnect_requires_activity_and_strictly_more_than_sixty_seconds(self):
        self.link.capture_picture({"connected": False})
        self.now = 100
        self.link.capture_picture({"connected": False})
        self.assertFalse(self.drain())
        self.link.publish("shot", {})
        self.link.capture_picture({"connected": False})
        self.now = 160
        self.link.capture_picture({"connected": False})
        self.assertFalse(self.drain())
        self.now = 160.01
        self.link.capture_picture({"connected": False})
        self.assertTrue(self.drain())
        self.assertEqual(self.link.debrief_status["reason"], "disconnect")
        self.now = 300
        self.link.capture_picture({"connected": False})
        self.assertFalse(self.drain())
        self.assertEqual(self.reports(), ["mission-1"])

    def test_boundary_with_overflowed_evidence_still_resolves_old_mission(self):
        self.link.capacity = 1
        self.link.publish("shot", {})
        self.link.set_identity(2, "fixture.miz", "fixture:1")
        self.link.publish("shot", {})
        self.assertEqual(self.link.dropped, 1)
        self.assertTrue(self.drain())
        self.assertEqual(self.reports(), ["mission-1"])

    def test_reconnect_resets_disconnect_grace_and_stale_picture_cannot_trigger(self):
        self.link.publish("shot", {})
        self.link.capture_picture({"connected": False})
        self.now = 59
        self.link.capture_picture({"connected": True})
        self.now = 60
        self.link.capture_picture({"connected": False})
        self.now = 121
        self.link.capture_picture({"connected": False, "_memory_generation": -1})
        self.assertFalse(self.drain())
        self.link.capture_picture({"connected": False})
        self.assertTrue(self.drain())

    def test_failed_automatic_attempt_is_visible_manual_regeneration_allowed(self):
        self.link.publish("event", {"kind": "mission_end"})
        self.assertTrue(self.link._send_one())
        with patch("integrations.hermes_radio.memory_link.request", side_effect=OSError):
            self.assertTrue(self.link._auto_debrief_one())
            self.link.debrief_thread.join(1)
        self.assertEqual(self.link.debrief_status["state"], "failed")
        self.assertFalse(self.link._auto_debrief_one())
        self.assertTrue(self.link.schedule_debrief())
        self.link.debrief_thread.join(1)
        self.assertEqual(self.link.debrief_status["state"], "written")

    def test_busy_report_preserves_next_mission_job_without_blocking_publish(self):
        entered, release = threading.Event(), threading.Event()

        def slow(payload, **kwargs):
            if payload["operation"] == "debrief":
                entered.set()
                release.wait(2)
            return self.request(payload, **kwargs)

        with patch("integrations.hermes_radio.memory_link.request", side_effect=slow):
            try:
                self.link.publish("event", {"kind": "mission_end"})
                self.assertTrue(self.link._send_one())
                self.assertTrue(self.link._auto_debrief_one())
                self.assertTrue(entered.wait(1))
                self.link.set_identity(2, "fixture.miz", "fixture:1")
                self.assertTrue(self.link.publish("event", {"kind": "mission_end"}))
                self.assertTrue(self.link._send_one())
                self.assertFalse(self.link._auto_debrief_one())
                self.assertFalse(release.is_set())
            finally:
                release.set()
                self.link.debrief_thread.join(1)
            self.assertTrue(self.drain())
        self.assertEqual(self.reports(), ["mission-1", "mission-2"])

    def test_sender_loop_drives_automatic_debrief_from_existing_eye_event_hook(self):
        reported = threading.Event()

        def rpc(payload, **kwargs):
            result = self.request(payload, **kwargs)
            if payload["operation"] == "debrief":
                reported.set()
            return result

        with patch("integrations.hermes_radio.memory_link.request", side_effect=rpc):
            link = MemoryLink()
            try:
                link.set_identity(1, "fixture.miz", "fixture:1")
                eye = Eye()
                eye.memory = link
                eye.on_event({"time": 111., "mission_end": {}})
                self.assertTrue(reported.wait(1))
                link.debrief_thread.join(1)
                self.assertEqual(link.debrief_status["state"], "written")
                self.assertEqual(self.reports(), ["mission-1"])
            finally:
                link.close()


if __name__ == "__main__":
    unittest.main()
