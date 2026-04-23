# Reproducibility Guide

This document describes how to reproduce the experiments reported in the EvoAgent paper.

> **⚠️ Pre-release preview.** The paper PDF and paper-to-code mapping referenced below will be attached in the official release. The code path structure described here is already stable.

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

## 2. Test corpus

RQ1/RQ2/RQ3 in the paper use 172 foreign-trade dialogue samples distilled from 664 multi-turn sessions in a deployed production environment. These samples contain proprietary business data and are **not bundled with this repository**.

- If you have access to the original corpus, place it at `evaluation/data/test_cases.jsonl` following the schema documented in `evaluation/README.md`.
- If you want to construct your own evaluation set, create a JSONL file following the same schema; each record needs `case_id`, `user_id`, `session_id`, `query`, and optional `turns` / `intent` / `tags`.

## 3. Running RQ1 (Capability Enhancement)

```bash
# Baseline run (pure LLM)
python evaluation/rq1_capability/run_eval.py \
    --test-set evaluation/data/test_cases.jsonl \
    --mode baseline \
    --output results/baseline.jsonl

# EvoAgent run (full harness)
python evaluation/rq1_capability/run_eval.py \
    --test-set evaluation/data/test_cases.jsonl \
    --mode evoagent \
    --output results/evoagent.jsonl

# LLM-as-Judge, 5-dimension rubric
python evaluation/rq1_capability/judge.py \
    --test-set evaluation/data/test_cases.jsonl \
    --baseline results/baseline.jsonl \
    --treatment results/evoagent.jsonl \
    --output results/rq1_scores.json
```

## 4. Running RQ2 (Model Transferability)

```bash
MODEL=gpt-4.1 python evaluation/rq2_transferability/run_model_swap.py \
    --test-set evaluation/data/test_cases.jsonl \
    --output-dir results/gpt-4.1

MODEL=qwen-3.5-35b-a3b python evaluation/rq2_transferability/run_model_swap.py \
    --test-set evaluation/data/test_cases.jsonl \
    --output-dir results/qwen-3.5
```

## 5. Running RQ3 (Boundary — Long Session)

```bash
python evaluation/rq3_boundary/long_session_stress_test.py \
    --max-turns 420 \
    --user-id stress_test \
    --output results/rq3_long_session.json
```

## 6. Version pin

A release corresponding to the paper will be tagged as `v1.0-paper` once the paper assets are attached. After that tag ships, check out `v1.0-paper` for bit-for-bit code parity with the published results:

```bash
git checkout v1.0-paper
```

## 7. Known caveats

A paper-to-code mapping (previously `paper/PAPER_CODE_MAP.md`) will ship alongside the paper PDF in the official release. It will cover how paper sections map to source files and document implementation details that evolved after submission (e.g. history-compression strategy, sub-agent delegation options).
