# Evaluation scripts

Two scripts for comparing a bare model with the full agent on your own test cases:

| Script | What it does |
|---|---|
| `rq1_capability/run_eval.py` | answers every test case under one condition and one model |
| `rq1_capability/judge.py` | scores two such result files with an LLM judge (5-dimension rubric) |

**No data is included.** The corpus behind the paper is private business data that cannot be released, so
the numbers in the paper are for reference only. Use these scripts on your own data.

## Test cases

JSONL, one object per line. Only `query` is required:

```jsonc
{
  "case_id": "fc_001",
  "query": "Draft an initial reply email",
  "intent": "inquiry_response",
  "turns": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "profile": "text of USER.md",
  "memory": "text of MEMORY.md",
  "skills": {"skill-name": "text of SKILL.md"}
}
```

## Conditions (`--mode`)

| Mode | What the model sees |
|---|---|
| `baseline` | the final query only (the protocol used for the paper) |
| `baseline_context` | persona prompt, profile, memory and the dialogue so far; no tools, skills, matching or compression |
| `evoagent` | the full agent loop, started from the same dialogue |

`baseline_context` versus `evoagent` isolates what the harness adds beyond the information it is given.
To compare models, repeat the run with another `--model` and judge with the same `--judge-model`.

## Running

Every call costs money: `--dry-run` prints the plan, and a real run needs `--yes`. Try `--limit 10` first.

```bash
python evaluation/rq1_capability/run_eval.py --test-set evaluation/data/test_cases.jsonl \
    --mode baseline_context --model gpt-5.2 --output evaluation/results/baseline.jsonl --yes
python evaluation/rq1_capability/run_eval.py --test-set evaluation/data/test_cases.jsonl \
    --mode evoagent --model gpt-5.2 --output evaluation/results/evoagent.jsonl --yes
python evaluation/rq1_capability/judge.py --test-set evaluation/data/test_cases.jsonl \
    --baseline evaluation/results/baseline.jsonl --treatment evaluation/results/evoagent.jsonl \
    --judge-model <a model that is not under test> --output evaluation/results/scores.json --yes
```

## The judge

The judge must be named (`--judge-model`) and must differ from the model that produced the responses.
Responses are shown in a seeded random order with the recent dialogue (`--both-orders` judges each pair in
both orders). Pairs that cannot be scored are counted by reason in the output, never dropped silently.
The output reports per-dimension means, differences with a 95% bootstrap interval over cases, and
win/tie/loss counts.

## Privacy

Keep the test set and everything under `evaluation/results/` out of version control. External plugins (web
search, page fetch) are off by default, so case text goes only to the model API. `scores.json` has a
`summary` (aggregates) and `per_case` (derived from your data; keep it private).

## Not measured

Improvement as state accumulates over sessions, memory recall, skill-matching accuracy, cost, and agreement
between the judge and human raters. The paper's results come from earlier versions of these scripts, where the
baseline saw only the query and the judge followed the model under test (see Section 4.4 of the paper).
