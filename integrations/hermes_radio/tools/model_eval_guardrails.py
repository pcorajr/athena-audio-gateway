"""Private guardrail evaluation. No adapter/DCS access; recorded pictures and one normal controller call.

--mode suite: the same 72-item study; historical dialogue is filtered for the new
controller using its storage policy. --mode history: replay preflight, first session,
and all 37 flight exchanges in order, retaining each controller's actual replies.
--mode repeats: five repetitions of the three history-affected asks (fixed routes
stay fixed; never force a model call). --mode stub: all 319 recorded exchanges.
Every Codex HTTP attempt and API fallback is counted BEFORE sending, including failures.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import types

from .. import simple_controller as new
from ..codex_llm import CodexLLM
from . import model_eval as study

ROOT = Path(__file__).resolve().parents[3]


def old_controller():
    module = types.ModuleType("integrations.hermes_radio._guardrails_old")
    source = subprocess.check_output(["git", "show", "ee138de:integrations/hermes_radio/simple_controller.py"], cwd=ROOT, text=True)
    exec(compile(source, "ee138de/simple_controller.py", "exec"), module.__dict__)
    return module


class Eye(study._Eye):
    def __init__(self, pic):
        super().__init__(pic)
        self.reset_at, self.session_id = 1.0, None
        self.memory = None
        self.announced = set()

    def picture(self):
        pic = super().picture()
        for field, keys in (("splash_to_announce", "_splash_keys"), ("rwr_alert_to_announce", "_rwr_keys")):
            items = pic.get(field) or []
            ks = pic.get(keys) or ["%s|%s" % (field, a) for a in items]
            pending = [(a, k) for a, k in zip(items, ks) if k not in self.announced]
            if field in pic:
                pic[field], pic[keys] = [a for a, _ in pending], [k for _, k in pending]
        return pic

    def mark_announced(self, keys):
        self.announced.update(keys)

    def unmark_announced(self, keys):
        self.announced.difference_update(keys)

    def pilot_report(self, text):
        return None


def consume():
    path = Path(os.environ["MODEL_EVAL_BUDGET"]).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        count = json.loads(raw)["calls"] if raw else 0
        if count >= int(os.environ.get("MODEL_EVAL_MAX_CALLS", "300")):
            raise RuntimeError("model call budget exhausted")
        f.seek(0); f.truncate()
        json.dump({"calls": count + 1}, f); f.flush()


def messages_hash(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=True, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class Model:
    def __init__(self, ctl, stub=False):
        self.rows = []
        self.stub = stub
        self.inner = None
        if not stub:
            def fallback(messages, deadline=None):
                consume()
                return ctl.ask(messages, deadline=deadline)
            self.inner = CodexLLM(fallback, model="gpt-6-luna", effort="low", timeout=6.0,
                                 fallback_label="openai-api:gpt-5.4-mini")
            post = self.inner._post
            def counted(*a, **kw):
                consume()
                return post(*a, **kw)
            self.inner._post = counted
            self.inner.credentials.get()

    def answer_by(self, messages, deadline):
        start = time.monotonic()
        if self.stub:
            text, meta = "Prometheus, Athena, MODEL.", {"backend": "stub", "model": "stub"}
        else:
            text, meta = self.inner.answer_by(messages, deadline)
        self.rows.append({"raw_reply": text, "meta": meta, "model_s": round(time.monotonic()-start, 3),
                          "messages_sha256": messages_hash(messages),
                          "prompt_chars": sum(len(m["content"]) for m in messages),
                          "history_sent": [m for m in messages if m["role"] == "assistant"]})
        return text, dict(meta)


class CachedModel:
    """Recheck a previously sampled real answer after a mechanical-only correction.

    Fail if the original model's inputs would change. This does not spend calls
    or pretend that a saved answer is a fresh model measurement.
    """
    def __init__(self, row):
        self.row, self.rows = row, []

    def answer_by(self, messages, deadline):
        assert messages_hash(messages) == self.row["messages_sha256"], "ordered model messages changed"
        fields = ("raw_reply", "meta", "model_s", "prompt_chars", "history_sent", "messages_sha256")
        self.rows.append({k: self.row[k] for k in fields})
        return self.row["raw_reply"], dict(self.row["meta"])


def controller(module, pic, stub=False):
    ctl = module.SimpleController(None, study._Log(), model="gpt-5.4-mini", api_key=study._api_key() if not stub else "",
                                  eye=Eye(pic))
    ctl._llm = Model(ctl, stub)
    return ctl


def exchange(ctl, item, variant, rep=0):
    ctl.eye.pic = copy.deepcopy(item["picture"])
    # Transcripts omit private Eye event keys. Call text is NOT an identity:
    # two different kills can have identical splash wording. Each recorded pending
    # observation gets an exchange-local key; supplied real keys remain untouched.
    for field, keys in (("splash_to_announce", "_splash_keys"), ("rwr_alert_to_announce", "_rwr_keys")):
        if keys not in ctl.eye.pic:
            ctl.eye.pic[keys] = ["%s|%s|%s" % (item.get("tid", item["id"]), field, n)
                                 for n, _ in enumerate(ctl.eye.pic.get(field) or [])]
    before = len(ctl._llm.rows)
    log_before = len(ctl.transcript.records)
    req = study._request(item["ask"])
    req.transmission_id = item.get("tid", item["id"])
    start = time.monotonic()
    reply = ctl.exchange(req)
    call = ctl._llm.rows[-1] if len(ctl._llm.rows) > before else {}
    records = ctl.transcript.records[log_before:]
    return dict(id=item["id"], time=item.get("time"), ask=item["ask"], reply=reply.get("speech"),
                route=reply.get("route"), variant=variant, rep=rep, total_s=round(time.monotonic()-start, 4),
                **call, history_after=list(ctl.history),
                rejections=[r.get("llm", {}).get("fallback_reason") for r in records if r.get("outcome")=="reply_check_rejected"],
                resets=[r.get("outcome") for r in records if r.get("route")=="reset"])


def flight():
    all_ex = study._exchanges("20261111")
    pre = [e for e in all_ex if e["line"] in (1621, 1624)]
    fly = [e for e in all_ex if 1627 <= e["line"] <= 1923]
    assert len(pre) == 2 and len(fly) == 37, (len(pre), len(fly))
    for e in pre + fly:
        e["id"] = "20261111:%d" % e["line"]
    return pre, fly


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("suite", "history", "repeats", "stub"), required=True)
    ap.add_argument("--old", action="store_true")
    ap.add_argument("--stub-model", action="store_true", help="offline routing replay")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cached-input", help="suite only: recheck saved real answers, not a new sample")
    args = ap.parse_args()
    cached = {}
    if args.cached_input:
        assert args.mode == "suite" and not args.old
        cached = {r["id"]: r for r in map(json.loads, Path(args.cached_input).read_text().splitlines())}
    module = old_controller() if args.old else new
    variant = "ee138de" if args.old else "guardrails"
    out = Path(args.out)
    assert not out.exists(), "refusing to repeat an existing run"
    with out.open("x") as f:
        def emit(row):
            f.write(json.dumps(row) + "\n"); f.flush()
            print(row["id"], row["route"], row.get("model_s"), row["reply"], flush=True)
        if args.mode in ("suite", "repeats"):
            items,_ = study.load_set(ROOT / "docs/model-study/eval-data.json.gz")
            if args.mode == "repeats":
                items = [i for i in items if i["id"] in ("20261111:1663", "20261111:1689", "20261111:1697")]
                assert len(items) == 3
            for rep in range(5 if args.mode == "repeats" else 1):
                for item in items:
                    ctl = controller(module, item["picture"], stub=bool(cached) or args.stub_model)
                    if cached:
                        ctl._llm = CachedModel(cached[item["id"]])
                    ctl.history.extend((h[0], h[1] if args.old else new.factual_history(h[1]))
                                       for h in item.get("history", [])
                                       if args.old or new.factual_history(h[1]))
                    row = exchange(ctl, item, variant, rep)
                    if cached:
                        row["cached_input"] = args.cached_input
                    emit(row)
        elif args.mode == "history":
            pre, fly = flight()
            ctl = controller(module, pre[0]["picture"], stub=args.stub_model)
            for item in pre:
                emit(exchange(ctl, item, variant))
            # Recorded 18:26 first connection, 18:27 pilot birth: no mission_start
            # event was received and reset_at did NOT change. First session is the hole.
            ctl.eye.session_id = "recorded-first-session"
            for item in fly:
                emit(exchange(ctl, item, variant))
        else:
            count = 0
            for day in study.DAYS:
                items = [e for e in study._exchanges(day) if day != "20261111" or e["line"] <= 1923]
                ctl = controller(module, {}, stub=True)
                for item in items:
                    item["id"] = "%s:%d" % (day, item["line"])
                    emit(exchange(ctl, item, variant)); count += 1
            print("STUB COUNT", count)


if __name__ == "__main__":
    main()
