"""Phase 1 item 2 (#240): one deadline per exchange, honest route/model/latency labels, calls marked said only
when the gateway transmitted them, controller memory reset on a new mission, and two small review bugs
(R1 F5 'any hits?' read the kill twice; R1 F10 a pilot report confirmed a kill with no DCS evidence)."""
from contextlib import contextmanager
from copy import deepcopy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.codex_llm import CodexLLM, HermesCodexCredentials
from integrations.hermes_radio.contract import ContractError, encode_response, response
from integrations.hermes_radio.simple_controller import (EXCHANGE_DEADLINE_S, FALLBACK, MAX_REPLY_CHARS,
                                                         SimpleController)
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye_round4 import A10, FakeTranscript, a10_eye, req
from integrations.hermes_radio.transcript import Transcript

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = json.loads((FIXTURES / "r9_live_20261109.json").read_text())
ANY_HITS = json.loads((FIXTURES / "p1_any_hits_20261109.json").read_text())["any_hits_2009"]
FIXED_ASKS = ("Athena, mission ended.", "Athena, ground picture", "Athena, tally.", "Athena, confirm kills",
              "Athena, what do I have left", "Athena, launch, launch", "Athena, picture", "Athena, running in",
              "Athena, confirm kills, what remains", "Athena, any hits?")
MODEL_ASKS = ("Athena, how is it looking", "Athena, radio check", "Athena, what's the nearest SAM")


class Memory:
    generation = 1

    def schedule_debrief(self):
        return True

    def publish(self, *a, **k):
        pass

    def summary(self):
        return None


class Eye:
    """The Eye's controller-facing surface: picture, announced set under a lock, reset time, session."""

    def __init__(self, pic):
        self.pic, self.memory, self.lock = deepcopy(pic), Memory(), threading.RLock()
        self.announced, self.reset_at, self.session_id = set(), 1.0, "s1"

    def picture(self):
        pic = deepcopy(self.pic)
        for field, keys in (("splash_to_announce", "_splash_keys"), ("rwr_alert_to_announce", "_rwr_keys")):
            pending = [(a, k) for a, k in zip(pic.get(field) or [], pic.get(keys) or []) if k not in self.announced]
            if field in pic:
                pic[field], pic[keys] = [a for a, _ in pending], [k for _, k in pending]
        return pic

    def mark_announced(self, keys):
        with self.lock:
            self.announced.update(keys)

    def unmark_announced(self, keys):
        with self.lock:
            self.announced.difference_update(keys)


class Stub:
    """Model stubs for an outage: ok, sleep past the deadline, empty answer, raise."""

    def __init__(self, mode="ok", answer="Prometheus, Athena, copy."):
        self.mode, self.answer_text, self.calls = mode, answer, 0

    def __call__(self, messages):
        self.calls += 1
        if self.mode == "sleep":
            time.sleep(30)
        if self.mode == "raise":
            raise RuntimeError("model down")
        return "" if self.mode == "empty" else self.answer_text


@contextmanager
def deadline(seconds):
    """A short exchange deadline so outage tests run fast (the logic is the same at 10 s)."""
    with mock.patch.object(sc, "EXCHANGE_DEADLINE_S", seconds), mock.patch.object(sc, "MIN_MODEL_S", seconds / 5):
        yield


def controller(pic, llm=None, transcript=None, eye=None):
    eye = eye or Eye(pic)
    return SimpleController(None, transcript or FakeTranscript(), model="gpt-5.4-mini", api_key="unused",
                            eye=eye, llm=llm), eye


def say(c, text, tid="t1"):
    t0 = time.monotonic()
    frames = list(c.stream(req(text, tid=tid)))
    return frames, time.monotonic() - t0


def replies(c):
    return [r for r in c.transcript.records if r["kind"] == "reply_generated"]


