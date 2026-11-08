"""Offline startup policy: execute only the memory block with every process seam stubbed.

Never execute or source the real up script: its other sections access live hosts.
"""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from integrations.hermes_radio.memory_link import source_matches


SCRIPT = Path.home() / ".hermes/cache/athena-srs-mvp-20261106/run/eye-up-memory.sh"


class MemoryStartupTests(unittest.TestCase):
    def startup(self, *, owned, health, stale=False):
        if not SCRIPT.exists():
            self.skipTest("operator startup variant is not installed on this host")
        source = SCRIPT.read_text()
        # Run exactly the reviewed memory policy and adapter invocation, not preflight,
        # credential lookup, process stopping, network checks or gateway startup.
        block = source.split("# memory6: independent source reader", 1)[1]
        block = block.split("unset OPENAI_API_KEY", 1)[0]
        stubs = """
set -eu
say() { printf '%s\\n' "$*"; }
alive() { test "$OWNED" = 1; }
cat() { printf 'fixture-pid'; }
mkdir() { :; }
sleep() { :; }
fake_python() { return "$HEALTH_STATUS"; }
setsid() { printf 'launch:%s enabled=%s\\n' "$*" "$ATHENA_MEMORY_ENABLED"; }
"""
        with tempfile.TemporaryDirectory(prefix="m6start") as tmp:
            root = Path(tmp)
            sock = root / "memory.sock"
            with socket.socket(socket.AF_UNIX) as stale_socket:
                if stale:
                    stale_socket.bind(str(sock))
                env = dict(os.environ, R=tmp, ATHENA_MEMORY_ROOT=tmp, ATHENA_MEMORY_SOCKET=str(sock),
                           ATHENA_MEMORY_ENABLED="1", OWNED=str(int(owned)), HEALTH_STATUS=str(int(not health)),
                           ATHENA_DCS_SOURCE="server", EYE_HOST="fixture.invalid", EYE_PORT="50052",
                           PYTHON="fake_python", OPENAI_API_KEY="offline-fixture-not-a-secret", PILOT_NAME="Fixture",
                           MODEL="fixture", COCKPIT_PORT="0", DYN_PROMPT=str(root / "prompt"), LOG_A=str(root / "adapter.log"))
                run = subprocess.run(["bash"], input=stubs + "\n#" + block + "\nwait\n", text=True,
                                     capture_output=True, env=env, timeout=3)
                self.assertEqual(run.returncode, 0, run.stderr)
                adapter = (root / "adapter.log").read_text()
                self.assertIn("integrations.hermes_radio --speaker", adapter)
                self.assertIn("enabled=" + str(int(health and not stale)), adapter)
                if stale:
                    self.assertTrue(sock.exists(), "unowned socket must remain untouched")
                self.assertFalse((root / "eye-memory.log").exists(), "must not start/restart the reader")
                return run.stdout

    def test_stale_unowned_socket_continues_to_eye_only_adapter(self):
        diagnostic = self.startup(owned=False, health=False, stale=True)
        self.assertIn("leaving it untouched; Eye continues alone", diagnostic)

    def test_existing_reader_mismatch_source_or_override_disables_link(self):
        health = {"ok": True, "source": "client", "endpoint": "client.fixture:50051"}
        for env in ({"ATHENA_DCS_SOURCE": "server", "ATHENA_DCS_SERVER_HOST": "server.fixture"},
                    {"ATHENA_DCS_SOURCE": "client", "ATHENA_DCS_CLIENT_HOST": "changed.fixture"},
                    {"ATHENA_DCS_SOURCE": "client", "ATHENA_DCS_CLIENT_HOST": "client.fixture", "ATHENA_DCS_CLIENT_PORT": "50052"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch(
                    "integrations.hermes_radio.memory_link.request", return_value=health):
                matches = source_matches()
                self.assertFalse(matches)
                self.assertIn("explicitly restart memory", self.startup(owned=True, health=matches))

    def test_matching_owned_reader_reused(self):
        with patch.dict(os.environ, {"ATHENA_DCS_SOURCE": "client", "ATHENA_DCS_CLIENT_HOST": "client.fixture"}, clear=True), patch(
                "integrations.hermes_radio.memory_link.request", return_value={"source": "client", "endpoint": "client.fixture:50051"}):
            self.assertTrue(source_matches())
        self.startup(owned=True, health=True)

    def test_unreachable_health_disables_link(self):
        with patch("integrations.hermes_radio.memory_link.request", side_effect=OSError):
            self.assertFalse(source_matches())
        self.assertIn("Eye continues alone", self.startup(owned=True, health=False))


if __name__ == "__main__":
    unittest.main()
