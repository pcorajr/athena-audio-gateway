"""Regenerate a mission debrief through the local memory socket (never DCS).

Usage: python -m integrations.hermes_radio.debrief --mission last [--read]
"""
import argparse
from pathlib import Path

from .memory_link import request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission", default="last")
    parser.add_argument("--read", action="store_true", help="also print the generated report")
    parser.add_argument("--socket", type=Path)
    args = parser.parse_args(argv)
    try:
        result = request({"operation": "debrief", "mission": args.mission}, path=args.socket, timeout=4)
        path = Path(result["path"])
        print(path)
        if args.read:
            print(path.read_text())
        return 0
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        print("Debrief unavailable: " + type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
