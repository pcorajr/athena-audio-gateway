import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

import athena.state_service_client as public_client
from athena.state_service_client import ClientQueryError, StateServiceClient

from integrations.hermes_radio.contract import MAX_SPEECH_CHARS, parse_request
from integrations.hermes_radio.facts import Facts, event_summary, render
from integrations.hermes_radio.fixture_data import bridge_request, event_row, issue, state_frame, wire
from integrations.hermes_radio.ownship import OwnshipReader
from integrations.hermes_radio.server import Adapter
from integrations.hermes_radio.tests.support import fake_state


def read_frame(transform=lambda value: value, *, intent="current_state", config_changes=None):
    def responder(connection, request):
        value = transform(state_frame(request["operation"], request["request_id"]))
        connection.sendall(value if type(value) is bytes else wire(value))
    with fake_state(responder, config_changes=config_changes) as (config, calls, closed):
        facts = Facts(config)
        try:
            return facts.query(intent)
        finally:
            facts.close()


def exchange(transform=lambda value: value, *, intent="current_state"):
    def responder(connection, request):
        value = transform(state_frame(request["operation"], request["request_id"]))
        connection.sendall(value if type(value) is bytes else wire(value))
    with fake_state(responder) as (config, calls, closed):
        facts = Facts(config)
        try:
            adapter = Adapter(Mock(), facts)
            return adapter.exchange(parse_request(wire(bridge_request("status" if intent == "current_state" else "recent events"))))
        finally:
            facts.close()


def put(path, value):
    def transform(frame):
        node = frame
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return frame
    return transform


