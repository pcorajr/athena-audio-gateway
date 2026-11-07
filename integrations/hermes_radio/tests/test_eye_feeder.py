"""End-to-end: GrpcFeeder against an in-process fake DCS-gRPC server built from the real 0.8 stubs.

Covers proto -> dict conversion on the wire, all streams, airbases, magnetic declination,
reconnect after the server goes away and session (mission) change reset.
"""
from concurrent import futures
import threading
import time
import unittest

try:
    import grpc
    from google.protobuf.json_format import ParseDict
    from dcs.common.v0 import common_pb2
    from dcs.custom.v0 import custom_pb2, custom_pb2_grpc
    from dcs.mission.v0 import mission_pb2, mission_pb2_grpc
    from dcs.world.v0 import world_pb2, world_pb2_grpc
    HAVE_GRPC = True
except ImportError:  # pragma: no cover
    HAVE_GRPC = False

from integrations.hermes_radio.eye import Eye, GrpcFeeder
from integrations.hermes_radio.tests import eye_fixture as fx


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


if HAVE_GRPC:
    class FakeDcs(mission_pb2_grpc.MissionServiceServicer, world_pb2_grpc.WorldServiceServicer,
                  custom_pb2_grpc.CustomServiceServicer):
        def __init__(self, units, events, session_id):
            self.units, self.events, self.session_id = units, events, session_id
            self.unit_requests = []

        def StreamUnits(self, request, context):
            self.unit_requests.append((request.category, request.poll_rate))
            name = common_pb2.GroupCategory.Name(request.category)
            for u in self.units:
                if name == "GROUP_CATEGORY_UNSPECIFIED" or u["group"]["category"] == name:
                    yield ParseDict(fx.units_message(u), mission_pb2.StreamUnitsResponse())
            while context.is_active():
                time.sleep(0.02)

        def StreamEvents(self, request, context):
            for e in self.events:
                yield ParseDict(e, mission_pb2.StreamEventsResponse())
            while context.is_active():
                time.sleep(0.02)

        def GetSessionId(self, request, context):
            return mission_pb2.GetSessionIdResponse(session_id=self.session_id)

        def GetAirbases(self, request, context):
            resp = world_pb2.GetAirbasesResponse()
            for ab in fx.AIRBASES:
                resp.airbases.append(ParseDict(ab, common_pb2.Airbase()))
            return resp

        def GetTheatre(self, request, context):
            return world_pb2.GetTheatreResponse(theatre="Caucasus")

        def GetMagneticDeclination(self, request, context):
            return custom_pb2.GetMagneticDeclinationResponse(declination=6.0)

        def Eval(self, request, context):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "eval disabled")

    def start_server(servicer, port=0):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
        mission_pb2_grpc.add_MissionServiceServicer_to_server(servicer, server)
        world_pb2_grpc.add_WorldServiceServicer_to_server(servicer, server)
        custom_pb2_grpc.add_CustomServiceServicer_to_server(servicer, server)
        bound = server.add_insecure_port("127.0.0.1:%d" % port)
        server.start()
        return server, bound


@unittest.skipUnless(HAVE_GRPC, "grpc / DCS-gRPC stubs not available")
class FeederTests(unittest.TestCase):
    def test_live_feed_picture_reconnect_and_mission_change(self):
        events = [fx.takeoff(fx.UNITS[1], fx.AIRBASES[1]), fx.shot(fx.PROMETHEUS, 5001, "AIM_120C")]
        first = FakeDcs(fx.UNITS, events, session_id=111)
        server, port = start_server(first)
        eye = Eye(source="fake")
        feeder = GrpcFeeder(eye, "127.0.0.1", port)
        feeder.start()
        try:
            self.assertTrue(wait_for(lambda: eye.ownship() is not None and len(eye.units) == len(fx.UNITS)
                                     and eye.airbases and eye.declination is not None and len(eye.events) == 2))
            pic = eye.picture()
            self.assertTrue(pic["connected"])
            self.assertEqual(pic["theatre"], "Caucasus")
            self.assertEqual(pic["ownship"]["type"], "F-16C_50")
            self.assertIsNone(pic["ownship"]["fuel_fraction"])  # eval disabled -> no fuel, no crash
            flankers = [g for g in pic["hostile_groups"] if "Su-27" in g["types"]][0]
            self.assertEqual(flankers["bearing_mag"], "054")
            self.assertEqual(flankers["clock"], "11 o'clock, ahead left")
            self.assertEqual(pic["airbases"]["nearest"][0]["name"], "Kutaisi")
            self.assertEqual(pic["my_shots_last_15_min"]["total"], 1)
            self.assertTrue(wait_for(lambda: not feeder.eval_ok, 15))
            cats = {c for c, _ in first.unit_requests}
            self.assertEqual(cats, {common_pb2.GROUP_CATEGORY_AIRPLANE, common_pb2.GROUP_CATEGORY_HELICOPTER,
                                    common_pb2.GROUP_CATEGORY_GROUND, common_pb2.GROUP_CATEGORY_SHIP})

            # Server goes away (mission ends / DCS quits), then a new mission starts on the same port.
            server.stop(0).wait()
            self.assertTrue(wait_for(lambda: not eye.connected, 5))
            second = FakeDcs([fx.PROMETHEUS, fx.UNITS[5]], [], session_id=222)
            server, _ = start_server(second, port)
            self.assertTrue(wait_for(lambda: eye.session_id == 222 and eye.connected
                                     and set(eye.units) == {100, 401}, 25))
            pic = eye.picture()
            self.assertEqual([list(g["types"]) for g in pic["hostile_groups"]], [["T-72B"]])
            self.assertEqual(pic["my_shots_last_15_min"]["total"], 0)  # old mission's shots cleared
        finally:
            feeder.close()
            server.stop(0)


if __name__ == "__main__":
    unittest.main()
