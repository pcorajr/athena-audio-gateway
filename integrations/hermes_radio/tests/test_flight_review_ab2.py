"""Re-review AB2 of the 11-11 flight fixes (REVIEW-flight-AB2.md), from the reviewer's reproductions:
an attack-run acknowledgement from the model is never swapped for an airfield answer, ATC asks never reach the model
(so no clearance can be spoken), a model reply loses only its clearance sentences, friendlies-near-a-place asks
never get the air-traffic all-clear, a deferred answer is always said out loud, Athena never says "cleared hot", and
laser / tally / pickle calls are attack context, never a landing."""
from copy import deepcopy
import re
import unittest
from integrations.hermes_radio.simple_controller import ADDRESS, FALLBACK, MAX_REPLY_CHARS, call_class
from integrations.hermes_radio.tests.test_flight_20261111 import NEVADA, answer, live
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say

FAR = live("18:35:00")  # Creech 27 nm


def quiet(t):
    pic = live(t)
    pic.pop("rwr_alert_to_announce", None)
    return pic


class AttackAckTests(unittest.TestCase):
    """F1: 'on final to the target' + a model 'copy, proceed.' was replaced by 'Creech, bearing ..., No traffic.'"""
    ASKS = ("Athena, on final to the target", "Athena, commencing approach to the target area",
            "Athena, on approach to the bridge", "Athena, running in, final attack heading three six zero",
            "Athena, on final to the target, any SAMs?", "Athena, approaching the target, which way to the SAM?",
            "Athena, starting my run, any threats?", "Athena, commencing attack run, what's the wind?",
            "Athena, on final, where's the triple A?", "Athena, is the approach to the target clear?",
            "Athena, short final on the SAM, anything painting me?")
    REPLIES = ("Prometheus, Athena, copy, proceed.", "Prometheus, Athena, copy, you're good.",
               "Prometheus, Athena, roger, go ahead.", "Prometheus, Athena, copy, continue.",
               "Prometheus, Athena, no SAM within ten miles, proceed.",
               "Prometheus, Athena, go ahead, SA-2 at your twelve, eighteen miles.",
               "Prometheus, Athena, you're good, no threats.",
               "Prometheus, Athena, SAM SA-2 tracking you, your twelve o'clock, eighteen miles. Continue.")

    def test_a_model_acknowledgement_on_an_attack_run_is_spoken_unchanged(self):
        for pic in (NEVADA, FAR):
            for ask in self.ASKS:
                self.assertNotIn(call_class(ask, deepcopy(pic)), ("landing", "airfield", "atc"), ask)
                for rep in self.REPLIES:
                    with self.subTest(ask=ask, rep=rep):
                        spoken, _, stub, _, _ = answer(deepcopy(pic), ask, Stub(answer=rep))
                        self.assertNotRegex(spoken, r"Creech|Nellis|traffic")
                        if stub.calls:  # "running in, ..." is a fixed intent acknowledgement
                            # Names and unrelated numbers are deliberately not checked.
                            self.assertEqual(spoken, rep)


ATC_ASKS = ("Athena, which runway is in use?", "Athena, request taxi", "Athena, what's the active runway",
            "Athena, continue approach", "Athena, I'm landing", "Athena, landing at Creech now",
            "Athena, approach, Prometheus inbound", "Athena, landing", "Athena, request landing at Creech, which runway?",
            "Athena, request takeoff", "Athena, can I land", "Athena, altimeter please",
            "Athena, request landing, what's the wind?", "Athena, requesting permission in land.")