class DeadlineTests(unittest.TestCase):
    def test_deadline_is_well_inside_the_gateway_bridge_timeout(self):
        self.assertLessEqual(EXCHANGE_DEADLINE_S, 12.0)  # gateway athena-hermes-timeout is 20 s
        self.assertGreater(EXCHANGE_DEADLINE_S, 6.0 + 2.0)  # Codex wall clock + an API answer (max seen 1.9 s)

    def test_outage_always_answers_inside_the_deadline_with_say_again(self):
        with deadline(0.5):
            for mode in ("sleep", "empty", "raise"):
                for name, pic in LIVE.items():
                    with self.subTest(mode=mode, picture=name):
                        c, _ = controller(pic, Stub(mode))
                        frames, took = say(c, "Athena, how is it looking")
                        self.assertLess(took, 0.8)
                        self.assertEqual(len(frames), 1)
                        self.assertTrue(frames[0]["speech"].endswith(FALLBACK.split("Athena, ", 1)[1]), frames[0])
                        self.assertLessEqual(len(frames[0]["speech"]), MAX_REPLY_CHARS)
                        self.assertEqual(frames[0]["route"], "fallback")
                        self.assertNotIn("model", frames[0])
                        rec = replies(c)[0]
                        self.assertEqual(rec["route"], "fallback")
                        self.assertTrue(rec["llm"]["fallback_reason"])
                        self.assertEqual(c.history, type(c.history)())  # a say again is never chat history

    def test_stalled_codex_falls_back_to_the_api_model_inside_the_deadline(self):
        class Creds:
            def current(self):
                return {"token": "t", "base_url": "https://codex.example", "headers": {}}

            def refresh(self, force=False):
                pass

        def stalled(url, headers, body, until):  # honours its wall-clock limit like _http_post
            time.sleep(max(0.0, until - time.monotonic()))
            raise TimeoutError("codex_deadline")
        api = []

        def fallback(messages, deadline=None):
            api.append(deadline)
            return "Prometheus, Athena, loud and clear."
        llm = CodexLLM(fallback, timeout=1.2, credentials=Creds(), post=stalled, fallback_reserve=1.0,
                       fallback_label="openai-api:gpt-5.4-mini")
        with deadline(3.0):
            c, _ = controller(LIVE["loadout_2035"], llm)
            frames, took = say(c, "Athena, radio check")
        self.assertLess(took, 2.0)
        self.assertEqual(frames[0]["speech"], "Prometheus, Athena, loud and clear.")
        self.assertEqual((frames[0]["route"], frames[0]["model"]), ("llm", "gpt-5.4-mini"))
        rec = replies(c)[0]
        self.assertEqual((rec["route"], rec["model"], rec["provider"]), ("llm", "gpt-5.4-mini", "openai-api"))
        self.assertIn("codex_deadline", rec["llm"]["fallback_reason"])
        self.assertTrue(api[0] is not None)  # the fallback gets the exchange deadline, not its own 12 s

    def test_stalled_codex_and_stalled_fallback_still_say_again(self):
        class Creds:
            def current(self):
                return {"token": "t", "base_url": "https://codex.example", "headers": {}}

            def refresh(self, force=False):
                pass
        llm = CodexLLM(lambda m: time.sleep(30), timeout=1.0, credentials=Creds(),
                       post=lambda *a: time.sleep(30))
        with deadline(1.5):
            c, _ = controller(LIVE["loadout_2035"], llm)
            frames, took = say(c, "Athena, radio check")
        self.assertLess(took, 2.0)
        self.assertEqual(frames[0]["speech"], FALLBACK)
        self.assertEqual(replies(c)[0]["llm"]["fallback_reason"], "exchange_deadline")

    def test_request_too_old_gets_no_model_call(self):
        stub = Stub()
        c, _ = controller(LIVE["loadout_2035"], stub)
        old = time.monotonic() - (EXCHANGE_DEADLINE_S - 1.0)  # waited 9 s behind another exchange
        frames = list(c._answer(req("Athena, how is it looking"), "Athena, how is it looking", old))
        self.assertEqual((frames[0]["speech"], frames[0]["route"], stub.calls), (FALLBACK, "fallback", 0))
        self.assertTrue(any(r.get("outcome") == "request_too_old_no_model_call" for r in c.transcript.records))
        # a fixed answer is instant and still useful
        frames = list(c._answer(req("Athena, running in", tid="t2"), "Athena, running in", old))
        self.assertEqual(frames[0]["speech"], "Prometheus, Athena, copy, running in.")

    def test_api_backend_bounds_its_reads_by_the_deadline(self):
        seen = {}

        class Conn:
            def __init__(self, host, timeout):
                seen["timeout"] = timeout
                self.sock = mock.Mock()

            def request(self, *a, **k):
                pass

            def getresponse(self):
                seen["read_timeout"] = self.sock.settimeout.call_args[0][0]
                return mock.Mock(status=200, read=lambda: b'{"choices":[{"message":{"content":" ok "}}]}')

            def close(self):
                pass
        c, _ = controller(LIVE["loadout_2035"])
        with mock.patch.object(http.client, "HTTPSConnection", Conn):
            self.assertEqual(c.ask([], deadline=time.monotonic() + 3.0), "ok")
        self.assertTrue(0 < seen["timeout"] <= 3.0 and 0 < seen["read_timeout"] <= 3.0, seen)
        with self.assertRaises(TimeoutError):
            c.ask([], deadline=time.monotonic() - 0.1)


