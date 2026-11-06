"""Producer-grounded factual admission, over real test-owned AF_UNIX sockets."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock

from athena.state_service_client import ClientQueryError, StateServiceClient
from metis.models import UnitData
from metis.state_service.capabilities import capabilities_for_component
from metis.state_service.direct_event_store import RecentNormalizedEventsPage
from metis.state_service.domain_queries import DomainQueryDispatcher
from metis.state_service.generation_scope_port import StateQueryCut
from metis.state_service.health import HealthRegistry
from metis.state_service.lifecycle import ComponentHealthStatus, HealthComponent, MissionRunState
from metis.state_service.mission_identity import MissionSessionIdentity, mission_id_from_filename
from metis.state_service.mission_scope import MissionScope
from metis.state_service.publisher import GenerationPublisher
from metis.state_service.restore import RestoreOutcome
from metis.state_service.socket_protocol import StateQueryRequest, encode_server_frame
from metis.state_service.units_stream import UnitStreamSnapshot
from metis.state_service.working_state import PublicationWindow

from integrations.hermes_radio.contract import parse_request
from integrations.hermes_radio.facts import Facts
from integrations.hermes_radio.fixture_data import (
    LIMITS, NOW, QUERY_LIMITS, RECENT_LIMITS, STAMP, bridge_request, issue, wire,
)
from integrations.hermes_radio.server import Adapter
from integrations.hermes_radio.tests.support import fake_state
from integrations.hermes_radio.tests.test_facts import exchange, put, read_frame

INTENTS = ("current_state", "recent_events")
# Explicit test expectations, independent of the adapter's relevant-health set.
RELEVANT = {
    "current_state": ("units_stream", "generation_publisher", "unix_socket_query_server"),
    "recent_events": ("events_stream", "unix_socket_query_server"),
}


def source_response(request, *, mode="normal", elapsed_us=0, health_status=None,
                    had_success=True):
    """Actual public producers, synthetic in-memory inputs; no store or DCS I/O."""
    identity = MissionSessionIdentity(mission_id_from_filename("synthetic.miz"), "synthetic.miz",
                                      "Synthetic Mission", 1, STAMP)
    scope = MissionScope(identity, "producer-synthetic-lineage", None, 0, None, RestoreOutcome.COLD_START)
    unit = UnitData(unit_id=1, unit_name="Synthetic Unit", player_name="Prometheus",
        lat=0.0, lon=0.0, alt=1000.0, heading=270.0, pitch=0.0, roll=0.0, coalition=3,
        unit_type="F-16", velocity_x=100.0, velocity_y=0.0, velocity_z=0.0, speed=100.0,
        group_id=1, group_name="Synthetic Group", number_in_group=1, callsign="TEST01")
    snapshot = UnitStreamSnapshot(scope, identity, {1: unit}, 1, 0.0, NOW, True)
    registry = HealthRegistry(NOW - timedelta(seconds=2))
    for component in HealthComponent:
        if had_success:
            registry.mark_success(component, NOW - timedelta(seconds=1))
        if health_status is not None:
            registry.mark_status(component, health_status, NOW,
                                 error_code="synthetic_source_caveat", error_message="Synthetic caveat.")
    publisher = GenerationPublisher(clock=lambda: NOW,
                                    pilot_player_name="Other" if mode == "null" else "Prometheus")
    generation = publisher.publish(PublicationWindow(scope, 1, 1, 1, 1), snapshot,
        component_health=registry.snapshot(), selected_unit_id=1 if mode == "operator" else None)
    if mode == "cached":
        generation = replace(generation, mission=replace(generation.mission, state=MissionRunState.PAUSED),
            freshness=replace(generation.freshness, status="cached", stale_reason="mission_paused"))
    cut = StateQueryCut(generation, identity, scope.lineage_id, 0)

    class Owner:
        def capture_query_cut(self):
            return cut
        def query_cut_still_valid(self, value):
            assert value is cut
            return "mission_changed_during_query" if mode == "rollover" else None

    class EmptyEvents:
        async def recent_events(self, query):
            assert (query.mission_id, query.session_id, query.lineage_id, query.limit) == (
                identity.mission_id, identity.session_id, scope.lineage_id, 3)
            return RecentNormalizedEventsPage(rows=(), total_count=0, has_more=False, limits=RECENT_LIMITS)

    dispatcher = DomainQueryDispatcher(owner=Owner(), limits=QUERY_LIMITS,
        utc_clock=lambda: NOW + timedelta(microseconds=elapsed_us),
        recent_events_reader=EmptyEvents(), recent_events_limits=RECENT_LIMITS)
    return asyncio.run(dispatcher.dispatch(StateQueryRequest(request["api_version"], request["request_id"],
                                                            request["operation"], request["params"]), None))


def producer_exchange(intent="current_state", *, transform=lambda value: value, **options):
    def responder(connection, request):
        produced = source_response(request, **options)
        document = json.loads(encode_server_frame(produced, limits=LIMITS))
        connection.sendall(wire(transform(document)))
    with fake_state(responder) as (config, calls, closed):
        facts = Facts(config)
        assert type(facts._events) is StateServiceClient
        try:
            classifier = Mock()
            result = Adapter(classifier, facts).exchange(parse_request(wire(bridge_request(
                "status" if intent == "current_state" else "recent events"))))
            classifier.classify.assert_not_called()
        finally:
            facts.close()
    assert len(calls) == 1
    assert calls[0]["operation"] == ("ownship" if intent == "current_state" else "recent_events")
    assert calls[0]["params"] == ({} if intent == "current_state" else {"limit": 3})
    assert closed == [True]
    return result


class ContextAdmissionTests(unittest.TestCase):
    def assert_unavailable(self, result):
        self.assertTrue(result["deliverable"])
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["speech"], "Athena not connected.")

    def test_timestamp_age_contradiction_both_intents(self):
        for intent in INTENTS:
            with self.subTest(intent=intent):
                self.assert_unavailable(exchange(put(["generated_at_utc"], "2026-01-01T01:00:00Z"), intent=intent))
                self.assert_unavailable(producer_exchange(intent,
                    transform=put(["generated_at_utc"], "2026-01-01T01:00:00Z")))

    def test_ownship_reader_itself_rejects_inconsistent_age(self):
        with self.assertRaises(ClientQueryError):
            read_frame(put(["generated_at_utc"], "2026-01-01T01:00:00Z"))

    def test_age_must_match_integer_milliseconds_not_rounding(self):
        def contradiction(frame):
            frame["generated_at_utc"] = "2026-01-01T00:00:00.001999Z"
            frame["context"]["state"]["age_ms"] = 2
            frame["context"]["state"]["freshness"]["age_ms"] = 2
            return frame
        for intent in INTENTS:
            with self.subTest(intent=intent):
                self.assert_unavailable(exchange(contradiction, intent=intent))

    def test_future_observation_is_not_truncated_to_zero_age(self):
        for intent in INTENTS:
            with self.subTest(intent=intent):
                self.assert_unavailable(exchange(put(["context", "state", "freshness", "observed_at_utc"],
                                                     "2026-01-01T00:00:00.000001Z"), intent=intent))

    def test_producer_integer_age_boundary_controls_both_intents(self):
        for elapsed in (0, 1, 999, 1000, 1001, 1999, 999999, 1000000, 1001000, 3600000000):
            for intent in INTENTS:
                with self.subTest(elapsed_us=elapsed, intent=intent):
                    result = producer_exchange(intent, elapsed_us=elapsed)
                    self.assertTrue(result["deliverable"])
                    self.assertEqual(result["state"], "degraded")
                    self.assertIn("Freshness budget unavailable.", result["speech"])
                    self.assertIn("F-16, heading 270, 194 knots." if intent == "current_state"
                                  else "No recorded events yet.", result["speech"])

    def test_producer_health_and_warning_invariants(self):
        for operation, params in (("ownship", {}), ("recent_events", {"limit": 3})):
            request = {"api_version": "athena.state.v1", "request_id": "source-control",
                       "operation": operation, "params": params}
            frame = source_response(request, elapsed_us=3600000000)
            self.assertEqual(frame.context.state.age_ms, 3600000)
            self.assertEqual(frame.context.state.freshness.age_ms, 3600000)
            for component, health in frame.context.component_health.items():
                self.assertEqual(health.status, ComponentHealthStatus.HEALTHY)
                self.assertIsNotNone(health.last_success_utc)
                self.assertIsNone(health.last_error_code)
                self.assertIsNone(health.last_error_message)
                self.assertEqual(health.required_for, tuple(c.value for c in capabilities_for_component(component)))
            cached = source_response(request, mode="cached")
            self.assertEqual([(w.code, w.severity) for w in cached.warnings], [("cached_state", "warning")])

    def test_healthy_without_success_rejected_for_each_relevant_component(self):
        for intent in INTENTS:
            for component in RELEVANT[intent]:
                with self.subTest(intent=intent, component=component):
                    self.assert_unavailable(producer_exchange(intent, transform=put(
                        ["context", "component_health", component, "last_success_utc"], None)))

    def test_healthy_with_either_error_field_rejected_for_each_relevant_component(self):
        for intent in INTENTS:
            for component in RELEVANT[intent]:
                for field, value in (("last_error_code", "source_failed"),
                                     ("last_error_message", "Synthetic source failure.")):
                    with self.subTest(intent=intent, component=component, field=field):
                        self.assert_unavailable(producer_exchange(intent, transform=put(
                            ["context", "component_health", component, field], value)))

    def test_wrong_dependency_declarations_rejected_for_each_relevant_component(self):
        for intent in INTENTS:
            for component in RELEVANT[intent]:
                for value in (["invented_capability"], ["durable_recovery"]):
                    with self.subTest(intent=intent, component=component, value=value):
                        self.assert_unavailable(producer_exchange(intent, transform=put(
                            ["context", "component_health", component, "required_for"], value)))

    def test_nonhealthy_producer_records_remain_qualified_with_or_without_success(self):
        for status in (ComponentHealthStatus.DEGRADED, ComponentHealthStatus.STALE,
                       ComponentHealthStatus.UNAVAILABLE):
            for had_success in (True, False):
                for intent in INTENTS:
                    with self.subTest(status=status, had_success=had_success, intent=intent):
                        result = producer_exchange(intent, health_status=status, had_success=had_success)
                        self.assertTrue(result["deliverable"])
                        self.assertEqual(result["state"], "degraded")
                        for component in RELEVANT[intent]:
                            self.assertIn(component.replace("_", " ").capitalize() + " " + status.value + ".",
                                          result["speech"])
                        self.assertNotIn("Synthetic caveat.", result["speech"])

    def test_error_severity_in_warnings_rejected_before_code_mapping(self):
        for intent in INTENTS:
            for code in ("cached_state", "truncated_result", "ownship_unavailable", "future_caveat"):
                with self.subTest(intent=intent, code=code):
                    self.assert_unavailable(exchange(put(["warnings"], [issue(code) | {"severity": "error"}]),
                                                     intent=intent))

    def test_producer_cached_warning_controls_and_error_mutations_both_intents(self):
        for intent in INTENTS:
            with self.subTest(intent=intent):
                valid = producer_exchange(intent, mode="cached")
                self.assertTrue(valid["deliverable"])
                self.assertEqual(valid["state"], "stale")
                for caveat in ("Cached state.", "Mission paused.", "State caveat: mission paused."):
                    self.assertIn(caveat, valid["speech"])
                self.assert_unavailable(producer_exchange(intent, mode="cached",
                    transform=put(["warnings", 0, "severity"], "error")))

    def test_producer_identity_null_operator_and_rollover_stay_fail_closed(self):
        for mode in ("null", "operator", "rollover"):
            with self.subTest(mode=mode):
                result = producer_exchange(mode=mode)
                if mode == "rollover":
                    self.assert_unavailable(result)
                else:
                    self.assertEqual(result["state"], "ownship_ambiguous")
                    self.assertIn("Ownship not confirmed.", result["speech"])
                    self.assertNotIn("F-16", result["speech"])

    def test_canonical_opaque_provenance_key_is_not_given_an_invented_prefix_rule(self):
        result = exchange(put(["data", "ownship", "provenance_key"], "opaque-source-reference"))
        self.assertEqual(result["state"], "ok")
        self.assertIn("F-16", result["speech"])
