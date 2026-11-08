"""Radio LLM backend on the ChatGPT/Codex subscription (Responses API), with automatic API fallback.

The controller calls ``llm(messages)`` with chat-style messages. ``CodexLLM`` sends them to
``<codex base>/responses`` (stream, store=false, reasoning effort configurable, default low) and returns the
text. Any failure - no credentials, HTTP error (quota 429, 5xx), timeout, empty answer - falls back to
``fallback(messages)`` (the OpenAI API mini model) so the radio never goes silent.

Credentials are NOT handled here: a small helper runs inside Hermes' own environment
(``hermes_cli.auth.resolve_codex_runtime_credentials`` + ``agent.codex_headers``), which owns login refresh
and locking. The token is cached in memory until shortly before its JWT expiry and re-fetched once on 401
(with force_refresh). The token is never logged, printed or written to the transcript.
"""
import base64
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
    """Fetch Codex credentials through Hermes; cache until 5 min before expiry (max 30 min)."""

    def __init__(self, python=HERMES_PYTHON, cwd=HERMES_DIR, runner=None, clock=time.time, timeout=20.0):
        self.python, self.cwd, self.timeout, self.clock = python, cwd, timeout, clock
        self._runner = runner or self._run
        self._lock = threading.Lock()
        self._cached, self._until = None, 0.0

    def _run(self, force):
        out = subprocess.run([self.python, "-c", _HELPER, "1" if force else "0"], cwd=self.cwd, capture_output=True,
                             text=True, timeout=self.timeout, check=False)
        if out.returncode != 0:
            # stderr may be long; never includes the token (helper prints it only on success to stdout)
            raise RuntimeError("codex_credentials_unavailable: %s" % (out.stderr.strip().splitlines() or ["?"])[-1][:200])
        return json.loads(out.stdout)

    def get(self, force=False):
        with self._lock:
            now = self.clock()
            if force or self._cached is None or now >= self._until:
                creds = self._runner(force)
                if not creds.get("token"):
                    raise RuntimeError("codex_credentials_unavailable: empty token")
                exp = _jwt_exp(creds["token"])
                self._until = min(now + 1800, exp - 300) if exp else now + 1800
                self._cached = creds
            return self._cached

    def warm(self):
        """Prefetch in the background so the first radio call does not pay the helper start-up."""
        threading.Thread(target=lambda: self._safe_get(), daemon=True, name="codex-creds-warm").start()

    def _safe_get(self):
        try:
            self.get()
        except Exception:  # noqa: BLE001 - first real call reports it
            pass


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


def _http_post(url, headers, body, timeout):
    """POST and read a server-sent-event stream; returns (status, text_or_error)."""
    parts = urlsplit(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=timeout)
    try:
        conn.request("POST", parts.path, body=body, headers=headers)
        reply = conn.getresponse()
        if reply.status != 200:
            return reply.status, reply.read()[:300].decode("utf-8", "replace")
        return parse_sse(raw.decode("utf-8", "replace") for raw in reply)
    finally:
        conn.close()


class CodexLLM:
    """llm(messages) on the Codex subscription; falls back to ``fallback`` on any failure."""

    def __init__(self, fallback, *, model="gpt-6-luna", effort="low", timeout=6.0, credentials=None, post=None,
                 fallback_label="openai-api"):
        self.fallback, self.model, self.effort, self.timeout = fallback, model, effort, timeout
        self.credentials = credentials or HermesCodexCredentials()
        self._post = post or _http_post
        self.fallback_label = fallback_label
        self.last = {"backend": "openai-codex", "model": model}
        self.counts = {"codex": 0, "fallback": 0}

    @property
    def label(self):
        return "openai-codex:%s/%s" % (self.model, self.effort)

    def _call(self, messages, force):
        creds = self.credentials.get(force=force)
        instructions, items = to_responses(messages)
        body = {"model": self.model, "instructions": instructions, "input": items, "store": False, "stream": True,
                "reasoning": {"effort": self.effort}}
        headers = {"Authorization": "Bearer " + creds["token"], "Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        headers.update(creds.get("headers") or {})
        return self._post(creds["base_url"] + "/responses", headers, json.dumps(body).encode(), self.timeout)

    def __call__(self, messages):
        reason = None
        try:
            status, text = self._call(messages, force=False)
            if status == 401:
                status, text = self._call(messages, force=True)
            if status == 200 and text.strip():
                self.counts["codex"] += 1
                self.last = {"backend": "openai-codex", "model": self.model, "effort": self.effort}
                return text.strip()
            reason = "codex_http_%d: %s" % (status, (text or "empty answer")[:160])
        except Exception as error:  # noqa: BLE001 - timeouts, credentials, network: fall back
            reason = "%s: %s" % (type(error).__name__, str(error)[:160])
        self.counts["fallback"] += 1
        self.last = {"backend": self.fallback_label, "fallback_reason": reason}
        return self.fallback(messages)