class CredentialTests(unittest.TestCase):
    TOKEN = "h." + __import__("base64").urlsafe_b64encode(json.dumps({"exp": 2000}).encode()).decode().rstrip("=") + ".s"

    def test_a_call_never_waits_for_the_credential_helper(self):
        started = threading.Event()

        def slow(force):
            started.set()
            time.sleep(0.5)
            return {"token": self.TOKEN, "base_url": "b", "headers": {}}
        now = [1000.0]
        creds = HermesCodexCredentials(runner=slow, clock=lambda: now[0])
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError):
            creds.current()  # nothing cached: refresh starts in the background, this call falls back
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertTrue(started.wait(1))
        deadline_ = time.monotonic() + 2
        while time.monotonic() < deadline_:
            try:
                self.assertEqual(creds.current()["token"], self.TOKEN)
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            self.fail("background refresh never landed")

    def test_refresh_ahead_of_expiry_in_the_background(self):
        runs = []

        def runner(force):
            runs.append(force)
            return {"token": self.TOKEN, "base_url": "b", "headers": {}}
        now = [1000.0]
        creds = HermesCodexCredentials(runner=runner, clock=lambda: now[0])
        creds.get()
        now[0] = 2000 - 200  # inside the last 5 min of the JWT: still usable, refreshed (forced) ahead
        self.assertEqual(creds.current()["token"], self.TOKEN)
        for _ in range(100):
            if len(runs) == 2:
                break
            time.sleep(0.01)
        self.assertEqual(runs, [False, True])
        now[0] = 2000 - 30  # past the usable window: no token for this call
        with self.assertRaises(RuntimeError):
            creds.current()


