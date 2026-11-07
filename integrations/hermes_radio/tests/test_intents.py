import json
import time
import unittest
from unittest.mock import Mock

from integrations.hermes_radio.contract import ContractError
from integrations.hermes_radio.hermes_client import INTENTS, HermesClassifier, classify, extract_intent, parse_intent
from integrations.hermes_radio.tests.support import fake_hermes, model_reply


class IntentTests(unittest.TestCase):
    def test_all_keywords_bypass_model(self):
        fallback = Mock()
        for phrase in ("status", "How am I?", "my aircraft", "ownship", "onship", "where am I"):
            self.assertEqual(classify("Athena, " + phrase, fallback), "current_state")
        for phrase in ("recent events", "what happened", "any events", "SITREP"):
            self.assertEqual(classify(phrase, fallback), "recent_events")
        fallback.classify.assert_not_called()

    def test_compound_keyword_hits_suppress_without_model(self):
        fallback = Mock()
        self.assertEqual(classify("status and recent events", fallback), "unrecognized")
        fallback.classify.assert_not_called()

    def test_word_boundaries_and_single_fallback(self):
        fallback = Mock()
        fallback.classify.return_value = "unrecognized"
        self.assertEqual(classify("statusful", fallback), "unrecognized")
        fallback.classify.assert_called_once_with("statusful")

    def test_one_stateless_no_tools_http_request(self):
        for intent in sorted(INTENTS):
            with fake_hermes(model_reply(intent)) as (endpoint, calls):
                client = HermesClassifier(endpoint, "synthetic-key", timeout=1)
                self.assertEqual(classify("Give me a check", client), intent)
                self.assertEqual(len(calls), 1)
                path, headers, payload = calls[0]
                self.assertEqual(path, "/v1/responses")
                self.assertEqual(headers["Authorization"], "Bearer synthetic-key")
                self.assertEqual(set(payload), {"model", "input", "instructions", "stream", "store", "tools", "tool_choice"})
                self.assertEqual(payload["model"], "athena-radio")
                self.assertEqual(payload["tools"], [])
                self.assertEqual(payload["tool_choice"], "none")
                self.assertIs(payload["store"], False)
                self.assertIs(payload["stream"], False)

    def test_model_output_is_exact_intent_not_facts_or_commands(self):
        for raw in (b'{}', b'{"intent":"action_preview"}', b'{"intent":"current_state","speech":"fake"}',
                    b'{"intent":"current_state","intent":"recent_events"}', b'```json\n{}\n```',
                    b'{"intent":true}', b'{"intent":[]}', b'{"intent":"current_state"}{}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ContractError):
                    parse_intent(raw)

    def test_tool_outputs_and_incomplete_or_multiple_answers_rejected(self):
        good = json.loads(model_reply("current_state"))
        variants = [good | {"status": "incomplete"}, good | {"error": {}}, good | {"output": []},
                    good | {"output": good["output"] * 2},
                    good | {"output": [{"type": "function_call", "name": "terminal"}, *good["output"]]}]
        for variant in variants:
            with self.assertRaises(ContractError):
                extract_intent(json.dumps(variant).encode())

    def test_http_error_redirect_malformed_oversize_fail_quiet_no_retry(self):
        for raw, code in [(model_reply("current_state"), 500), (model_reply("current_state"), 302),
                          (b"bad", 200), (b"x" * 65537, 200), (model_reply("action_preview"), 200)]:
            with fake_hermes(raw, status=code) as (endpoint, calls):
                self.assertEqual(HermesClassifier(endpoint, "synthetic-key", timeout=1).classify("check"), "unrecognized")
                self.assertEqual(len(calls), 1)

    def test_cumulative_timeout_and_no_retry(self):
        def slow(handler):
            time.sleep(0.15)
        with fake_hermes(responder=slow) as (endpoint, calls):
            start = time.monotonic()
            self.assertEqual(HermesClassifier(endpoint, "synthetic-key", timeout=0.04).classify("check"), "unrecognized")
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertEqual(len(calls), 1)

    def test_no_default_endpoint_proxy_or_external_address(self):
        for endpoint in (None, "", "http://localhost:8650/v1/responses", "https://127.0.0.1:8650/v1/responses",
                         "http://example.invalid/v1/responses", "http://127.0.0.1:8650/other",
                         "http://user:pass@127.0.0.1:8650/v1/responses", "http://127.0.0.1:8650/v1/responses?q=x"):
            with self.assertRaises(ValueError):
                HermesClassifier(endpoint, "synthetic-key", timeout=1)
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                HermesClassifier("http://127.0.0.1:8650/v1/responses", "synthetic-key", timeout=timeout)

    def test_incomplete_content_length_is_not_accepted(self):
        def incomplete(handler):
            body = model_reply("current_state")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body) + 1))
            handler.end_headers()
            handler.wfile.write(body)
        with fake_hermes(responder=incomplete) as (endpoint, calls):
            self.assertEqual(HermesClassifier(endpoint, "synthetic-key", timeout=0.5).classify("check"), "unrecognized")
            self.assertEqual(len(calls), 1)

    def test_drip_fed_headers_and_body_share_one_deadline(self):
        def slow_body(handler):
            body = model_reply("current_state")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            try:
                for byte in body:
                    handler.wfile.write(bytes([byte]))
                    handler.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
        with fake_hermes(responder=slow_body) as (endpoint, calls):
            start = time.monotonic()
            self.assertEqual(HermesClassifier(endpoint, "synthetic-key", timeout=0.05).classify("check"), "unrecognized")
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertEqual(len(calls), 1)
