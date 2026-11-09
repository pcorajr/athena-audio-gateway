"""Synthetic-only fixtures. Not imported by the production entry point."""
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json

from metis.state_service.capabilities import capabilities_for_component, evaluate_all_capabilities
from metis.state_service.domain_queries import QueryLimits
from metis.state_service.lifecycle import ComponentHealthStatus, HealthComponent
from metis.state_service.models import ComponentHealth
from metis.state_service.recent_event_contract import RecentEventsLimits
from metis.state_service.serialization import STATE_API_VERSION, component_health_to_dict
from metis.state_service.socket_protocol import SocketProtocolLimits

LIMITS = SocketProtocolLimits(65536, 65536)
QUERY_LIMITS = QueryLimits(10, 10, 10, 10)
RECENT_LIMITS = RecentEventsLimits(max_rows=10, max_event_types=10, max_event_type_bytes=100,
    max_subject_bytes=200, max_since_utc_bytes=40, max_text_bytes=500, max_data_bytes=4000,
    max_warning_count=10, max_warning_bytes=500, max_subjects_per_event=10, max_source_id_bytes=100)
STAMP = "2026-01-01T00:00:00Z"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def bridge_request(transcript="status"):
    return {"transmission_id": "synthetic-transmission", "transcript": transcript, "speaker": "Prometheus",
            "frequency_hz": 133000000, "modulation": "AM", "received_at": STAMP, "transcribed_at": STAMP}


def issue(code):
    return {"code": code, "severity": "warning", "message": "Synthetic qualifier.",
            "field_paths": [], "source": "domain_query", "retryable": False, "details": {}}


def state_frame(operation="ownship", request_id="synthetic-state", *, scenario="current_state", events=None, total=None):
    health = {component: ComponentHealth(component, ComponentHealthStatus.HEALTHY, NOW, NOW,
              None, None, 0, tuple(c.value for c in capabilities_for_component(component))) for component in HealthComponent}
    if scenario == "degraded":
        component = HealthComponent.UNITS_STREAM
        health[component] = ComponentHealth(component, ComponentHealthStatus.DEGRADED, NOW, NOW,
            "fixture_degraded", "Synthetic degradation.", 0, tuple(c.value for c in capabilities_for_component(component)))
    context = {"mission": {"mission_id": "dcs-mission-59395ee8-d91b-56f4-bc4a-ec58af29bc66",
        "mission_filename": "synthetic.miz", "mission_name": "Synthetic Mission", "session_id": 1,
        "scenario_start_identity": STAMP, "state": "running"},
        "state": {"lineage_id": "synthetic-lineage", "generation": 1, "source": "live_memory",
            "mission_time_seconds": 0.0, "published_at_utc": STAMP, "age_ms": 0,
            "freshness": {"status": "fresh", "age_ms": 0, "max_age_ms": 1000.0,
                          "observed_at_utc": STAMP, "stale_reason": None}},
        "component_health": {c.value: component_health_to_dict(h) for c, h in health.items()}}
    if scenario == "stale":
        context["state"]["freshness"]["status"] = "stale"
        context["state"]["freshness"]["stale_reason"] = "source_stale"
    ownship = {"unit_id": "synthetic-unit", "unit_name": "Synthetic Unit", "player_name": "Prometheus",
               "callsign": "TEST01", "coalition": "blue", "unit_type": "F-16",
               "position": {"lat": 0.0, "lon": 0.0, "alt_m": 1000.0},
               "heading_deg": 270.0, "speed_kts": 350.0, "identity_confidence": "high",
               "provenance_key": "ownship.synthetic-unit"}
    if scenario == "ambiguous":
        ownship["identity_confidence"] = "low"
    capabilities = {key.value: {"status": value.status.value,
        "required": [c.value for c in value.required], "blocking": [c.value for c in value.blocking],
        "advisory": [c.value for c in value.advisory], "reasons": list(value.reasons)}
        for key, value in evaluate_all_capabilities(health).items()}
    evidence = {"generation": 1, "mission_id": context["mission"]["mission_id"],
        "lineage_id": "synthetic-lineage", "rows_considered": 0, "rows_matched": 0,
        "derivation": "ownship.generation_projection.v1", "health_cut": "generation",
        "capabilities": capabilities, "provenance": {"source": "live_memory", "generation": 1}}
    limits, data, warnings = {}, {"ownship": ownship}, []
    if operation == "recent_events":
        events = [event_row()] if events is None else events
        total = len(events) if total is None else total
        data = {"events": events, "total": total}
        evidence = {"source_kind": "normalized_direct_event_ledger", "raw_evidence_available": True}
        limits = {"limit": 3, "row_cap": 10, "returned_count": len(events), "total_count": total,
                  "truncated": len(events) < total}
        if limits["truncated"]:
            warnings.append(issue("truncated_result"))
    return {"message_type": "response", "api_version": STATE_API_VERSION, "request_id": request_id,
            "operation": operation, "ok": True, "generated_at_utc": STAMP, "context": context,
            "data": data, "evidence": evidence, "warnings": warnings, "errors": [], "limits": limits}


def event_row(kind="mission_started", *, sequence=1, data=None):
    source = "raw-dcs-event-" + hashlib.sha256(str(sequence).encode("ascii")).hexdigest()
    normalizer = "mission_event.v1" if kind == "objective_added" else "direct_event.v1"
    digest = hashlib.sha256(json.dumps(["athena.event.v1", source, normalizer, kind],
        ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()
    return {"schema_version": "athena.event.v1", "event_id": "athena-event-" + digest,
        "source_sequence": sequence, "event_type": kind, "observed_at_utc": STAMP,
        "mission_time_seconds": 0.0, "evidence_class": "observed", "source_event_ids": [source],
        "source_generation_ids": [], "subject_ids": [], "data": {} if data is None else data,
        "warnings": [], "normalizer_id": normalizer}


def client_document(path, *, timeout_ms=500):
    return {"socket_path": str(path), "max_request_bytes": LIMITS.max_request_bytes,
        "max_response_bytes": LIMITS.max_response_bytes, "query_timeout_ms": timeout_ms,
        "query_limits": asdict(QUERY_LIMITS), "recent_events_limits": asdict(RECENT_LIMITS)}


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
