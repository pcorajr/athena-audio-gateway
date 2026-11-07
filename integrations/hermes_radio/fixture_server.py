"""Frozen fake-only launcher for the gateway composition target.

No production config/env/key/socket is consulted. Fakes pass wire fixtures
through the same public and adapter validators before deterministic rendering.
"""
import argparse
import json
import sys
import uuid

from athena.state_service_client import ClientQueryError, validate_query_response
from metis.state_service.socket_protocol import decode_server_frame

from .fixture_data import LIMITS, QUERY_LIMITS, RECENT_LIMITS, state_frame, wire
from .hermes_client import extract_intent
from .ownship import validate_ownship
from .server import Adapter, RadioServer, serve

SCENARIOS = ("current_state", "recent_events", "unavailable", "stale", "ambiguous", "unrecognized")


class FixtureClassifier:
    def __init__(self, scenario):
        self.scenario = scenario

    def classify(self, text):
        intent = self.scenario if self.scenario in {"recent_events", "unrecognized"} else "current_state"
        return extract_intent(json.dumps({"status": "completed", "output": [{"type": "message",
            "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({"intent": intent})}]}]}).encode())


class FixtureFacts:
    def __init__(self, scenario):
        self.scenario = scenario

    def query(self, intent):
        if self.scenario == "unavailable":
            raise ClientQueryError("socket_unavailable")
        request_id = uuid.uuid4().hex
        operation = "ownship" if intent == "current_state" else "recent_events"
        decoded = decode_server_frame(wire(state_frame(operation, request_id, scenario=self.scenario)), limits=LIMITS)
        if operation == "ownship":
            return validate_ownship(decoded, request_id=request_id)
        return validate_query_response(decoded, request_id=request_id, operation=operation,
            limits=LIMITS, query_limits=QUERY_LIMITS, request_params={"limit": 3}, recent_events_limits=RECENT_LIMITS)


def main():
    parser = argparse.ArgumentParser(description="Synthetic-only adapter fixture server")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--scenario", choices=SCENARIOS + ("streaming",), required=True)
    args = parser.parse_args()
    try:
        selected = Adapter(FixtureClassifier(args.scenario), FixtureFacts(args.scenario))
        if args.scenario == "streaming":
            from .stream_fixture import adapter
            selected = adapter()
        server = RadioServer(selected,
                             port=args.port, request_timeout=2.0)
        def ready(bound):
            print(json.dumps({"ready": True,
                "url": f"http://127.0.0.1:{bound.server_port}/radio/v1/exchange"}, separators=(",", ":")), flush=True)
        serve(server, ready=ready)
    except (OSError, ValueError):
        print("fixture_startup_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
