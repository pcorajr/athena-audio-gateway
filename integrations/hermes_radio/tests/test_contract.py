import json
from pathlib import Path
import unittest

from integrations.hermes_radio.contract import (
    ContractError, MAX_BODY_BYTES, MAX_SPEECH_CHARS, encode_response, parse_request, response, strict_json,
)
from integrations.hermes_radio.tests.bridge import bridge_request, wire


class ContractTests(unittest.TestCase):
    def test_contract_artifact_and_native_fixture_match_runtime(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "contract.json").read_text())
        sample = json.loads((root / "tests/fixtures/bridge-v1.json").read_text())
        request = parse_request(wire(sample["request"]))
        encoded = json.loads(encode_response(sample["response"]))
        self.assertEqual(encoded["transmission_id"], request.transmission_id)
        self.assertEqual(set(schema["$defs"]["request"]["required"]), set(sample["request"]))
        self.assertEqual(set(schema["$defs"]["response"]["required"]), set(sample["response"]))
        self.assertEqual(schema["x-limits"]["request_bytes"], MAX_BODY_BYTES)

    def test_native_request_and_optional_empty_addressee(self):
        for add in ({}, {"addressee": ""}):
            request = parse_request(wire(bridge_request() | add))
            self.assertEqual(request.speaker, "Prometheus")
            self.assertEqual(request.addressee, "")

    def test_reject_unknown_missing_fields(self):
        source = bridge_request()
        for value in [source | {"audio": "forbidden"}, source | {"addressee": "hermes"},
                      *[{key: value for key, value in source.items() if key != missing} for missing in source]]:
            with self.subTest(keys=list(value)):
                with self.assertRaises(ContractError):
                    parse_request(wire(value))

    def test_reject_wrong_types_and_unsafe_text(self):
        cases = [("frequency_hz", x) for x in (True, -1, 0, 1.5, 2**64, "133000000", None)]
        cases += [("modulation", x) for x in ("am", [], None)]
        cases += [(key, x) for key in ("transmission_id", "transcript", "speaker")
                  for x in (None, "", " ", [], "bad\x00text", "bad\u202etext", "bad\ud800text")]
        cases += [("speaker", "Other"), ("addressee", None), ("addressee", 0)]
        for key, value in cases:
            with self.subTest(key=key, type=type(value).__name__):
                with self.assertRaises(ContractError):
                    parse_request(json.dumps(bridge_request() | {key: value}).encode())

    def test_transcript_byte_limit_and_whole_body_limit(self):
        parse_request(wire(bridge_request("x" * 32000)))
        for value in ("x" * 32001, "é" * 16001):
            with self.assertRaises(ContractError):
                parse_request(wire(bridge_request(value)))
        with self.assertRaises(ContractError):
            strict_json(b" " * (MAX_BODY_BYTES + 1))

    def test_strict_json_duplicates_utf8_nonfinite_and_trailing(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'{"a":NaN}',
                    b'{"a":Infinity}', b'{"a":1e9999}', b'{"a":"\xff"}', b'{} {}',
                    b'\xef\xbb\xbf{}', b'{', b'[' * 2000):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ContractError):
                    strict_json(raw)

    def test_timestamps_utc_and_nanosecond_order(self):
        for bad in ("0001-01-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-01-01T00:00:00", "2026-01-01T00:00:00+01:00", None):
            with self.assertRaises(ContractError):
                parse_request(wire(bridge_request() | {"received_at": bad}))
        with self.assertRaises(ContractError):
            parse_request(wire(bridge_request() | {"received_at": "2026-01-01T00:00:00.000000002Z",
                                                  "transcribed_at": "2026-01-01T00:00:00.000000001Z"}))
        parse_request(wire(bridge_request() | {"received_at": "2026-01-01T00:00:00+00:00"}))

    def test_only_short_factual_deliverable(self):
        request = parse_request(wire(bridge_request()))
        for intent in ("cue", "action_preview", "unrecognized"):
            with self.assertRaises(ContractError):
                encode_response(response(request, speech="No.", intent=intent))
        for text in ("x" * (MAX_SPEECH_CHARS + 1), "unsafe\ntext"):
            with self.assertRaises(ContractError):
                encode_response(response(request, speech=text))
        self.assertEqual(json.loads(encode_response(response(request, speech="x" * MAX_SPEECH_CHARS)))["speech"],
                         "x" * MAX_SPEECH_CHARS)

    def test_suppression_consistency_and_correlation(self):
        request = parse_request(wire(bridge_request()))
        result = response(request, reason="unrecognized_intent", intent="unrecognized")
        checked = json.loads(encode_response(result))
        self.assertFalse(checked["deliverable"])
        self.assertEqual(checked["speech"], "")
        self.assertEqual(checked["transmission_id"], request.transmission_id)
        for change in ({"deliverable": 1}, {"suppression_reason": ""}, {"speech": "not allowed"}, {"state": None}):
            with self.assertRaises(ContractError):
                encode_response(result | change)
