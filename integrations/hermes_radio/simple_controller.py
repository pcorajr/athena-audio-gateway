"""Simple conversational radio controller: one rich live picture + one LLM call per transmission.

Design (user direction 2026-11-07): a capable model reads Athena's live data and talks like a
human GCI controller. Minimal rules: the picture is the only source of facts; everything else is
the model's judgement. With an Athena Eye attached (ATHENA_EYE_HOST), the picture comes from the
in-memory DCS-gRPC tracker in eye.py; otherwise from the Athena state service (legacy path).
"""
from collections import deque
import http.client
import json
import re
import threading
import time
import unicodedata

from .contract import response, speaker_matches

PROMPT = """You are "Athena", an experienced human AWACS/GCI controller on the radio with one pilot, callsign "Prometheus", flying in DCS World.

Every turn you get a live PICTURE (JSON) from the Athena Eye tracking system. It is your only source of truth: ownship (Prometheus' aircraft), contact groups already computed from Prometheus (bearing, range in nautical miles, altitude, aspect, closure, and "clock" = clock position relative to Prometheus' nose with ahead/behind/left/right), nearby airbases, Prometheus' own shots and their results, recent kills and losses, recent events and alive totals. data_age_s tells you how old the data is; if it is old or the picture is not connected, say so briefly and still give what you have. Never invent contacts, kills or numbers that are not in the picture.

Facts that matter:
- Every unit listed in contacts and totals is ALIVE. Something is dead or killed ONLY if it appears in recent_kills_and_losses or a shot result says kill. Never call something destroyed otherwise.
- my_shots: result pending = weapon still flying or not known yet, hit = hit but no kill reported, kill = target destroyed, miss = no hit within the window. intended_target is a guess (DCS does not report the locked target); say "looks like" if you rely on it.
- Bearings are magnetic when the picture says so, otherwise true; do not mention it unless asked.

How to talk:
- Calm, natural, clear and short: usually one or two sentences. Summarise groups instead of listing everything.
- Start with "Prometheus, Athena," (or just "Prometheus," for a quick follow-up).
- EVERY positional call uses this exact order, BRAA first then clock:
  Air: "<call>, bearing zero three eight, twelve miles, angels fifteen. Target is at your ten o'clock, high." (vertical = high/low/level from the picture).
  Ground: "<call>, bearing zero three eight, three miles, ground target. Target is at your eleven o'clock." (no angels, no high/low for ground).
  Read bearings digit by digit, ranges as whole miles in words, angels = thousands of feet ("low" under a thousand).
- "bandit" only for enemy aircraft. Enemy ground units are "enemy armor", "SAM site", "triple A", "enemy trucks" and so on. Friendly aircraft are "friendly".
- Air threats first (nearest and hot first), then SAMs, then ground.
- No markdown, no lists, no parentheses, no abbreviations with periods. Write numbers so a text-to-speech voice reads them well.
- Radio checks and chit-chat: answer naturally ("Prometheus, Athena, loud and clear.").
- Follow-up questions refer to the previous exchanges.
- Development notes: the pilot is also the developer of Athena. When he says things like "feature request", "note for development", "Athena should be able to ...", "add a feature" or reports a bug or a wrong answer, do not treat it as a tactical call: reply briefly "Prometheus, Athena, copy, noted for development: <one-line restatement>." If it is unclear, ask one short clarifying question.
- Basic ATC is fine: for takeoff, landing or approach requests at an airfield, check the picture for aircraft near that field (within about five miles and below about five thousand feet) and say "no traffic" or call the traffic with the same BRAA + clock format, then approve ("cleared for takeoff", "cleared to land"). Don't invent runway numbers, winds or other data that is not in the picture.
"""

SENTENCE_END = re.compile(r"(?<!\d)[.!?](?=\s|$)")
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp", "Zs"})
_JUNK = re.compile(r"^[\W_]*$")
PART_CHARS = 420
FALLBACK = "Prometheus, Athena, say again, I had a problem on my end."


def _num(value):
    return None if value is None else round(value)


def clean(text):
    text = "".join(" " if unicodedata.category(c) in _UNSAFE else c for c in (text or "").replace("*", ""))
    return " ".join(text.split())


def split_parts(answer, limit=PART_CHARS):
    """First sentence alone (fast first audio), then the rest packed into parts of at most `limit` chars."""
    sentences, start = [], 0
    for m in SENTENCE_END.finditer(answer):
        sentences.append(answer[start:m.end()].strip())
        start = m.end()
    if answer[start:].strip():
        sentences.append(answer[start:].strip())
    parts = []
    for sentence in sentences:
        pieces = []
        while len(sentence) > limit:  # pathological run-on sentence: break on a word boundary
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        pieces.append(sentence)
        for piece in pieces:
            if len(parts) >= 2 and len(parts[-1]) + 1 + len(piece) <= limit:
                parts[-1] = parts[-1] + " " + piece
            elif piece:
                parts.append(piece)
    return parts


