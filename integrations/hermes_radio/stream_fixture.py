"""Synthetic controller fixture with a Go-TTS acknowledgement barrier."""
import os
from pathlib import Path
import time
from .controller import Controller
from .fixture_server import FixtureFacts
from .transcript import Transcript


class Snapshot:
    epoch = 0
    def get(self):
        own = FixtureFacts("current_state").query("current_state")
        return own, {"synthetic": "validated ownship fixture"}
    def invalidate(self):
        pass


class Provider:
    model = "synthetic-mini"
    def stream(self, messages, tools, *, deadline):
        yield "text", "Prometheus, synthetic first sentence. "
        # The next part cannot exist until the gateway actually enters TTS.
        ack = Path(os.environ["SRSMVP_STREAM_ACK"])
        until = time.monotonic() + 2
        while not ack.exists():
            if time.monotonic() >= until:
                raise ValueError("fixture_ack_timeout")
            time.sleep(0.01)
        yield "text", "Synthetic second sentence."


def adapter():
    return Controller(Provider(), None, Snapshot(), Transcript())