class LabelTests(unittest.TestCase):
    def test_fixed_routes_make_no_network_call(self):
        attempts = []

        def no_network(*a, **k):
            attempts.append(a)
            raise OSError("network disabled in this test")
        fixed = 0
        with mock.patch.object(http.client, "HTTPSConnection", no_network):
            for name, pic in list(LIVE.items()) + [("any_hits", ANY_HITS)]:
                for text in FIXED_ASKS:
                    del attempts[:]
                    c, _ = controller(pic)  # default backend: the real API client
                    frames, _ = say(c, text)
                    rec = replies(c)[0]
                    with self.subTest(picture=name, ask=text, route=rec["route"]):
                        self.assertEqual(frames[0]["route"], rec["route"])
                        self.assertLessEqual(len(frames[0]["speech"]), MAX_REPLY_CHARS)
                        if rec["route"] in ("fixed", "debrief"):
                            fixed += 1
                            self.assertEqual(attempts, [])
                            self.assertEqual(rec["timings_ms"]["llm"], 0)
                            self.assertIsNone(rec["model"])
                        else:  # the API was tried (network off): an honest say again, never silence
                            self.assertEqual((rec["route"], frames[0]["speech"]), ("fallback", FALLBACK))
        self.assertGreaterEqual(fixed, 40)  # 43 of 70 on these trimmed live pictures

    def test_model_reply_labels_this_calls_backend_never_the_previous_one(self):
        class Backend:
            label, model = "openai-codex:gpt-6-luna/low", "gpt-6-luna"

            def __init__(self):
                self.metas = [{"backend": "openai-api", "model": "gpt-5.4-mini", "fallback_reason": "x"},
                              {"backend": "openai-codex", "model": "gpt-6-luna", "effort": "low"}]

            def answer_by(self, messages, deadline):
                return "Prometheus, Athena, loud and clear.", self.metas.pop(0)
        c, _ = controller(LIVE["loadout_2035"], Backend())
        first, _ = say(c, "Athena, radio check", "a")
        second, _ = say(c, "Athena, radio check", "b")
        recs = replies(c)
        self.assertEqual([r["model"] for r in recs], ["gpt-5.4-mini", "gpt-6-luna"])
        self.assertEqual([f[0]["model"] for f in (first, second)], ["gpt-5.4-mini", "gpt-6-luna"])
        self.assertEqual([r["llm"]["backend"] for r in recs], ["openai-api", "openai-codex"])
        for r in recs:
            self.assertEqual(r["route"], "llm")
            self.assertIn("total", r["timings_ms"])
        tools = [r for r in c.transcript.records if r["kind"] == "tool"]
        self.assertTrue(all("llm" not in r and "model" not in r for r in tools))  # R1 F11
        ingress = [r for r in c.transcript.records if r["kind"] == "adapter_ingress"]
        self.assertTrue(all("route" not in r for r in ingress))

    def test_frames_with_labels_pass_the_wire_contract_and_old_frames_stay_valid(self):
        r = req("x")
        for frame in (response(r, speech="Prometheus, Athena, copy.", state="ok"),
                      response(r, speech="Prometheus, Athena, copy.", state="ok", route="fixed"),
                      response(r, speech="Prometheus, Athena, copy.", state="ok", route="llm", model="gpt-6-luna"),
                      response(r, reason="unaddressed_or_noncommand", route="llm")):
            encode_response(frame)
        self.assertNotIn("route", response(r, reason="x", route="llm"))
        for bad in ({"route": "guess"}, {"model": "gpt"}, {"route": "llm", "model": ""},
                    {"route": "llm", "model": "m" * 65}):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                encode_response(dict(response(r, speech="Prometheus, Athena, copy.", state="ok"), **bad))

    def test_transcript_records_carry_a_route_only_when_given(self):
        with tempfile.TemporaryDirectory() as root:
            t = Transcript(Path(root) / "radio")
            t.log(kind="adapter_ingress", request=req("x"), text="x")
            t.log(kind="reply_generated", request=req("x"), text="y", route="fixed")
            rows = [json.loads(line) for line in next((Path(root) / "radio").glob("*.jsonl")).read_text().splitlines()]
        self.assertNotIn("route", rows[0])
        self.assertEqual(rows[1]["route"], "fixed")


def gateway_outcome(transcript, tid, outcome):
    """What the Go gateway appends to the shared transcript after speaking (or not speaking) a reply."""
    transcript.append({"kind": "exchange_outcome", "transmission_id": tid, "outcome": outcome, "route": "fixed"})


