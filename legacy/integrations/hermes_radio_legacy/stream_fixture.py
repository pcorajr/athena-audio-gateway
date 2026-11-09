"""Synthetic controller fixture with a Go-TTS acknowledgement barrier."""
import os
from pathlib import Path
import time
from .controller import Controller
from .fixture_server import FixtureFacts
from integrations.hermes_radio.transcript import Transcript
from .tools import Evidence


class Snapshot:
    epoch = 0
    clock = staticmethod(time.monotonic)
    def get(self):
        own = FixtureFacts("current_state").query("current_state")
        fresh = own.context.state.freshness
        return own, Evidence({"synthetic": "validated ownship fixture"}, self.clock() + (fresh.max_age_ms-fresh.age_ms)/1000)
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
