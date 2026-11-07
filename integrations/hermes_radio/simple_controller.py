"""Simple conversational radio controller: one rich live picture + one LLM call per transmission.

Design (user direction 2026-11-07): a capable model reads Athena's live data and talks like a
human GCI controller. Minimal rules: the picture is the only source of facts; everything else is
the model's judgement.
"""
from collections import deque
import http.client
import json
import os
import re
import time

from .contract import response
from athena.state_service_client import ClientQueryError
from . import tools as _tools_mod
from .tools import _plain, category, geometry

_strict_usable = _tools_mod.usable


def _lenient_usable(frame):
    try:
        return _strict_usable(frame)
    except ClientQueryError as error:
        if str(error) != "evidence_expired":
            raise


_tools_mod.usable = _lenient_usable

PROMPT = """You are "Athena", an experienced AWACS/GCI controller talking on the radio to a single pilot, callsign "Prometheus", flying in DCS World.

Every turn you receive a live PICTURE (JSON) produced by the Athena tracking system. It is your source of truth: ownship (Prometheus' own aircraft), nearby hostile and friendly units with BRAA already computed from Prometheus (bearing in degrees TRUE, range in nautical miles, altitude in feet MSL), unit categories (air/ground/sam/ship/unknown), totals, and recent mission events. If the picture is unavailable or missing something, say so plainly and tell the pilot what you DO know. Never invent contacts or numbers that are not in the picture.

How to talk:
- Sound like a real, calm, competent human controller. Natural radio phraseology, brevity words where they fit (picture, bogey dope, BRAA, angels, hot/cold, clean, copy, say again, loud and clear).
- Start with "Prometheus, Athena," (or just "Prometheus," for quick follow-ups).
- Keep it short: usually 1-2 sentences, never more than about 20 seconds of speech. Group and summarise instead of listing everything.
- Position calls must be unambiguous to the ear. ALWAYS say the words "bearing", "miles" and "angels"/"feet": e.g. "bandits, bearing zero four eight, thirty eight miles, angels fifteen" or for ground units "enemy armor, bearing zero four eight, thirty eight miles". Read bearings digit by digit ("zero four eight"), ranges as whole miles in words. Never say the bare word "BRAA" and never give two numbers back to back without their units. Angels = altitude in thousands of feet; under one thousand feet say "low"; ground units get no altitude.
- Say "bandit" only for enemy aircraft; enemy ground units are "enemy armor", "AAA", "SAM site", etc. When there are several contacts in roughly the same place, call them as one group with a short description.
- Clearly separate air threats from ground units and SAM/air-defence sites. When asked about "enemies" or "threats", lead with the most relevant ones (nearest air threats first, then SAMs).
- Write numbers so a text-to-speech voice reads them well. No markdown, no lists, no parentheses.
- Answer chit-chat and radio checks naturally ("Prometheus, Athena, loud and clear.").
- Follow-up questions ("what type is it?", "where's that group now?") refer to the previous exchanges.
"""

ADDRESS = re.compile(r"^[\s\-\u2013\u2014.,:;!?'\"]*athena\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<!\d)[.!?](?=\s|$)")
FOLLOWUP_WINDOW_S = 45.0


def _num(value):
    return None if value is None else round(value)