class DeliveryTests(unittest.TestCase):
    PIC = dict(LIVE["loadout_2035"], splash_to_announce=["Splash one Ural-375"], _splash_keys=["k-splash"],
               rwr_alert_to_announce=["Spike, SA-2 at your six o'clock."], _rwr_keys=["k-rwr"])

    def run_two(self, outcome, first="Athena, running in"):
        with tempfile.TemporaryDirectory() as root:
            transcript = Transcript(Path(root) / "radio")
            c, eye = controller(self.PIC, Stub(), transcript=transcript)
            frames, _ = say(c, first, "tx-1")
            said_first = set(eye.announced)
            if outcome:
                gateway_outcome(transcript, "tx-1", outcome)
            again, _ = say(c, first, "tx-2")
            return frames[0]["speech"], said_first, again[0]["speech"], eye, c

    def test_outcome_of_reads_the_gateway_record(self):
        with tempfile.TemporaryDirectory() as root:
            t = Transcript(Path(root) / "radio")
            self.assertIsNone(t.outcome_of("tx-1"))
            t.log(kind="reply_generated", request=req("x", tid="tx-1"), text="y", route="fixed")
            self.assertIsNone(t.outcome_of("tx-1"))
            gateway_outcome(t, "tx-1", "say_again_fallback_sent:bridge_failed")
            gateway_outcome(t, "tx-2", "sent")
            self.assertEqual(t.outcome_of("tx-1"), "say_again_fallback_sent:bridge_failed")
            self.assertEqual(t.outcome_of("tx-2"), "sent")

    def test_calls_in_a_reply_the_gateway_did_not_send_are_said_again(self):
        for outcome in ("say_again_fallback_sent:bridge_failed", "too_long_fallback_sent:speech_duration_exceeded",
                        "expired"):
            with self.subTest(outcome=outcome):
                first, said, again, eye, c = self.run_two(outcome)
                self.assertIn("Splash one Ural-375", first)
                self.assertEqual(said, {"k-splash", "k-rwr"})
                self.assertIn("Splash one Ural-375", again)  # re-armed: the pilot never heard it
                self.assertIn("Spike, SA-2", again)

    def test_sent_or_unknown_outcome_keeps_them_said(self):
        for outcome in ("sent", None):
            with self.subTest(outcome=outcome):
                _, _, again, _, _ = self.run_two(outcome)
                self.assertNotIn("Splash one", again)
                self.assertNotIn("Spike", again)

    def test_kill_read_back_in_an_unsent_reply_is_read_again(self):
        with tempfile.TemporaryDirectory() as root:
            transcript = Transcript(Path(root) / "radio")
            c, _ = controller(LIVE["confirm_2051"], Stub(), transcript=transcript)
            first, _ = say(c, "Athena, confirm kills", "k-1")
            self.assertIn("Confirmed", first[0]["speech"])
            gateway_outcome(transcript, "k-1", "say_again_fallback_sent:bridge_failed")
            again, _ = say(c, "Athena, confirm kills", "k-2")
            self.assertEqual(again[0]["speech"], first[0]["speech"])
            gateway_outcome(transcript, "k-2", "sent")
            third, _ = say(c, "Athena, confirm kills", "k-3")
            self.assertNotIn("Confirmed", third[0]["speech"])  # delivered: read back once only

    def test_unsent_model_reply_leaves_the_chat_history(self):
        with tempfile.TemporaryDirectory() as root:
            transcript = Transcript(Path(root) / "radio")
            c, _ = controller(LIVE["loadout_2035"], Stub(), transcript=transcript)
            say(c, "Athena, how is it looking", "h-1")
            self.assertEqual(len(c.history), 1)
            gateway_outcome(transcript, "h-1", "say_again_fallback_sent:bridge_failed")
            say(c, "Athena, radio check", "h-2")
            self.assertEqual([q for q, _ in c.history], ["Athena, radio check"])


class MissionResetTests(unittest.TestCase):
    def test_new_mission_clears_history_kills_and_missiles(self):
        stub = Stub()
        c, eye = controller(LIVE["confirm_2051"], stub)
        say(c, "Athena, how is it looking", "m-1")
        first, _ = say(c, "Athena, confirm kills", "m-2")
        say(c, "Athena, launch, launch", "m-3")
        self.assertEqual(len(c.history), 3)
        self.assertTrue(c._confirmed_kills)
        eye.reset_at = 2.0  # the Eye was reset: mission_start / session_changed
        again, _ = say(c, "Athena, confirm kills", "m-4")
        self.assertEqual(again[0]["speech"], first[0]["speech"])  # the new mission's kills are new
        self.assertEqual([q for q, _ in c.history], ["Athena, confirm kills"])
        self.assertTrue(any(r.get("route") == "reset" for r in c.transcript.records))

    def test_first_prompt_of_a_new_session_has_no_previous_history(self):
        seen = []
        c, eye = controller(LIVE["loadout_2035"], lambda m: seen.append(m) or "Prometheus, Athena, copy.")
        say(c, "Athena, radio check", "s-1")
        eye.session_id, eye.reset_at = "s2", 2.0  # the feeder resets the Eye on a new DCS session id
        say(c, "Athena, how is it looking", "s-2")
        self.assertEqual([m["role"] for m in seen[1] if m["role"] in ("user", "assistant")], ["user"])

    def test_same_mission_keeps_memory(self):
        c, _ = controller(LIVE["loadout_2035"], Stub())
        say(c, "Athena, radio check", "a")
        say(c, "Athena, how is it looking", "b")
        self.assertEqual(len(c.history), 2)


