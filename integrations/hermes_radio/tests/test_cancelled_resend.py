"""Live 2026-11-11 10:21:31 (DCS server): after an outage every stream's reconnect cancelled the other streams.
The helicopter stream was cancelled about a second into its full resend, the reconnect sweep still ran on that
cut-short resend, and four helicopters that were alive were reported 'no longer showing'."""
import unittest
from unittest import mock

import grpc

from integrations.hermes_radio.eye import Eye, GrpcFeeder
from integrations.hermes_radio.tests import eye_fixture as fx

HELO = "GROUP_CATEGORY_HELICOPTER"


class Cancelled(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.CANCELLED


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def helo(uid):
    return fx.unit(uid, "Hip %d" % uid, "Mi-8MT", "red", "Hips", "HELICOPTER", *fx.offset(90, 30 + uid), 500.0)


class CancelledResendTests(unittest.TestCase):
    def run_stream(self, eye, clock, units, cancel):
        """One StreamUnits subscription: DCS re-sends `units`, then the call is cancelled or simply ends."""
        feeder = GrpcFeeder(eye, "127.0.0.1", 1)
        feeder._to_dict = lambda m: m

        def stream(_request):
            for u in units:
                yield fx.units_message(u)
            clock.t += 1.0
            if cancel:
                raise Cancelled()

        stub = mock.Mock()
        stub.StreamUnits = stream
        with mock.patch("dcs.mission.v0.mission_pb2_grpc.MissionServiceStub", return_value=stub), \
                mock.patch.object(feeder, "_check_session"):
            feeder._units("units-helo", HELO, 1, 1)
        feeder.close()

    def test_a_resend_cut_short_by_a_resubscribe_never_sweeps_live_units(self):
        clock = Clock()
        eye = Eye(clock=clock)
        hips = [helo(i) for i in (1, 2, 3, 4)]
        self.run_stream(eye, clock, hips, cancel=True)       # first subscription: all four seen
        clock.t += 10.0
        self.run_stream(eye, clock, hips[:2], cancel=True)   # cancelled by another stream's reconnect mid-resend
        clock.t += 11.0                                      # past the sweep window, before the next subscription
        eye.tick()
        self.assertEqual(len(eye.units), 4)
        self.assertEqual(list(eye.vanished), [])
        self.assertNotIn(HELO, eye.unit_sync)

    def test_a_complete_resend_still_sweeps_what_was_not_re_sent(self):
        clock = Clock()
        eye = Eye(clock=clock)
        hips = [helo(i) for i in (1, 2, 3, 4)]
        self.run_stream(eye, clock, hips, cancel=True)
        clock.t += 10.0
        self.run_stream(eye, clock, hips[:2], cancel=False)  # stream stays subscribed (the generator just ends here)
        clock.t += 11.0
        eye.tick()
        self.assertEqual(sorted(u["name"] for u in eye.units.values()), ["Hip 1", "Hip 2"])
        self.assertEqual(eye.last_sweep["removed"], 2)


if __name__ == "__main__":
    unittest.main()
