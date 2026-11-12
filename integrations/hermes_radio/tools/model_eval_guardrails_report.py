"""Summarize saved guardrail runs; never contacts a model or the adapter.
Usage: python -m integrations.hermes_radio.tools.model_eval_guardrails_report ~/.p1/guardrails
The manual verdicts below refer only to the immutable saved runs named here.
"""
import collections
import gzip
import json
from pathlib import Path
import statistics
import sys
from . import model_eval as study
from .. import simple_controller as sc


def main():
    directory = Path(sys.argv[1]).expanduser()
    docs = Path(__file__).resolve().parents[3] / "docs/model-study"
    items, _ = study.load_set(docs / "eval-data.json.gz")
    by = {i["id"]: i for i in items}
    def load(name):
        return [json.loads(line) for line in (directory / (name + ".jsonl")).read_text().splitlines()]
    def latency(values):
        values = sorted(values)
        return {"median_s": round(statistics.median(values), 3), "p95_s": round(values[round(.95*(len(values)-1))], 3)} if values else {}
    manual, verdicts, tables = {}, [], {}
    corrections = {
        ("suite-old", "w27"): "The target-position caveat is true; the reply supplies nearby friendly ground groups.",
        ("suite-old", "w32"): "No bandits showing is true: the air picture is empty. Kills and remaining totals are given.",
        ("suite-rechecked", "w27"): "The target-position caveat is true; the reply supplies nearby friendly ground groups.",
        ("suite-rechecked", "20261111:1577"): "No SAMs ahead is true: the two EWR rows are behind at seven o'clock.",
        ("suite-rechecked", "20261111:1606"): "The scorer ignores the negation in 'No SAM is tracking you'; radar tracking_you is empty.",
    }
    for name in ("suite-old", "suite-rechecked"):
        rows = load(name)
        assert len(rows) == len({r["id"] for r in rows}) == 72
        original, reviewed = collections.Counter(), collections.Counter()
        for row in rows:
            item = by[row["id"]]
            v, reasons, flags = study.score_reply(item, row["reply"])
            if v == "manual":
                v = "wrong" if name == "suite-old" and row["id"] == "20261111:1484" else "right"
                reason = ("Four airborne is not the recorded seven/eight airborne or twelve/thirteen total." if v == "wrong"
                          else item["truth"]["manual"])
                manual[row["id"] + "|" + row["reply"]] = {"verdict": v, "reason": reason}
            original[v] += 1
            original.update(flags)
            correction = corrections.get((name, row["id"]))
            audited = "right" if correction else v
            reviewed[audited] += 1
            if not correction:
                reviewed.update(flags)
            if correction:
                manual[row["id"] + "|" + row["reply"]] = {"verdict": audited, "reason": correction,
                                                           "raw_verdict": v, "raw_reasons": reasons}
            verdicts.append({"run": name, "id": row["id"], "reply": row["reply"], "raw_verdict": v,
                             "audited_verdict": audited, "reasons": reasons, "correction": correction})
        tables[name] = {"n": 72, "study_grader": dict(original), "audited": dict(reviewed),
                        "say_again": sum("say again" in r["reply"].lower() for r in rows),
                        "fallback_route": sum(r["route"] == "fallback" for r in rows),
                        "model_replies": sum("raw_reply" in r for r in rows),
                        **latency([r["model_s"] for r in rows if r.get("model_s")])}
    actual_runs = ["suite-new", "suite-old", "suite-final", "suite-verified", "history-old", "history-new",
                   "history-final", "history-verified", "history-release"]
    call_counts = {name: sum(1 + bool(r.get("meta", {}).get("fallback_reason")) for r in load(name) if "raw_reply" in r)
                   for name in actual_runs}
    budget = json.loads((directory / "budget.json").read_text())["calls"]
    assert sum(call_counts.values()) == budget <= 300
    all_rows = []
    for name in actual_runs + ["suite-rechecked", "repeats-new", "repeats-final", "repeats-verified"]:
        all_rows.extend(dict(r, run=name, cached_input=Path(r["cached_input"]).name) if r.get("cached_input")
                        else dict(r, run=name) for r in load(name))
    with gzip.open(docs / "guardrails-results.jsonl.gz", "wt") as out:
        for row in all_rows:
            out.write(json.dumps(row) + "\n")
    comparisons = {}
    for label, old, new, count in [("all", "stub-old", "stub-release", 319),
                                    ("flight_with_preflight", "flight-stub-old", "flight-stub-new", 39)]:
        a, b = load(old), load(new)
        assert len(a) == len(b) == count
        differences = []
        rows = []
        for x, y in zip(a, b):
            assert x["id"] == y["id"]
            if (x["reply"], x["route"]) != (y["reply"], y["route"]):
                differences.append(x["id"])
            rows.append({"id": x["id"], "ask": x["ask"], "old_reply": x["reply"], "new_reply": y["reply"],
                         "old_route": x["route"], "new_route": y["route"]})
        assert not differences, differences
        comparisons[label] = {"count": count, "fixed": sum(r["route"] == "fixed" for r in a),
                              "changed": differences, "rows": rows}
    with gzip.open(docs / "guardrails-stub-replay.json.gz", "wt") as out:
        json.dump(comparisons, out)
    old, new = load("history-old"), load("history-release")
    assert len(old) == len(new) == 39
    assert new[2]["resets"] == ["chat_context_cleared"]
    assert not any("Tracking disconnected" in m["content"] for r in new[2:] for m in r.get("history_sent", []))
    assert any("Tracking disconnected" in m["content"] for r in old[2:] for m in r.get("history_sent", []))
    assert "Creech" in next(r["reply"] for r in new if r["id"] == "20261111:1729")
    def cell(text): return text.replace("|", "\\|").replace("\n", " ")
    flight_lines = ["# Real-model sequential flight replay", "", "Two preflight exchanges, then the first-session/spawn transition; 37 flight exchanges.",
                    "The old column is ee138de (already r12.7), not the pre-r12.7 airborne recording.", "", "| Time | Pilot | ee138de | Guardrails |", "|---|---|---|---|"]
    for a, b in zip(old, new):
        assert a["id"] == b["id"]
        flight_lines.append("| %s | %s | %s | %s |" % (a["time"][11:19], cell(a["ask"]), cell(a["reply"]), cell(b["reply"])))
    flight_lines += ["", "## Six original flight failures", "", "These six asks are already FIXED routes in ee138de. Neither replay forces them through a model.",
                     "| Time | Recorded airborne answer | ee138de replay | Guardrails replay |", "|---|---|---|---|"]
    from .model_eval_set import FLIGHT_FAILURES
    old_by, new_by = {r["id"]:r for r in old}, {r["id"]:r for r in new}
    for fid in FLIGHT_FAILURES:
        a, b = old_by[fid], new_by[fid]
        assert a["route"] == b["route"] == "fixed" and a["reply"] == b["reply"]
        flight_lines.append("| %s | %s | %s | %s |" % (a["time"][11:19],cell(by[fid]["recorded_reply"]),cell(a["reply"]),cell(b["reply"])))
    (docs / "guardrails-flight.md").write_text("\n".join(flight_lines) + "\n")
    repeats = load("repeats-verified")
    assert len(repeats) == 15 and all(study.score_reply(by[r["id"]],r["reply"])[0] == "right" for r in repeats)
    summary = {"tables": tables, "calls": budget, "calls_by_run": call_counts,
               "model_generations": sum("raw_reply" in r for name in actual_runs for r in load(name)),
               "history": {"flight_exchanges": 37, "old_say_again": sum("say again" in r["reply"].lower() for r in old[2:]),
                           "new_say_again": sum("say again" in r["reply"].lower() for r in new[2:]),
                           "new_fallbacks": sum(r["route"] == "fallback" for r in new[2:]),
                           "old_latency": latency([r["model_s"] for r in old[2:] if r.get("model_s")]),
                           "new_latency": latency([r["model_s"] for r in new[2:] if r.get("model_s")])},
               "repeats": {"right": 15, "model_calls": 0},
               "stub": {k: {f: v[f] for f in ("count","fixed","changed")} for k,v in comparisons.items()},
               "offline_check": json.loads((directory / "offline-check.json").read_text()),
               "check_cpu": json.loads((directory / "check-latency.json").read_text())}
    (docs / "guardrails-summary.json").write_text(json.dumps(summary,indent=2) + "\n")
    (docs / "guardrails-manual.json").write_text(json.dumps(manual,indent=2) + "\n")
    (docs / "guardrails-verdicts.jsonl").write_text("\n".join(json.dumps(r) for r in verdicts) + "\n")
    print(json.dumps(summary,indent=2))


if __name__ == "__main__":
    main()
