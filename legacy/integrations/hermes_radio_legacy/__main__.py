"""Legacy CLI: State Service picture source (Controller / SimpleController without an Eye).
Not on the live path. Run: python -m legacy.integrations.hermes_radio_legacy --state-client-config FILE
"""
import argparse
import os
import sys
from integrations.hermes_radio.server import RadioServer, serve
from integrations.hermes_radio.transcript import Transcript
from .controller import Controller
from .openai_provider import OpenAIProvider
from .simple_legacy import LegacySimpleController
from .tools import RadioTools, SnapshotCache


def main():
    parser = argparse.ArgumentParser(description="Legacy loopback text-only Athena radio adapter (State Service)")
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
    parser.add_argument("--speaker", default=os.environ.get("ATHENA_RADIO_SPEAKER", "Prometheus,Player,Client"),
                        help="comma-separated, case-insensitive SRS pilot names")
    args = parser.parse_args()
    cache = None
    try:
        transcript = Transcript(args.transcript_dir)
        if not transcript.log(kind="adapter_start", outcome="starting", provider="openai-api", model=args.model):
            return 1
        provider = OpenAIProvider(os.environ.get("OPENAI_API_KEY"), base_url=args.openai_base_url,
                                  model=args.model, reasoning=args.reasoning, timeout=args.controller_timeout)
        tools = RadioTools.from_path(args.state_client_config)
        cache = SnapshotCache(tools, interval=args.cache_interval)
        if os.environ.get("ATHENA_RADIO_SIMPLE") == "1":
            controller = LegacySimpleController(tools, transcript, model=args.model, api_key=os.environ.get("OPENAI_API_KEY"),
                                          speaker=args.speaker)
        else:
            controller = Controller(provider, tools, cache, transcript, timeout=args.controller_timeout)
        server = RadioServer(controller, port=args.port,
                             request_timeout=args.request_timeout, speaker=args.speaker)
        cache.start()
        serve(server)
    except (ValueError, OSError, TypeError, ImportError) as error:
        print("radio_adapter_startup_failed: %s" % type(error).__name__, file=sys.stderr)
        return 1
    finally:
        if cache is not None:
            cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
