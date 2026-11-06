"""Admission of supplied factual context, not a freshness or health authority."""
import math

from athena.state_service_client import ClientQueryError
from metis.state_service.capabilities import capabilities_for_component
from metis.state_service.lifecycle import ComponentHealthStatus, HealthComponent
from metis.state_service.socket_protocol import SocketResponseContext

# These are the components whose qualifications the two existing templates use.
RELEVANT_HEALTH = {
    "current_state": (HealthComponent.GENERATION_PUBLISHER, HealthComponent.UNITS_STREAM,
                      HealthComponent.UNIX_SOCKET_QUERY_SERVER),
    "recent_events": (HealthComponent.EVENTS_STREAM, HealthComponent.UNIX_SOCKET_QUERY_SERVER),
}


def validate_factual_context(frame, intent):
    """Reject contradictions in decoded evidence; never modify source metadata."""
    if type(frame.context) is not SocketResponseContext:
        raise ClientQueryError("state_unavailable")
    state = frame.context.state
    fresh = state.freshness
    if (type(fresh.age_ms) not in (int, float) or not math.isfinite(fresh.age_ms) or
            fresh.age_ms < 0 or fresh.age_ms != state.age_ms or fresh.observed_at_utc is None or
            state.published_at_utc > frame.generated_at_utc):
        raise ClientQueryError("state_unavailable")
    # Match DomainQueryDispatcher._success's exact integer-millisecond rule.
    # This compares supplied evidence, not age against a live clock or new budget.
    age = (frame.generated_at_utc - fresh.observed_at_utc).total_seconds() * 1000
    if not math.isfinite(age) or age < 0 or fresh.age_ms != int(age):
        raise ClientQueryError("state_unavailable")

    for component in RELEVANT_HEALTH[intent]:
        health = frame.context.component_health.get(component)
        if (health is None or health.component is not component or
                health.required_for != tuple(c.value for c in capabilities_for_component(component))):
            raise ClientQueryError("state_unavailable")
        # HealthRegistry only enters healthy via mark_success, clearing errors.
        # Other statuses may legitimately retain success and/or error metadata.
        if health.status is ComponentHealthStatus.HEALTHY and (
                health.last_success_utc is None or health.last_error_code is not None or
                health.last_error_message is not None):
            raise ClientQueryError("state_unavailable")

    # SocketIssue admits both severities regardless of its containing array.
    # Check every issue before any code-based qualifier mapping or suppression.
    if any(issue.severity != "warning" for issue in frame.warnings):
        raise ClientQueryError("state_unavailable")
