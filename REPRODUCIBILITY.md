# Reproducibility Guide

This document says what can and cannot be reproduced from this repository, and how to run the evaluation scripts on your own data.

**What cannot be reproduced.** The numbers in the paper were measured once, on an internal business version of EvoAgent, with a proprietary corpus. This repository is a scrubbed release (see the README), and no code snapshot in it reproduces those numbers. They are for reference only.

## 1. Environment

```bash
conda create -n evoagent python=3.11 -y
conda activate evoagent
pip install -r requirements.txt
```

Create a `.env` file with:

```bash
OPENAI_API_KEY=your_key
OPENAI_API_BASE=https://api.openai.com/v1     # or an OpenAI-compatible endpoint
MODEL=gpt-5.2                                  # backbone under test
COMPRESSION_MODEL=gpt-5.4-mini                 # used by history compressor
EMBEDDING_MODEL=text-embedding-3-small
```

## 2. Test cases

The experiments in the paper use 172 foreign-trade dialogue samples distilled from 664 multi-turn sessions in a deployed production environment. These samples contain proprietary business data that **cannot be released**, so the reported results are for reference only.

To run the scripts on your own data, create a JSONL file following the schema in `evaluation/README.md` (only `query` is required; `turns`, `intent`, `profile`, `memory` and `skills` are optional) and keep it, and everything generated from it, under `evaluation/data/` and `evaluation/results/` (not version-controlled).

## 3. Running the evaluation

```bash
# Baselines: the query only (as in the paper), or with the same context as the agent
python evaluation/rq1_capability/run_eval.py --test-set evaluation/data/test_cases.jsonl \
    --mode baseline_context --output evaluation/results/baseline.jsonl --yes

# EvoAgent run (full harness)
python evaluation/rq1_capability/run_eval.py --test-set evaluation/data/test_cases.jsonl \
    --mode evoagent --output evaluation/results/evoagent.jsonl --yes

# LLM-as-Judge, 5-dimension rubric; the judge must not be the model under test
python evaluation/rq1_capability/judge.py --test-set evaluation/data/test_cases.jsonl \
    --baseline evaluation/results/baseline.jsonl --treatment evaluation/results/evoagent.jsonl \
    --judge-model <judge> --output evaluation/results/scores.json --yes
```

Every call costs money: use `--dry-run` for the plan and `--limit 10` for a pilot. To compare backbone models, repeat the run with another `--model` and the same `--judge-model`.

The paper's results come from earlier versions of these scripts (a baseline that saw only the query, a judge that followed the model under test, and a separate long-session stress test). They are not part of the current branch; they remain in the repository history.

## 4. Known caveats

- The experiments compare a bare model with the whole harness in a single run, without confidence intervals or an ablation over components (paper, Section 4.4).
- Several mechanisms in the released code, such as user-confirmed skill proposals, were added after the experiments and are not evaluated in the paper (Section 3.4 and Appendix C).
- The README describes how the paper's four components map to source files.
