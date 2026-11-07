"""Real public producer -> local test socket -> unchanged client -> radio speech."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from metis.models import UnitData
from metis.state_service.domain_queries import DomainQueryDispatcher
from metis.state_service.generation_scope_port import StateQueryCut
from metis.state_service.health import HealthRegistry
from metis.state_service.lifecycle import HealthComponent, MissionRunState, UnitPresenceState
from metis.state_service.mission_identity import MissionSessionIdentity, mission_id_from_filename
from metis.state_service.mission_scope import MissionScope
from metis.state_service.models import GenerationUnit, GeoPosition
from metis.state_service.publisher import GenerationPublisher
from metis.state_service.restore import RestoreOutcome
from metis.state_service.socket_protocol import StateQueryRequest, encode_server_frame
from metis.state_service.units_stream import UnitStreamSnapshot
from metis.state_service.working_state import PublicationWindow

from integrations.hermes_radio.contract import MAX_SPEECH_CHARS, encode_response, parse_request
from integrations.hermes_radio.facts import CAPABILITIES, Facts
from integrations.hermes_radio.fixture_data import LIMITS, NOW, QUERY_LIMITS, STAMP, bridge_request, issue, wire
from integrations.hermes_radio.hermes_client import HermesClassifier, classify
from integrations.hermes_radio.server import Adapter
from integrations.hermes_radio.tactical import bearing_range, category
from integrations.hermes_radio.tests.support import fake_hermes, fake_state, model_reply
from integrations.hermes_radio.tests.test_server import running, post


def unit(identifier, unit_type="MiG-29S", *, coalition="red", lat: float = 0, lon: float = 1,
         alt: float | None = 3048, group: int | str | None = 10, **changes):
    value = GenerationUnit(identifier, "Synthetic unit", unit_type, coalition, group,
                           "Synthetic group", GeoPosition(lat, lon, alt), None, None,
                           UnitPresenceState.PRESENT, False, "units." + str(identifier))
    return replace(value, **changes)


def producer(units=(), *, alter=lambda value: value, limit=10):
    identity = MissionSessionIdentity(mission_id_from_filename("synthetic.miz"), "synthetic.miz",
                                      "Synthetic Mission", 1, STAMP)
    scope = MissionScope(identity, "synthetic-lineage", None, 0, None, RestoreOutcome.COLD_START)
    own = UnitData(unit_id=1, unit_name="Synthetic Ownship", player_name="Prometheus",
        lat=0.0, lon=0.0, alt=3048.0, heading=270.0, pitch=0.0, roll=0.0, coalition=3,
        unit_type="F-16C_50", velocity_x=100.0, velocity_y=0.0, velocity_z=0.0, speed=100.0,
        group_id=1, group_name="Ownship group", number_in_group=1, callsign="TEST01")
    snapshot = UnitStreamSnapshot(scope, identity, {1: own}, 1, 0.0, NOW, True)
    registry = HealthRegistry(NOW - timedelta(seconds=2))
    for component in HealthComponent:
        registry.mark_success(component, NOW - timedelta(seconds=1))
    generation = GenerationPublisher(clock=lambda: NOW, pilot_player_name="Prometheus").publish(
        PublicationWindow(scope, 1, 1, 1, 1), snapshot, component_health=registry.snapshot())
    generation = alter(replace(generation, units=dict(generation.units) | {str(u.unit_id): u for u in units},
                               freshness=replace(generation.freshness, max_age_ms=1000)))
    cut = StateQueryCut(generation, identity, scope.lineage_id, 0)

    class Owner:
        def capture_query_cut(self):
            return cut
        def query_cut_still_valid(self, value):
            return None

    dispatcher = DomainQueryDispatcher(owner=Owner(), limits=replace(QUERY_LIMITS, max_rows=limit), utc_clock=lambda: NOW)

    def respond(request):
        response = asyncio.run(dispatcher.dispatch(StateQueryRequest(request["api_version"], request["request_id"],
            request["operation"], request["params"]), None))
        return json.loads(encode_server_frame(response, limits=LIMITS))
    return respond


def exchange(text="picture", units=(), *, alter=lambda value: value,
             transform=lambda value: value, limit=10, classifier=None, http=False):
    produce = producer(units, alter=alter, limit=limit)
    def respond(connection, request):
        value = transform(produce(request))
        connection.sendall(value if type(value) is bytes else wire(value))
    with fake_state(respond, config_changes={"query_limits": {
            "max_rows": limit, "max_nested_type_rows": 10, "max_candidate_ids": 10, "work_quantum": 10}}) as (config, calls, closed):
        facts = Facts(config)
        fallback = classifier if classifier is not None else Mock()
        try:
            adapter = Adapter(fallback, facts)
            if http:
                with running(adapter) as server:
                    code, raw = post(server.server_port, wire(bridge_request(text)))
                    assert code == 200
                    result = json.loads(raw)
            else:
                result = adapter.exchange(parse_request(wire(bridge_request(text))))
            encode_response(result)
            if classifier is None:
                fallback.classify.assert_not_called()
        finally:
            facts.close()
    assert closed == [True] * len(calls)
    return result, calls


def target_only(change):
    def transform(frame):
        if frame["operation"] != "ownship":
            change(frame)
        return frame
    return transform


class TacticalTests(unittest.TestCase):
    def test_every_keyword_alias_and_overlap(self):
        groups = {"picture": ("picture",), "bogey_dope": ("bogey dope", "nearest threat", "nearest bandit"),
                  "threats": ("threats", "SAM"), "mission_status": ("mission status", "situation"),
                  "home_plate": ("home plate", "nearest airbase"), "help": ("help", "say again")}
        fallback = Mock()
        for intent, phrases in groups.items():
            for phrase in phrases:
                for wrapper in (phrase, "Athena, " + phrase + ".", "Please give me " + phrase + " please"):
                    with self.subTest(text=wrapper):
                        self.assertEqual(classify(wrapper, fallback), intent)
        fallback.classify.assert_not_called()
        self.assertEqual(classify("ownship status", fallback), "unrecognized")
        self.assertEqual(classify("mission status ownship", fallback), "unrecognized")

    def test_no_state_capabilities_exact_for_help_unknown_actions_and_compounds(self):
        for text in ("help", "say again", "attack nearest bandit", "bomb picture", "delete ownship",
                     "picture and threats", "picture; ownship", "mission status then recent events",
                     "watch the clouds", "watch the ownship", "tell me a joke about picture"):
            with self.subTest(text=text):
                classifier, facts = Mock(), Mock()
                result = Adapter(classifier, facts).exchange(parse_request(wire(bridge_request(text))))
                self.assertEqual(result["speech"], CAPABILITIES)
                self.assertEqual(result["intent_class"], "factual")
                self.assertEqual(result["state"], "ok")
                self.assertTrue(result["deliverable"])
                self.assertEqual(result["transmission_id"], "synthetic-transmission")
                encode_response(result)
                facts.query.assert_not_called()
                classifier.classify.assert_not_called()
        with fake_hermes(model_reply("unrecognized")) as (endpoint, calls):
            facts = Mock()
            result = Adapter(HermesClassifier(endpoint, "synthetic-key", timeout=1), facts).exchange(
                parse_request(wire(bridge_request("Something obscure"))))
            self.assertEqual(result["speech"], CAPABILITIES)
            self.assertEqual(len(calls), 1)
            facts.query.assert_not_called()

    def test_single_fallback_all_tactical_intents_through_real_http(self):
        for intent in ("picture", "bogey_dope", "threats", "mission_status", "home_plate"):
            with self.subTest(intent=intent), fake_hermes(model_reply(intent)) as (endpoint, model_calls):
                result, calls = exchange("Give me a check", [unit(2)], http=True,
                    classifier=HermesClassifier(endpoint, "synthetic-key", timeout=1))
                self.assertTrue(result["deliverable"])
                self.assertEqual(result["intent_class"], "factual")
                self.assertEqual(len(model_calls), 1)
                self.assertEqual(model_calls[0][2]["tools"], [])
                self.assertEqual(model_calls[0][2]["tool_choice"], "none")
                self.assertTrue(calls)

    def test_model_facts_tool_calls_extra_keys_never_become_speech(self):
        for raw in (b'{"intent":"picture","speech":"enemy at 123"}',
                    b'{"intent":"launch"}', b'Enemy at 123'):
            envelope = json.loads(model_reply("picture"))
            envelope["output"][0]["content"][0]["text"] = raw.decode()
            with fake_hermes(json.dumps(envelope).encode()) as (endpoint, calls):
                facts = Mock()
                result = Adapter(HermesClassifier(endpoint, "synthetic-key", timeout=1), facts).exchange(
                    parse_request(wire(bridge_request("Give me a check"))))
                self.assertEqual(result["speech"], CAPABILITIES)
                facts.query.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_picture_excludes_ground_neutral_friendly_and_gone(self):
        units = [unit(2, lon=1), unit(3, "T-72B", lon=0.01), unit(4, coalition="blue", lon=0.01),
                 unit(5, coalition="neutral", lon=0.01), unit(6, state=UnitPresenceState.GONE, lon=0.01)]
        result, calls = exchange(units=units)
        self.assertEqual(result["speech"], "Prometheus, Athena. 1 known hostile aircraft. Nearest known group, "
                         "closest aircraft BRAA 090 true, 60.0 nm, 10.0 thousand feet MSL.")
        self.assertEqual([c["operation"] for c in calls], ["ownship", "query_units"])
        self.assertEqual(calls[1]["params"], {"coalition": "red", "state": "present", "limit": 10})
        self.assertNotEqual(calls[0]["request_id"], calls[1]["request_id"])

    def test_red_ownship_reverses_coalitions_not_neutral(self):
        alter = lambda g: replace(g, ownship=replace(g.ownship, coalition="red"),
                                 units=dict(g.units) | {"1": replace(g.units["1"], coalition="red")})
        result, calls = exchange("bogey dope", [unit(2, coalition="blue", lon=-1), unit(3, coalition="neutral", lon=0.01)], alter=alter)
        self.assertIn("270 true, 60.0 nm", result["speech"])
        self.assertEqual(calls[1]["params"]["coalition"], "blue")

    def test_contradictory_ownship_cannot_be_reported_as_hostile(self):
        result, _ = exchange("bogey dope", [unit(2)],
            alter=lambda g: replace(g, ownship=replace(g.ownship, coalition="red")))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["speech"], "Athena not connected.")

    def test_stable_nearest_group_closest_member_and_ties(self):
        units = [unit(20, lon=1, group=2, alt=3048), unit(30, lon=-1, group=3, alt=6096),
                 unit(21, lon=10, group=2)]
        first, _ = exchange(units=units)
        second, _ = exchange(units=list(reversed(units)))
        self.assertEqual(first, second)
        self.assertIn("closest aircraft BRAA 090 true, 60.0 nm, 10.0 thousand", first["speech"])
        # Nearest aircraft selection remains deterministic when group IDs tie.
        result, _ = exchange("bogey dope", [unit(20, lon=-1), unit(10, lon=1)])
        self.assertIn("090 true", result["speech"])

    def test_group_name_fallback_and_missing_group_do_not_invent_group(self):
        result, _ = exchange(units=[unit(2, group=None, group_name="Known group")])
        self.assertIn("Nearest known group", result["speech"])
        result, _ = exchange(units=[unit(2, group=None, group_name=None)])
        self.assertIn("Group identity unavailable.", result["speech"])
        self.assertIn("Nearest known aircraft", result["speech"])

    def test_limited_page_is_not_total_or_globally_nearest(self):
        result, calls = exchange(units=[unit(2, lon=2), unit(3, lon=0.1)], limit=1)
        self.assertIn("At least 1 known hostile aircraft.", result["speech"])
        self.assertIn("Limited unit page.", result["speech"])
        self.assertIn("Nearest returned group", result["speech"])
        self.assertIn("120.1 nm", result["speech"])
        self.assertEqual(calls[1]["params"]["limit"], 1)

    def test_missing_positions_unknown_types_and_altitude_are_qualified(self):
        result, _ = exchange(units=[unit(2), unit(3, position=None), unit(4, "unknown aircraft", lon=0.01)])
        for phrase in ("At least 2 known hostile aircraft", "Unclassified types omitted.",
                       "Some positions unavailable.", "Nearest returned group"):
            self.assertIn(phrase, result["speech"])
        result, _ = exchange("bogey dope", [unit(2, alt=None)])
        self.assertIn("altitude unavailable", result["speech"])
        self.assertNotIn("feet", result["speech"])
        result, _ = exchange("bogey dope", [unit(2, position=None)])
        self.assertIn("target positions unavailable", result["speech"])
        self.assertNotIn("nm", result["speech"])
        result, _ = exchange(units=[unit(2)], alter=lambda g: replace(g, ownship=replace(g.ownship, position=None)))
        self.assertIn("ownship position unavailable", result["speech"])
        self.assertNotIn("nm", result["speech"])

    def test_true_bearings_range_altitude_and_degenerate_geometry(self):
        origin = {"lat": 0, "lon": 0}
        for position, expected in (({"lat": 1, "lon": 0}, 0), ({"lat": 0, "lon": 1}, 90),
                                   ({"lat": -1, "lon": 0}, 180), ({"lat": 0, "lon": -1}, 270)):
            bearing, distance = bearing_range(origin, position)
            self.assertAlmostEqual(bearing, expected)
            self.assertAlmostEqual(distance, 60.04046, places=4)
        bearing, distance = bearing_range({"lat": 0, "lon": 179.5}, {"lat": 0, "lon": -179.5})
        self.assertAlmostEqual(bearing, 90)
        self.assertAlmostEqual(distance, 60.04046, places=4)
        for lat, lon in ((0, 0), (0, 180)):
            result, _ = exchange("bogey dope", [unit(2, lat=lat, lon=lon)])
            self.assertIn("bearing unavailable", result["speech"])
            self.assertNotIn("000 true", result["speech"])

    def test_sam_uses_only_known_hostile_air_defence_types(self):
        units = [unit(2, "T-72B", lon=0.01), unit(3, "Hawk ln", lon=1),
                 unit(4, "Tor 9A331", coalition="blue", lon=0.01), unit(5, "Su-27", lon=0.01)]
        result, _ = exchange("SAM", units)
        self.assertIn("Nearest known air defence 090 true, 60.0 nm.", result["speech"])
        self.assertNotIn("feet", result["speech"])
        for units in ([], [unit(2, "T-72B", unit_name="SAM")], [unit(2, "not a Hawk ln")]):
            result, _ = exchange("threats", units)
            self.assertIn("No SAM data.", result["speech"])
            self.assertNotIn("nm", result["speech"])
        result, _ = exchange("threats", [unit(2, "Hawk ln", position=None)])
        self.assertIn("No SAM data with positions.", result["speech"])

    def test_mission_status_counts_types_not_whole_coalitions(self):
        units = [unit(2), unit(3, "T-72B"), unit(4, "Hawk ln"), unit(5, "T-72B", coalition="blue"),
                 unit(6, "Su-27", coalition="neutral")]
        result, calls = exchange("mission status", units)
        self.assertIn("Recorded type-based counts: Friendly 1 air, 1 ground; hostile 1 air, 2 ground.", result["speech"])
        self.assertEqual([c["operation"] for c in calls], ["ownship", "count_units", "count_units"])
        self.assertEqual([c["params"]["coalition"] for c in calls[1:]], ["blue", "red"])
        self.assertEqual(len({c["request_id"] for c in calls}), 3)

    def test_limited_type_rows_and_unknown_types_never_claim_exact_totals(self):
        result, _ = exchange("situation", [unit(2, "Su-27"), unit(3, "T-72B")], limit=1)
        self.assertIn("hostile at least 1 air, at least 0 ground", result["speech"])
        self.assertIn("Limited type counts.", result["speech"])
        result, _ = exchange("mission status", [unit(2, "Mystery")])
        self.assertIn("Unclassified types omitted.", result["speech"])
        self.assertIn("hostile at least 0 air, at least 0 ground", result["speech"])

    def test_home_plate_uses_pinned_identity_only_projection_no_invented_airfield(self):
        result, calls = exchange("home plate", [unit(2, "Airbase", coalition="blue", unit_name="Friendly airport")])
        self.assertEqual([c["operation"] for c in calls], ["mission_intel"])
        self.assertEqual(calls[0]["params"], {})
        self.assertIn("Unable, no friendly airfield data.", result["speech"])
        self.assertIn("Mission identity only.", result["speech"])
        self.assertNotIn("nm", result["speech"])

    def test_missing_or_ambiguous_ownship_never_produces_relative_facts(self):
        changes = [lambda own: None, lambda own: replace(own, player_name="Other"),
                   lambda own: replace(own, identity_confidence="low"),
                   lambda own: replace(own, identity_confidence="operator_selected_test")]
        for text in ("picture", "bogey dope", "mission status", "threats"):
            for change in changes:
                result, _ = exchange(text, [unit(2)], alter=lambda g: replace(g, ownship=change(g.ownship)))
                self.assertEqual(result["state"], "ownship_ambiguous")
                self.assertIn("Ownship not confirmed.", result["speech"])
                self.assertNotIn("nm", result["speech"])
        for side in (None, "neutral", "unknown"):
            result, calls = exchange(units=[unit(2)], alter=lambda g: replace(g, ownship=replace(g.ownship, coalition=side)))
            self.assertIn("ownship coalition unavailable", result["speech"])
            self.assertEqual(len(calls), 1)

    def test_stale_paused_ended_and_missing_provenance_caveats(self):
        def stale(g):
            return replace(g, freshness=replace(g.freshness, status="cached", stale_reason="mission_paused"),
                           mission=replace(g.mission, state=MissionRunState.PAUSED))
        result, _ = exchange("bogey dope", [unit(2)], alter=stale)
        for phrase in ("Cached state.", "Mission paused.", "State caveat: mission paused."):
            self.assertIn(phrase, result["speech"])
        self.assertEqual(result["state"], "stale")
        result, _ = exchange("bogey dope", [unit(2, stale=True)])
        self.assertIn("Stale unit data.", result["speech"])
        self.assertEqual(result["state"], "stale")
        result, _ = exchange("bogey dope", [unit(2)], alter=lambda g: replace(g,
            ownship=replace(g.ownship, provenance_key=None), mission=replace(g.mission, state=MissionRunState.ENDED)))
        self.assertIn("Ownship provenance unavailable.", result["speech"])
        self.assertIn("Mission ended.", result["speech"])

    def test_changed_generation_or_mission_scope_cannot_be_composed(self):
        def generation(frame):
            frame["context"]["state"]["generation"] = 2
            frame["evidence"]["generation"] = 2
            frame["evidence"]["provenance"]["generation"] = 2
        changes = [generation,
            lambda f: f["context"]["mission"].update(session_id=2),
            lambda f: (f["context"]["state"].update(lineage_id="other"), f["evidence"].update(lineage_id="other"))]
        for text in ("picture", "mission status"):
            for change in changes:
                result, _ = exchange(text, [unit(2)], transform=target_only(change))
                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(result["speech"], "Athena not connected.")

    def test_corrupt_projection_filters_numbers_and_evidence_fail_closed(self):
        changes = [lambda f: f.update(request_id="wrong"),
                   lambda f: f["evidence"].update(generation=99),
                   lambda f: f["evidence"].update(capabilities={}),
                   lambda f: f["evidence"].update(rows_matched=99),
                   lambda f: f["context"]["component_health"]["units_stream"].update(last_success_utc=None),
                   lambda f: f["context"]["state"]["freshness"].update(age_ms=999),
                   lambda f: f["data"]["rows"][0].update(coalition="blue"),
                   lambda f: f["data"]["rows"][0].update(state="gone"),
                   lambda f: f["data"]["rows"][0].update(category="aircraft"),
                   lambda f: f["data"]["rows"][0]["position"].update(lat=91),
                   lambda f: f["data"]["rows"][0]["position"].update(alt_m=float("nan")),
                   lambda f: f["data"]["rows"][0].update(provenance_key=""),
                   lambda f: f.update(warnings=[issue("cached_state") | {"severity": "error"}])]
        for change in changes:
            result, _ = exchange(units=[unit(2)], transform=target_only(change))
            self.assertEqual(result["state"], "unavailable")

    def test_unknown_caveat_suppresses_whole_never_clips(self):
        result, _ = exchange(units=[unit(2)], transform=target_only(lambda f: f.update(warnings=[issue("new_caveat")])))
        self.assertFalse(result["deliverable"])
        self.assertEqual(result["suppression_reason"], "unsupported_state_caveat")
        result, _ = exchange(units=[unit(2)], alter=lambda g: replace(g,
            freshness=replace(g.freshness, stale_reason="x" * MAX_SPEECH_CHARS)))
        self.assertFalse(result["deliverable"])
        self.assertEqual(result["speech"], "")
        self.assertEqual(result["suppression_reason"], "speech_budget_exceeded")

    def test_mission_intel_strict_null_contract_and_no_arbitrary_airbase_schema(self):
        changes = [lambda f: f["data"].update(airbases=[{"name": "invented", "coalition": "blue"}]),
                   lambda f: f["data"].update(session_id=True),
                   lambda f: f["data"].update(mission_name="Other"),
                   lambda f: f["data"].update(extra="not allowed"),
                   lambda f: f.update(warnings=[]),
                   lambda f: f["limits"].update(limit=1),
                   lambda f: f.update(request_id="wrong")]
        for change in changes:
            result, _ = exchange("home plate", transform=target_only(change))
            self.assertEqual(result["state"], "unavailable")
            self.assertIn("Unable", result["speech"])
            self.assertNotIn("nm", result["speech"])

    def test_negative_counts_duplicates_truncation_and_protocol_errors_rejected(self):
        for text, change in (("mission status", lambda f: f["data"].update(total=-1)),
                             ("mission status", lambda f: f["data"]["rows"][0].update(count=-1)),
                             ("picture", lambda f: f["data"]["rows"][1].update(unit_id=2)),
                             ("picture", lambda f: f["limits"].update(truncated=True))):
            result, _ = exchange(text, [unit(2), unit(3)], transform=target_only(change))
            self.assertEqual(result["state"], "unavailable")
        result, _ = exchange(units=[unit(2), unit(3)], limit=1,
                             transform=target_only(lambda f: f.update(warnings=[])))
        self.assertEqual(result["state"], "unavailable")
        for text in ("home plate", "picture", "threats", "mission status"):
            result, _ = exchange(text, [unit(2)], transform=lambda f: wire(f) + b"{}\n")
            self.assertEqual(result["state"], "unavailable")

    def test_prompt_hotwords_and_exact_type_vocabulary_safety(self):
        text = (Path(__file__).resolve().parents[1] / "whisper-prompt.txt").read_text()
        for hotword in ("Athena", "ownship", "picture", "bogey dope", "BRAA", "bandit", "home plate", "SAM", "recent events"):
            self.assertIn(hotword, text)
        for name in (None, "unknown", "truck aircraft", "F-16C_50 launcher", "CVN_71", "SAM bait"):
            self.assertEqual(category(name), "unknown")
        self.assertEqual(category("Su-27"), "air")
        self.assertEqual(category("Hawk ln"), "ground")
        self.assertEqual(category("T-72B"), "ground")