class SimpleController:
    def __init__(self, tools, transcript, *, model, api_key, base_url="https://api.openai.com/v1",
                 timeout=12.0, radius_nm=200.0):
        self.tools, self.transcript = tools, transcript
        self.model, self._key = model, api_key
        self._host = base_url.split("//", 1)[1].split("/", 1)[0]
        self._path = "/" + base_url.split("//", 1)[1].split("/", 1)[1].rstrip("/") + "/chat/completions"
        self.timeout, self.radius_nm = timeout, radius_nm
        self.history = deque(maxlen=8)
        self.last_exchange = 0.0
        # Duck-typed for server code that may consult a provider attribute.
        self.provider = self

    # ---------------------------------------------------------------- picture
    def picture(self):
        own_frame = self.tools.read("ownship", {})
        own = _plain(own_frame.data)["ownship"]
        if own is None:
            return {"available": False, "reason": "Prometheus is not in an aircraft slot (no ownship)"}
        mine = own.get("coalition")
        enemy = "red" if mine == "blue" else "blue"
        cap = min(100, self.tools.config.query_limits.max_rows)
        pic = {"available": True, "ownship": {k: own.get(k) for k in own if k not in ("provenance_key",)}}
        if own.get("position"):
            pos = own["position"]
            pic["ownship"]["altitude_ft_msl"] = _num(pos["alt_m"] / 0.3048) if pos.get("alt_m") is not None else None
        for side, coalition in (("hostile", enemy), ("friendly", mine)):
            params = {"coalition": coalition, "state": "present", "limit": cap}
            if own.get("position"):
                params["near"] = {"anchor": "ownship", "radius_m": self.radius_nm * 1852}
            try:
                frame = self.tools.read("query_units", params)
                data = _plain(frame.data)
            except Exception as error:  # report, don't hide
                pic[side] = {"error": type(error).__name__ + ": " + str(error)}
                continue
            rows = []
            for row in data.get("rows", []):
                if row.get("unit_id") == own.get("unit_id"):
                    continue
                braa = geometry(own, row)
                rows.append({"unit_id": row.get("unit_id"), "type": row.get("unit_type"),
                             "category": category(row.get("unit_type")), "group": row.get("group_name"),
                             "bearing_true": braa["bearing_true_deg"], "range_nm": braa["range_nm"],
                             "alt_ft": braa["altitude_ft_msl"]})
            rows.sort(key=lambda r: (r["range_nm"] is None, r["range_nm"] or 0))
            by_cat = {}
            for r in rows:
                by_cat.setdefault(r["category"], []).append(r)
            pic[side] = {
                "within_nm": self.radius_nm if own.get("position") else None,
                "total_in_area": data.get("total"),
                "returned": len(rows),
                "truncated": bool(getattr(frame, "limits", {}) and frame.limits.get("truncated")),
                "nearest_by_category": {c: v[:12] for c, v in by_cat.items()},
            }
        try:
            counts = _plain(self.tools.read("count_units", {"group_by": "coalition", "limit": 10}).data)
            pic["mission_totals_by_coalition"] = counts
        except Exception as error:
            pic["mission_totals_by_coalition"] = {"error": str(error)}
        try:
            events = _plain(self.tools.read("recent_events", {"limit": 12}).data)
            pic["recent_events"] = [{k: e.get(k) for k in ("event_type", "observed_at_utc", "subject_ids", "text", "data")}
                                    for e in events.get("events", [])]
        except Exception as error:
            pic["recent_events"] = {"error": str(error)}
        return pic

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
        start = time.monotonic()
        text = request.transcript.strip()
        self._log(request, "adapter_ingress", text=text, outcome="received")
        addressed = ADDRESS.match(text) is not None
        followup = (time.monotonic() - self.last_exchange) < FOLLOWUP_WINDOW_S
        if request.speaker != "Prometheus" or not (addressed or followup):
            self._log(request, "route", route="ignored", outcome="not_addressed_to_athena")
            yield response(request, reason="unaddressed_or_noncommand")
            return
        try:
            pic = self.picture()
            self._log(request, "tool", tool="picture", args={}, outcome="ok" if pic.get("available") else "unavailable",
                      result={"ok": bool(pic.get("available")), "reason": pic.get("reason", "")},
                      timings_ms={"tool": round((time.monotonic() - start) * 1000)})
        except Exception as error:
            pic = {"available": False, "reason": "Athena tracking system not reachable (%s)" % type(error).__name__}
            self._log(request, "tool", tool="picture", args={}, outcome="error",
                      result={"ok": False, "reason": type(error).__name__ + ": " + str(error)[:200]})
        messages = [{"role": "system", "content": PROMPT}]
        for q, a in self.history:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "system", "content": "PICTURE " + json.dumps(pic, ensure_ascii=True, default=str)})
        messages.append({"role": "user", "content": text})
        t_llm = time.monotonic()
        try:
            answer = self.ask(messages)
        except Exception as error:
            self._log(request, "controller_error", outcome=type(error).__name__ + ": " + str(error)[:300],
                      timings_ms={"total": round((time.monotonic() - start) * 1000)})
            answer = "Prometheus, Athena, say again, I had a problem on my end."
        answer = " ".join(answer.replace("*", "").split())
        llm_ms = round((time.monotonic() - t_llm) * 1000)
        self.history.append((text, answer))
        self.last_exchange = time.monotonic()
        # Two parts max: first sentence (gets on air fast), then the rest.
        match = SENTENCE_END.search(answer)
        parts = [answer] if not match or match.end() >= len(answer) - 1 else [answer[:match.end()].strip(), answer[match.end():].strip()]
        for index, part in enumerate(parts, 1):
            self._log(request, "reply_generated", text=part, outcome="intended", part=index,
                      timings_ms={"llm": llm_ms, "total": round((time.monotonic() - start) * 1000)})
            yield response(request, speech=part, state="ok")

    def exchange(self, request):
        parts = list(self.stream(request))
        if not parts[0]["deliverable"]:
            return parts[0]
        return response(request, speech=" ".join(p["speech"] for p in parts), state="ok")
