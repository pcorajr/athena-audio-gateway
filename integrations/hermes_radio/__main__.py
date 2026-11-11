"""Explicit production configuration. Does not install or start dependencies."""
import argparse
import os
import sys

from .transcript import Transcript
from .server import RadioServer, serve


def main():
    parser = argparse.ArgumentParser(description="Loopback text-only Athena radio adapter")
    parser.add_argument("--port", type=int, default=8650)
    parser.add_argument("--eye-host", default=os.environ.get("ATHENA_EYE_HOST"),
                        help="DCS-gRPC host for the in-memory Athena Eye (simple mode)")
    parser.add_argument("--eye-port", type=int, default=int(os.environ.get("ATHENA_EYE_PORT", "50051")))
    parser.add_argument("--cockpit-port", type=int, default=int(os.environ.get("ATHENA_COCKPIT_PORT", "7778")),
                        help="UDP port for the AthenaExport.lua cockpit feed from the DCS PC (eye mode; 0 = off)")
    parser.add_argument("--cockpit-host", default=os.environ.get("ATHENA_COCKPIT_HOST", "0.0.0.0"),
                        help="address the cockpit feed listener binds to")
    parser.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--model", default=os.environ.get("ATHENA_RADIO_MODEL", "gpt-5.4-mini"))
    parser.add_argument("--llm-backend", choices=("openai", "codex"),
                        default=os.environ.get("ATHENA_RADIO_BACKEND", "openai"),
                        help="eye mode: codex = ChatGPT/Codex subscription via Hermes credentials, falling back to "
                             "--model on the OpenAI API on any failure")
    parser.add_argument("--codex-model", default=os.environ.get("ATHENA_RADIO_CODEX_MODEL", "gpt-6-luna"))
    parser.add_argument("--codex-effort", choices=("none", "minimal", "low", "medium", "high"),
                        default=os.environ.get("ATHENA_RADIO_CODEX_EFFORT", "low"))
    parser.add_argument("--codex-timeout", type=float, default=float(os.environ.get("ATHENA_RADIO_CODEX_TIMEOUT", "6")),
                        help="wall-clock seconds the Codex call may take before the API fallback answers (inside the 10 s "
                             "exchange deadline)")
    parser.add_argument("--request-timeout", type=float, default=2.0, help="HTTP header/body deadline in seconds")
    parser.add_argument("--transcript-dir", default=os.environ.get("ATHENA_RADIO_TRANSCRIPT_DIR"))
    parser.add_argument("--speaker", default=os.environ.get("ATHENA_RADIO_SPEAKER", "Prometheus,Player,Client"),
                        help="comma-separated, case-insensitive SRS pilot names")
    parser.add_argument("--stores-dir", default=os.environ.get("ATHENA_EYE_STORES_DIR"),
                        help="durable loadout/shot log directory (stores-YYYYMMDD.jsonl); default: transcript dir")
    parser.add_argument("--whisper-prompt-out", default=os.environ.get("ATHENA_WHISPER_DYNAMIC_PROMPT"),
                        help="file the eye keeps current with callsign + nearby airfields for the gateway's "
                             "--athena-dynamic-prompt-file; default: <transcript dir>/whisper-prompt-dynamic.txt")
    args = parser.parse_args()
    feeder = listener = None
    try:
        transcript = Transcript(args.transcript_dir)
        if not transcript.log(kind="adapter_start", outcome="starting", provider="openai-api", model=args.model):
            return 1
        if args.eye_host:
            # Athena Eye mode: in-memory DCS-gRPC tracker + simple controller; no state service needed.
            import logging
            from .eye import build
            from .simple_controller import SimpleController
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
            eye, feeder = build(args.eye_host, args.eye_port, player=args.speaker,
                                stores_dir=args.stores_dir or str(transcript.root),
                                prompt_path=args.whisper_prompt_out or str(transcript.root / "whisper-prompt-dynamic.txt"))
            feeder.start()
            transcript.log(kind="eye_start", outcome="starting", source="%s:%d" % (args.eye_host, args.eye_port))
            if args.cockpit_port:
                from .cockpit import CockpitListener
                listener = CockpitListener(eye, host=args.cockpit_host, port=args.cockpit_port).start()
                transcript.log(kind="cockpit_start", outcome="listening" if listener.error is None else "failed",
                               source="udp %s:%d" % (args.cockpit_host, args.cockpit_port),
                               error=listener.error or "")
            controller = SimpleController(None, transcript, model=args.model, api_key=os.environ.get("OPENAI_API_KEY"),
                                          base_url=args.openai_base_url, eye=eye, speaker=args.speaker)
            if args.llm_backend == "codex":
                from .codex_llm import CodexLLM
                controller._llm = CodexLLM(controller.ask, model=args.codex_model, effort=args.codex_effort,
                                           timeout=args.codex_timeout, fallback_label="openai-api:" + args.model)
                controller._llm.credentials.warm()
                transcript.log(kind="llm_backend", outcome="codex", provider="openai-codex", model=args.codex_model,
                               effort=args.codex_effort, fallback="openai-api:" + args.model)
            server = RadioServer(controller, port=args.port, request_timeout=args.request_timeout, speaker=args.speaker)
            serve(server)
            return 0
        print("radio_adapter_startup_failed: --eye-host required (legacy state-service mode: python -m legacy.integrations.hermes_radio_legacy)", file=sys.stderr)
        return 1
    except (ValueError, OSError, TypeError, ImportError) as error:
        print("radio_adapter_startup_failed: %s" % type(error).__name__, file=sys.stderr)
        return 1
    finally:
        if feeder is not None:
            feeder.close()
        if listener is not None:
            listener.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
