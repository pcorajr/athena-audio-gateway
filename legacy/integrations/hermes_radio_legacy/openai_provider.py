"""Bounded direct OpenAI chat-completions SSE; no Hermes tools or fallback."""
import http.client
import json
import math
import socket
import threading
import time
from urllib.parse import urlsplit

from integrations.hermes_radio.contract import safe_text, strict_json


class ProviderError(ValueError):
    def __init__(self):
        super().__init__("provider_failed")


class OpenAIProvider:
    def __init__(self, api_key, *, base_url="https://api.openai.com/v1", model="gpt-5.4-mini",
                 reasoning=None, timeout=6.0, connection_factory=None):
        safe_text(api_key)
        safe_text(model)
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or
                url.fragment or not url.path.rstrip("/").endswith("/v1") or
                not math.isfinite(timeout) or timeout <= 0 or
                reasoning not in {None, "none", "minimal", "low", "medium", "high"}):
            raise ValueError("invalid_provider_config")
        self.model, self.reasoning, self.timeout = model, reasoning, timeout
        self._key, self._url = api_key, url
        self._factory = connection_factory or http.client.HTTPSConnection

    def stream(self, messages, tools, *, deadline):
        remaining = min(self.timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise ProviderError()
        payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto",
                   "parallel_tool_calls": False, "stream": True, "store": False, "max_completion_tokens": 512}
        # Selected mini rejects reasoning_effort=low with tools. Omit by default.
        if self.reasoning is not None:
            payload["reasoning_effort"] = self.reasoning
        connection = self._factory(self._url.hostname, self._url.port or 443, timeout=remaining)
        timer, reply = None, None
        try:
            connection.connect()
            sock = connection.sock
            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            connection.request("POST", self._url.path.rstrip("/") + "/chat/completions",
                body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json",
                         "Accept": "text/event-stream", "Connection": "close"})
            reply = connection.getresponse()
            if reply.status != 200 or reply.getheader("Content-Type", "").split(";")[0].strip() != "text/event-stream":
                raise ProviderError()
            calls, finished, size, text_seen = {}, None, 0, False
            while True:
                if time.monotonic() >= deadline:
                    raise ProviderError()
                line = reply.readline(65537)
                size += len(line)
                if not line or len(line) > 65536 or size > 262144:
                    raise ProviderError()
                line = line.strip()
                if not line or line.startswith(b":"):
                    continue
                if not line.startswith(b"data: "):
                    raise ProviderError()
                raw = line[6:]
                if raw == b"[DONE]":
                    if finished not in {"stop", "tool_calls"}:
                        raise ProviderError()
                    if calls:
                        if finished != "tool_calls" or text_seen:
                            raise ProviderError()
                        yield "calls", [calls[key] for key in sorted(calls)]
                    elif finished != "stop":
                        raise ProviderError()
                    return
                event = strict_json(raw)
                if event.get("error") or type(event.get("choices")) is not list or len(event["choices"]) != 1:
                    raise ProviderError()
                choice = event["choices"][0]
                if choice.get("index") != 0 or finished is not None:
                    raise ProviderError()
                delta = choice.get("delta", {})
                if delta.get("refusal"):
                    raise ProviderError()
                text = delta.get("content")
                if text:
                    if type(text) is not str or calls:
                        raise ProviderError()
                    text_seen = True
                    yield "text", text
                for call in delta.get("tool_calls", []):
                    index = call.get("index")
                    if type(index) is not int or not 0 <= index < 4 or text_seen:
                        raise ProviderError()
                    item = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("type", "function") != "function":
                        raise ProviderError()
                    item["id"] += call.get("id", "")
                    for key in ("name", "arguments"):
                        item["function"][key] += call.get("function", {}).get(key, "")
                        if len(item["function"][key]) > 4000:
                            raise ProviderError()
                finished = choice.get("finish_reason")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, http.client.HTTPException):
            raise ProviderError() from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if reply is not None:
                reply.close()
            connection.close()
