"""Live Transcript: rotation, private files, symlink/public-dir rejection, visible failure; synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from integrations.hermes_radio.transcript import Transcript
class TranscriptTests(unittest.TestCase):
    def test_rotation_whole_records_private_files_and_one_line_text(self):
        with tempfile.TemporaryDirectory() as root:
            clock = [datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc).astimezone()]
            writer = Transcript(root, clock=lambda: clock[0])
            def append(i):
                self.assertTrue(writer.log(kind="inbound", text="synthetic\nradio\rtext", outcome="recognized", sequence=i))
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(append, range(80)))
            first_day = clock[0].strftime("%Y%m%d")
            self.assertEqual(len(Path(root, f"transcript-{first_day}.txt").read_text().splitlines()), 80)
            records = [json.loads(line) for line in Path(root, f"transcript-{first_day}.jsonl").read_text().splitlines()]
            self.assertEqual({r["sequence"] for r in records}, set(range(80)))
            clock[0] = clock[0].replace(day=clock[0].day+1)
            writer.log(kind="inbound", text="", outcome="stt_failed")
            self.assertEqual(len(list(Path(root).glob("*.jsonl"))), 2)
            for path in Path(root).iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            # Another process uses the same protocol and never truncates.
            result = subprocess.run([sys.executable, "-B", "-m", "integrations.hermes_radio.transcript_fixture", root, "20"],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sum(len(p.read_text().splitlines()) for p in Path(root).glob("*.jsonl")), 101)

    def test_failure_is_visible_and_next_record_recovers_without_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            bad = Path(root, "bad")
            bad.write_text("synthetic existing non-directory")
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertFalse(Transcript(bad).log(kind="error", text="secret-error-body"))
            self.assertEqual(error.getvalue(), "athena_radio_transcript_write_failed\n")
            self.assertTrue(Transcript(Path(root, "good")).log(kind="recovered", text=""))

    def test_symlink_and_public_directory_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "public")
            path.mkdir(mode=0o755)
            path.chmod(0o755)  # independent of the worker's private umask
            with self.assertRaises(ValueError):
                Transcript(path).append({"kind": "inbound"})
            link = Path(root, "link")
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                Transcript(link).append({"kind": "inbound"})


if __name__ == "__main__":
    unittest.main()