EVASIONS = (
    "you're clear to land.", "cleared to land runway two one.", "cleared for landing.", "cleared for the option.",
    "land runway two seven, no traffic.", "runway two seven, wind calm.", "use runway three two left.",
    "the active is runway two seven.", "the active is two seven.", "landing runway is two seven.", "the pattern is yours.",
    "pattern is yours, no traffic.", "continue approach.", "continue, no traffic.", "report final.", "report three mile final.",
    "line up and wait.", "line up runway two seven.", "taxi to runway two seven.", "hold short runway two seven.",
    "wind two seven zero at five.", "wind is calm.", "winds are two seven zero at ten knots.", "altimeter two niner niner two.",
    "QNH one zero one three.", "squawk one two zero zero.", "you're number one for landing.", "number two, follow the A-10.",
    "approved as requested.", "request approved.", "approach is approved.", "affirmative, land at your discretion.",
    "affirm, you can land.", "go ahead and land.", "you're clear to come in.", "come on in, no traffic.",
    "you are cleared in.", "cleared in, no traffic.", "runway is yours.", "field is yours.", "it's all yours, land.",
    "land at your discretion.", "landing is at your discretion.", "clear to touch down.", "permission granted.",
    "permission to land granted.", "granted, no traffic.", "roger, landing approved.", "cleared straight in runway two seven.",
    "cleared I L S runway eight.", "enter left downwind runway two seven.", "make right traffic.", "cleared touch and go.",
    "you're cleared to depart.", "takeoff at your discretion.", "cleared for immediate takeoff.", "the runway is clear, land.",
    "runway two-seven, cleared.", "runway twenty seven in use.", "landing two seven.", "land two seven.",
    "use two seven.", "expect runway two seven.", "expect the ILS two seven.", "contact tower on two five one.",
    "switch tower.", "you're good to come in.", "clear to the field.", "cleared to the field, no traffic.",
    "cleared approach.", "you may proceed with landing.", "affirmative.", "yes, you can land.", "sure, land.",
    "Creech, no aircraft traffic, your discretion.")  # the last one: the model study (results.jsonl)
# Harmless on a non-ATC ask ("radio check"); on an ATC ask the model is never called.
NON_ATC_OK = {"affirmative.", "number two, follow the A-10.", "continue, no traffic."}
LEGIT = ("you are clear of the SA-2.", "you're clear to the north, no threats.", "clear to the north.",
         "clear low level to the west.", "no traffic.", "SAM SA-2 tracking you, your twelve o'clock, eighteen miles.",
         "RWR is clean, nothing painting you.", "fuel 3,640 pounds.", "the area is clear of bandits.",
         "clear of threats, egress west.", "you're clear, break left.", "approach the target from the south.",
         "SA-2 site is down, approach from the north is clear.", "no friendly airfield showing.", "loud and clear.",
         "copy, good luck.", "proceed to the IP.", "the pattern is clear of bandits.", "wind unknown, I have no weather.",
         "I have no runway information.", "I don't have the active runway.", "copy, proceed.", "roger, go ahead.")


class AtcTests(unittest.TestCase):
    """F2: 29 of 74 clearance evasions leaked; an ATC ask now never calls the model."""
    def test_atc_asks_never_call_the_model_and_get_field_and_traffic(self):
        pic = quiet("19:01:46")
        for ask in ATC_ASKS:
            for ev in EVASIONS[:5] + EVASIONS[-3:]:
                with self.subTest(ask=ask, ev=ev):
                    stub = Stub(answer=ADDRESS + ev)
                    spoken, reply, _, _, _ = answer(deepcopy(pic), ask, stub)
                    self.assertEqual(stub.calls, 0)
                    self.assertEqual(reply["route"], "fixed")
                    self.assertTrue(spoken.startswith(ADDRESS + "Creech, bearing zero six nine, five miles"), spoken)
                    self.assertIn("No traffic.", spoken)
                    info = bool(re.search(r"runway|wind|altimeter", ask, re.I))
                    self.assertEqual(spoken.endswith("I have no runway, wind or altimeter data."), info, spoken)
                    self.assertNotRegex(spoken.lower(), r"clear|approv|discretion|granted|yours")

    def test_landing_gear_and_lights_are_no_atc_ask(self):
        for text in ("Athena, landing gear damaged", "Athena, I think my landing gear is damaged", "Athena, landing light on"):
            self.assertNotEqual(call_class(text, deepcopy(NEVADA)), "atc", text)

    def test_every_evasion_is_dropped_from_a_non_atc_reply_and_never_replaced_by_a_field(self):
        pic = quiet("19:01:46")
        blocked = 0
        for ev in EVASIONS:
            with self.subTest(ev=ev):
                spoken = answer(deepcopy(pic), "Athena, radio check", Stub(answer=ADDRESS + ev))[0]
                self.assertNotIn("Creech, bearing", spoken)
                if ev not in NON_ATC_OK:
                    self.assertEqual(spoken, FALLBACK)
                blocked += spoken == FALLBACK
        self.assertGreaterEqual(blocked, len(EVASIONS) - len(NON_ATC_OK))
        # a grounded threat sentence survives; only the clearance sentence goes
        pic["threats"] = {"sam_in_range": [{"call": "SAM SA-2 tracking you, your twelve o'clock."}]}
        rep = ADDRESS + "SAM SA-2 tracking you, your twelve o'clock. The active is two seven."
        self.assertEqual(answer(deepcopy(pic), "Athena, any threats?", Stub(answer=rep))[0],
                         ADDRESS + "SAM SA-2 tracking you, your twelve o'clock.")

    def test_legitimate_replies_pass_unchanged(self):
        pic = quiet("19:01:46")
        for text in LEGIT:
            for ask in ("Athena, radio check", "Athena, any threats near Creech?", "Athena, on final to the target, any SAMs?"):
                with self.subTest(text=text, ask=ask):
                    # Model facts are not checked; existing clearance/key guards remain.
                    expected = ADDRESS + text
                    self.assertEqual(answer(deepcopy(pic), ask, Stub(answer=ADDRESS + text))[0], expected)

    def test_athena_never_clears_weapons(self):
        pic = quiet("19:01:46")
        for text in ("cleared hot.", "you're cleared hot.", "clear to engage.", "you're cleared to engage.",
                     "weapons free.", "cleared hot, no friendlies."):
            with self.subTest(text=text):
                self.assertEqual(answer(deepcopy(pic), "Athena, in hot, am I clear?", Stub(answer=ADDRESS + text))[0],
                                 FALLBACK)