class SimpleController:
    # RadioServer: hold concurrent transmissions for this controller (answered in turn) instead of 503.
    queues = True

    def __init__(self, tools, transcript, *, model, api_key, base_url="https://api.openai.com/v1",
                 timeout=12.0, radius_nm=200.0, eye=None, llm=None, speaker="Prometheus,Player,Client"):
        self.tools, self.transcript, self.eye = tools, transcript, eye
        self.model, self._key = model, api_key
        self._host = base_url.split("//", 1)[1].split("/", 1)[0]
        self._path = "/" + base_url.split("//", 1)[1].split("/", 1)[1].rstrip("/") + "/chat/completions"
        self.timeout, self.radius_nm = timeout, radius_nm
        self.history = deque(maxlen=8)
        self.speaker = speaker
        self._llm = llm or self.ask
        self._turn = threading.Lock()  # one exchange at a time; later arrivals wait their turn
        self._waiting = 0
        self._waiting_lock = threading.Lock()
        # Duck-typed for server code that may consult a provider attribute.
        self.provider = self

    # ---------------------------------------------------------------- picture
    def picture(self):
        if self.eye is not None:
            return self.eye.picture()
        return self._state_service_picture()

    def _state_service_picture(self):
        """Legacy path: Athena state service (kept for fallback when no eye is configured)."""
        from athena.state_service_client import ClientQueryError
        from . import tools as tools_mod
        from .tools import _plain, category, geometry

        if not getattr(tools_mod, "_eye_lenient", False):
            strict = tools_mod.usable

            def lenient(frame):
                try:
                    return strict(frame)
                except ClientQueryError as error:
                    if str(error) != "evidence_expired":
                        raise
            tools_mod.usable, tools_mod._eye_lenient = lenient, True
        own = _plain(self.tools.read("ownship", {}).data)["ownship"]
        if own is None:
            return {"available": False, "reason": "Prometheus is not in an aircraft slot (no ownship)"}
        mine = own.get("coalition")
        enemy = "red" if mine == "blue" else "blue"
        cap = min(100, self.tools.config.query_limits.max_rows)
        pic = {"available": True, "ownship": {k: own.get(k) for k in own if k not in ("provenance_key",)}}
        if own.get("position") and own["position"].get("alt_m") is not None:
            pic["ownship"]["altitude_ft_msl"] = _num(own["position"]["alt_m"] / 0.3048)
        for side, coalition in (("hostile", enemy), ("friendly", mine)):
            params = {"coalition": coalition, "state": "present", "limit": cap}
            if own.get("position"):
                params["near"] = {"anchor": "ownship", "radius_m": self.radius_nm * 1852}
            try:
                data = _plain(self.tools.read("query_units", params).data)
            except Exception as error:  # noqa: BLE001 - report, don't hide
                pic[side] = {"error": type(error).__name__ + ": " + str(error)}
                continue
            rows = []
            for row in data.get("rows", []):
                if row.get("unit_id") == own.get("unit_id"):
                    continue
                braa = geometry(own, row)
                rows.append({"type": row.get("unit_type"), "category": category(row.get("unit_type")),
                             "group": row.get("group_name"), "bearing_true": braa["bearing_true_deg"],
                             "range_nm": braa["range_nm"], "alt_ft": braa["altitude_ft_msl"]})
            rows.sort(key=lambda r: (r["range_nm"] is None, r["range_nm"] or 0))
            pic[side] = rows[:25]
        try:
            events = _plain(self.tools.read("recent_events", {"limit": 12}).data)
            pic["recent_events"] = [{k: e.get(k) for k in ("event_type", "observed_at_utc", "text")}
                                    for e in events.get("events", [])]
        except Exception as error:  # noqa: BLE001
            pic["recent_events"] = {"error": str(error)}
        return pic

    def _picture_with_retry(self):
        last = None
        for _ in range(2):
            try:
                return self.picture(), None
            except Exception as error:  # noqa: BLE001 - retry once, then tell the pilot
                last = error
                time.sleep(0.05)
        return {"available": False, "reason": "Athena tracking not reachable (%s)" % type(last).__name__}, last

    @staticmethod
    def picture_summary(pic):
        own = pic.get("ownship") or {}
        hostile = pic.get("hostile_groups", pic.get("hostile")) or []
        friendly = pic.get("friendly_groups", pic.get("friendly")) or []
        out = {"connected": pic.get("connected", pic.get("available")), "ownship": own.get("type"),
               "hostile_groups": len(hostile) if isinstance(hostile, list) else None,
               "friendly_groups": len(friendly) if isinstance(friendly, list) else None,
               "units_age_s": (pic.get("data_age_s") or {}).get("units")}
        if isinstance(pic.get("airbases"), dict):
            out["airbases"] = pic["airbases"].get("count")
        if isinstance(pic.get("my_shots_last_15_min"), dict):
            out["shots"] = pic["my_shots_last_15_min"].get("by_result")
        if isinstance(pic.get("recent_kills_and_losses_last_10_min"), list):
            out["kills_losses"] = len(pic["recent_kills_and_losses_last_10_min"])
        if isinstance(hostile, list) and hostile and isinstance(hostile[0], dict) and "clock" in hostile[0]:
            h = hostile[0]
            out["nearest_hostile"] = "%s %s %snm %s" % (h.get("count"), h.get("category"), h.get("range_nm"),
                                                        h.get("clock"))
        if pic.get("reason"):
            out["reason"] = pic["reason"]
        return out

    # ---------------------------------------------------------------- llm
    def ask(self, messages):
        body = json.dumps({"model": self.model, "messages": messages, "max_completion_tokens": 400,
                           "store": False}).encode()
        conn = http.client.HTTPSConnection(self._host, timeout=self.timeout)
        try:
            conn.request("POST", self._path, body=body, headers={
                "Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
            reply = conn.getresponse()
            raw = reply.read()
        finally:
            conn.close()
        if reply.status != 200:
            raise RuntimeError("openai_http_%d: %s" % (reply.status, raw[:300].decode("utf-8", "replace")))
        return json.loads(raw)["choices"][0]["message"]["content"].strip()

    # ---------------------------------------------------------------- radio
    def _log(self, request, kind, **values):
        self.transcript.log(request=request, kind=kind, provider="openai-api", model=self.model, **values)

    def stream(self, request):
        """Answer EVERY transmission from the pilot on the command frequency (no 'Athena' prefix needed)."""
        text = request.transcript.strip()
        self._log(request, "adapter_ingress", text=text, outcome="received")
        if not speaker_matches(request.speaker, self.speaker):
            self._log(request, "route", route="ignored", outcome="not_pilot")
            yield response(request, reason="unaddressed_or_noncommand")
            return
        if _JUNK.match(text):
            self._log(request, "route", route="ignored", outcome="empty_transcript")
            yield response(request, reason="unaddressed_or_noncommand")
            return
        with self._waiting_lock:
            self._waiting += 1  # in flight: answering or waiting
            queued = self._waiting > 1
        if queued:
            self._log(request, "route", route="LLM", outcome="queued_behind_previous")
        try:
            with self._turn:
                yield from self._answer(request, text)
        finally:
            with self._waiting_lock:
                self._waiting -= 1

    def _answer(self, request, text):
        start = time.monotonic()
        pic, error = self._picture_with_retry()
        self._log(request, "tool", tool="picture", args={}, outcome="error" if error else "ok",
                  result={"summary": self.picture_summary(pic),
                          "error": (type(error).__name__ + ": " + str(error)[:200]) if error else ""},
                  timings_ms={"tool": round((time.monotonic() - start) * 1000)})
        messages = [{"role": "system", "content": PROMPT}]
        for q, a in self.history:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "system", "content": "PICTURE " + json.dumps(
            pic, ensure_ascii=True, default=str, separators=(",", ":"))})
        messages.append({"role": "user", "content": text})
        t_llm = time.monotonic()
        answer = None
        for attempt in (1, 2):
            try:
                answer = self._llm(messages)
                break
            except Exception as failure:  # noqa: BLE001 - one retry, then a spoken fallback
                self._log(request, "controller_error", outcome=type(failure).__name__ + ": " + str(failure)[:300],
                          attempt=attempt, timings_ms={"total": round((time.monotonic() - start) * 1000)})
        answer = clean(answer) or FALLBACK
        llm_ms = round((time.monotonic() - t_llm) * 1000)
        self.history.append((text, answer))
        for index, part in enumerate(split_parts(answer), 1):
            self._log(request, "reply_generated", text=part, outcome="intended", part=index,
                      timings_ms={"llm": llm_ms, "total": round((time.monotonic() - start) * 1000)})
            yield response(request, speech=part, state="ok")

    def exchange(self, request):
        parts = list(self.stream(request))
        if not parts[0]["deliverable"]:
            return parts[0]
        return response(request, speech=" ".join(p["speech"] for p in parts), state="ok")
