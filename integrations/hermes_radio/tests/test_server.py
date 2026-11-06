from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from integrations.hermes_radio.contract import MAX_BODY_BYTES
from integrations.hermes_radio.facts import Facts
from integrations.hermes_radio.fixture_data import bridge_request, state_frame, wire
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
        headers_sent, rejected_closed = threading.Event(), threading.Event()
        rejected_peers = []
        test = self

        def held_state(connection, request):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test release timeout")
            connection.sendall(wire(state_frame(request["operation"], request["request_id"])))

        class SplitBodyConnection(http.client.HTTPConnection):
            sends = 0
            def send(self, data):
                self.sends += 1
                super().send(data)
                if self.sends == 1:
                    # Force the observed header -> real busy close -> body
                    # schedule, without sleeps or changing the rejection path.
                    headers_sent.set()
                    test.assertTrue(rejected_closed.wait(2), "busy connection did not close")

        with fake_hermes(model_reply("current_state")) as (endpoint, model_calls), \
                fake_state(held_state, timeout_ms=4000) as (config, state_calls, closed):
            facts = Facts(config)
            adapter = Mock(wraps=Adapter(HermesClassifier(endpoint, "synthetic-key", timeout=1), facts))
            try:
                with running(adapter) as server, ThreadPoolExecutor(max_workers=1) as clients:
                    first = clients.submit(post, server.server_port, wire(bridge_request("Give me a check")))
                    connection = SplitBodyConnection("127.0.0.1", server.server_port, timeout=2)
                    original_process = server.process_request

                    def reject_after_headers(request, address):
                        test.assertTrue(headers_sent.wait(2), "busy headers not sent")
                        rejected_peers.append(address)
                        original_process(request, address)  # unmodified early reject/close
                        rejected_closed.set()

                    try:
                        self.assertTrue(entered.wait(2))
                        with patch.object(server, "process_request", side_effect=reject_after_headers):
                            connection.connect()
                            rejected_address = connection.sock.getsockname()
                            try:
                                connection.request("POST", "/radio/v1/exchange",
                                    body=wire(bridge_request("Check the rejected request") |
                                              {"transmission_id": "rejected-transmission"}),
                                    headers={"Content-Type": "application/json"})
                                reply = connection.getresponse()
                                self.assertEqual((reply.status, reply.read()), (503, b""))
                                outcome = "503"
                            except (BrokenPipeError, ConnectionResetError) as error:
                                # Only this proven-busy request may see the real
                                # early-close race. Timeouts/other errors fail.
                                self.assertIn(error.errno, (errno.EPIPE, errno.ECONNRESET))
                                self.assertEqual(connection.sends, 2)
                                outcome = type(error).__name__
                        self.assertTrue(rejected_closed.is_set())
                        self.assertEqual(rejected_peers, [rejected_address])
                        self.assertEqual(adapter.exchange.call_count, 1)
                        self.assertEqual(len(model_calls), 1)
                        self.assertEqual(len(state_calls), 1)
                    finally:
                        connection.close()
                        release.set()
                    # No exceptions are accepted for the admitted or recovery request.
                    status, raw = first.result(timeout=2)
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(raw)["deliverable"])
                    self.assertIn("F-16", json.loads(raw)["speech"])
                    # Join the handler's cleanup boundary before proving recovery.
                    self.assertTrue(server._active.acquire(timeout=2))
                    server._active.release()
                    self.assertEqual(adapter.exchange.call_count, 1)
                    status, raw = post(server.server_port, wire(bridge_request("Give me another check") |
                                                              {"transmission_id": "recovery-transmission"}))
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(raw)["deliverable"])
                    self.assertEqual(json.loads(raw)["transmission_id"], "recovery-transmission")
            finally:
                release.set()
                facts.close()
        # Server/worker shutdown drains all possible work: rejection never runs later.
        self.assertEqual(adapter.exchange.call_count, 2)
        self.assertEqual([call.args[0].transmission_id for call in adapter.exchange.call_args_list],
                         ["synthetic-transmission", "recovery-transmission"])
        self.assertEqual(len(model_calls), 2)
        self.assertEqual(len(state_calls), 2)
        self.assertEqual(closed, [True, True])
        print("busy rejection:", outcome, "first/recovery=200; model/state calls=2; rejected work=0")

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