class ReviewBugTests(unittest.TestCase):
    def test_any_hits_reads_each_kill_once_live_20261109_2009(self):
        # r11 code: "Splash one Container 40Ft. Good hit, splash one container." for TWO container kills
        pic = dict(ANY_HITS, _splash_keys=["k1245", "k1256"])
        c, eye = controller(pic, Stub())
        frames, _ = say(c, "Athena, any hits?")
        self.assertEqual(frames[0]["speech"], "Prometheus, Athena, Splash two Container 40Ft.")
        self.assertEqual(eye.announced, {"k1245", "k1256"})
        self.assertEqual(frames[0]["route"], "fixed")

    def test_any_hits_without_a_pending_splash_still_reports_the_shot(self):
        pic = dict(ANY_HITS)
        pic.pop("splash_to_announce")
        c, _ = controller(pic, Stub())
        frames, _ = say(c, "Athena, any hits?")
        self.assertNotIn("Splash", frames[0]["speech"])
        self.assertRegex(frames[0]["speech"], r"(Good hit|Hit observed|No hit|no result)")

    def test_pilot_kill_report_never_creates_a_kill_without_dcs_evidence(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))  # a shot, no kill event, nothing vanished
        clock.t += 5
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye,
                             llm=lambda m: "Prometheus, Athena, copy.")
        list(c.stream(req("Athena, target destroyed")))
        pic = eye.picture()
        self.assertEqual(pic["my_kills_this_mission"], [])
        self.assertNotEqual(pic["my_shots_last_15_min"]["shots"][0]["result"], "kill")
        tool = [r for r in c.transcript.records if r["kind"] == "tool"][0]
        self.assertIsNone(tool["result"]["pilot_report"]["confirmed"])

    def test_pilot_kill_report_still_upgrades_a_likely_kill_dcs_saw(self):
        eye, clock = a10_eye()
        eye.on_event(fx.shot(A10, 0, "GAU_8"))
        clock.t += 75
        eye.on_units_message(fx.gone_message(11987, "Unit #650"))  # vanished long after the shot: likely
        self.assertEqual(eye.picture()["my_kills_this_mission"][0]["confidence"], "likely")
        c = SimpleController(None, FakeTranscript(), model="test", api_key="x", eye=eye,
                             llm=lambda m: "Prometheus, Athena, copy, splash one T-55.")
        list(c.stream(req("Athena, target destroyed")))
        self.assertEqual(eye.picture()["my_kills_this_mission"][0]["confidence"], "confirmed")


class ReplayTests(unittest.TestCase):
    """Every real picture x fixed and model asks x model behaviours: one frame, inside the budget and deadline."""

    def test_replay_real_pictures(self):
        long = "Prometheus, Athena, " + " ".join("Group %d is out there, far away." % i for i in range(40))
        pictures = list(LIVE.items()) + [("any_hits", dict(ANY_HITS, _splash_keys=["k1", "k2"]))]
        with deadline(0.25):
            for name, pic in pictures:
                for text in FIXED_ASKS + MODEL_ASKS:
                    for mode in ("ok", "long", "sleep", "empty", "raise") if text in MODEL_ASKS else ("long", "sleep"):
                        stub = Stub("ok" if mode == "long" else mode, long if mode == "long" else "Prometheus, Athena, copy.")
                        c, _ = controller(pic, stub)
                        frames, took = say(c, text)
                        with self.subTest(picture=name, ask=text, mode=mode):
                            self.assertEqual(len(frames), 1)
                            self.assertTrue(frames[0]["speech"])
                            self.assertLessEqual(len(frames[0]["speech"]), MAX_REPLY_CHARS)
                            self.assertLess(took, 0.6)
                            rec = replies(c)[0]
                            self.assertIn(rec["route"], ("fixed", "llm", "debrief", "fallback"))
                            self.assertEqual(frames[0]["route"], rec["route"])
                            encode_response(frames[0])
                            if rec["route"] in ("fixed", "debrief"):
                                self.assertEqual(stub.calls, 0)


if __name__ == "__main__":
    unittest.main()
