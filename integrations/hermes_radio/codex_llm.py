"""Radio LLM backend on the ChatGPT/Codex subscription (Responses API), with automatic API fallback.

The controller calls ``llm.answer_by(messages, deadline)`` with chat-style messages. ``CodexLLM`` sends them to
``<codex base>/responses`` (stream, store=false, reasoning effort configurable, default low) and returns the
text. Any failure - no credentials, HTTP error (quota 429, 5xx), timeout, empty answer - falls back to
``fallback(messages, deadline=...)`` (the OpenAI API mini model) inside the same exchange deadline.

Credentials are NOT handled here: a small helper runs inside Hermes' own environment
(``hermes_cli.auth.resolve_codex_runtime_credentials`` + ``agent.codex_headers``), which owns login refresh
and locking. The token is cached in memory and refreshed in the background ahead of its JWT expiry, and after
a 401 (with force_refresh). The token is never logged, printed or written to the transcript.
"""
import base64
import inspect
import http.client
import json
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit

HERMES_DIR = os.path.expanduser(os.environ.get("ATHENA_HERMES_DIR", "~/.hermes/hermes-agent"))
HERMES_PYTHON = os.environ.get("ATHENA_HERMES_PYTHON", os.path.join(HERMES_DIR, "venv", "bin", "python"))

_HELPER = r"""
import json, sys
from hermes_cli.auth import resolve_codex_runtime_credentials
from agent.codex_headers import codex_account_headers, codex_cloudflare_headers
creds = resolve_codex_runtime_credentials(force_refresh=sys.argv[1] == "1")
token = creds.get("api_key") or creds.get("access_token")
base = (creds.get("base_url") or "https://chatgpt.com/backend-api/codex").rstrip("/")
headers = dict(codex_cloudflare_headers(token, base_url=base))
headers.update(codex_account_headers(token))
json.dump({"token": token, "base_url": base, "headers": headers}, sys.stdout)
"""


def _jwt_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:  # noqa: BLE001 - opaque token: use the max cache age
        return 0.0


class HermesCodexCredentials:
    """Codex credentials through Hermes, cached until shortly before expiry (max 30 min).

    A radio call never waits for the Hermes helper (a subprocess that may take up to `timeout` s): current()
    returns the cached token and starts a background refresh 5 min before it expires; with no usable token it
    starts one and raises, so that call goes to the API fallback. get() is the blocking fetch the refresh uses."""

    def __init__(self, python=HERMES_PYTHON, cwd=HERMES_DIR, runner=None, clock=time.time, timeout=20.0):
        self.python, self.cwd, self.timeout, self.clock = python, cwd, timeout, clock
        self._runner = runner or self._run
        self._lock = threading.Lock()
        self._cached, self._until, self._refresh_at, self._exp = None, 0.0, 0.0, 0.0
        self._refreshing = False

    def _run(self, force):
        out = subprocess.run([self.python, "-c", _HELPER, "1" if force else "0"], cwd=self.cwd, capture_output=True,
                             text=True, timeout=self.timeout, check=False)
        if out.returncode != 0:
            # stderr may be long; never includes the token (helper prints it only on success to stdout)
            raise RuntimeError("codex_credentials_unavailable: %s" % (out.stderr.strip().splitlines() or ["?"])[-1][:200])
        return json.loads(out.stdout)

    def _fetch(self, force):
        creds = self._runner(force)
        if not creds.get("token"):
            raise RuntimeError("codex_credentials_unavailable: empty token")
        now, exp = self.clock(), _jwt_exp(creds["token"])
        with self._lock:
            self._cached, self._exp = creds, exp
            self._until = min(now + 1800, exp - 60) if exp else now + 1800
            self._refresh_at = min(now + 1500, exp - 300) if exp else now + 1500
        return creds

    def get(self, force=False):
        """Blocking: the cached credentials, else the helper's."""
        with self._lock:
            if not force and self._cached is not None and self.clock() < self._until:
                return self._cached
        return self._fetch(force)

    def current(self):
        """Never blocks: the cached credentials (refreshed ahead in the background), else RuntimeError."""
        with self._lock:
            now = self.clock()
            cached = self._cached if self._cached is not None and now < self._until else None
            due = cached is None or now >= self._refresh_at
            force = bool(self._exp) and now >= self._exp - 300  # the JWT itself is about to expire
        if due:
            self.refresh(force=force)
        if cached is None:
            raise RuntimeError("codex_credentials_unavailable: refreshing in the background")
        return cached

    def refresh(self, force=False):
        """Fetch in the background, one at a time (start-up warm-up, ahead of expiry, after a 401)."""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True

        def run():
            try:
                self._fetch(force)
            except Exception:  # noqa: BLE001 - the next call falls back and tries again
                pass
            finally:
                with self._lock:
                    self._refreshing = False
        threading.Thread(target=run, daemon=True, name="codex-creds-refresh").start()

    def warm(self):
        """Prefetch so the first radio call does not pay the helper start-up."""
        self.refresh()


def to_responses(messages):
    """Chat messages -> (instructions, Responses input items)."""
    instructions, items = [], []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role in ("system", "developer"):
            instructions.append(content)
        elif role == "assistant":
            items.append({"role": "assistant", "content": [{"type": "output_text", "text": content}]})
        else:
            items.append({"role": "user", "content": [{"type": "input_text", "text": content}]})
    return "\n\n".join(instructions), items


