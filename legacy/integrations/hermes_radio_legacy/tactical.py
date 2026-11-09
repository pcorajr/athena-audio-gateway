"""Fixed read compositions and conservative, type-backed radio descriptions.

The pinned query_units projection has no category. These exact DCS type names
are a deliberately incomplete vocabulary, NOT a classifier of arbitrary names.
Unlisted types stay unknown; no position, speed or group-name guesses.
"""
import math
import uuid

from athena.state_service_client import ClientQueryError

from .ownship import _exact, _invalid, _number, validate_projection

TACTICAL_INTENTS = frozenset({"picture", "bogey_dope", "threats", "mission_status", "home_plate"})
AIR_TYPES = frozenset({
    "A-10A", "A-10C", "A-10C_2", "AJS37", "AV8BNA", "B-1B", "B-52H",
    "C-130", "C-17A", "E-2C", "E-3A", "F-4E", "F-5E", "F-5E-3", "F-14A-135-GR",
    "F-14B", "F-15C", "F-15E", "F-15ESE", "F-16", "F-16A", "F-16C_50", "FA-18C_hornet",
    "JF-17", "J-11A", "KC-135", "KC135MPRS", "M-2000C", "Mirage-F1CE", "Mirage-F1EE",
    "MiG-15bis", "MiG-19P", "MiG-21Bis", "MiG-23MLD", "MiG-25PD", "MiG-25RBT",
    "MiG-27K", "MiG-29A", "MiG-29G", "MiG-29S", "MiG-31", "Su-17M4", "Su-24M",
    "Su-24MR", "Su-25", "Su-25T", "Su-27", "Su-30", "Su-33", "Su-34",
    "Tu-22M3", "Tu-95MS", "Tu-160", "AH-64D_BLK_II", "AH-1W", "CH-47D", "CH-47Fbl1",
    "CH-53E", "Ka-50", "Ka-50_3", "Mi-8MT", "Mi-24P", "Mi-24V", "Mi-26",
    "Mi-28N", "OH-58D", "SA342M", "SA342L", "SA342Minigun", "SA342Mistral", "UH-1H", "UH-60A",
})
AIR_DEFENCE_TYPES = frozenset({
    "1L13 EWR", "55G6 EWR", "Dog Ear radar", "Kub 1S91 str", "Kub 2P25 ln",
    "Osa 9A33 ln", "Tor 9A331", "2S6 Tunguska", "Strela-1 9P31", "Strela-10M3",
    "S-300PS 5P85C ln", "S-300PS 5P85D ln", "S-300PS 40B6M tr", "S-300PS 40B6MD sr",
    "S-300PS 64H6E sr", "S-300PS 54K6 cp", "SA-11 Buk LN 9A310M1", "SA-11 Buk SR 9S18M1",
    "SA-11 Buk CC 9S470M1", "SNR_75V", "S_75M_Volhov", "p-19 s-125 sr", "snr s-125 tr",
    "5p73 s-125 ln", "Patriot ln", "Patriot str", "Patriot cp", "Hawk ln", "Hawk tr",
    "Hawk sr", "Hawk cwar", "Hawk pcp", "Roland ADS", "Roland Radar", "rapier_fsa_launcher",
    "rapier_fsa_blindfire_radar", "rapier_fsa_optical_tracker_unit", "ZSU-23-4 Shilka",
    "ZU-23 Emplacement", "ZU-23 Closed Insurgent", "Gepard", "Vulcan", "Avenger", "M48 Chaparral",
})
GROUND_TYPES = AIR_DEFENCE_TYPES | frozenset({
    "T-55", "T-72B", "T-80UD", "T-90", "M-1 Abrams", "M-2 Bradley", "M-113", "BMP-1",
    "BMP-2", "BMP-3", "BTR-80", "BTR-82A", "BRDM-2", "MTLB", "Ural-375", "Ural-4320T",
    "Ural-4320-31", "UAZ-469", "M 818", "Hummer", "Leopard-2", "Leclerc", "Challenger2",
})


