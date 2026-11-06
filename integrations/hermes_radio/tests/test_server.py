from contextlib import contextmanager
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock

from integrations.hermes_radio.contract import MAX_BODY_BYTES
from integrations.hermes_radio.facts import Facts
from integrations.hermes_radio.fixture_data import bridge_request, wire
from integrations.hermes_radio.fixture_server import FixtureClassifier, FixtureFacts
from integrations.hermes_radio.hermes_client import HermesClassifier
from integrations.hermes_radio.server import Adapter, RadioServer
from integrations.hermes_radio.tests.support import fake_hermes, fake_state, model_reply, post


@contextmanager
def running(adapter=None, *, timeout=0.3):
    if adapter is None:
        adapter = Adapter(FixtureClassifier("current_state"), FixtureFacts("current_state"))
    server = RadioServer(adapter, port=0, request_timeout=timeout)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(2)
        server.server_close()
        if worker.is_alive():
            raise AssertionError("HTTP server did not stop")


def raw_http(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = connection.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


class ServerTests(unittest.TestCase):
    def test_actual_http_fake_model_real_state_socket_both_intents(self):
        for intent in ("current_state", "recent_events"):
            with fake_hermes(model_reply(intent)) as (endpoint, model_calls), fake_state() as (config, state_calls, closed):
                facts = Facts(config)
                try:
                    adapter = Adapter(HermesClassifier(endpoint, "synthetic-key", timeout=1), facts)
                    with running(adapter) as server:
                        status, raw = post(server.server_port, wire(bridge_request("Give me a check")))
                    result = json.loads(raw)
                    self.assertEqual(status, 200)
                    self.assertTrue(result["deliverable"])
                    self.assertEqual(result["transmission_id"], "synthetic-transmission")
                    self.assertEqual(result["intent_class"], "factual")
                    self.assertEqual(len(model_calls), 1)
                    self.assertEqual(len(state_calls), 1)
                    self.assertEqual(state_calls[0]["operation"], "ownship" if intent == "current_state" else "recent_events")
                    self.assertIn("F-16" if intent == "current_state" else "Last events:", result["speech"])
                finally:
                    facts.close()

    def test_keyword_http_never_calls_model(self):
        classifier = Mock()
        with running(Adapter(classifier, FixtureFacts("current_state"))) as server:
            status, raw = post(server.server_port, wire(bridge_request()))
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["deliverable"])
        classifier.classify.assert_not_called()

    def test_model_failure_suppresses_and_never_queries_state(self):
        with fake_hermes(status=500) as (endpoint, model_calls):
            facts = Mock()
            adapter = Adapter(HermesClassifier(endpoint, "synthetic-key", timeout=1), facts)
            with running(adapter) as server:
                status, raw = post(server.server_port, wire(bridge_request("Unclear request")))
            result = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertFalse(result["deliverable"])
            self.assertEqual(result["speech"], "")
            self.assertEqual(result["intent_class"], "unrecognized")
            facts.query.assert_not_called()
            self.assertEqual(len(model_calls), 1)

    def test_malformed_oversize_and_contract_violations_never_exchange(self):
        for body in (b"{", b"{}", b"\xff", b"{}{}", b"x" * (MAX_BODY_BYTES + 1),
                     wire(bridge_request() | {"speaker": "Other"}),
                     wire(bridge_request() | {"audio": "forbidden"}),
                     wire(bridge_request("x" * 32001))):
            adapter = Mock()
            with running(adapter) as server:
                status, raw = post(server.server_port, body)
            self.assertIn(status, (400, 413))
            self.assertEqual(raw, b"")
            adapter.exchange.assert_not_called()

    def test_http_framing_and_path_method_fail_quiet(self):
        body = wire(bridge_request())
        prefix = b"POST /radio/v1/exchange HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
        variants = [prefix + b"\r\n" + body,
            prefix + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            prefix + b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
            prefix + b"Content-Length: 2\r\nContent-Encoding: gzip\r\n\r\n{}",
            prefix + b"Content-Length: -1\r\n\r\n{}",
            prefix + b"Content-Length: 2\r\nExpect: 100-continue\r\n\r\n{}",
            prefix + b"Content-Length: 999\r\n\r\n{}",
            b"GET /radio/v1/exchange HTTP/1.0\r\n\r\n",
            b"POST /other HTTP/1.0\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"]
        for value in variants:
            adapter = Mock()
            with running(adapter) as server:
                received = raw_http(server.server_port, value)
            self.assertTrue(received.startswith((b"HTTP/1.0 400", b"HTTP/1.0 404", b"HTTP/1.0 501")), received[:100])
            self.assertEqual(received.split(b"\r\n\r\n")[1], b"")
            adapter.exchange.assert_not_called()

    def test_concurrent_exchange_rejected_no_queue(self):
        entered, release = threading.Event(), threading.Event()
        real = Adapter(FixtureClassifier("current_state"), FixtureFacts("current_state"))
        class BlockingAdapter:
            calls = 0
            def exchange(self, request):
                self.calls += 1
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test release timeout")
                return real.exchange(request)
        adapter = BlockingAdapter()
        with running(adapter) as server:
            first = []
            worker = threading.Thread(target=lambda: first.append(post(server.server_port, wire(bridge_request()))))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                status, raw = post(server.server_port, wire(bridge_request()))
                self.assertEqual(status, 503)
                self.assertEqual(raw, b"")
                self.assertEqual(adapter.calls, 1)
            finally:
                release.set()
                worker.join(2)
            self.assertEqual(first[0][0], 200)

    def test_cumulative_slow_header_and_body_deadline(self):
        for partial in (b"POST /radio/v1/exchange HTTP/1.0\r\n", b"POST /radio/v1/exchange HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"):
            adapter = Mock()
            with running(adapter, timeout=0.08) as server:
                with socket.create_connection(server.server_address, timeout=1) as connection:
                    connection.sendall(partial)
                    start = time.monotonic()
                    for _ in range(10):
                        time.sleep(0.025)
                        try:
                            connection.sendall(b" ")
                        except OSError:
                            break
                    self.assertLess(time.monotonic() - start, 0.4)
                    # Deadline terminates header/body ingestion, not a per-byte reset.
                    result = connection.recv(1024)
                    self.assertNotIn(b"200 OK", result)
            adapter.exchange.assert_not_called()

    def test_loopback_only_and_invalid_configuration(self):
        with running() as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        for port in (-1, 65536, True):
            with self.assertRaises(ValueError):
                RadioServer(Mock(), port=port, request_timeout=1)
        for timeout in (0, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                RadioServer(Mock(), port=0, request_timeout=timeout)
