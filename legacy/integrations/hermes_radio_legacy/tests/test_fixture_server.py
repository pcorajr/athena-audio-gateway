import json
import os
import select
import signal
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from legacy.integrations.hermes_radio_legacy.fixture_data import bridge_request, wire
from legacy.integrations.hermes_radio_legacy.facts import CAPABILITIES
from legacy.integrations.hermes_radio_legacy.fixture_server import FixtureFacts, SCENARIOS
from legacy.integrations.hermes_radio_legacy.tests.support import post


class FixtureLauncherTests(unittest.TestCase):
    def test_all_cli_scenarios_readiness_http_and_clean_signal_teardown(self):
        expected = {"current_state": ("ok", "F-16"), "recent_events": ("ok", "Last events:"),
                    "unavailable": ("unavailable", "Athena not connected."), "stale": ("stale", "Stale state."),
                    "ambiguous": ("ownship_ambiguous", "Ownship not confirmed."),
                    "unrecognized": ("ok", CAPABILITIES)}
        for index, scenario in enumerate(SCENARIOS):
            with self.subTest(scenario=scenario):
                env = os.environ.copy()
                # Sentinel values must never be consulted by fixture mode.
                env.update(ATHENA_RADIO_HERMES_ENDPOINT="http://invalid.invalid/v1/responses",
                           ATHENA_RADIO_HERMES_KEY="unused-synthetic-key")
                process = subprocess.Popen([sys.executable, "-B", "-m", "legacy.integrations.hermes_radio_legacy.fixture_server",
                    "--port", "0", "--scenario", scenario], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 8)[0], "fixture readiness timeout")
                    ready = json.loads(process.stdout.readline())
                    self.assertEqual(set(ready), {"ready", "url"})
                    self.assertIs(ready["ready"], True)
                    url = urlsplit(ready["url"])
                    self.assertEqual(url.scheme, "http")
                    self.assertEqual(url.hostname, "127.0.0.1")
                    self.assertEqual(url.path, "/radio/v1/exchange")
                    # Keyword classification remains real. The unrecognized
                    # scenario requires a transcript with no keyword hit.
                    text = "recent events" if scenario == "recent_events" else "unclear check" if scenario == "unrecognized" else "status"
                    status, raw = post(url.port, wire(bridge_request(text)))
                    result = json.loads(raw)
                    self.assertEqual(status, 200)
                    self.assertEqual(result["state"], expected[scenario][0])
                    self.assertIn(expected[scenario][1], result["speech"])
                    self.assertTrue(result["deliverable"])
                    self.assertEqual(result["intent_class"], "factual")
                    self.assertLessEqual(len(result["speech"]), 350)
                    process.send_signal(signal.SIGINT if index % 2 else signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0)
                    self.assertEqual(stdout, b"")
                    self.assertEqual(stderr, b"")
                    with socket.socket() as connection:
                        connection.settimeout(0.2)
                        self.assertNotEqual(connection.connect_ex((url.hostname, url.port)), 0)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                    process.stdout.close()
                    process.stderr.close()

    def test_fixture_facts_do_not_access_config_or_sockets(self):
        with patch("socket.socket.connect", side_effect=AssertionError("unexpected connect")), \
             patch("athena.state_service_client.load_client_config", side_effect=AssertionError("unexpected config")):
            for scenario in ("current_state", "recent_events", "stale", "ambiguous"):
                result = FixtureFacts(scenario).query("recent_events" if scenario == "recent_events" else "current_state")
                self.assertTrue(result.ok)

    def test_startup_error_nonzero_without_ready_or_payload(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            completed = subprocess.run([sys.executable, "-B", "-m", "legacy.integrations.hermes_radio_legacy.fixture_server",
                "--port", str(occupied.getsockname()[1]), "--scenario", "current_state"], capture_output=True, timeout=8)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"fixture_startup_failed\n")

    def test_production_cli_requires_explicit_configuration(self):
        completed = subprocess.run([sys.executable, "-B", "-m", "legacy.integrations.hermes_radio_legacy", "--help"],
                                   capture_output=True, timeout=8)
        self.assertEqual(completed.returncode, 0)
        self.assertIn(b"--state-client-config", completed.stdout)
        self.assertIn(b"--controller-timeout", completed.stdout)
        self.assertIn(b"--openai-base-url", completed.stdout)
        self.assertIn(b"--model", completed.stdout)
        self.assertIn(b"--request-timeout", completed.stdout)
