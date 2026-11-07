"""Exact-evidence expiry with deterministic monotonic time and fake state/model."""
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from integrations.hermes_radio.controller import Controller
from integrations.hermes_radio.tests.test_v2 import FakeProvider, call, req, world
from integrations.hermes_radio.fixture_data import NOW


class ExpiryTests(unittest.TestCase):
    def test_older_tool_result_limits_fresh_snapshot(self):
        def older(frame):
            if frame["operation"] == "get_unit":
                frame["generated_at_utc"] = (NOW + timedelta(milliseconds=800)).isoformat()
                frame["context"]["state"]["age_ms"] = 800
                frame["context"]["state"]["freshness"]["age_ms"] = 800
            return frame
        for delay, expected in ((0.1, "Prometheus, contact MiG-29."), (0.3, "Prometheus, unable.")):
            with self.subTest(delay=delay), world(transform=older) as (tools, cache, _):
                clock = [10.0]
                tools.clock = cache.clock = lambda: clock[0]
                class Delayed(FakeProvider):
                    def stream(self, *args, **kwargs):
                        if not self.requests:
                            self.requests.append(True)
                            yield call("get_unit", {"unit_id": "bandit"})
                        else:
                            clock[0] += delay
                            yield "text", "Prometheus, contact MiG-29."
                self.assertEqual(Controller(Delayed(), tools, cache, Mock()).exchange(req())["speech"], expected)

    def test_transcript_lock_wait_cannot_emit_expired_generated_text(self):
        with world() as (tools, cache, _):
            clock = [10.0]
            tools.clock = cache.clock = lambda: clock[0]
            log = Mock()
            def delayed_log(**record):
                if record["kind"] == "reply_generated":
                    clock[0] += 1.1
            log.log.side_effect = delayed_log
            result = Controller(FakeProvider([("text", "Prometheus, contact MiG-29.")]), tools, cache, log).exchange(req())
            self.assertEqual(result["speech"], "Prometheus, unable.")

    def test_snapshot_generation_expiry_and_fresh_positive_control(self):
        for delay, expected in ((0.5, "Prometheus, contact MiG-29."), (1.1, "Prometheus, unable.")):
            with self.subTest(delay=delay), world() as (tools, cache, _):
                clock = [10.0]
                tools.clock = cache.clock = lambda: clock[0]
                class Delayed(FakeProvider):
                    def stream(self, *args, **kwargs):
                        clock[0] += delay
                        yield "text", "Prometheus, contact MiG-29."
                adapter = Controller(Delayed(), tools, cache, Mock())
                result = adapter.exchange(req("Athena, picture?"))
                self.assertEqual(result["speech"], expected)
                self.assertEqual(result["state"], "ok" if delay < 1 else "unavailable")

    def test_expiry_between_parts_stops_without_replay_or_continuation(self):
        with world() as (tools, cache, _):
            clock = [10.0]
            tools.clock = cache.clock = lambda: clock[0]
            class Delayed(FakeProvider):
                def stream(self, *args, **kwargs):
                    yield "text", "Prometheus, contact MiG-29. "
                    clock[0] += 1.1
                    yield "text", "Contact is east."
            log = Mock()
            adapter = Controller(Delayed(), tools, cache, log)
            parts = list(adapter.stream(req("Athena, picture?")))
            self.assertEqual([p["speech"] for p in parts], ["Prometheus, contact MiG-29."])
            self.assertFalse(adapter.continuation)
            generated = [c.kwargs["text"] for c in log.log.call_args_list if c.kwargs["kind"] == "reply_generated"]
            self.assertEqual(generated, ["Prometheus, contact MiG-29."])

    def test_same_scope_refresh_cannot_renew_old_wording(self):
        with world() as (tools, cache, _):
            clock = [10.0]
            tools.clock = cache.clock = lambda: clock[0]
            old = cache.get()
            epoch = cache.epoch
            class Refreshed(FakeProvider):
                def stream(self, *args, **kwargs):
                    clock[0] += 0.8
                    self_snapshot = cache.refresh()
                    assert self_snapshot is not old and cache.epoch == epoch
                    clock[0] += 0.3
                    yield "text", "Prometheus, contact MiG-29."
            result = Controller(Refreshed(), tools, cache, Mock()).exchange(req())
            self.assertEqual(result["speech"], "Prometheus, unable.")

    def test_query_duration_and_cache_residence_both_count(self):
        with world() as (tools, cache, _):
            clock = [10.0]
            tools.clock = cache.clock = lambda: clock[0]
            raw_query, client_query = tools.raw.query, tools.client.query
            def delayed(query):
                def run(*args, **kwargs):
                    result = query(*args, **kwargs)
                    clock[0] += 0.1
                    return result
                return run
            with patch.object(tools.raw, "query", delayed(raw_query)), patch.object(tools.client, "query", delayed(client_query)):
                cache.get()  # Three reads consume 0.3s, not a new lifetime at publication.
            clock[0] += 0.4
            class Delayed(FakeProvider):
                def stream(self, *args, **kwargs):
                    clock[0] += 0.4
                    yield "text", "Prometheus, contact MiG-29."
            self.assertEqual(Controller(Delayed(), tools, cache, Mock()).exchange(req())["speech"], "Prometheus, unable.")

    def test_slow_query_is_not_fresh_on_return(self):
        with world() as (tools, cache, _):
            clock = [10.0]
            tools.clock = cache.clock = lambda: clock[0]
            original = tools.raw.query
            def delayed(*args, **kwargs):
                result = original(*args, **kwargs)
                clock[0] += 1.1
                return result
            with patch.object(tools.raw, "query", delayed):
                self.assertIsNone(cache.get())

    def test_tool_only_evidence_expires_in_generation_and_between_parts(self):
        for between in (False, True):
            with self.subTest(between=between), world() as (tools, cache, _):
                clock = [10.0]
                tools.clock = cache.clock = lambda: clock[0]
                class ToolThenText(FakeProvider):
                    def stream(self, *args, **kwargs):
                        if not self.requests:
                            self.requests.append(True)
                            yield call("get_unit", {"unit_id": "bandit"})
                        else:
                            if between:
                                yield "text", "Prometheus, contact MiG-29. "
                            clock[0] += 1.1
                            yield "text", "Contact is east." if between else "Prometheus, contact MiG-29."
                adapter = Controller(ToolThenText(), tools, cache, Mock())
                with patch.object(cache, "get", return_value=None):
                    parts = list(adapter.stream(req()))
                self.assertEqual([p["speech"] for p in parts], ["Prometheus, contact MiG-29." if between else "Prometheus, unable."])
                self.assertFalse(adapter.continuation)


if __name__ == "__main__":
    unittest.main()
