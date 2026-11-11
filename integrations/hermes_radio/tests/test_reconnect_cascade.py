"""Reconnect cascade (live 2026-11-11 10:20-10:21, five rounds in ~40 s): after a full outage every unit/event stream
is in `_errored`; each one's first message used to cancel the others, so the five streams cancelled each other five
times. One reconnect round must be enough. Real GrpcFeeder._live/_resubscribe_others with fake calls, no network."""
import unittest
from integrations.hermes_radio.eye import Eye, GrpcFeeder

NAMES = ["units-helo", "units-ground", "units-air", "units-ship", "events"]


class Call:
    def __init__(self, name):
        self.name, self.cancelled = name, False

    def cancel(self):
        self.cancelled = True


class Sim:
    def __init__(self, errored):
        self.f = GrpcFeeder(Eye(), "127.0.0.1", 1)
        self.f._errored = set(errored)
        self.calls = {}
        for n in NAMES:
            self.subscribe(n)
        self.rounds = self.cancels = 0

    def subscribe(self, n):
        c = self.calls[n] = Call(n)
        if n == "events":
            self.f._events_call = c
        else:
            self.f._unit_calls.add(c)

    def first_message(self, n):
        """The stream's first message; a stream cancelled before it resubscribes (and will get one again)."""
        if self.calls[n].cancelled:
            self.f._unit_calls.discard(self.calls[n])
            self.subscribe(n)
            return False
        before = {m for m in NAMES if self.calls[m].cancelled}
        self.f._live(n, self.calls[n])
        hit = [m for m in NAMES if m != n and self.calls[m].cancelled and m not in before]
        if hit:
            self.rounds += 1
            self.cancels += len(hit)
        return True

    def run(self):
        queue, steps = list(NAMES), 0
        while queue and steps < 100:
            steps += 1
            n = queue.pop(0)
            if not self.first_message(n):
                queue.append(n)
            for m in NAMES:  # cancelled streams reconnect and will send a first message again
                if self.calls[m].cancelled and m not in queue:
                    queue.append(m)
        return steps


class CascadeTests(unittest.TestCase):
    def test_a_full_outage_converges_in_one_round(self):
        sim = Sim(NAMES + ["world"])
        self.assertLess(sim.run(), 100)
        self.assertEqual((sim.rounds, sim.cancels), (1, 4))
        self.assertFalse(set(NAMES) & sim.f._errored)

    def test_one_stream_failing_still_resubscribes_the_others_once(self):
        sim = Sim(["units-air"])
        sim.calls["units-helo"].cancelled = False
        self.assertTrue(sim.first_message("units-helo"))  # healthy streams do nothing
        self.assertEqual(sim.rounds, 0)
        self.assertTrue(sim.first_message("units-air"))  # the one that failed: the others resubscribe
        self.assertEqual((sim.rounds, sim.cancels), (1, 4))
        self.assertTrue(all(sim.calls[n].cancelled for n in NAMES if n != "units-air"))
        self.assertFalse(sim.calls["units-air"].cancelled)
        self.assertEqual(sim.f._errored, set())
        for n in NAMES:  # nothing left to cascade
            sim.first_message(n)
        self.assertEqual(sim.rounds, 1)

    def test_the_events_stream_failing_alone_resubscribes_the_unit_streams(self):
        sim = Sim(["events"])
        self.assertTrue(sim.first_message("events"))
        self.assertEqual(sim.cancels, 4)
        self.assertEqual(sim.f.eye.resubscribes["reconnect:events"], 1)


if __name__ == "__main__":
    unittest.main()