def category(unit_type):
    if unit_type in AIR_TYPES:
        return "air"
    if unit_type in GROUND_TYPES:
        return "ground"
    return "unknown"


def _scope(frame):
    context, state = frame.context, frame.context.state
    # Age/generated-at may legitimately advance between queries. Everything
    # defining the immutable cut must agree; no retries or mixed-generation facts.
    return (context.mission, state.lineage_id, state.generation, state.source,
            state.published_at_utc, state.mission_time_seconds,
            state.freshness.observed_at_utc, state.freshness.max_age_ms,
            state.freshness.status, state.freshness.stale_reason, context.component_health)


def same_scope(frames):
    if any(not frame.ok or frame.context is None for frame in frames):
        raise ClientQueryError("state_unavailable")
    if any(_scope(frame) != _scope(frames[0]) for frame in frames[1:]):
        raise ClientQueryError("scope_changed")


def validate_units(frame, params):
    """Supplement the unchanged public client with projection/filter evidence."""
    if not frame.ok:
        raise ClientQueryError("state_unavailable")
    validate_projection(frame, rows_matched=frame.data["total"])
    _exact(frame.limits, {"limit", "row_cap", "returned_count", "total_count", "truncated"})
    if frame.limits["truncated"] != any(issue.code == "truncated_result" for issue in frame.warnings):
        _invalid()
    if frame.operation == "query_units":
        seen = set()
        for row in frame.data["rows"]:
            identity = (type(row["unit_id"]), row["unit_id"])
            if identity in seen or row["coalition"] != params["coalition"] or row["state"] != "present":
                _invalid()
            seen.add(identity)
            position = row["position"]
            if position is not None:
                _number(position["lat"], minimum=-90, maximum=90)
                _number(position["lon"], minimum=-180, maximum=180)
            if not row["provenance_key"]:
                _invalid()
    return frame


def collect(intent, ownship_reader, client, limit):
    def read(operation, params):
        frame = client.query(operation=operation, params=params, request_id=uuid.uuid4().hex)
        return validate_units(frame, params)

    if intent == "home_plate":
        return (ownship_reader.query(operation="mission_intel", request_id=uuid.uuid4().hex),)
    own_frame = ownship_reader.query(request_id=uuid.uuid4().hex)
    own = own_frame.data["ownship"]
    if own is None or own["identity_confidence"] != "high" or own["coalition"] not in {"blue", "red"}:
        return (own_frame,)
    friendly = own["coalition"]
    hostile = "red" if friendly == "blue" else "blue"
    if intent == "mission_status":
        frames = (own_frame, *(read("count_units", {"limit": limit, "group_by": "type", "coalition": side})
                              for side in (friendly, hostile)))
    else:
        frames = (own_frame, read("query_units", {"limit": limit, "coalition": hostile, "state": "present"}))
    same_scope(frames)
    if intent != "mission_status" and any(_token(row["unit_id"]) == _token(own["unit_id"])
                                          for row in frames[1].data["rows"]):
        raise ClientQueryError("contradictory_ownship_coalition")
    return frames


