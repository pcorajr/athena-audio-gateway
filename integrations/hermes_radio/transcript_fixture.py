"""Synthetic-only cross-language append fixture. Never uses the default log root."""
import sys
from .transcript import Transcript


def main():
    writer = Transcript(sys.argv[1])
    for i in range(int(sys.argv[2])):
        if not writer.log(kind="fixture", text="synthetic Python record", outcome="fixture", sequence=i):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
