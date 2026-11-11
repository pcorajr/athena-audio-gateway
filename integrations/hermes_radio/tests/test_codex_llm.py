"""Codex-subscription radio backend: request shape, streaming, 401 refresh, fallback, no token leaks."""
import json
import time
import unittest

from integrations.hermes_radio.codex_llm import CodexLLM, HermesCodexCredentials, to_responses
from integrations.hermes_radio.simple_controller import SimpleController
from integrations.hermes_radio.tests.test_eye_round4 import FakeTranscript, req

SECRET = "header.eyJleHAiOjQxMDI0NDQ4MDB9.sig-SECRET"  # exp 2100-01-01


class FakeCreds:
    def __init__(self):
        self.calls, self.refreshes = [], []

    def current(self):
        self.calls.append(False)
        return {"token": SECRET, "base_url": "https://chatgpt.example/backend-api/codex",
                "headers": {"ChatGPT-Account-ID": "acct"}}

    def refresh(self, force=False):
        self.refreshes.append(force)


class Post:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def __call__(self, url, headers, body, deadline):
        self.seen.append((url, headers, json.loads(body), deadline - time.monotonic()))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


MESSAGES = [{"role": "system", "content": "You are Athena."}, {"role": "user", "content": "radio check"},
            {"role": "assistant", "content": "loud and clear"}, {"role": "user", "content": "picture"}]


class CodexLLMTests(unittest.TestCase):
    def test_request_shape(self):
        post = Post((200, "Prometheus, picture clean."))
        llm = CodexLLM(lambda m: "FALLBACK", credentials=FakeCreds(), post=post)
        self.assertEqual(llm(MESSAGES), "Prometheus, picture clean.")
        url, headers, body, timeout = post.seen[0]
        self.assertEqual(url, "https://chatgpt.example/backend-api/codex/responses")
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["instructions"], "You are Athena.")
        self.assertEqual([i["role"] for i in body["input"]], ["user", "assistant", "user"])
        self.assertEqual(body["input"][1]["content"][0]["type"], "output_text")
        self.assertEqual(headers["ChatGPT-Account-ID"], "acct")
        self.assertTrue(5.5 < timeout <= 6.0, timeout)  # wall-clock budget, not a per-read idle timeout
        self.assertEqual(llm.last["backend"], "openai-codex")

    def test_401_refreshes_in_the_background_and_falls_back(self):
        # phase 1: the 20 s credential helper never runs inside a radio call
        creds = FakeCreds()
        llm = CodexLLM(lambda m: "FALLBACK", credentials=creds, post=Post((401, "expired")))
        self.assertEqual(llm(MESSAGES), "FALLBACK")
        self.assertEqual(creds.refreshes, [True])
        self.assertIn("codex_http_401", llm.last["fallback_reason"])

    def test_fallback_on_quota_timeout_empty_and_credentials(self):
        for reply in ((429, "usage limit reached"), TimeoutError("timed out"), (200, "  "), (502, "failed")):
            llm = CodexLLM(lambda m: "FALLBACK", credentials=FakeCreds(), post=Post(reply))
            self.assertEqual(llm(MESSAGES), "FALLBACK", reply)
            self.assertEqual(llm.last["backend"], "openai-api")
            self.assertNotIn("SECRET", json.dumps(llm.last))

        class Broken:
            def current(self):
                raise RuntimeError("codex_credentials_unavailable: not logged in")
        llm = CodexLLM(lambda m: "FALLBACK", credentials=Broken(), post=Post())
        self.assertEqual(llm(MESSAGES), "FALLBACK")
        self.assertIn("not logged in", llm.last["fallback_reason"])

    def test_credentials_cache_until_expiry_and_force(self):
        runs = []
        now = [1000.0]

        def runner(force):
            runs.append(force)
            return {"token": SECRET, "base_url": "b", "headers": {}}
        creds = HermesCodexCredentials(runner=runner, clock=lambda: now[0])
        creds.get()
        creds.get()
        self.assertEqual(runs, [False])
        now[0] += 1801  # max cache age
        creds.get()
        creds.get(force=True)
        self.assertEqual(runs, [False, False, True])

    def test_to_responses_merges_system(self):
        instructions, items = to_responses([{"role": "system", "content": "a"}, {"role": "system", "content": "b"},
                                            {"role": "user", "content": "c"}])
        self.assertEqual(instructions, "a\n\nb")
        self.assertEqual(len(items), 1)


class ControllerIntegrationTests(unittest.TestCase):
    def test_transcript_records_backend_without_token(self):
        class Eye:
            def picture(self):
                return {"connected": True, "ownship": {"type": "A-10C_2", "coalition": "blue"},
                        "data_age_s": {"units": 1}}
        transcript = FakeTranscript()
        controller = SimpleController(None, transcript, model="gpt-5.4-mini", api_key="unused", eye=Eye())
        controller._llm = CodexLLM(lambda m: "FALLBACK", credentials=FakeCreds(), post=Post((200, "Prometheus, Athena, loud and clear.")))
        out = controller.exchange(req("Athena, radio check"))
        self.assertIn("loud and clear", out["speech"])
        dumped = json.dumps([{k: v for k, v in r.items() if k != "request"} for r in transcript.records], default=str)
        self.assertNotIn("SECRET", dumped)
        self.assertIn("openai-codex", dumped)


class SSEParseTests(unittest.TestCase):
    def test_deltas_done_and_completed_fallbacks(self):
        from integrations.hermes_radio.codex_llm import parse_sse

        def ev(obj):
            return "data: " + json.dumps(obj)
        self.assertEqual(parse_sse([ev({"type": "response.output_text.delta", "delta": "a"}),
                                    ev({"type": "response.output_text.delta", "delta": "b"})]), (200, "ab"))
        self.assertEqual(parse_sse([ev({"type": "response.output_text.done", "text": "done text"})]),
                         (200, "done text"))
        completed = {"type": "response.completed", "response": {"output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": "final text"}]}]}}
        self.assertEqual(parse_sse([ev(completed)]), (200, "final text"))
        self.assertEqual(parse_sse([ev({"type": "response.failed", "response": {}})])[0], 502)
        self.assertEqual(parse_sse(["event: ping", "data: not json"]), (200, ""))


if __name__ == "__main__":
    unittest.main()
