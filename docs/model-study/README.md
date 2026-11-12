# Model-accuracy study (radio model route)

Study tool only: nothing here is imported by the live adapter. Results: `RESULTS.md` (same text as the study report).

Run on Athena (private process; Codex subscription is shared with the live radio - keep within the call budget):

```
W=~/Ai-Workspace/worktrees/athena-audio-gateway-model-study; cd $W; umask 077
export PYTHONPATH=~/Ai-Workspace/projects/project-athena/src:$W TMPDIR=~/.p1/study
PY=~/Ai-Workspace/projects/project-athena/.venv/bin/python
$PY -m integrations.hermes_radio.tools.model_eval extract ~/.p1/study/candidates.json.gz      # model-route asks from transcripts
$PY -m integrations.hermes_radio.tools.model_eval build ~/.p1/study/candidates.json.gz docs/model-study/eval-data.json.gz
$PY -m integrations.hermes_radio.tools.model_eval run docs/model-study/eval-data.json.gz OUT.jsonl --variant baseline
$PY -m integrations.hermes_radio.tools.model_eval table docs/model-study/eval-data.json.gz OUT.jsonl --manual docs/model-study/manual-reads.json
```

Variants: `baseline`, `slim`, `facts`, `diet`, `nohist`, `medium` (Codex effort medium), `mini` (gpt-5.4-mini API);
the reply check is applied offline (`score --check`, and the `check` row of `table`). Every model call is counted in
`$MODEL_EVAL_BUDGET` (default `~/.p1/study/budget.json`) and the runner stops at `MODEL_EVAL_MAX_CALLS` (400).

Files: `eval-data.json.gz` recorded pictures + real asks/histories (2026-11-08/09/11 transcripts); `results.jsonl`
every call of the study (reply, route, model, latency, prompt size); `manual-reads.json` hand verdicts for replies the
mechanical scorer cannot judge (key = `id|reply`).

## Guardrail implementation measurement

`guardrails.md` describes the final code, honest grading limitations and all call accounting; `guardrails-flight.md`
has the complete old/new sequential replay. Raw trials are in `guardrails-results.jsonl.gz`, with per-answer
verdicts, manual corrections and summary alongside. These artifacts are offline; nothing here contacts DCS/SRS.

New runner (from the guardrail worktree, private existing TMPDIR, umask 077, normal project PYTHONPATH/venv):

```text
MODEL_EVAL_BUDGET=~/.p1/guardrails/budget.json MODEL_EVAL_MAX_CALLS=300
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode suite --out OUT.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode history --old --out OLD.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode history --out NEW.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode repeats --out REPEATS.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode stub --out STUB.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode history --stub-model --out FLIGHT-STUB.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails --mode suite --cached-input SAVED.jsonl --out RECHECK.jsonl
python -m integrations.hermes_radio.tools.model_eval_guardrails_report ~/.p1/guardrails
```

Export the budget variables before running; the completed budget already contains 295 calls and must not be reset
or reused for another study without approval. Every HTTP attempt is counted before sending. `--old` loads only
the controller at the pinned ee138de revision; recorded pictures are served by a fake Eye in this private process.
The report command uses the named immutable run files described in its source and makes no model calls.
`--cached-input` is explicitly a saved-real-output check, not fresh inference; changed prompt length or dialogue
causes an assertion. Outputs are exclusive-created to prevent accidental repeat runs over existing evidence.
