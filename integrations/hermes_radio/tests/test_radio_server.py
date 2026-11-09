"""Live RadioServer coverage: plain JSON, NDJSON streaming + [DONE], request validation."""
from contextlib import contextmanager
import http.client
import json
import socket
import threading
import unittest
from integrations.hermes_radio.contract import response
from integrations.hermes_radio.server import RadioServer
from integrations.hermes_radio.tests.bridge import bridge_request, post, wire


class StubAdapter:
    def exchange(self, request):
        return response(request, speech="Stub reply.", state="ok")


class StreamingAdapter(StubAdapter):
    def stream(self, request):
        yield response(request, speech="First.", state="ok")
        yield response(request, speech="Second.", state="ok")


@contextmanager
def running(adapter):
    server = RadioServer(adapter, port=0, request_timeout=1.0)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(2)
        server.server_close()


def raw_http(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(payload)
        try:
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
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


def request(port, body, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", "/radio/v1/exchange", body=body, headers=headers)
        reply = connection.getresponse()
        return reply.status, reply.getheader("Content-Type"), reply.read()
    finally:
        connection.close()


class RadioServerTests(unittest.TestCase):
    def test_plain_json_exchange(self):
        with running(StubAdapter()) as server:
            status, raw = post(server.server_port, wire(bridge_request("status")))
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual((result["speech"], result["transmission_id"]), ("Stub reply.", "synthetic-transmission"))

    def test_ndjson_streams_each_part_then_done(self):
        with running(StreamingAdapter()) as server:
            status, ctype, raw = request(server.server_port, wire(bridge_request("status")),
                                         {"Content-Type": "application/json", "Accept": "application/x-ndjson"})
        self.assertEqual((status, ctype), (200, "application/x-ndjson"))
        lines = raw.split(b"\n")
        self.assertEqual([json.loads(x)["speech"] for x in lines[:2]], ["First.", "Second."])
        self.assertEqual(lines[2], b"[DONE]")

    def test_ndjson_without_stream_wraps_single_exchange(self):
        with running(StubAdapter()) as server:
            _, _, raw = request(server.server_port, wire(bridge_request()),
                                {"Content-Type": "application/json", "Accept": "application/x-ndjson"})
        lines = raw.split(b"\n")
        self.assertEqual(json.loads(lines[0])["speech"], "Stub reply.")
        self.assertEqual(lines[1], b"[DONE]")

    def test_validation_rejections(self):
        good = wire(bridge_request())
        cases = {
            "bad json": ("/radio/v1/exchange", "application/json", b"{not json}\n", 400),
            "wrong content type": ("/radio/v1/exchange", "text/plain", b"", 400),
            "zero length": ("/radio/v1/exchange", "application/json", b"", 413),
            "unknown path": ("/elsewhere", "application/json", b"", 404),
        }
        for name, (path, ctype, body, expected) in cases.items():
            # fresh server per case: the server is single-flight and answers 503 while a prior exchange winds down
            with self.subTest(name), running(StubAdapter()) as server:
                raw = raw_http(server.server_port, ("POST %s HTTP/1.0\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n"
                                                    % (path, ctype, len(body))).encode() + body)  # rejected before any body is read
                self.assertTrue(raw.startswith(b"HTTP/1.0 %d" % expected) or raw.startswith(b"HTTP/1.1 %d" % expected), raw[:40])


if __name__ == "__main__":
    unittest.main()
