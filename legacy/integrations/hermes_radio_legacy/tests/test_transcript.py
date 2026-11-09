"""Cross-process append/rotation and visible failures; synthetic data only."""
import json
from pathlib import Path
import tempfile
import unittest

from integrations.hermes_radio.transcript import Transcript
from legacy.integrations.hermes_radio_legacy.controller import Controller
from legacy.integrations.hermes_radio_legacy.text_smoke import smoke
from legacy.integrations.hermes_radio_legacy.tests.test_v2 import FakeProvider, world
from legacy.integrations.hermes_radio_legacy.tests.test_server import running


class TranscriptTests(unittest.TestCase):
    def test_request_and_reply_speakers_remain_distinct(self):
        with world() as (tools, cache, _), tempfile.TemporaryDirectory() as root:
            writer = Transcript(root)
            adapter = Controller(FakeProvider([("text", "Prometheus, contact MiG-29.")]), tools, cache, writer)
            with running(adapter) as server:
                result = smoke(f"http://127.0.0.1:{server.server_port}/radio/v1/exchange", "Athena, picture?", writer)
            records = [json.loads(line) for p in Path(root).glob("*.jsonl") for line in p.read_text().splitlines()]
            by_kind = {r["kind"]: r for r in records}
            for kind in ("text_smoke_input", "adapter_ingress", "reply_generated", "text_smoke_output"):
                record = by_kind[kind]
                self.assertEqual(record["speaker"], "Athena" if kind in {"reply_generated", "text_smoke_output"} else "Prometheus")
                self.assertEqual(record["request_speaker"], "Prometheus")
                self.assertEqual(record["transmission_id"], result["id"])
                self.assertEqual((record["frequency_hz"], record["modulation"]), (133000000, "AM"))
            self.assertEqual(by_kind["reply_generated"]["outcome"], "intended_not_sent")
            self.assertEqual(by_kind["text_smoke_output"]["outcome"], "text_only_not_spoken")
            self.assertFalse(result["radio_delivery"])

if __name__ == "__main__":
    unittest.main()
