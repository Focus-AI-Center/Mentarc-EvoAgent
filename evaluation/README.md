# Evaluation Scripts

Reference scaffolding for reproducing the three research questions (RQ1–RQ3) in the EvoAgent paper.

The test corpus (172 foreign-trade dialogue samples, distilled from 664 multi-turn sessions in a deployed production environment) contains proprietary business data and is **not shipped** with this repository. Scripts below operate on a JSONL file whose schema is described in each subdirectory; researchers can either request the original corpus from the authors or construct their own test set following the same schema.

## Structure

```
evaluation/
├── rq1_capability/            RQ1: does the evolutionary mechanism add value?
│   ├── run_eval.py             compare baseline LLM vs. LLM+EvoAgent on 5 axes
│   └── judge.py                LLM-as-Judge scorer (5-dimension rubric)
├── rq2_transferability/       RQ2: does EvoAgent transfer across backbone models?
│   └── run_model_swap.py       rerun RQ1 protocol with swapped `MODEL` env var
└── rq3_boundary/              RQ3: capability boundaries
    └── long_session_stress_test.py  420-turn dialogue stress test
```

## Input schema (`test_cases.jsonl`)

```jsonc
{
  "case_id": "fc_001",
  "user_id": "test_user_fc",
  "session_id": "sess_001",
  "turns": [
    {"role": "user", "content": "A customer sent an inquiry..."},
    {"role": "assistant", "content": "..."}      // reference (optional)
  ],
  "query": "Draft an initial reply email",              // final turn to evaluate
  "intent": "inquiry_response",                  // for grouping analysis
  "tags": ["email", "first_contact"]
}
```

## RQ1 — Capability Enhancement

```bash
# Baseline: backbone LLM only
python evaluation/rq1_capability/run_eval.py \
  --test-set path/to/test_cases.jsonl \
  --mode baseline \
  --output results/baseline.jsonl

# With EvoAgent
python evaluation/rq1_capability/run_eval.py \
  --test-set path/to/test_cases.jsonl \
  --mode evoagent \
  --output results/evoagent.jsonl

# LLM-as-Judge scoring across 5 dimensions
python evaluation/rq1_capability/judge.py \
  --test-set path/to/test_cases.jsonl \
  --baseline results/baseline.jsonl \
  --treatment results/evoagent.jsonl \
  --output results/scores.json
```

## RQ2 — Model Transferability

```bash
MODEL=gpt-4.1 python evaluation/rq2_transferability/run_model_swap.py
MODEL=qwen-3.5-35b-a3b python evaluation/rq2_transferability/run_model_swap.py
```

## RQ3 — Capability Boundary

```bash
python evaluation/rq3_boundary/long_session_stress_test.py --max-turns 420
```

## Data access

If you intend to reproduce the exact numbers reported in the paper, the foreign-trade dialogue corpus is available upon reasonable request (academic collaboration). See the Citation section in the repository `README.md` for contact information.
