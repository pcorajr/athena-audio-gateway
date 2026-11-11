"""Strict native gateway bridge contract. Phase 1 adds two optional label fields to the response, route and
model, which the gateway copies into its transcript records; frames without them stay valid.

Deploy order (review P1, wire contract): the v12 Go gateway rejects any unknown response key, so this Python (which
labels every frame with `route`) must never run against v12. Deploy the new Go binary first (or both in one
stop/start); roll back in reverse (Python first, then Go). Old Python with the new Go is fine.

Optional `test_pilot` (boolean, deliverable frames only): the TEST stand-in is the pilot for this reply. The Go
gateway remembers it to mark its own "say again" "Test pilot." too. The Go gateway built before it (v13) rejects
the key like any unknown key, so the new Go binary must be running BEFORE a Python that sends it: new Go + old
Python is fine (no flag, no marker on the gateway line), old Go + new Python answers every call with the gateway's "say again" (not silence) as soon
as ATHENA_TEST_OWNSHIP is set. With ATHENA_TEST_OWNSHIP unset this Python never sends the key, so it also runs
against old Go. Roll back in reverse: Python first, then Go."""
from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
import unicodedata

MAX_BODY_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = 32000
MAX_SPEECH_CHARS = 900
REQUEST_KEYS = frozenset({
    "transmission_id", "transcript", "speaker", "frequency_hz", "modulation",
    "received_at", "transcribed_at",
})
STATES = frozenset({"ok", "stale", "degraded", "unavailable", "ownship_ambiguous"})
INTENT_CLASSES = frozenset({"factual", "cue", "action_preview", "unrecognized"})
ROUTES = frozenset({"fixed", "llm", "debrief", "fallback"})  # optional response label (phase 1)
MAX_MODEL_CHARS = 64
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|\+00:00)\Z")


class ContractError(ValueError):
    def __init__(self):
        super().__init__("invalid_contract")


def safe_text(value, *, nonempty=True):
    if type(value) is not str:
        raise ContractError()
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ContractError() from None
    if (nonempty and not value) or value != value.strip():
        raise ContractError()
    if any(unicodedata.category(c) in _UNSAFE or
           (unicodedata.category(c) == "Zs" and c != " ") for c in value):
        raise ContractError()
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError()
        result[key] = value
    return result


def _constant(_):
    raise ContractError()


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ContractError()
    return value


def strict_json(raw, *, maximum=MAX_BODY_BYTES):
    if type(raw) is not bytes or len(raw) > maximum:
        raise ContractError()
    try:
        return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                          parse_constant=_constant, parse_float=_float)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ContractError() from None


def _utc(value):
    safe_text(value)
    if not _UTC.fullmatch(value):
        raise ContractError()
    try:
        # Compare the nanosecond suffix separately; datetime alone loses the
        # final three digits emitted by Go's time.Time JSON encoder.
        parsed = datetime.fromisoformat(value)
        fraction = re.search(r"\.([0-9]+)", value)
        nanos = int(fraction[1].ljust(9, "0")) if fraction else 0
        if parsed.replace(tzinfo=None) == datetime.min and nanos == 0:
            raise ContractError()
        return parsed.replace(microsecond=0), nanos
    except ValueError:
        raise ContractError() from None


@dataclass(frozen=True)
class Request:
    transmission_id: str
    transcript: str
    speaker: str
    frequency_hz: int
    modulation: str
    received_at: str
    transcribed_at: str
    addressee: str = ""


def parse_request(raw, *, speaker="Prometheus"):
    data = strict_json(raw)
    if type(data) is not dict or set(data) not in (REQUEST_KEYS, REQUEST_KEYS | {"addressee"}):
        raise ContractError()
    for key in ("transmission_id", "transcript", "speaker"):
        safe_text(data[key])
    if not speaker_matches(data["speaker"], speaker) or data.get("addressee", "") != "":
        raise ContractError()
    if len(data["transcript"].encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
        raise ContractError()
    if type(data["frequency_hz"]) is not int or not 0 < data["frequency_hz"] < 2**64:
        raise ContractError()
    if type(data["modulation"]) is not str or data["modulation"] not in {"AM", "FM"}:
        raise ContractError()
    if _utc(data["transcribed_at"]) < _utc(data["received_at"]):
        raise ContractError()
    return Request(**data)


def response(request, *, speech="", state="unavailable", reason="", intent="factual", route=None, model=None,
             test_pilot=None):
    value = {"transmission_id": request.transmission_id, "deliverable": bool(speech),
             "speech": speech, "intent_class": intent, "state": state}
    if not speech:
        value["suppression_reason"] = reason or "not_deliverable"
    elif route:
        value["route"] = route  # which path wrote the words; model = which model, when one did
        if model:
            value["model"] = model
        if test_pilot is not None:  # only while the TEST stand-in setting is configured
            value["test_pilot"] = bool(test_pilot)
    return value


def encode_response(value):
    required = {"transmission_id", "deliverable", "speech", "intent_class", "state"}
    if type(value) is not dict or not required <= set(value) <= required | {"suppression_reason", "route", "model", "test_pilot"}:
        raise ContractError()
    if "test_pilot" in value and (value["test_pilot"] is not True and value["test_pilot"] is not False or
                                  value.get("deliverable") is not True):
        raise ContractError()
    if "route" in value or "model" in value:  # optional labels: only on a deliverable frame, model needs a route
        if value.get("deliverable") is not True or value.get("route") not in ROUTES:
            raise ContractError()
        if "model" in value and len(safe_text(value["model"])) > MAX_MODEL_CHARS:
            raise ContractError()
    safe_text(value["transmission_id"])
    safe_text(value["speech"], nonempty=False)
    safe_text(value.get("suppression_reason", ""), nonempty=False)
    if (type(value["deliverable"]) is not bool or
            type(value["state"]) is not str or value["state"] not in STATES or
            type(value["intent_class"]) is not str or value["intent_class"] not in INTENT_CLASSES):
        raise ContractError()
    if value["deliverable"]:
        if (value["intent_class"] != "factual" or not value["speech"] or
                len(value["speech"]) > MAX_SPEECH_CHARS or value.get("suppression_reason")):
            raise ContractError()
    elif value["speech"] or not value.get("suppression_reason"):
        raise ContractError()
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BODY_BYTES:
        raise ContractError()
    return raw


def speaker_matches(name, allowed):
    """Case-insensitive match against a comma-separated list of pilot names."""
    name = (name or "").strip().casefold()
    return bool(name) and any(name == item.strip().casefold() for item in str(allowed).split(","))