FRIENDLY_GROUND = {"side": "friendly", "domain": "ground", "range_nm": 1.2, "category": "armor",
                   "call": "Friendly M1A2, two seven zero, one mile, ground target, nine o'clock."}
FRIENDLY_FAR = {"side": "friendly", "domain": "ground", "range_nm": 22.0, "category": "armor",
                "call": "Friendly M1A2, zero nine zero, twenty two, ground target, three o'clock."}
NEAR_ASKS = ("any friendlies near the target", "any friendlies near the convoy", "any friendlies near my aim point",
             "any friendlies near the T-55s", "any friendlies by the bridge", "any friendlies near where I'm shooting",
             "any friendlies in the area", "any friendlies close to the target", "any friendlies near the SAM",
             "any friendlies nearby", "any friendlies around", "any friendlies down there", "friendlies in the vicinity",
             "any friendly troops near the target", "is the target area clear of friendlies",
             "any friendly aircraft near the target", "are there any friendlies near the tanks", "any blue near the target",
             "any friendlies close by", "any friendlies close to my target", "friendlies near the armor?",
             "any friendlies within a mile of the target", "any friendly units near the triple A",
             "any friendlies at the target", "friendlies clear of the target?", "any friendlies next to the trucks")
AIR_ASKS = ("any friendlies in the air", "friendly air traffic", "any friendlies airborne",
            "where are the friendly aircraft", "Athena, are there any friendlies in the air?")


class FriendlyNearTests(unittest.TestCase):
    """F3: 'any friendlies near the convoy' got 'No blue aircraft showing within one hundred fifty miles.'"""
    def test_friendlies_near_a_place_never_get_the_traffic_answer(self):
        for groups in ([], [FRIENDLY_FAR], [FRIENDLY_GROUND, FRIENDLY_FAR], None):
            pic = deepcopy(NEVADA)
            if groups is not None:
                pic["friendly_groups"] = groups
            for ask in NEAR_ASKS:
                with self.subTest(ask=ask, groups=groups):
                    self.assertNotEqual(call_class("Athena, " + ask, deepcopy(pic)), "traffic")
                    spoken, reply, stub, _, _ = answer(deepcopy(pic), "Athena, " + ask)
                    self.assertNotRegex(spoken, r"aircraft showing|No (blue|friendly) aircraft")
                    if groups is None:
                        self.assertEqual(stub.calls, 1)  # no friendly data: the model answers
                        continue
                    self.assertEqual(stub.calls, 0)
                    self.assertTrue(spoken.startswith(ADDRESS + "I can't see your target."), spoken)
                    if FRIENDLY_GROUND in groups:
                        self.assertIn(FRIENDLY_GROUND["call"], spoken)
                    else:
                        self.assertIn("No friendly units within five miles of you.", spoken)
                    self.assertNotIn(FRIENDLY_FAR["call"], spoken)

    def test_many_friendlies_close_keep_the_caveat_first(self):
        pic = deepcopy(NEVADA)
        pic["friendly_groups"] = [dict(FRIENDLY_GROUND, range_nm=1.0 + i / 2) for i in range(6)]
        spoken = answer(pic, "Athena, any friendlies near the convoy")[0]
        self.assertTrue(spoken.startswith(ADDRESS + "I can't see your target. " + FRIENDLY_GROUND["call"]), spoken)
        self.assertTrue(spoken.endswith("Plus three more."), spoken)
        self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)

    def test_plain_air_asks_keep_the_traffic_answer(self):
        for ask in AIR_ASKS:
            with self.subTest(ask=ask):
                self.assertEqual(call_class(ask, deepcopy(NEVADA)), "traffic")


