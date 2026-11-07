"""Radio-v2 offline tests: actual public producer/client, fake model only."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from athena.state_service_client import ClientQueryError

from integrations.hermes_radio.controller import Controller, CORE_PROMPT
from integrations.hermes_radio.contract import parse_request
from integrations.hermes_radio.fixture_data import bridge_request, state_frame, wire
from integrations.hermes_radio.openai_provider import OpenAIProvider, ProviderError
from integrations.hermes_radio.tools import RadioTools, SnapshotCache, SCHEMAS, arguments, geometry
from integrations.hermes_radio.tests.support import fake_state, fake_hermes
from integrations.hermes_radio.tests.test_tactical import producer, unit
from integrations.hermes_radio.tests.test_server import running
from integrations.hermes_radio.tests.support import post
from integrations.hermes_radio.text_smoke import smoke
from integrations.hermes_radio.transcript import Transcript


@contextmanager
def world(units=None, transform=lambda x: x):
    produce = producer([unit("bandit")]) if units is None else producer(units)
    def respond(connection, request):
        if request["operation"] == "recent_events":
            value = state_frame("recent_events", request["request_id"])
            value["limits"]["limit"] = request["params"]["limit"]
            value["context"] = produce(dict(request, operation="ownship", params={}))['context']
        else:
            value = produce(request)
        connection.sendall(wire(transform(value)))
    with fake_state(respond) as (config, calls, _):
        tools = RadioTools(config)
        cache = SnapshotCache(tools)
        try:
            yield tools, cache, calls
        finally:
            cache.close()


class FakeProvider:
    model = "gpt-5.4-mini"
    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.requests = []
    def stream(self, messages, tools, *, deadline):
        self.requests.append(deepcopy(messages))
        events = self.rounds.pop(0)
        if isinstance(events, Exception):
            raise events
        yield from events


def call(name, args, identifier="tool-1"):
    return "calls", [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]


def req(text="Athena, what type is it?"):
    return parse_request(wire(bridge_request(text)))


class ToolsTests(unittest.TestCase):
    def test_all_nine_tools_through_real_public_socket_contract(self):
        with world() as (tools, cache, requests):
            anchor = tools.own()
            cases = {"ownship": {}, "query_units": {"coalition": "hostile", "category": "air", "radius_nm": 100},
                     "get_unit": {"unit_id": "bandit"}, "count_units": {"coalition": "hostile", "group_by": "type"},
                     "group_summary": {}, "current_summary": {}, "mission_intel": {}, "recent_events": {"limit": 3},
                     "braa": {"unit_id": "bandit"}}
            self.assertEqual(set(cases), set(SCHEMAS))
            for name, args in cases.items():
                with self.subTest(name=name):
                    result = tools.call(name, args, anchor=anchor)
                    self.assertTrue(result["ok"])
                    self.assertIn("freshness", result["context"])
                    self.assertIn("health", result["context"])
            spatial = next(r for r in requests if "near" in r["params"])
            self.assertEqual(spatial["params"]["near"], {"anchor": "ownship", "radius_m": 185200})
            self.assertEqual(spatial["params"]["coalition"], "red")
            snapshot = cache.get()
            self.assertIsNotNone(snapshot)
            before = len(requests)
            self.assertIs(cache.get(), snapshot)
            self.assertEqual(len(requests), before)

    def test_no_model_defaults_and_strict_arguments(self):
        for name in SCHEMAS:
            with self.subTest(name=name), self.assertRaises(ValueError):
                arguments(name, {"shell": "never"})
        for args in ({"radius_nm": True}, {"limit": True}, {"limit": 0}, {"radius_nm": float('nan')},
                     {"coalition": "enemy"}, {"category": "guess"}):
            with self.assertRaises(ValueError):
                arguments("query_units", args)
        value = geometry({"position": None}, {"position": None})
        self.assertIsNone(value["bearing_true_deg"])
        self.assertIsNone(value["altitude_ft_msl"])
        self.assertEqual(value["aspect"], "unable")

    def test_filter_and_summary_evidence_tampering_rejected(self):
        changes = [
            ("query_units", lambda f: f["data"]["rows"][0].update(coalition="blue")),
            ("get_unit", lambda f: f["data"]["unit"].update(unit_id="other")),
            ("current_summary", lambda f: f["data"].update(total_units=999)),
            ("group_summary", lambda f: f["data"]["rows"][0].update(count=999)),
            ("ownship", lambda f: f["context"]["state"].update(age_ms=999)),
        ]
        for operation, change in changes:
            def transform(frame):
                if frame["operation"] == operation:
                    change(frame)
                return frame
            with self.subTest(operation=operation), world(transform=transform) as (tools, _, _):
                with self.assertRaises((ValueError, ClientQueryError)):
                    tools.call(operation, {"coalition": "hostile"} if operation == "query_units" else
                               {"unit_id": "bandit"} if operation == "get_unit" else {})

    def test_page_counts_and_nearest_are_not_global(self):
        with world([unit(str(i), lon=float(i+1)) for i in range(12)]) as (tools, _, _):
            result = tools.call("query_units", {"coalition": "hostile", "limit": 2})
            self.assertEqual(result["data"]["producer_matching_total"], 12)
            self.assertEqual(result["data"]["returned_filtered_count"], 2)
            self.assertEqual(result["data"]["nearest_scope"], "returned_page_only")
            count = tools.call("count_units", {"coalition": "hostile", "limit": 1})
            self.assertEqual(count["data"]["total"], 12)

    def test_cache_outage_and_scope_change_do_not_retain_facts(self):
        broken = [False]
        def change(frame):
            if broken[0]:
                frame["context"]["state"]["freshness"]["status"] = "stale"
            return frame
        with world(transform=change) as (_, cache, _):
            self.assertIsNotNone(cache.refresh())
            broken[0] = True
            self.assertIsNone(cache.refresh())
            broken[0] = False
            self.assertIsNotNone(cache.get())


class ControllerTests(unittest.TestCase):
    def test_addressed_then_bare_followup_then_mission_change_via_http(self):
        session = [1]
        def transform(frame):
            frame["context"]["mission"]["session_id"] = session[0]
            return frame
        with world(transform=transform) as (tools, cache, requests):
            provider = FakeProvider([call("get_unit", {"unit_id": "bandit"}, "first")],
                [("text", "Prometheus, contact MiG-29. Aspect unable.")],
                [call("get_unit", {"unit_id": "bandit"}, "followup")],
                [("text", "Prometheus, MiG-29.")],
                [call("braa", {"unit_id": "bandit"}, "where")],
                [("text", "Prometheus, BRAA 090 true, 60 nautical miles, ten thousand feet. Aspect unable.")])
            log = Mock()
            adapter = Controller(provider, tools, cache, log)
            with running(adapter) as server:
                def exchange(text):
                    code, raw = post(server.server_port, wire(bridge_request(text)))
                    self.assertEqual(code, 200)
                    return json.loads(raw)
                self.assertFalse(exchange("what type is it?")["deliverable"])
                self.assertTrue(exchange("Athena, give me that contact.")["deliverable"])
                self.assertEqual(exchange("what type is it?")["speech"], "Prometheus, MiG-29.")
                self.assertTrue(exchange("where did it go?")["deliverable"])
                self.assertGreaterEqual(sum(r["operation"] == "get_unit" for r in requests), 3)
                before = len(provider.requests)
                session[0] = 2
                self.assertIsNotNone(cache.refresh())
                self.assertFalse(exchange("what type is it?")["deliverable"])
                self.assertEqual(len(provider.requests), before)
                self.assertFalse(adapter.history)
                self.assertFalse(adapter.references)
            self.assertTrue(any(c.kwargs.get("kind") == "adapter_ingress" and
                                c.kwargs.get("text") == "what type is it?" for c in log.log.call_args_list))

    def test_text_smoke_is_labeled_and_cannot_claim_radio_delivery(self):
        with world() as (tools, cache, _), tempfile.TemporaryDirectory() as root:
            adapter = Controller(FakeProvider([("text", "Prometheus, unable.")]), tools, cache, Mock())
            with running(adapter) as server:
                result = smoke(f"http://127.0.0.1:{server.server_port}/radio/v1/exchange", "Athena, status?", Transcript(root))
            self.assertFalse(result["radio_delivery"])
            self.assertEqual(result["answer"], "Prometheus, unable.")

    def test_split_decimal_does_not_create_a_false_sentence(self):
        with world() as (tools, cache, _):
            provider = FakeProvider([("text", "Prometheus, range 60."), ("text", "5 nautical miles.")])
            result = Controller(provider, tools, cache, Mock()).exchange(req("Athena, range?"))
            self.assertEqual(result["speech"], "Prometheus, range 60.5 nautical miles.")

    def test_cache_failure_during_model_generation_rejects_stale_sentence(self):
        with world() as (tools, cache, _):
            class Failing(FakeProvider):
                def stream(self, *args, **kwargs):
                    cache.invalidate()
                    yield "text", "Prometheus, MiG-29."
            result = Controller(Failing(), tools, cache, Mock()).exchange(req())
            self.assertEqual(result["speech"], "Prometheus, unable.")

    def test_natural_language_uses_llm_and_first_sentence_precedes_final(self):
        with world() as (tools, cache, requests):
            finished = []
            class Streaming(FakeProvider):
                def stream(self, *args, **kwargs):
                    yield "text", "Prometheus, nearest returned MiG-29. "
                    finished.append(True)
                    yield "text", "Aspect unable."
            provider = Streaming()
            adapter = Controller(provider, tools, cache, Mock())
            stream = adapter.stream(req("Athena, could you tell me what is out there?"))
            first = next(stream)
            self.assertEqual(first["speech"], "Prometheus, nearest returned MiG-29.")
            self.assertEqual(finished, [])
            self.assertEqual(next(stream)["speech"], "Aspect unable.")
            with self.assertRaises(StopIteration):
                next(stream)

    def test_followup_refreshes_id_and_rolls_ten_exchanges(self):
        with world() as (tools, cache, _):
            provider = FakeProvider([call("get_unit", {"unit_id": "bandit"})],
                                    [("text", "Prometheus, MiG-29.")], [("text", "Prometheus, unable.")])
            adapter = Controller(provider, tools, cache, Mock())
            adapter.exchange(req())
            self.assertEqual(adapter.references, ["bandit"])
            adapter.exchange(req("Athena, where did it go?"))
            self.assertIn('bandit', provider.requests[-1][-2]["content"])
            self.assertEqual(provider.requests[0][0]["content"], CORE_PROMPT)
            for _ in range(12):
                provider.rounds.append([("text", "Prometheus, unable.")])
                adapter.exchange(req("Athena, check again?"))
            self.assertEqual(len(adapter.history), 10)

    def test_unaddressed_nonpilot_wrong_route_never_call_model(self):
        provider = FakeProvider()
        adapter = Controller(provider, Mock(), Mock(), Mock())
        for request in (req("what is my status"), replace(req(), speaker="other"),
                        replace(req(), frequency_hz=133000001), replace(req(), modulation="FM")):
            self.assertFalse(adapter.exchange(request)["deliverable"])
        self.assertEqual(provider.requests, [])

    def test_provider_tool_and_empty_failures_are_safe_unable(self):
        for events in (ProviderError(), [], [call("shell", {})], [("text", "invented facts")],
                       [("text", "Prometheus, " + "word "*40 + ".")]):
            with self.subTest(events=events), world() as (tools, cache, _):
                recorder = Mock()
                provider = FakeProvider(events)
                adapter = Controller(provider, tools, cache, recorder)
                self.assertEqual(adapter.exchange(req())["speech"], "Prometheus, unable.")
                self.assertNotIn("secret-provider-body", str(recorder.mock_calls))
        with world() as (tools, cache, _):
            provider = FakeProvider([call("get_unit", {"unit_id": "missing"})])
            self.assertEqual(Controller(provider, tools, cache, Mock()).exchange(req())["speech"], "Prometheus, unable.")

    def test_http_first_frame_before_provider_completion(self):
        with world() as (tools, cache, _):
            release, ended = threading.Event(), threading.Event()
            class Delayed(FakeProvider):
                def stream(self, *args, **kwargs):
                    yield "text", "Prometheus, MiG-29. "
                    release.wait(2)
                    ended.set()
                    yield "text", "Aspect unable."
            with running(Controller(Delayed(), tools, cache, Mock())) as server:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/radio/v1/exchange", body=wire(bridge_request("Athena, picture")),
                                   headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"})
                reply = connection.getresponse()
                self.assertEqual(json.loads(reply.readline())["speech"], "Prometheus, MiG-29.")
                self.assertFalse(ended.is_set())
                release.set()
                self.assertEqual(json.loads(reply.readline())["speech"], "Aspect unable.")
                self.assertEqual(reply.readline(), b"[DONE]\n")
                connection.close()


class ProviderTests(unittest.TestCase):
    def test_selected_tool_call_sse_shape_and_fragmented_arguments(self):
        def responder(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            chunks = [({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                        "function": {"name": "get_unit", "arguments": '{"unit_id":'}}]}, None),
                      ({"tool_calls": [{"index": 0, "function": {"arguments": '"bandit"}'}}]}, None),
                      ({}, "tool_calls")]
            for delta, finish in chunks:
                handler.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}).encode() + b"\n\n")
            handler.wfile.write(b"data: [DONE]\n\n")
        with fake_hermes(responder=responder) as (endpoint, requests):
            port = int(endpoint.split(":")[2].split("/")[0])
            provider = OpenAIProvider("synthetic-key", connection_factory=lambda *_a, **kw:
                                      http.client.HTTPConnection("127.0.0.1", port, **kw))
            from integrations.hermes_radio.tools import TOOLS
            result = list(provider.stream([], TOOLS, deadline=time.monotonic()+2))
            self.assertEqual(result[0][0], "calls")
            self.assertEqual(result[0][1][0]["id"], "call-1")
            self.assertEqual(result[0][1][0]["function"]["name"], "get_unit")
            self.assertEqual(json.loads(result[0][1][0]["function"]["arguments"]), {"unit_id": "bandit"})
            self.assertEqual(requests[0][0], "/v1/chat/completions")
            self.assertNotIn("reasoning_effort", requests[0][2])
            self.assertEqual({t["function"]["name"] for t in requests[0][2]["tools"]}, set(SCHEMAS))

    def test_selected_request_shape_stream_and_no_credentials_in_errors(self):
        def responder(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            for delta, finish in (({"content": "Prometheus, unable."}, None), ({}, "stop")):
                handler.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}).encode() + b"\n\n")
                handler.wfile.flush()
            handler.wfile.write(b"data: [DONE]\n\n")
        with fake_hermes(responder=responder) as (endpoint, requests):
            port = int(endpoint.split(":")[2].split("/")[0])
            provider = OpenAIProvider("synthetic-key-never-print", connection_factory=lambda *_a, **kw:
                                      http.client.HTTPConnection("127.0.0.1", port, **kw))
            self.assertEqual(list(provider.stream([], [], deadline=time.monotonic()+2)), [("text", "Prometheus, unable.")])
            payload = requests[0][2]
            self.assertEqual(payload["model"], "gpt-5.4-mini")
            self.assertNotIn("reasoning_effort", payload)
            self.assertTrue(payload["stream"])
            self.assertFalse(payload["store"])
        with fake_hermes(raw=b"secret-provider-body", status=401) as (endpoint, _):
            port = int(endpoint.split(":")[2].split("/")[0])
            provider = OpenAIProvider("synthetic-key", connection_factory=lambda *_a, **kw:
                                      http.client.HTTPConnection("127.0.0.1", port, **kw))
            with self.assertRaisesRegex(ProviderError, "^provider_failed$"):
                list(provider.stream([], [], deadline=time.monotonic()+2))


if __name__ == "__main__":
    unittest.main()