def _completed_text(response):
    out = []
    for item in (response or {}).get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text") and part.get("text"):
                out.append(part["text"])
    return "".join(out)


def parse_sse(lines):
    """Server-sent events -> (status, text). Deltas first; else output_text.done; else the completed response."""
    deltas, done, completed, failed = [], None, "", None
    for line in lines:
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "response.output_text.delta":
            deltas.append(event.get("delta", ""))
        elif kind == "response.output_text.done" and event.get("text"):
            done = event["text"] if done is None else done + event["text"]
        elif kind in ("response.failed", "response.incomplete", "error"):
            failed = json.dumps(event)[:300]
        elif kind == "response.completed":
            completed = _completed_text(event.get("response"))
            break
    if failed:
        return 502, failed  # a failed response is never a usable answer, even with partial text
    return 200, "".join(deltas) or done or completed


def read_chunks(reply, sock, left):
    """Body bytes of an http.client reply, chunk by chunk, each socket wait bounded by left() (seconds still allowed;
    raises TimeoutError at the limit). A peer that trickles bytes is cut at the limit, not at its own pace (review P1:
    readline()/read() bound each recv, not the total). `sock` is the connection's socket taken BEFORE getresponse():
    http.client sets conn.sock = None for a "Connection: close" or unchunked HTTP/1.0 reply."""
    while True:
        sock.settimeout(left())
        piece = reply.read1(16384)
        if not piece:
            return
        yield piece


def _http_post(url, headers, body, deadline):
    """POST and read a server-sent-event stream by `deadline` (time.monotonic(), a wall-clock limit: every
    socket wait is bounded by the time left, not by a per-read idle timeout); returns (status, text_or_error)."""
    parts = urlsplit(url)

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("codex_deadline")
        return remaining
    conn = http.client.HTTPSConnection(parts.netloc, timeout=left())
    try:
        conn.request("POST", parts.path, body=body, headers=headers)
        sock = conn.sock
        sock.settimeout(left())
        reply = conn.getresponse()
        if reply.status != 200:
            out = b""
            for piece in read_chunks(reply, sock, left):
                out += piece
                if len(out) >= 300:
                    break
            return reply.status, out[:300].decode("utf-8", "replace")

        def lines():
            buf = b""
            for piece in read_chunks(reply, sock, left):
                buf += piece
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode("utf-8", "replace")
            if buf:
                yield buf.decode("utf-8", "replace")
        return parse_sse(lines())
    finally:
        conn.close()


class CodexLLM:
    """llm.answer_by(messages, deadline) on the Codex subscription, falling back to the API model ``fallback``.

    Codex gets at most ``timeout`` s of wall-clock time and never the last ``fallback_reserve`` s before the
    exchange deadline, which belong to the fallback. The credential helper never runs inside a call."""

    def __init__(self, fallback, *, model="gpt-6-luna", effort="low", timeout=6.0, credentials=None, post=None,
                 fallback_label="openai-api", fallback_reserve=3.0):
        self.fallback, self.model, self.effort, self.timeout = fallback, model, effort, timeout
        self.fallback_reserve = fallback_reserve
        self.credentials = credentials or HermesCodexCredentials()
        self._post = post or _http_post
        self.fallback_label = fallback_label
        try:
            self._fallback_deadline = "deadline" in inspect.signature(fallback).parameters
        except (TypeError, ValueError):
            self._fallback_deadline = False
        self.last = {"backend": "openai-codex", "model": model}
        self.counts = {"codex": 0, "fallback": 0}

    @property
    def label(self):
        return "openai-codex:%s/%s" % (self.model, self.effort)

    def _call(self, messages, creds, until):
        instructions, items = to_responses(messages)
        body = {"model": self.model, "instructions": instructions, "input": items, "store": False, "stream": True,
                "reasoning": {"effort": self.effort}}
        headers = {"Authorization": "Bearer " + creds["token"], "Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        headers.update(creds.get("headers") or {})
        return self._post(creds["base_url"] + "/responses", headers, json.dumps(body).encode(), until)

    def answer_by(self, messages, deadline):
        """(text, meta) by `deadline` (time.monotonic()). meta = who answered THIS call, for the transcript."""
        reason = None
        try:
            until = min(time.monotonic() + self.timeout, deadline - self.fallback_reserve)
            if until - time.monotonic() < 1.0:
                raise TimeoutError("no time left for codex")
            status, text = self._call(messages, self.credentials.current(), until)
            if status == 401:
                self.credentials.refresh(force=True)  # in the background; this call uses the fallback
            if status == 200 and text.strip():
                self.counts["codex"] += 1
                self.last = {"backend": "openai-codex", "model": self.model, "effort": self.effort}
                return text.strip(), dict(self.last)
            reason = "codex_http_%d: %s" % (status, (text or "empty answer")[:160])
        except Exception as error:  # noqa: BLE001 - timeouts, credentials, network: fall back
            reason = "%s: %s" % (type(error).__name__, str(error)[:160])
        self.counts["fallback"] += 1
        backend, _, model = self.fallback_label.partition(":")
        self.last = {"backend": backend, "model": model or None, "fallback_reason": reason}
        meta = dict(self.last)
        text = self.fallback(messages, deadline=deadline) if self._fallback_deadline else self.fallback(messages)
        return text, meta

    def __call__(self, messages):
        """Without an exchange deadline: Codex for `timeout` s, then the fallback."""
        return self.answer_by(messages, time.monotonic() + self.timeout + self.fallback_reserve + 12.0)[0]
