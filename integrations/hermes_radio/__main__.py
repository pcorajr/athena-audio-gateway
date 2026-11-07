"""Explicit production configuration. Does not install or start dependencies."""
import argparse
import os
import sys

from .controller import Controller
from .openai_provider import OpenAIProvider
from .tools import RadioTools, SnapshotCache
from .transcript import Transcript
from .server import RadioServer, serve


def main():
    parser = argparse.ArgumentParser(description="Loopback text-only Athena radio adapter")
    parser.add_argument("--port", type=int, default=8650)
    parser.add_argument("--state-client-config", required=True, help="Explicit Athena client JSON document")
    parser.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--model", default=os.environ.get("ATHENA_RADIO_MODEL", "gpt-5.4-mini"))
    parser.add_argument("--reasoning", choices=("none", "minimal", "low", "medium", "high"), default=None,
                        help="Omitted by default; selected mini rejects low reasoning with tools")
    parser.add_argument("--controller-timeout", type=float, default=6.0)
    parser.add_argument("--request-timeout", type=float, default=2.0, help="HTTP header/body deadline in seconds")
    parser.add_argument("--cache-interval", type=float, default=2.0)
    parser.add_argument("--transcript-dir", default=os.environ.get("ATHENA_RADIO_TRANSCRIPT_DIR"))
    parser.add_argument("--speaker", choices=("Prometheus",), default="Prometheus")
    args = parser.parse_args()
    cache = None
    try:
        provider = OpenAIProvider(os.environ.get("OPENAI_API_KEY"), base_url=args.openai_base_url,
                                  model=args.model, reasoning=args.reasoning, timeout=args.controller_timeout)
        tools = RadioTools.from_path(args.state_client_config)
        cache = SnapshotCache(tools, interval=args.cache_interval)
        transcript = Transcript(args.transcript_dir)
        if not transcript.log(kind="adapter_start", outcome="starting", provider="openai-api", model=args.model):
            return 1
        if os.environ.get("ATHENA_RADIO_SIMPLE") == "1":
            from .simple_controller import SimpleController
            controller = SimpleController(tools, transcript, model=args.model, api_key=os.environ.get("OPENAI_API_KEY"))
        else:
            controller = Controller(provider, tools, cache, transcript, timeout=args.controller_timeout)
        server = RadioServer(controller, port=args.port,
                             request_timeout=args.request_timeout, speaker=args.speaker)
        cache.start()
        serve(server)
    except (ValueError, OSError, TypeError):
        print("radio_adapter_startup_failed", file=sys.stderr)
        return 1
    finally:
        if cache is not None:
            cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