class DeferralTests(unittest.TestCase):
    """F4: 3 alerts + a close bandit left no room for 'Ask again for ...': the answer vanished silently."""
    BANDIT = {"key": "cb1", "call": "Bandit MiG-29, two seven zero, eight miles, angels ten, nine o'clock level."}
    SAM = "SAM SA-2 tracking you, your eleven o'clock, nineteen miles."
    L = ["Missile launch, SA-2, your three o'clock, thirteen miles.", "Missile launch, SA-2, your three o'clock, fifteen miles.",
         "Missile launch, SA-6, your two o'clock, nine miles.", "Missile launch, SA-6, your one o'clock, eight miles.",
         "Missile launch, SA-11, your twelve o'clock, seven miles."]

    def test_the_requested_answer_or_its_deferral_is_always_spoken(self):
        wants = {"Athena, fuel check": "Fuel 3,640 pounds.", "Athena, request landing at Creech": "Creech, bearing",
                 "Athena, confirm kills": "T-55", "Athena, vector to nearest air base": "Creech, bearing"}
        for alerts in ([self.SAM], self.L[:2] + [self.SAM], self.L[:3], self.L[:4], self.L[:5]):
            for bandit in (False, True):
                for ask, want in wants.items():
                    with self.subTest(n=len(alerts), bandit=bandit, ask=ask):
                        p = live("18:59:23")
                        p["rwr_alert_to_announce"] = list(alerts)
                        p["_rwr_keys"] = ["k%d" % i for i in range(len(alerts))]
                        if bandit:
                            p["_close_bandits"] = [dict(self.BANDIT)]
                        spoken = answer(p, ask)[0]
                        self.assertLessEqual(len(spoken), MAX_REPLY_CHARS)
                        self.assertIn(self.L[0] if alerts[0] == self.L[0] else self.SAM, spoken)  # the first threat
                        self.assertTrue(want in spoken or "Ask again for" in spoken, spoken)


class AttackWordTests(unittest.TestCase):
    """F5: 'starting approach, laser on' within 15 nm of Creech, or after 'the bomb landed short', got a landing line."""
    WORDS = ("starting approach, laser on", "on final, lasing", "starting approach, tally target", "on approach, spot",
             "starting approach, wings level", "on final, in hot", "on final, off target", "on final, bombs away",
             "on final, pickle", "short final, pickle pickle", "on final, shooting", "on approach, release",
             "on approach, mark")

    def test_attack_words_are_never_a_landing(self):
        for pic, hist in ((NEVADA, []), (FAR, []), (FAR, ["Athena, the bomb landed short"]),
                          (FAR, ["Athena, any SAMs near the airfield?"]), (FAR, ["Athena, did my bombs land on target?"])):
            for text in self.WORDS:
                with self.subTest(text=text, hist=hist):
                    self.assertNotIn(call_class("Athena, " + text, deepcopy(pic), hist), ("landing", "airfield", "atc"))

    def test_a_bomb_landing_is_no_landing_context(self):
        for prev in ("Athena, the bomb landed short", "Athena, any SAMs near the airfield?",
                     "Athena, did my bombs land on target?"):
            with self.subTest(prev=prev):
                c, _ = controller(deepcopy(FAR), Stub(answer="Prometheus, Athena, copy."))
                say(c, prev, tid="h1")
                spoken = say(c, "Athena, starting approach", tid="h2")[0][0]["speech"]
                self.assertNotRegex(spoken, r"Creech|traffic")
