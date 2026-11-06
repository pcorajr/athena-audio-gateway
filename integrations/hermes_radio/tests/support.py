"""Test-owned sockets and explicit synthetic config; never production I/O."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
from unittest.mock import patch

from athena.state_service_client import load_client_config
from integrations.hermes_radio.fixture_data import client_document, state_frame, wire


@contextmanager
def fake_state(responder=None, *, timeout_ms=500, config_changes=None):
    with tempfile.TemporaryDirectory(prefix="hr-", dir=os.environ["TMPDIR"]) as directory:
        root = Path(directory)
        path = root / "state.sock"
        config_path = root / "client.json"
        document = client_document(path, timeout_ms=timeout_ms)
        document.update(config_changes or {})
        config_path.write_text(json.dumps(document), encoding="utf-8")
        # Only relocate the accepted client's path root to the test-owned dir.
        # All config/codec/semantic validation and actual AF_UNIX I/O remain.
        with patch("metis.state_service.config._SOCKET_ROOT", root):
            config = load_client_config(config_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(0.05)
        stop = threading.Event()
        requests, errors, closed = [], [], []

        def run():
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    with connection:
                        connection.settimeout(2)
                        raw = bytearray()
                        while not raw.endswith(b"\n"):
                            chunk = connection.recv(4096)
                            if not chunk:
                                break
                            raw.extend(chunk)
                        if not raw:
                            continue
                        request = json.loads(raw)
                        requests.append(request)
                        if responder:
                            responder(connection, request)
                        else:
                            connection.sendall(wire(state_frame(request["operation"], request["request_id"])))
                        try:
                            closed.append(connection.recv(1) == b"")
                        except (OSError, TimeoutError):
                            closed.append(True)
                except (BrokenPipeError, ConnectionResetError):
                    closed.append(True)
                except Exception as error:
                    errors.append(error)
        worker = threading.Thread(target=run, name="fake-state")
        worker.start()
        try:
            yield config, requests, closed
        finally:
            stop.set()
            worker.join(3)
            listener.close()
            if worker.is_alive():
                raise AssertionError("fake state did not stop")
            if errors:
                raise errors[0]


@contextmanager
def fake_hermes(raw=None, *, status=200, responder=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            if responder:
                responder(self)
                return
            body = raw if raw is not None else model_reply("current_state")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/responses", requests
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def model_reply(intent):
    return json.dumps({"status": "completed", "output": [{"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps({"intent": intent})}]}]}).encode()


def post(port, body):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", "/radio/v1/exchange", body=body, headers={"Content-Type": "application/json"})
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()
