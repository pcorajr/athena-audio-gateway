"""Allowlisted failure codes only; never log exception bodies or state errors."""
from athena.state_service_client import ClientQueryError

SAFE_CODES = frozenset({
    "socket_unavailable", "query_timeout", "response_too_large", "protocol_error",
    "version_mismatch", "correlation_mismatch", "operation_mismatch",
    "event_loop_blocking", "unsupported_operation", "invalid_params",
    "recent_events_config_required", "state_unavailable", "ownship_unavailable",
    "scope_changed", "contradictory_ownship_coalition", "not_found", "busy",
    "evidence_expired", "freshness_not_fresh", "mission_not_running",
    "source_not_live", "component_unhealthy", "unsupported_state_warning",
    "provider_failed", "state_query_failed", "invalid_arguments",
})


def safe_code(code):
    return code if type(code) is str and code in SAFE_CODES else "state_query_failed"


def failure_code(error):
    if isinstance(error, ClientQueryError):
        return safe_code(error.code)
    return "protocol_error"


def response_failure(frame):
    # Decoded unsuccessful envelopes carry structured errors, not trustworthy
    # free-form messages. Only a single recognized error code survives.
    code = frame.errors[0].code if len(frame.errors) == 1 else None
    return ClientQueryError(safe_code(code))
