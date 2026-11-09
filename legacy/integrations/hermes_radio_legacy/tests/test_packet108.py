"""Real-shaped null-budget envelopes, using only test-owned state sockets."""
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from athena.state_service_client import ClientQueryError
from legacy.integrations.hermes_radio_legacy.controller import Controller, compact_result
from legacy.integrations.hermes_radio_legacy.fixture_data import NOW
from legacy.integrations.hermes_radio_legacy.tests.test_v2 import FakeProvider, call, req, world


def live_shape(age=3685, maximum=None):
    def transform(frame):
        frame["generated_at_utc"] = (NOW + timedelta(milliseconds=age)).isoformat()
        state = frame["context"]["state"]
        state["age_ms"] = state["freshness"]["age_ms"] = age
        state["freshness"]["max_age_ms"] = maximum
        return frame
    return transform


class Packet108Tests(unittest.TestCase):
    def test_null_producer_budget_ownship_and_hostile_query(self):
        with world(transform=live_shape()) as (tools, cache, _):
            tools.clock = cache.clock = lambda: 10.0
            own = tools.own()
            self.assertAlmostEqual(own.expires_at, 11.315)
            for name, args in (("ownship", {}), ("query_units", {"coalition": "hostile"})):
                result = tools.call(name, args, anchor=own)
                self.assertTrue(result["ok"])
                self.assertIsNone(result["context"]["freshness"]["max_age_ms"])
            self.assertIsNotNone(cache.get())

    def test_finite_local_bound_intersects_producer_bound(self):
        for age, maximum, remaining in ((3685, None, 1.315), (0, 1000, 1.0),
                                         (3685, 9000, 1.315), (5000, None, None),
                                         (5001, None, None), (1000, 1000, None)):
            with self.subTest(age=age, maximum=maximum), world(transform=live_shape(age, maximum)) as (tools, cache, _):
                tools.clock = cache.clock = lambda: 10.0
                if remaining is None:
                    with self.assertRaises(ClientQueryError) as raised:
                        tools.own()
                    self.assertEqual(raised.exception.code, "evidence_expired")
                else:
                    self.assertAlmostEqual(tools.own().expires_at, 10.0 + remaining)

    def test_null_budget_query_duration_and_each_sentence_expire(self):
        for between in (False, True):
            with self.subTest(between=between), world(transform=live_shape()) as (tools, cache, _):
                clock = [10.0]
                tools.clock = cache.clock = lambda: clock[0]
                class Delayed(FakeProvider):
                    def stream(self, *args, **kwargs):
                        if between:
                            yield "text", "Prometheus, contact MiG-29. "
                        clock[0] += 1.316
                        yield "text", "Contact is east." if between else "Prometheus, contact MiG-29."
                controller = Controller(Delayed(), tools, cache, Mock())
                parts = list(controller.stream(req()))
                self.assertEqual([p["speech"] for p in parts],
                                 ["Prometheus, contact MiG-29." if between else "Prometheus, unable."])
                self.assertFalse(controller.continuation)
        with world(transform=live_shape()) as (tools, cache, _):
            clock = [10.0]
            tools.clock = cache.clock = lambda: clock[0]
            query = tools.raw.query
            def slow(**kwargs):
                result = query(**kwargs)
                clock[0] += 1.316
                return result
            with patch.object(tools.raw, "query", slow), self.assertRaises(ClientQueryError):
                tools.own()

    def test_tool_failure_keeps_safe_code_not_arbitrary_error_text(self):
        for code, expected in (("socket_unavailable", "socket_unavailable"),
                               ("query_timeout", "query_timeout"),
                               ("protocol_error", "protocol_error"),
                               ("correlation_mismatch", "correlation_mismatch"),
                               ("private path /secret/raw response", "state_query_failed")):
            with self.subTest(code=code), world() as (tools, cache, _):
                log = Mock()
                with patch.object(cache, "get", return_value=None), patch.object(tools, "own", side_effect=ClientQueryError(code)):
                    result = Controller(FakeProvider([call("ownship", {})]), tools, cache, log).exchange(req())
                self.assertEqual(result["speech"], "Prometheus, unable.")
                records = [c.kwargs for c in log.log.call_args_list if c.kwargs["kind"] == "tool"]
                self.assertEqual(records[0]["result"], {"ok": False, "reason": expected})
                self.assertEqual(records[0]["outcome"], expected)
                self.assertEqual(compact_result({"ok": False, "reason": code})["reason"], expected)


if __name__ == "__main__":
    unittest.main()
