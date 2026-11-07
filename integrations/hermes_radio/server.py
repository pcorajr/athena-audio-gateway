"""Loopback HTTP boundary: one active exchange and no application queue."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
import signal
import socket
from socketserver import ThreadingMixIn
import threading

from athena.state_service_client import ClientQueryError

from .contract import ContractError, MAX_BODY_BYTES, encode_response, parse_request, response, safe_text
from .facts import CAPABILITIES, render
from .hermes_client import classify
from .tactical import TACTICAL_INTENTS


class Adapter:
    def __init__(self, classifier, facts):
        self.classifier, self.facts = classifier, facts

    def exchange(self, request):
        intent = classify(request.transcript, self.classifier)
        if intent not in {"current_state", "recent_events"} | TACTICAL_INTENTS:
            return response(request, speech=CAPABILITIES, state="ok")
        try:
            return render(request, intent, self.facts.query(intent))
        except (ClientQueryError, ValueError, TypeError, KeyError, OverflowError):
            speech = ("No SAM data. Athena not connected." if intent == "threats" else
                      "Unable, Athena not connected." if intent == "home_plate" else "Athena not connected.")
            return response(request, speech=speech, state="unavailable")


class RadioServer(ThreadingMixIn, HTTPServer):
    # Only the single admitted handler gets a thread. Backlog is bounded;
    # requests accepted while active are rejected, never queued for exchange.
    request_queue_size = 1
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, adapter, *, port=8650, request_timeout, speaker="Prometheus"):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid_port")
        if type(request_timeout) not in (int, float) or not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("invalid_request_timeout")
        safe_text(speaker)
        self.adapter, self.request_timeout, self.speaker = adapter, request_timeout, speaker
        self._active = threading.Lock()
        super().__init__(("127.0.0.1", port), RadioHandler)

    def process_request(self, request, client_address):
        if not self._active.acquire(blocking=False):
            try:
                request.settimeout(self.request_timeout)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._active.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._active.release()

    def handle_error(self, request, client_address):
        # No tracebacks/URLs/transcripts/facts in default server logs.
        pass


class RadioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.request_timeout)
        self._expired = threading.Event()

        def expire():
            self._expired.set()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # Bounds header AND body acquisition cumulatively, including slow drip.
        self._timer = threading.Timer(self.server.request_timeout, expire)
        self._timer.daemon = True
        self._timer.start()

    def _stop_timer(self):
        self._timer.cancel()
        self._timer.join()

    def finish(self):
        self._stop_timer()
        super().finish()

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        self.close_connection = True
        if self.path != "/radio/v1/exchange":
            self.send_error(404)
            return
        lengths = self.headers.get_all("Content-Length", [])
        types = self.headers.get_all("Content-Type", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or
                len(lengths[0]) > 10 or self.headers.get_all("Transfer-Encoding") or
                self.headers.get_all("Content-Encoding") or self.headers.get_all("Expect") or
                len(types) != 1 or types[0].lower() not in {"application/json", "application/json; charset=utf-8"}):
            self.send_error(400)
            return
        size = int(lengths[0])
        if size <= 0 or size > MAX_BODY_BYTES:
            self.send_error(413)
            return
        try:
            raw = self.rfile.read(size)
            if len(raw) != size or self._expired.is_set():
                self.send_error(400)
                return
            request = parse_request(raw, speaker=self.server.speaker)
            self._stop_timer()
            if self._expired.is_set():
                return
            # No second HTTP request on this connection; protocol 1.0 close.
            wire = encode_response(self.server.adapter.exchange(request))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(wire)
        except ContractError:
            self.send_error(400)
        except (OSError, TimeoutError):
            pass


def serve(server, *, ready=None):
    """Main-thread lifecycle; SIGINT/SIGTERM stop only this server process."""
    stop = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="radio-http")
    worker.start()
    try:
        if ready is not None:
            ready(server)
        stop.wait()
    finally:
        server.shutdown()
        worker.join()
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
