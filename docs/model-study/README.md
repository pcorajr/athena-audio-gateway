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
