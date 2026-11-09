"""Strict validation for public summary projections not in the pinned client."""
from athena.state_service_client import ClientQueryError
from metis.state_service.models import GeoPosition
from metis.state_service.serialization import STATE_API_VERSION
from metis.state_service.socket_protocol import StateQueryResponse

from integrations.hermes_radio.contract import safe_text
from .ownship import _exact, _number, _invalid, validate_projection


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        _invalid()


def page(value, returned, total, limit, cap, *, nested=False):
    _exact(value, {"limit", "returned_count", "total_count", "truncated"} |
           (set() if nested else {"row_cap"}))
    for key in ("limit", "returned_count", "total_count"):
        integer(value[key])
    if not nested and value["row_cap"] != cap:
        _invalid()
    if (value["limit"] != limit or value["returned_count"] != returned or
            value["total_count"] != total or returned != min(limit, total) or
            type(value["truncated"]) is not bool or value["truncated"] != (returned < total)):
        _invalid()


def buckets(rows, dimension, total, truncated):
    seen, counted = set(), 0
    for row in rows:
        _exact(row, {dimension, "count"})
        value = row[dimension]
        if value is not None:
            if type(value) is str:
                safe_text(value)
            elif dimension != "group" or type(value) is not int:
                _invalid()
        token = (type(value), value)
        if token in seen:
            _invalid()
        seen.add(token)
        integer(row["count"], 1)
        counted += row["count"]
    if counted > total or (not truncated and counted != total):
        _invalid()


def validate_summary(frame, *, request_id, operation, params, config):
    if (type(frame) is not StateQueryResponse or frame.api_version != STATE_API_VERSION or
            frame.request_id != request_id or frame.operation != operation or not frame.ok):
        raise ClientQueryError("state_unavailable")
    limit, cap = params["limit"], config.query_limits.max_rows
    if operation == "current_summary":
        _exact(frame.data, {"total_units", "sections"})
        total = frame.data["total_units"]
        integer(total)
        validate_projection(frame, rows_matched=0, rows_considered=total)
        _exact(frame.limits, {"row_cap"})
        if frame.limits["row_cap"] != cap:
            _invalid()
        _exact(frame.data["sections"], {"coalition", "type", "group"})
        truncated = False
        for dimension, section in frame.data["sections"].items():
            _exact(section, {"rows", "limit", "row_cap", "returned_count", "total_count", "truncated"})
            page({k: v for k, v in section.items() if k != "rows"}, len(section["rows"]),
                 section["total_count"], limit, cap)
            buckets(section["rows"], dimension, total, section["truncated"])
            truncated |= section["truncated"]
    else:
        _exact(frame.data, {"rows", "total"})
        total, rows = frame.data["total"], frame.data["rows"]
        integer(total)
        matched = frame.evidence["rows_matched"]
        validate_projection(frame, rows_matched=matched, rows_considered=matched)
        _exact(frame.limits, {"limit", "row_cap", "returned_count", "total_count", "truncated", "nested_type_row_cap"})
        page({k: v for k, v in frame.limits.items() if k != "nested_type_row_cap"}, len(rows), total, limit, cap)
        nested_cap = config.query_limits.max_nested_type_rows
        if frame.limits["nested_type_row_cap"] != nested_cap:
            _invalid()
        truncated = frame.limits["truncated"]
        counted, seen = 0, set()
        for row in rows:
            _exact(row, {"group_id", "group_name", "count", "types", "type_limits", "centroid", "max_distance_from_centroid_m", "caveats"})
            gid, name = row["group_id"], row["group_name"]
            if gid is not None and type(gid) not in (str, int):
                _invalid()
            if type(gid) is str:
                safe_text(gid)
            if name is not None:
                safe_text(name)
            key = (type(gid), gid) if gid is not None else ("name", name)
            if key in seen:
                _invalid()
            seen.add(key)
            integer(row["count"], 1)
            counted += row["count"]
            sub = row["type_limits"]
            page(sub, len(row["types"]), sub["total_count"], nested_cap, nested_cap, nested=True)
            buckets(row["types"], "type", row["count"], sub["truncated"])
            truncated |= sub["truncated"]
            position = row["centroid"]
            if position is not None:
                _exact(position, {"lat", "lon", "alt_m"})
                _number(position["lat"], minimum=-90, maximum=90)
                _number(position["lon"], minimum=-180, maximum=180)
                GeoPosition(**position)
                _number(row["max_distance_from_centroid_m"], minimum=0)
            elif row["max_distance_from_centroid_m"] is not None:
                _invalid()
            allowed = {"arithmetic_centroid_equirectangular_spread", "longitude_wrap_not_geodesic",
                       "missing_altitude", "missing_position", "conflicting_group_name"}
            if any(type(c) is not str or c not in allowed for c in row["caveats"]):
                _invalid()
        if counted > matched or (not frame.limits["truncated"] and counted != matched):
            _invalid()
    if truncated != any(w.code == "truncated_result" for w in frame.warnings):
        _invalid()
    return frame