class OwnshipTests(unittest.TestCase):
    def test_original_canonical_ownship_socket_regression(self):
        fixture = Path(public_client.__file__).resolve().parents[2] / "tests/fixtures/state_service/e12_domain_queries.json"
        cases = json.loads(fixture.read_text())["cases"]
        canonical = next(case["response"] for case in cases if case["operation"] == "ownship")
        frame = read_frame(lambda value: deepcopy(canonical) | {"request_id": value["request_id"]})
        self.assertTrue(frame.ok)
        self.assertEqual(frame.operation, "ownship")
        self.assertEqual(frame.data["ownship"]["player_name"], "synthetic-player")
        self.assertEqual(frame.context.state.generation, 1)
        self.assertEqual(frame.evidence["derivation"], "ownship.generation_projection.v1")

    def test_factual_template_and_fresh_ids_exact_operation_in_worker(self):
        with fake_state() as (config, calls, closed):
            facts = Facts(config)
            try:
                worker_names = []
                original = facts._ownship.query
                def observe(**kwargs):
                    worker_names.append(threading.current_thread().name)
                    return original(**kwargs)
                # Observe scheduling only; delegate to the unmodified full reader.
                with patch.object(facts._ownship, "query", side_effect=observe):
                    frames = [facts.query("current_state"), facts.query("current_state")]
                result = render(parse_request(wire(bridge_request())), "current_state", frames[0])
                self.assertEqual(result["speech"], "Prometheus, Athena. F-16, heading 270, heading reference unavailable, 350 knots.")
                self.assertEqual(result["state"], "ok")
                self.assertTrue(all(name.startswith("radio-state") for name in worker_names))
                self.assertEqual(len(calls), 2)
                self.assertNotEqual(calls[0]["request_id"], calls[1]["request_id"])
                for request in calls:
                    self.assertEqual(request["operation"], "ownship")
                    self.assertEqual(request["params"], {})
                    self.assertEqual(request["api_version"], "athena.state.v1")
            finally:
                facts.close()
        self.assertEqual(closed, [True, True])

    def test_null_mismatch_and_non_high_identity_never_speak_aircraft(self):
        changes = [put(["data", "ownship"], None), put(["data", "ownship", "player_name"], "Other"),
                   put(["data", "ownship", "player_name"], None)]
        changes += [put(["data", "ownship", "identity_confidence"], value) for value in ("low", "medium", "none", "unknown")]
        for transform in changes:
            result = exchange(transform)
            self.assertTrue(result["deliverable"])
            self.assertEqual(result["state"], "ownship_ambiguous")
            self.assertIn("Ownship not confirmed.", result["speech"])
            self.assertNotIn("F-16", result["speech"])

    def test_stale_degraded_cached_paused_caveats(self):
        for scenario, expected in (("stale", "Stale state."), ("degraded", "Units stream degraded.")):
            result = exchange(lambda frame: state_frame(request_id=frame["request_id"], scenario=scenario))
            self.assertTrue(result["deliverable"])
            self.assertIn(expected, result["speech"])
            self.assertNotEqual(result["state"], "ok")
        def cached(frame):
            frame["context"]["state"]["freshness"]["status"] = "cached"
            frame["context"]["mission"]["state"] = "paused"
            frame["warnings"] = [issue("cached_state")]
            return frame
        result = exchange(cached)
        self.assertIn("Cached state.", result["speech"])
        self.assertIn("Mission paused.", result["speech"])
        self.assertEqual(result["state"], "stale")

    def test_missing_context_health_and_freshness_fail_unavailable(self):
        transforms = [put(["context"], None), put(["context", "component_health"], {}),
                      put(["context", "state", "freshness", "observed_at_utc"], None),
                      put(["context", "state", "freshness", "age_ms"], -1),
                      put(["context", "state", "freshness", "age_ms"], 999),
                      put(["context", "state", "freshness", "status"], "unknown")]
        for transform in transforms:
            result = exchange(transform)
            self.assertEqual(result["speech"], "Athena not connected.")
            self.assertEqual(result["state"], "unavailable")

    def test_ownship_and_position_exact_keys(self):
        for parent in (("data",), ("data", "ownship"), ("data", "ownship", "position")):
            original = state_frame()
            node = original
            for key in parent:
                node = node[key]
            variants = [dict(node) | {"extra": "not allowed"}]
            variants += [{key: value for key, value in node.items() if key != missing} for missing in node]
            for variant in variants:
                with self.subTest(parent=parent, keys=list(variant)):
                    with self.assertRaises(ClientQueryError):
                        read_frame(put([*parent], variant))

    def test_invalid_leaf_types_numbers_and_text(self):
        cases = [("unit_id", True), ("unit_id", None), ("unit_type", 5), ("unit_type", ""),
                 ("player_name", []), ("player_name", "Prometheus\n"), ("player_name", " Prometheus"),
                 ("callsign", "x\u202e"), ("identity_confidence", True), ("provenance_key", {}),
                 ("heading_deg", "270"), ("heading_deg", True), ("heading_deg", -1), ("heading_deg", 361),
                 ("speed_kts", -1), ("speed_kts", False), ("speed_kts", float("nan")),
                 ("speed_kts", float("inf")), ("position", {"lat": 91, "lon": 0, "alt_m": None}),
                 ("position", {"lat": 0, "lon": -181, "alt_m": None}),
                 ("position", {"lat": True, "lon": 0, "alt_m": None}),
                 ("position", {"lat": 0, "lon": 0, "alt_m": "high"})]
        for key, value in cases:
            with self.subTest(key=key, type=type(value).__name__):
                with self.assertRaises(ClientQueryError):
                    read_frame(put(["data", "ownship", key], value))

    def test_evidence_limits_and_context_consistency(self):
        changes = [("generation", 2), ("generation", True), ("mission_id", "other"), ("lineage_id", "other"),
                   ("rows_considered", 1), ("rows_matched", False), ("derivation", "get_unit.generation_projection.v1"),
                   ("health_cut", "other"), ("capabilities", {}),
                   ("provenance", {"source": "sqlite_checkpoint", "generation": 1}),
                   ("provenance", {"source": "live_memory", "generation": True})]
        for key, value in changes:
            with self.subTest(key=key):
                with self.assertRaises(ClientQueryError):
                    read_frame(put(["evidence", key], value))
        for transform in (put(["limits"], {"limit": 1}), put(["evidence"], {}),
                          put(["context", "state", "generation"], 2),
                          put(["context", "state", "source"], "sqlite_checkpoint")):
            with self.assertRaises(ClientQueryError):
                read_frame(transform)

    def test_wrong_frame_correlation_operation_version_and_error(self):
        changes = [("request_id", "wrong"), ("operation", "get_unit"), ("api_version", "athena.state.v2"),
                   ("message_type", "event"), ("ok", "true")]
        for key, value in changes:
            with self.assertRaises(ClientQueryError):
                read_frame(put([key], value))
        def unavailable(frame):
            frame.update(ok=False, context=None, data=None, evidence={}, limits={},
                         errors=[issue("state_unavailable") | {"severity": "error"}])
            return frame
        self.assertEqual(exchange(unavailable)["speech"], "Athena not connected.")

    def test_strict_wire_duplicate_utf8_nonfinite_and_extra_frames(self):
        transforms = [lambda f: wire(f).replace(b'"operation":"ownship"', b'"operation":"ownship","operation":"ownship"'),
            lambda f: wire(f).replace(b'"unit_type":"F-16"', b'"unit_type":"\xff"'),
            lambda f: wire(f).replace(b'"speed_kts":350.0', b'"speed_kts":1e999'),
            lambda f: wire(f) + b"{}\n", lambda f: wire(f) + b"garbage", lambda f: b"\xef\xbb\xbf" + wire(f),
            lambda f: wire(f).replace(b'"lat":0.0', b'"lat":0.0,"lat":0.0')]
        for transform in transforms:
            with self.assertRaises(ClientQueryError):
                read_frame(transform)

    def test_oversized_request_and_response_caps(self):
        with self.assertRaises(ClientQueryError):
            read_frame(config_changes={"max_request_bytes": 1})
        with self.assertRaises(ClientQueryError):
            read_frame(config_changes={"max_response_bytes": 100})

    def test_partial_reads_and_writes_use_real_socket(self):
        original = socket.socket
        writes, reads = [], []
        class PartialSocket:
            def __init__(self, *args, **kwargs):
                self.socket = original(*args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.socket, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.socket.close()
            def send(self, data):
                count = self.socket.send(data[:3])
                writes.append(count)
                return count
            def recv(self, size, flags=0):
                result = self.socket.recv(min(size, 17), flags)
                reads.append(len(result))
                return result
        with fake_state() as (config, calls, closed):
            with patch("integrations.hermes_radio.ownship.socket.socket", PartialSocket):
                frame = OwnshipReader(config).query(request_id="partial-io")
                self.assertTrue(frame.ok)
        self.assertGreater(len(writes), 1)
        self.assertGreater(len(reads), 1)
        self.assertEqual(closed, [True])

    def test_partial_no_newline_and_timeout_close_socket(self):
        def incomplete(connection, request):
            connection.sendall(wire(state_frame(request_id=request["request_id"]))[:-1])
            connection.shutdown(socket.SHUT_WR)
        with fake_state(incomplete) as (config, calls, closed):
            with self.assertRaises(ClientQueryError):
                OwnshipReader(config).query(request_id="incomplete")
        self.assertEqual(closed, [True])
        def slow(connection, request):
            time.sleep(0.12)
        with fake_state(slow, timeout_ms=30) as (config, calls, closed):
            start = time.monotonic()
            with self.assertRaises(ClientQueryError) as caught:
                OwnshipReader(config).query(request_id="timeout")
            self.assertEqual(caught.exception.code, "query_timeout")
            self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(closed, [True])

    def test_cumulative_deadline_includes_validation(self):
        from integrations.hermes_radio.ownship import validate_ownship
        def slow_validation(*args, **kwargs):
            result = validate_ownship(*args, **kwargs)
            time.sleep(0.06)
            return result
        with fake_state(timeout_ms=30) as (config, calls, closed):
            with patch("integrations.hermes_radio.ownship.validate_ownship", side_effect=slow_validation):
                with self.assertRaises(ClientQueryError) as caught:
                    OwnshipReader(config).query(request_id="validation-budget")
            self.assertEqual(caught.exception.code, "query_timeout")

    def test_missing_refused_socket_no_fallback(self):
        with fake_state() as (config, calls, closed):
            pass
        with self.assertRaises(ClientQueryError):
            OwnshipReader(config).query(request_id="missing")
        with fake_state() as (config, calls, closed):
            path = config.socket_path.with_name("refused.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unlistened:
                unlistened.bind(str(path))
                with patch("metis.state_service.config._SOCKET_ROOT", path.parent):
                    refused = replace(config, socket_path=path)
                with self.assertRaises(ClientQueryError):
                    OwnshipReader(refused).query(request_id="refused")

    def test_async_loop_is_rejected_but_facts_worker_is_allowed(self):
        with fake_state() as (config, calls, closed):
            facts = Facts(config)
            async def check():
                with self.assertRaises(ClientQueryError) as caught:
                    OwnshipReader(config).query(request_id="loop")
                self.assertEqual(caught.exception.code, "event_loop_blocking")
                self.assertTrue(facts.query("current_state").ok)
            try:
                asyncio.run(check())
            finally:
                facts.close()

    def test_unknown_warning_and_overlong_caveat_suppress_not_clip(self):
        result = exchange(put(["warnings"], [issue("future_caveat")]))
        self.assertFalse(result["deliverable"])
        self.assertEqual(result["speech"], "")
        result = exchange(put(["context", "state", "freshness", "stale_reason"], "x" * (MAX_SPEECH_CHARS + 50)))
        self.assertFalse(result["deliverable"])
        self.assertEqual(result["speech"], "")
        self.assertEqual(result["suppression_reason"], "speech_budget_exceeded")

    def test_ownship_unavailable_warning_cannot_admit_aircraft_facts(self):
        result = exchange(put(["warnings"], [issue("ownship_unavailable")]))
        self.assertEqual(result["speech"], "Athena not connected.")
        self.assertNotIn("F-16", result["speech"])
        result = exchange(put(["warnings"], [issue("ownship_unavailable")]), intent="recent_events")
        self.assertFalse(result["deliverable"])

    def test_missing_aircraft_fields_are_not_invented(self):
        def missing(frame):
            frame["data"]["ownship"].update(unit_type=None, heading_deg=None, speed_kts=None, provenance_key=None)
            return frame
        result = exchange(missing)
        self.assertTrue(result["deliverable"])
        for text in ("Aircraft type unavailable", "heading unavailable", "speed unavailable", "provenance unavailable"):
            self.assertIn(text, result["speech"])

    def test_negotiation_and_protocol_error_frames_are_not_query_success(self):
        def negotiated(frame):
            return {"message_type": "negotiated", "request_id": frame["request_id"], "ok": True,
                    "selected_api_version": "athena.state.v1", "supported_api_versions": ["athena.state.v1"], "error": None}
        def protocol_error(frame):
            return {"message_type": "protocol_error", "api_version": "athena.state.v1",
                    "request_id": frame["request_id"], "error": issue("synthetic_error") | {"severity": "error"}, "close": True}
        for transform in (negotiated, protocol_error):
            with self.assertRaises(ClientQueryError):
                read_frame(transform)

    def test_cumulative_deadline_across_partial_reads_and_cleanup(self):
        def drip(connection, request):
            try:
                for chunk in (b'{', b'"', b'x', b'"', b':', b'0', b'}', b'\n'):
                    connection.sendall(chunk)
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
        with fake_state(drip, timeout_ms=50) as (config, calls, closed):
            start = time.monotonic()
            with self.assertRaises(ClientQueryError) as caught:
                OwnshipReader(config).query(request_id="drip")
            self.assertEqual(caught.exception.code, "query_timeout")
            self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(closed, [True])

    def test_direct_facts_concurrency_is_not_queued(self):
        entered, release = threading.Event(), threading.Event()
        def held(connection, request):
            entered.set()
            if not release.wait(1):
                raise AssertionError("test release timeout")
            connection.sendall(wire(state_frame(request_id=request["request_id"])))
        with fake_state(held, timeout_ms=1500) as (config, calls, closed):
            facts = Facts(config)
            results = []
            thread = threading.Thread(target=lambda: results.append(facts.query("current_state")))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(ClientQueryError) as caught:
                    facts.query("recent_events")
                self.assertEqual(caught.exception.code, "busy")
            finally:
                release.set()
                thread.join(2)
                facts.close()
        self.assertEqual(len(calls), 1)
        self.assertTrue(results[0].ok)

    def test_fresh_flag_does_not_erase_exceeded_age_or_checkpoint(self):
        def aged(frame):
            # Keep the producer's timestamp/age relation valid: this test is
            # about an exceeded supplied budget, not contradictory evidence.
            frame["generated_at_utc"] = "2026-01-01T00:00:02Z"
            frame["context"]["state"]["age_ms"] = 2000
            frame["context"]["state"]["freshness"]["age_ms"] = 2000
            return frame
        result = exchange(aged)
        self.assertEqual(result["state"], "stale")
        self.assertIn("Stale state.", result["speech"])
        def checkpoint(frame):
            frame["context"]["state"]["source"] = "sqlite_checkpoint"
            frame["evidence"]["provenance"]["source"] = "sqlite_checkpoint"
            return frame
        result = exchange(checkpoint)
        self.assertEqual(result["state"], "stale")
        self.assertIn("Cached state.", result["speech"])
        result = exchange(put(["context", "state", "freshness", "max_age_ms"], None))
        self.assertEqual(result["state"], "degraded")
        self.assertIn("Freshness budget unavailable.", result["speech"])


class RecentEventsTests(unittest.TestCase):
    def test_public_client_exact_limit_and_fresh_ids(self):
        with fake_state() as (config, calls, closed):
            facts = Facts(config)
            self.assertIs(type(facts._events), StateServiceClient)
            try:
                for _ in range(2):
                    frame = facts.query("recent_events")
                    self.assertEqual(frame.data["events"][0]["event_type"], "mission_started")
            finally:
                facts.close()
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["request_id"], calls[1]["request_id"])
        self.assertTrue(all(r["operation"] == "recent_events" and r["params"] == {"limit": 3} for r in calls))
        self.assertEqual(closed, [True, True])

    def test_empty_valid_page_distinct_from_failure(self):
        def empty(frame):
            return state_frame("recent_events", frame["request_id"], events=[])
        result = exchange(empty, intent="recent_events")
        self.assertTrue(result["deliverable"])
        self.assertEqual(result["state"], "ok")
        self.assertIn("No recorded events yet.", result["speech"])
        def unavailable(frame):
            frame.update(ok=False, data=None, context=None, evidence={}, limits={},
                errors=[issue("normalized_event_store_unavailable") | {"severity": "error"}])
            return frame
        result = exchange(unavailable, intent="recent_events")
        self.assertEqual(result["speech"], "Athena not connected.")
        self.assertNotIn("No recorded events", result["speech"])

    def test_public_validator_rejects_invalid_rows_evidence_order_counts(self):
        cases = [put(["data", "events", 0, "event_id"], "invented"),
                 put(["data", "events", 0, "event_type"], "future_kind"),
                 put(["data", "events", 0, "data"], {"invented": "field"}),
                 put(["data", "events", 0, "source_generation_ids"], [1]),
                 put(["evidence", "raw_evidence_available"], False),
                 put(["limits", "total_count"], 2), put(["limits", "limit"], 2),
                 put(["context"], None)]
        for transform in cases:
            result = exchange(transform, intent="recent_events")
            self.assertEqual(result["speech"], "Athena not connected.")
        def wrong_order(frame):
            return state_frame("recent_events", frame["request_id"], events=[event_row(sequence=1), event_row(sequence=2)])
        self.assertEqual(exchange(wrong_order, intent="recent_events")["state"], "unavailable")

    def test_last_three_events_and_truncation_caveat(self):
        def truncated(frame):
            return state_frame("recent_events", frame["request_id"], events=[event_row(sequence=i) for i in (3, 2, 1)], total=4)
        result = exchange(truncated, intent="recent_events")
        self.assertIn("Last events:", result["speech"])
        self.assertIn("More recorded events omitted.", result["speech"])
        self.assertLessEqual(len(result["speech"]), 350)

    def test_supported_kind_without_template_uses_known_timestamp(self):
        def reserved(frame):
            event = event_row("objective_added", data={"mission_event_id": "synthetic-objective",
                "payload": {"objective_id": "fixture", "state": "active"}, "time_source": "raw_envelope"})
            return state_frame("recent_events", frame["request_id"], events=[event])
        result = exchange(reserved, intent="recent_events")
        self.assertTrue(result["deliverable"])
        self.assertIn("event recorded at 00:00:00 UTC", result["speech"])
        self.assertNotIn("synthetic-objective", result["speech"])
        self.assertEqual(event_summary({"event_type": "future", "observed_at_utc": "2026-01-01T12:00:00Z"}),
                         "event recorded at 12:00:00 UTC")

    def test_row_warnings_suppress_and_unhealthy_empty_keeps_qualifier(self):
        result = exchange(put(["data", "events", 0, "warnings"], ["unknown qualification"]), intent="recent_events")
        self.assertFalse(result["deliverable"])
        def unhealthy(frame):
            frame = state_frame("recent_events", frame["request_id"], events=[])
            frame["context"]["component_health"]["events_stream"]["status"] = "unavailable"
            return frame
        result = exchange(unhealthy, intent="recent_events")
        self.assertTrue(result["deliverable"])
        self.assertEqual(result["state"], "degraded")
        self.assertIn("Events stream unavailable.", result["speech"])

    def test_missing_recent_event_limits_fails_startup(self):
        with fake_state() as (config, calls, closed):
            # replace revalidates path; relocate only the test path root.
            with patch("metis.state_service.config._SOCKET_ROOT", config.socket_path.parent):
                without_events = replace(config, recent_events_limits=None)
            with self.assertRaises(ValueError):
                Facts(without_events)

    def test_configured_event_row_caps_are_respected(self):
        for general, recent in ((1, 10), (10, 2)):
            with fake_state() as (config, calls, closed):
                with patch("metis.state_service.config._SOCKET_ROOT", config.socket_path.parent):
                    config = replace(config, query_limits=replace(config.query_limits, max_rows=general),
                        recent_events_limits=replace(config.recent_events_limits, max_rows=recent))
                facts = Facts(config)
                try:
                    # Fake's default response deliberately still claims limit3:
                    # the unchanged client must reject, not weaken the cap.
                    with self.assertRaises(ClientQueryError):
                        facts.query("recent_events")
                    self.assertEqual(calls[0]["params"], {"limit": min(3, general, recent)})
                finally:
                    facts.close()

    def test_recent_event_missing_or_contradictory_freshness_fails_safe(self):
        for key, value in (("observed_at_utc", None), ("age_ms", -1), ("age_ms", 2000), ("max_age_ms", -1)):
            result = exchange(put(["context", "state", "freshness", key], value), intent="recent_events")
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["speech"], "Athena not connected.")
