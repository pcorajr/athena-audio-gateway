"""Bounded reads for ownship/mission_intel, unsupported by the pinned client."""
import asyncio
from collections.abc import Mapping
import math
import select
import socket
import time

from athena.state_service_client import ClientQueryError, StateServiceClientConfig
from metis.state_service.capabilities import evaluate_all_capabilities
from metis.state_service.models import GeoPosition, OwnshipView
from metis.state_service.serialization import STATE_API_VERSION
from metis.state_service.socket_protocol import (
    SocketResponseContext, StateQueryRequest, StateQueryResponse,
    decode_server_frame, encode_client_frame,
)

from .context import validate_factual_context
from .contract import safe_text
from .failures import response_failure

_OWN_KEYS = frozenset({"unit_id", "unit_name", "player_name", "callsign", "coalition",
                       "unit_type", "position", "heading_deg", "speed_kts",
                       "identity_confidence", "provenance_key"})
_EVIDENCE_KEYS = frozenset({"generation", "mission_id", "lineage_id", "rows_considered",
                            "rows_matched", "derivation", "health_cut", "capabilities", "provenance"})


def _invalid():
    raise ClientQueryError("protocol_error")


def _exact(value, keys):
    if not isinstance(value, Mapping) or set(value) != keys:
        _invalid()


