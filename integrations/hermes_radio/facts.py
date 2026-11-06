"""Two fixed factual reads and deterministic, caveat-preserving speech."""
from concurrent.futures import ThreadPoolExecutor
import math
import threading
import uuid

from athena.state_service_client import ClientQueryError, StateServiceClient, load_client_config

from .context import RELEVANT_HEALTH, validate_factual_context
from .contract import MAX_SPEECH_CHARS, response, safe_text
from .ownship import OwnshipReader

_EVENT_LABELS = {
    "mission_started": "mission started", "mission_ended": "mission ended",
    "base_captured": "base captured", "player_connected": "player connected",
    "player_disconnected": "player disconnected", "player_entered_unit": "player entered aircraft",
    "player_left_unit": "player left aircraft", "player_changed_slot": "player changed slot",
    "weapon_launched": "weapon launched", "weapon_hit": "weapon hit",
    "unit_killed": "unit killed", "entity_dead": "entity dead", "pilot_dead": "pilot dead",
    "unit_lost": "unit lost", "simulation_fps_observed": "simulation rate recorded",
    "human_failure_observed": "human failure recorded", "detailed_failure_observed": "failure recorded",
}


class Facts:
    """Serial worker for blocking socket reads, with no queued concurrent work."""
    def __init__(self, config):
        if config.recent_events_limits is None:
            raise ValueError("recent_events_config_required")
        self._ownship = OwnshipReader(config)
        self._events = StateServiceClient(config)
        self._limit = min(3, config.query_limits.max_rows, config.recent_events_limits.max_rows)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radio-state")
        self._active = threading.Lock()

    @classmethod
    def from_path(cls, path):
        return cls(load_client_config(path))

    def query(self, intent):
        if not self._active.acquire(blocking=False):
            raise ClientQueryError("busy")
        try:
            request_id = uuid.uuid4().hex
            if intent == "current_state":
                future = self._worker.submit(self._ownship.query, request_id=request_id)
            elif intent == "recent_events":
                future = self._worker.submit(self._events.query, operation="recent_events",
                                             params={"limit": self._limit}, request_id=request_id)
            else:
                raise ClientQueryError("unsupported_intent")
            return future.result()
        finally:
            self._active.release()

    def close(self):
        self._worker.shutdown(wait=True, cancel_futures=True)


def _caveats(frame, intent):
    # The public recent-events client still owns row/protocol semantics. Shared
    # admission checks only supplied context consistency before either template.
    validate_factual_context(frame, intent)
    context = frame.context
    state, fresh = context.state, context.state.freshness
    caveats, status = [], "ok"
    if fresh.max_age_ms is None:
        caveats.append("Freshness budget unavailable.")
        status = "degraded"
    elif (type(fresh.max_age_ms) not in (int, float) or
          not math.isfinite(fresh.max_age_ms) or fresh.max_age_ms < 0):
        raise ClientQueryError("state_unavailable")
    if fresh.status in {"unknown", "unavailable"}:
        raise ClientQueryError("state_unavailable")
    if (fresh.status == "stale" or
            (fresh.age_ms is not None and fresh.max_age_ms is not None and fresh.age_ms > fresh.max_age_ms)):
        caveats.append("Stale state.")
        status = "stale"
    if fresh.status == "cached" or state.source.value == "sqlite_checkpoint":
        caveats.append("Cached state.")
        status = "stale"
    if context.mission.state.value == "paused":
        caveats.append("Mission paused.")
        status = "stale"
    elif context.mission.state.value == "ended":
        caveats.append("Mission ended.")
        status = "stale"
    if fresh.stale_reason:
        safe_text(fresh.stale_reason)
        caveats.append("State caveat: " + fresh.stale_reason.replace("_", " ") + ".")
        if status == "ok":
            status = "degraded"
    for component in RELEVANT_HEALTH[intent]:
        health = context.component_health[component]
        if health.status.value != "healthy":
            caveats.append(component.value.replace("_", " ").capitalize() + " " + health.status.value + ".")
            if status == "ok":
                status = "degraded"
    # Explicitly known qualifiers only. An unknown warning must not be
    # silently discarded or rephrased into an unqualified factual answer.
    known = {"ownship_unavailable": "Ownship not confirmed.",
             "cached_state": "Cached state.",
             "truncated_result": "More recorded events omitted."}
    for issue in frame.warnings:
        if (issue.code not in known or
                (issue.code == "truncated_result" and intent != "recent_events") or
                (issue.code == "ownship_unavailable" and intent != "current_state")):
            return None, "degraded"
        if issue.code != "ownship_unavailable":
            caveats.append(known[issue.code])
            if status == "ok":
                status = "degraded"
    if intent == "recent_events":
        if frame.limits["truncated"]:
            caveats.append("More recorded events omitted.")
        if any(event["warnings"] for event in frame.data["events"]):
            return None, "degraded"
    return list(dict.fromkeys(caveats)), status


def event_summary(event):
    """Only public known fields; no raw payloads, attribution or tactical inference."""
    label = _EVENT_LABELS.get(event["event_type"], "event recorded")
    # Reserved/future public kinds not in our wording table retain a safe
    # timestamp; unknown kinds rejected by the pinned public client stay errors.
    stamp = event["observed_at_utc"][11:19]
    return label + " at " + stamp + " UTC"


def render(request, intent, frame):
    if not frame.ok or frame.context is None:
        return response(request, speech="Athena not connected.", state="unavailable")
    caveats, state = _caveats(frame, intent)
    if caveats is None:
        return response(request, state=state, reason="unsupported_state_caveat")
    if intent == "current_state":
        own = frame.data["ownship"]
        if own is None or own["player_name"] != request.speaker or own["identity_confidence"] != "high":
            body, state = "Ownship not confirmed.", "ownship_ambiguous"
        else:
            fields = [own["unit_type"] or "Aircraft type unavailable"]
            if own["heading_deg"] is not None:
                fields.append(f"heading {own['heading_deg']:.0f}")
            else:
                fields.append("heading unavailable")
            if own["speed_kts"] is not None:
                fields.append(f"{own['speed_kts']:.0f} knots")
            else:
                fields.append("speed unavailable")
            if own["provenance_key"] is None:
                caveats.append("Ownship provenance unavailable.")
                if state == "ok":
                    state = "degraded"
            body = ", ".join(fields) + "."
    elif intent == "recent_events":
        events = frame.data["events"]
        body = ("Last events: " + "; ".join(event_summary(event) for event in events) + "."
                if events else "No recorded events yet.")
    else:
        return response(request, intent="unrecognized", reason="unrecognized_intent")
    speech = " ".join([request.speaker + ", Athena.", *caveats, body])
    if len(speech) > MAX_SPEECH_CHARS:
        return response(request, state=state, reason="speech_budget_exceeded")
    return response(request, speech=speech, state=state)
