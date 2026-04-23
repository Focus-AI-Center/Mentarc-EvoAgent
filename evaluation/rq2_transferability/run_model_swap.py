"""RQ2: Does the EvoAgent harness transfer across backbone models?

Swap the `MODEL` environment variable and rerun the RQ1 protocol. The paper
reports results for GPT-4.1 and Qwen3.5-35B-A3B alongside the default GPT-5.2.

Usage:
    MODEL=gpt-4.1 python evaluation/rq2_transferability/run_model_swap.py \
        --test-set path/to/test_cases.jsonl --output-dir results/gpt-4.1
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from configs.settings import MODEL


def run_subprocess(cmd: list[str]) -> int:
    print(" ".join(cmd))
    return subprocess.call(cmd)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-set", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running RQ2 with MODEL={MODEL}")
    baseline = out_dir / "baseline.jsonl"
    treatment = out_dir / "evoagent.jsonl"
    scores = out_dir / "scores.json"

    rc = run_subprocess(
        [
            sys.executable,
            "evaluation/rq1_capability/run_eval.py",
            "--test-set",
            args.test_set,
            "--mode",
            "baseline",
            "--output",
            str(baseline),
        ]
    )
    if rc != 0:
        sys.exit(rc)

    rc = run_subprocess(
        [
            sys.executable,
            "evaluation/rq1_capability/run_eval.py",
            "--test-set",
            args.test_set,
            "--mode",
            "evoagent",
            "--output",
            str(treatment),
        ]
    )
    if rc != 0:
        sys.exit(rc)

    rc = run_subprocess(
        [
            sys.executable,
            "evaluation/rq1_capability/judge.py",
            "--test-set",
            args.test_set,
            "--baseline",
            str(baseline),
            "--treatment",
            str(treatment),
            "--output",
            str(scores),
        ]
    )
    sys.exit(rc)


if __name__ == "__main__":
    asyncio.run(main())
