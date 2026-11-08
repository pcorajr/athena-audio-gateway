"""Offline end-to-end fixture replay through the actual local memory socket.

Executable sample: python -m integrations.hermes_radio.tests.test_memory_replay --sample
The fixture is synthetic and explicitly labelled; no DCS/SRS host is contacted.
"""
import asyncio
import gzip
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from integrations.hermes_radio.eye import Eye
from integrations.hermes_radio.memory_link import MemoryLink, request
from integrations.hermes_radio.tests import eye_fixture as fx
from metis.state_service.memory_runtime import MemoryRuntime
from metis.state_service.memory_source import DCSSource
from metis.state_service.mission_archive import MissionArchive
from metis.state_service.memory_debrief import write_report


async def replay(*, report_root=None):
    from google.protobuf.json_format import MessageToDict, ParseDict
    from dcs.mission.v0 import mission_pb2
    with tempfile.TemporaryDirectory(prefix="m6") as tmp:
        root = Path(tmp)
        runtime = MemoryRuntime(MissionArchive(root / "db.sqlite"), root / "s",
                                source=DCSSource("client", "fixture.invalid", 50051))
        runner = asyncio.create_task(runtime.run(offline=True))
        link = None
        try:
            await asyncio.wait_for(runtime.ready.wait(), 2)
            clock = [datetime(2026, 11, 7, 18, 30, tzinfo=timezone.utc).timestamp()]
            mission = await runtime.attach(dict(session_id=61107, filename="memory6-offline-fixture.miz",
                name="Offline-fixture-memory6", theatre="Caucasus", real_time=0., now=clock[0]))
            eye = Eye(clock=lambda: clock[0], source="fixture.invalid:50051")
            for unit in fx.UNITS:
                eye.on_unit(unit)
            eye.set_airbases(fx.AIRBASES)
            link = eye.memory = MemoryLink(path=root / "s")
            link.set_identity(61107, "memory6-offline-fixture.miz", "fixture.invalid:50051")
            events = [fx.takeoff(fx.PROMETHEUS, fx.AIRBASES[0]),
                      fx.shot(fx.PROMETHEUS, 5001, "AIM_120C"),
                      fx.hit(fx.PROMETHEUS, 5001, "AIM_120C", fx.UNITS[1]),
                      fx.kill(fx.PROMETHEUS, 5001, "AIM_120C", fx.UNITS[1])]
            for raw in events:
                wire = ParseDict(raw, mission_pb2.StreamEventsResponse())
                event = MessageToDict(wire, preserving_proto_field_name=True)
                await runtime.ingest(event, wire=wire.SerializeToString(), received_at=clock[0])
                eye.on_event(event)
                clock[0] += 1
            # Explicit fixture fault injection: a wrong Eye weapon interpretation.
            # It is an observation, not substituted raw DCS evidence.
            wrong = {"kind": "kill", "mission_time": 111., "initiator": {"name": fx.PROMETHEUS["name"]},
                     "target": {"name": fx.UNITS[1]["name"]}, "weapon": "fixture-wrong-weapon", "fixture_fault": True}
            link.publish("event", wrong, at=clock[0])
            link.capture_picture(eye.picture(), at=clock[0])
            link.publish("radio_call", {"request": "picture", "reply": "Fixture reply", "delivery": "generated"}, at=clock[0])
            async with asyncio.timeout(3):
                while True:
                    with link.lock:
                        empty = not link.queue
                    records = await runtime.archive.call("records", mission["mission_key"])
                    if empty and len(records["observations"]) == link.sent:
                        break
                    await asyncio.sleep(.01)
            start = time.perf_counter()
            health = await asyncio.to_thread(request, {"operation": "health"}, path=root / "s")
            latency = (time.perf_counter() - start) * 1000
            summary = await asyncio.to_thread(link.summary)
            assert summary["takeoff_airfield"] == "Kutaisi"
            assert any(a["verdict"] == "Eye wrong" for a in summary["recent_disagreements"])
            # Exercise the actual debrief socket and CLI without writing test reports into the vault.
            destination = report_root or root / "reports"
            real_writer = write_report
            with patch("metis.state_service.memory_debrief.write_report",
                       side_effect=lambda records: real_writer(records, root=destination)):
                receipt = await asyncio.to_thread(request, {"operation": "debrief", "mission": "last"}, path=root / "s", timeout=4)
                from integrations.hermes_radio.debrief import main
                assert await asyncio.to_thread(main, ["--mission", "last", "--socket", str(root / "s")]) == 0
            path = Path(receipt["path"])
            text = path.read_text()
            assert "Eye vs Memory audit" in text and "fixture-wrong-weapon" in text
            assert "| Eye wrong |" in text
            main, appendix = text.split("## Evidence sidecar")
            assert "13:30:00" in main and "America/New_York" in main
            assert "Red CAP 1-1 (Su-27)" in main and "F-16C_50" in main
            assert "{" not in main and "mission_key" not in main
            assert "evidence.jsonl.gz" in appendix
            with gzip.open(path.with_name("evidence.jsonl.gz"), "rt") as evidence:
                rows = [json.loads(line) for line in evidence]
            assert sum(row["record_type"] == "raw_events" for row in rows) == 4
            assert all(row["record"].get("digest") for row in rows if row["record_type"] == "raw_events")
            assert "fired AIM-120C; destroyed Su-27 at 13:30:03" in main
            assert "fired AIM-120C; hit" not in main
            assert "Captured duration: 00:00:04" in main
            return {"raw_events": len(records["raw_events"]), "observations": len(records["observations"]),
                    "dropped": link.dropped, "health_latency_ms": round(latency, 3),
                    "source": health["endpoint"], "connected": health["connected"], "report": str(path)}
        finally:
            if link:
                link.close()
            runtime.stop.set()
            await runner


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_replay_and_report(self):
        result = await replay()
        self.assertEqual(result["raw_events"], 4)
        self.assertGreater(result["observations"], 4)
        self.assertEqual(result["dropped"], 0)
        self.assertFalse(result["connected"])


if __name__ == "__main__":
    import sys
    if "--sample" in sys.argv:
        print(json.dumps(asyncio.run(replay(report_root=Path.home() / "athena-missions")), sort_keys=True))
    else:
        unittest.main()