def _number(value, *, minimum=None, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        _invalid()
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        _invalid()


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if type(value) is tuple:
        return [_plain(nested) for nested in value]
    return value


def validate_ownship(response, *, request_id):
    """Validate a decoded public response, not a permissive model alone."""
    if type(response) is not StateQueryResponse:
        _invalid()
    if response.api_version != STATE_API_VERSION:
        raise ClientQueryError("version_mismatch")
    if response.request_id != request_id:
        raise ClientQueryError("correlation_mismatch")
    if response.operation != "ownship":
        raise ClientQueryError("operation_mismatch")
    if not response.ok:
        raise response_failure(response)
    if type(response.context) is not SocketResponseContext:
        _invalid()
    _exact(response.data, {"ownship"})
    _exact(response.limits, set())
    validate_projection(response, rows_matched=0, rows_considered=0)
    own = response.data["ownship"]
    if own is None:
        return response
    if any(issue.code == "ownship_unavailable" for issue in response.warnings):
        _invalid()
    _exact(own, _OWN_KEYS)
    position = own["position"]
    if position is not None:
        _exact(position, {"lat", "lon", "alt_m"})
        _number(position["lat"], minimum=-90, maximum=90)
        _number(position["lon"], minimum=-180, maximum=180)
        position = GeoPosition(**position)
    OwnshipView(**(dict(own) | {"position": position}))
    for key in _OWN_KEYS - {"position", "heading_deg", "speed_kts"}:
        value = own[key]
        if value is not None and type(value) is str:
            safe_text(value)
    if own["heading_deg"] is not None:
        _number(own["heading_deg"], minimum=0, maximum=360)
    if own["speed_kts"] is not None:
        _number(own["speed_kts"], minimum=0)
    return response


def validate_projection(response, *, rows_matched, rows_considered=None):
    """Shared exact evidence contract from DomainQueryDispatcher._success."""
    validate_factual_context(response, "current_state")
    context = response.context
    _exact(response.evidence, _EVIDENCE_KEYS)
    evidence = response.evidence
    state = context.state
    if (type(evidence["generation"]) is not int or evidence["generation"] != state.generation or
            evidence["mission_id"] != context.mission.identity.mission_id or
            evidence["lineage_id"] != state.lineage_id or
            evidence["derivation"] != response.operation + ".generation_projection.v1" or
            evidence["health_cut"] != "generation"):
        _invalid()
    for key in ("rows_considered", "rows_matched"):
        if type(evidence[key]) is not int or evidence[key] < 0:
            _invalid()
    if (evidence["rows_matched"] != rows_matched or
            evidence["rows_considered"] < rows_matched or
            (rows_considered is not None and evidence["rows_considered"] != rows_considered)):
        _invalid()
    _exact(evidence["provenance"], {"source", "generation"})
    if (evidence["provenance"]["source"] != state.source.value or
            type(evidence["provenance"]["generation"]) is not int or
            evidence["provenance"]["generation"] != state.generation):
        _invalid()
    # Public fixed health graph: validate the projection's health evidence,
    # never derive or repair battlefield data. Incomplete health is invalid.
    decisions = evaluate_all_capabilities(context.component_health)
    expected = {key.value: {"status": value.status.value,
                "required": [c.value for c in value.required],
                "blocking": [c.value for c in value.blocking],
                "advisory": [c.value for c in value.advisory], "reasons": list(value.reasons)}
                for key, value in decisions.items()}
    if _plain(evidence["capabilities"]) != expected:
        _invalid()
    fresh = state.freshness
    if fresh.max_age_ms is not None:
        _number(fresh.max_age_ms, minimum=0)
    if fresh.stale_reason is not None:
        safe_text(fresh.stale_reason)


def validate_mission_intel(response, *, request_id):
    """The pinned producer exposes identity plus explicit nulls, not airbases."""
    if (type(response) is not StateQueryResponse or response.api_version != STATE_API_VERSION or
            response.request_id != request_id or response.operation != "mission_intel" or
            not response.ok or type(response.context) is not SocketResponseContext):
        _invalid()
    validate_projection(response, rows_matched=0, rows_considered=0)
    _exact(response.limits, set())
    _exact(response.data, {"mission_id", "mission_filename", "mission_name", "session_id",
                           "scenario_start_identity", "brief", "objectives", "phase", "theatre", "airbases"})
    identity = response.context.mission.identity
    for key in ("mission_id", "mission_filename", "mission_name", "session_id", "scenario_start_identity"):
        if type(response.data[key]) is not type(getattr(identity, key)) or response.data[key] != getattr(identity, key):
            _invalid()
    if any(response.data[key] is not None for key in ("brief", "objectives", "phase", "theatre", "airbases")):
        _invalid()
    if not any(issue.code == "partial_data" for issue in response.warnings):
        _invalid()
    return response


class OwnshipReader:
    def __init__(self, config):
        if type(config) is not StateServiceClientConfig:
            raise ValueError("invalid_state_config")
        self.config = config

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientQueryError("query_timeout")
        return remaining

    def query(self, *, request_id, operation="ownship", params=None):
        if operation not in {"ownship", "mission_intel", "current_summary", "group_summary"}:
            raise ClientQueryError("unsupported_operation")
        params = {} if params is None else params
        if operation in {"current_summary", "group_summary"}:
            if (set(params) != {"limit"} or type(params["limit"]) is not int or
                    not 1 <= params["limit"] <= self.config.query_limits.max_rows):
                raise ClientQueryError("invalid_params")
        elif params:
            raise ClientQueryError("invalid_params")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ClientQueryError("event_loop_blocking")
        deadline = time.monotonic() + self.config.query_timeout_ms / 1000
        try:
            request = StateQueryRequest(STATE_API_VERSION, request_id, operation, params)
            wire = encode_client_frame(request, limits=self.config.limits)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._remaining(deadline))
                connection.connect(str(self.config.socket_path))
                sent = 0
                while sent < len(wire):
                    connection.settimeout(self._remaining(deadline))
                    count = connection.send(wire[sent:])
                    if count <= 0:
                        raise ClientQueryError("socket_unavailable")
                    sent += count
                received = bytearray()
                maximum = self.config.limits.max_response_bytes
                while True:
                    connection.settimeout(self._remaining(deadline))
                    chunk = connection.recv(min(4096, maximum + 1 - len(received)))
                    if not chunk:
                        raise ClientQueryError("socket_unavailable")
                    received.extend(chunk)
                    if len(received) > maximum:
                        raise ClientQueryError("response_too_large")
                    if b"\n" in chunk:
                        if received.count(b"\n") != 1 or not received.endswith(b"\n"):
                            _invalid()
                        # The service keeps connections open. Never require EOF,
                        # but reject already queued trailing bytes as well.
                        if select.select([connection], [], [], 0)[0] and connection.recv(1, socket.MSG_PEEK):
                            _invalid()
                        break
            decoded = decode_server_frame(bytes(received), limits=self.config.limits)
            if operation in {"current_summary", "group_summary"}:
                from .projections import validate_summary
                checked = validate_summary(decoded, request_id=request_id, operation=operation,
                                           params=params, config=self.config)
            else:
                validator = validate_ownship if operation == "ownship" else validate_mission_intel
                checked = validator(decoded, request_id=request_id)
            self._remaining(deadline)
            return checked
        except ClientQueryError:
            raise
        except TimeoutError:
            raise ClientQueryError("query_timeout") from None
        except OSError:
            raise ClientQueryError("socket_unavailable") from None
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ClientQueryError("protocol_error") from None
