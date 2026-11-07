"""Read-only, bounded radio tools. Geometry and filters are code, not model math."""

import math
from dataclasses import dataclass
import threading
import time
import uuid

from athena.state_service_client import ClientQueryError, StateServiceClient, load_client_config
from .context import RELEVANT_HEALTH, validate_factual_context
from .contract import safe_text
from .ownship import OwnshipReader, _plain, _invalid, _number, _exact, validate_projection
from .projections import page
from .tactical import bearing_range, category, same_scope


def schema(name, description, properties=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required), "additionalProperties": False}}}


ID = {"type": "string", "minLength": 1, "maxLength": 200}
SIDE = {"type": "string", "enum": ["blue", "red", "neutral", "friendly", "hostile"]}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
TOOLS = [
    schema("ownship", "Confirmed Prometheus ownship; heading reference may be unavailable."),
    schema("query_units", "Bounded present-unit page. Categories use incomplete exact type vocabulary. Nearest RETURNED is not globally nearest.",
           {"coalition": SIDE, "category": {"type": "string", "enum": ["air", "ground", "unknown", "all"]},
            "radius_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 2000}, "limit": LIMIT}),
    schema("get_unit", "Refresh a referenced unit ID; not-found does not prove destruction.", {"unit_id": ID}, ("unit_id",)),
    schema("count_units", "Authoritative matching total, with bounded bucket rows. Does not filter presence state.",
           {"coalition": SIDE, "group_by": {"type": "string", "enum": ["coalition", "type", "group"]}, "limit": LIMIT}),
    schema("group_summary", "Bounded groups with producer centroid caveats; no model geometry.", {"limit": LIMIT}),
    schema("current_summary", "Current producer totals and bounded coalition/type/group sections.", {"limit": LIMIT}),
    schema("mission_intel", "Supplied mission identity; unsupported brief/objectives/airbases remain null."),
    schema("recent_events", "Latest normalized events, not inferred events.", {"limit": LIMIT, "subject_id": ID}),
    schema("braa", "Deterministic ownship-to-unit TRUE bearing/range/altitude; aspect unable without supported true heading reference.", {"unit_id": ID}, ("unit_id",)),
]
SCHEMAS = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOLS}


def arguments(name, value):
    if name not in SCHEMAS or type(value) is not dict:
        raise ValueError("invalid_tool")
    spec = SCHEMAS[name]
    if set(value) - set(spec["properties"]) or set(spec["required"]) - set(value):
        raise ValueError("invalid_arguments")
    for key, item in value.items():
        rule = spec["properties"][key]
        if rule["type"] == "string":
            safe_text(item)
            if len(item) > rule.get("maxLength", 200) or not item:
                raise ValueError("invalid_arguments")
        elif rule["type"] == "integer":
            if type(item) is not int or not rule["minimum"] <= item <= rule["maximum"]:
                raise ValueError("invalid_arguments")
        elif type(item) not in (float, int) or not math.isfinite(item) or not 0 < item <= rule["maximum"]:
            raise ValueError("invalid_arguments")
        if "enum" in rule and item not in rule["enum"]:
            raise ValueError("invalid_arguments")
    return dict(value)


def identity(frame):
    return (frame.context.mission.identity, frame.context.state.lineage_id)


def metadata(frame):
    state = frame.context.state
    return {"mission_id": frame.context.mission.identity.mission_id,
        "session_id": frame.context.mission.identity.session_id, "lineage_id": state.lineage_id,
        "generation": state.generation, "source": state.source.value,
        "generated_at_utc": frame.generated_at_utc.isoformat(),
        "freshness": {"status": state.freshness.status, "age_ms": state.age_ms,
            "max_age_ms": state.freshness.max_age_ms,
            "observed_at_utc": state.freshness.observed_at_utc.isoformat(),
            "stale_reason": state.freshness.stale_reason},
        "health": {c.value: h.status.value for c, h in frame.context.component_health.items()},
        "warnings": [w.code for w in frame.warnings], "limits": _plain(frame.limits),
        "provenance": _plain(frame.evidence.get("provenance", {}))}


def usable(frame):
    validate_factual_context(frame, "recent_events" if frame.operation == "recent_events" else "current_state")
    fresh = frame.context.state.freshness
    if (fresh.status != "fresh" or fresh.max_age_ms is None or fresh.age_ms > fresh.max_age_ms or
            frame.context.mission.state.value != "running" or frame.context.state.source.value != "live_memory"):
        raise ClientQueryError("state_unavailable")
    components = RELEVANT_HEALTH["recent_events" if frame.operation == "recent_events" else "current_state"]
    if any(frame.context.component_health[c].status.value != "healthy" for c in components):
        raise ClientQueryError("state_unavailable")
    if any(w.code not in {"truncated_result", "partial_data", "ownship_unavailable"} for w in frame.warnings):
        raise ClientQueryError("state_unavailable")


@dataclass(frozen=True)
class TimedFrame:
    """Local timing envelope; never changes the public state-service schema."""
    frame: object
    expires_at: float

    def __getattr__(self, name):
        return getattr(self.frame, name)


class Evidence(dict):
    """JSON-compatible facts with a local, non-serialized evidence deadline."""
    def __init__(self, value, expires_at):
        super().__init__(value)
        self.expires_at = expires_at


class RadioTools:
    def __init__(self, config, *, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.client = StateServiceClient(config)
        self.raw = OwnshipReader(config)

    @classmethod
    def from_path(cls, path):
        return cls(load_client_config(path))

    def read(self, operation, params):
        started = self.clock()
        request_id = uuid.uuid4().hex
        if operation in {"ownship", "mission_intel", "group_summary", "current_summary"}:
            frame = self.raw.query(operation=operation, params=params, request_id=request_id)
        else:
            frame = self.client.query(operation=operation, params=params, request_id=request_id)
            if not frame.ok:
                raise ClientQueryError("state_unavailable")
            if operation != "recent_events":
                validate_projection(frame, rows_matched=1 if operation == "get_unit" else frame.data["total"])
            if operation in {"get_unit", "query_units"}:
                rows = [frame.data["unit"]] if operation == "get_unit" else frame.data["rows"]
                seen = set()
                for row in rows:
                    token = (type(row["unit_id"]), row["unit_id"])
                    if token in seen or not row["provenance_key"]:
                        _invalid()
                    seen.add(token)
                    for key in ("coalition", "state"):
                        if key in params and row[key] != params[key]:
                            _invalid()
                    if "unit_id" in params and str(row["unit_id"]) != params["unit_id"]:
                        _invalid()
                    if row["position"] is not None:
                        _number(row["position"]["lat"], minimum=-90, maximum=90)
                        _number(row["position"]["lon"], minimum=-180, maximum=180)
                    if row["heading_deg"] is not None:
                        _number(row["heading_deg"], minimum=0, maximum=360)
            if operation in {"query_units", "count_units"}:
                _exact(frame.limits, {"limit", "row_cap", "returned_count", "total_count", "truncated"})
                page(frame.limits, len(frame.data["rows"]), frame.limits["total_count"],
                     params["limit"], self.config.query_limits.max_rows)
                if frame.limits["truncated"] != any(w.code == "truncated_result" for w in frame.warnings):
                    _invalid()
                if operation == "count_units" and params.get("group_by") == "coalition" and "coalition" in params:
                    if any(row["coalition"] != params["coalition"] for row in frame.data["rows"]):
                        _invalid()
        usable(frame)
        fresh = frame.context.state.freshness
        # Charge the entire query/validation duration conservatively. Receipt
        # time must never grant the producer's evidence a new freshness budget.
        expires_at = started + (fresh.max_age_ms - fresh.age_ms) / 1000
        if self.clock() >= expires_at:
            raise ClientQueryError("state_unavailable")
        return TimedFrame(frame, expires_at)

    def own(self):
        frame = self.read("ownship", {})
        own = frame.data["ownship"]
        if (own is None or own["player_name"] != "Prometheus" or own["identity_confidence"] != "high" or
                own["coalition"] not in {"blue", "red"} or not own["provenance_key"]):
            raise ClientQueryError("ownship_unavailable")
        return frame

    def call(self, name, args, *, anchor=None):
        args = arguments(name, args)
        operation = "get_unit" if name == "braa" else name
        own_frame = anchor if anchor is not None else self.own()
        if self.clock() >= own_frame.expires_at:
            raise ClientQueryError("state_unavailable")
        own = own_frame.data["ownship"]
        params = {k: v for k, v in args.items() if k not in {"category", "radius_nm"}}
        if "coalition" in params and params["coalition"] in {"friendly", "hostile"}:
            params["coalition"] = (own["coalition"] if params["coalition"] == "friendly" else
                                   "red" if own["coalition"] == "blue" else "blue")
        if name in {"query_units", "count_units", "group_summary", "current_summary", "recent_events"}:
            cap = min(100, self.config.query_limits.max_rows)
            if name == "recent_events":
                if self.config.recent_events_limits is None:
                    raise ClientQueryError("state_unavailable")
                cap = min(cap, self.config.recent_events_limits.max_rows)
            params.setdefault("limit", min(10, cap))
            if params["limit"] > cap:
                raise ValueError("invalid_arguments")
        if name == "query_units":
            params["state"] = "present"
            if "radius_nm" in args:
                if own["position"] is None:
                    raise ClientQueryError("ownship_unavailable")
                params["near"] = {"anchor": "ownship", "radius_m": args["radius_nm"] * 1852}
        if name == "count_units":
            params.setdefault("group_by", "coalition")
        frame = own_frame if name == "ownship" else self.read(operation, params)
        same_scope((own_frame, frame))
        data = _plain(frame.data)
        if name == "ownship":
            data["ownship"]["heading_reference"] = "unavailable"
        if name in {"query_units", "get_unit", "braa"}:
            rows = data["rows"] if name == "query_units" else [data["unit"]]
            for row in rows:
                row["category"] = category(row["unit_type"])
                row["heading_reference"] = "unavailable"
                row["braa"] = geometry(own, row)
                if "radius_nm" in args and (row["braa"]["range_nm"] is None or
                        row["braa"]["range_nm"] > args["radius_nm"] + 0.01):
                    _invalid()
            if name == "query_units":
                requested = args.get("category", "all")
                selected = [r for r in rows if requested == "all" or r["category"] == requested]
                selected.sort(key=lambda row: (row["braa"]["range_nm"] is None, row["braa"]["range_nm"] or 0, str(row["unit_id"])))
                data = {"rows": selected, "producer_matching_total": data["total"],
                        "returned_filtered_count": len(selected),
                        "nearest_scope": "returned_page_only" if frame.limits["truncated"] or any(r["position"] is None for r in rows) else "known_positioned_units",
                        "category_vocabulary_incomplete": True}
            elif name == "braa":
                data = {"unit_id": data["unit"]["unit_id"], "braa": data["unit"]["braa"], "provenance_key": data["unit"]["provenance_key"]}
        if name == "recent_events":
            if any(row["warnings"] for row in data["events"]):
                raise ClientQueryError("state_unavailable")
            # No arbitrary event data or raw/error bodies in model/log context.
            data["events"] = [{k: row[k] for k in ("event_id", "event_type", "observed_at_utc", "subject_ids", "warnings")} for row in data["events"]]
        expires_at = min(own_frame.expires_at, frame.expires_at)
        if self.clock() >= expires_at:
            raise ClientQueryError("state_unavailable")
        return Evidence({"ok": True, "data": data, "context": metadata(frame)}, expires_at)


def geometry(own, row):
    result = {"bearing_true_deg": None, "range_nm": None, "altitude_ft_msl": None,
              "aspect": "unable", "aspect_reason": "true_heading_reference_unavailable"}
    if row["position"] is not None:
        altitude = row["position"]["alt_m"]
        if altitude is not None:
            result["altitude_ft_msl"] = round(altitude / 0.3048)
        if own["position"] is not None:
            bearing, distance = bearing_range(own["position"], row["position"])
            result["bearing_true_deg"] = None if bearing is None else round(bearing) % 360
            result["range_nm"] = round(distance, 1)
    return result


class SnapshotCache:
    def __init__(self, tools, *, interval=2.0, clock=time.monotonic):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("invalid_cache_interval")
        self.tools, self.interval, self.clock = tools, interval, clock
        self.lock, self.refresh_lock = threading.Lock(), threading.Lock()
        self.value, self.stamp = None, 0.0
        self.epoch = 0
        self.stop = threading.Event()
        self.thread = None

    def invalidate(self):
        with self.lock:
            if self.value is not None:
                self.epoch += 1
            self.value = None

    def refresh(self):
        with self.refresh_lock:
            try:
                own = self.tools.own()
                data = {"ownship": self.tools.call("ownship", {}, anchor=own)}
                for side in ("hostile", "friendly"):
                    value = self.tools.call("query_units", {"coalition": side,
                        "limit": min(100, self.tools.config.query_limits.max_rows)}, anchor=own)
                    value["data"]["rows"] = value["data"]["rows"][:10]
                    value["data"]["snapshot_rows_limited_to_ten"] = True
                    data[side] = value
                expires_at = min(value.expires_at for value in data.values())
                if self.clock() >= expires_at:
                    raise ClientQueryError("state_unavailable")
                with self.lock:
                    if self.value is None or identity(self.value[0]) != identity(own):
                        self.epoch += 1
                    self.value, self.stamp = (own, Evidence(data, expires_at)), self.clock()
                return self.value
            except (ClientQueryError, ValueError, TypeError, KeyError, OverflowError):
                self.invalidate()
                return None

    def get(self):
        with self.lock:
            value = self.value
            if value is not None:
                elapsed = self.clock() - self.stamp
                if elapsed <= self.interval and self.clock() < value[1].expires_at:
                    return value
        # Re-read after outages; never let a cached "unable" control a new turn.
        return self.refresh()

    def start(self):
        def run():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=run, name="radio-snapshot", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
