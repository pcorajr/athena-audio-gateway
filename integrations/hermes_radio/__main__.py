"""Explicit production configuration. Does not install or start dependencies."""
import argparse
import os
import sys

from .facts import Facts
from .hermes_client import HermesClassifier
from .server import Adapter, RadioServer, serve


def main():
    parser = argparse.ArgumentParser(description="Loopback text-only Athena radio adapter")
    parser.add_argument("--port", type=int, default=8650)
    parser.add_argument("--state-client-config", required=True, help="Explicit Athena client JSON document")
    parser.add_argument("--hermes-endpoint", default=os.environ.get("ATHENA_RADIO_HERMES_ENDPOINT"))
    parser.add_argument("--hermes-timeout", type=float, required=True, help="Classification deadline in seconds; no retries")
    parser.add_argument("--request-timeout", type=float, required=True, help="HTTP header/body deadline in seconds")
    parser.add_argument("--speaker", default="Prometheus")
    args = parser.parse_args()
    facts = None
    try:
        classifier = HermesClassifier(args.hermes_endpoint, os.environ.get("ATHENA_RADIO_HERMES_KEY"),
                                      timeout=args.hermes_timeout)
        facts = Facts.from_path(args.state_client_config)
        server = RadioServer(Adapter(classifier, facts), port=args.port,
                             request_timeout=args.request_timeout, speaker=args.speaker)
        serve(server)
    except (ValueError, OSError, TypeError):
        print("radio_adapter_startup_failed", file=sys.stderr)
        return 1
    finally:
        if facts is not None:
            facts.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