def bearing_range(origin, target):
    """Initial great-circle TRUE bearing and horizontal nautical-mile range."""
    lat1, lat2 = math.radians(origin["lat"]), math.radians(target["lat"])
    delta_lon = math.radians(target["lon"] - origin["lon"])
    a = math.sin((lat2 - lat1) / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2)**2
    angle = 2 * math.atan2(math.sqrt(min(1, max(0, a))), math.sqrt(max(0, 1 - a)))
    distance = 6371000 * angle / 1852
    x = math.sin(delta_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    # Coincident/antipodal points have no unique initial bearing.
    bearing = None if math.hypot(x, y) < 1e-12 else math.degrees(math.atan2(x, y)) % 360
    return bearing, distance


def _token(value):
    return type(value).__name__, str(value)


def _group(row):
    if row["group_id"] is not None:
        return "id", *_token(row["group_id"])
    if row["group_name"] is not None:
        return "name", row["group_name"]
    return "unit", *_token(row["unit_id"])


def _braa(origin, row, *, altitude):
    bearing, distance = bearing_range(origin, row["position"])
    direction = "bearing unavailable" if bearing is None else f"{round(bearing) % 360:03d} true"
    result = f"{direction}, {distance:.1f} nm"
    if altitude:
        alt = row["position"]["alt_m"]
        result += ", altitude unavailable" if alt is None else f", {alt / 304.8:.1f} thousand feet MSL"
    return result + "."


def describe(request, intent, frames):
    """Return body, additional qualifications and state, never model wording."""
    same_scope(frames)
    if intent == "home_plate":
        return "Unable, no friendly airfield data.", [], "degraded"
    own = frames[0].data["ownship"]
    if own is None or own["player_name"] != request.speaker or own["identity_confidence"] != "high":
        return "Ownship not confirmed.", [], "ownship_ambiguous"
    if own["coalition"] not in {"blue", "red"}:
        return "Unable, ownship coalition unavailable.", [], "degraded"
    caveats = []
    if own["provenance_key"] is None:
        caveats.append("Ownship provenance unavailable.")
    if intent == "mission_status":
        parts = []
        for label, frame in zip(("Friendly", "hostile"), frames[1:]):
            counts = {"air": 0, "ground": 0, "unknown": 0}
            for row in frame.data["rows"]:
                counts[category(row["type"])] += row["count"]
            incomplete = frame.limits["truncated"] or counts["unknown"] > 0
            prefix = "at least " if incomplete else ""
            parts.append(f"{label} {prefix}{counts['air']} air, {prefix}{counts['ground']} ground")
            if counts["unknown"]:
                caveats.append("Unclassified types omitted.")
        return "Recorded type-based counts: " + "; ".join(parts) + ".", caveats, "degraded" if caveats else "ok"
    frame = frames[1]
    rows = frame.data["rows"]
    unknown = any(category(row["unit_type"]) == "unknown" for row in rows)
    if unknown:
        caveats.append("Unclassified types omitted.")
    if any(row["stale"] for row in rows):
        caveats.append("Stale unit data.")
    candidates = [row for row in rows if (row["unit_type"] in AIR_DEFENCE_TYPES if intent == "threats"
                                         else category(row["unit_type"]) == "air")]
    positioned = [row for row in candidates if row["position"] is not None]
    if len(positioned) != len(candidates):
        caveats.append("Some positions unavailable.")
    partial = frame.limits["truncated"] or unknown
    count = ("At least " if partial else "") + str(len(candidates)) + " known hostile aircraft. "
    prefix = count if intent == "picture" else ""
    if not candidates:
        body = "No SAM data." if intent == "threats" else "No known hostile aircraft in returned data."
        return prefix + body, caveats, "degraded" if partial or caveats else "ok"
    if own["position"] is None:
        return prefix + "Unable, ownship position unavailable.", caveats, "degraded"
    if not positioned:
        return prefix + ("No SAM data with positions." if intent == "threats" else "Unable, target positions unavailable."), caveats, "degraded"
    # Stable closest-member group selection, not an invented group centroid.
    selected = min(positioned, key=lambda row: (bearing_range(own["position"], row["position"])[1],
                                               _group(row), _token(row["unit_id"])))
    bounded = partial or len(positioned) != len(candidates)
    nearest = "Nearest returned " if bounded else "Nearest known "
    if intent == "picture":
        if selected["group_id"] is None and selected["group_name"] is None:
            caveats.append("Group identity unavailable.")
            label = "aircraft BRAA "
        else:
            label = "group, closest aircraft BRAA "
    else:
        label = "air defence " if intent == "threats" else "hostile aircraft BRAA "
    body = prefix + nearest + label + _braa(own["position"], selected, altitude=intent != "threats")
    return body, caveats, "stale" if any(row["stale"] for row in rows) else "degraded" if caveats or bounded else "ok"
