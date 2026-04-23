"""LLM-as-Judge scorer for RQ1 (5-dimension rubric).

Five dimensions (each 1-5 Likert, as defined in the paper):
- professionalism    domain-terminology + logic
- accuracy           factual/policy correctness
- completeness       coverage of key aspects
- practicality       actionability
- language_quality   fluency, formatting, readability

To mitigate position bias, A/B responses are randomly swapped per case.
Outputs per-dimension means and overall delta vs. baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path
from statistics import mean

import httpx
from openai import AsyncOpenAI

from configs.settings import (
    MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    PROXY_SERVER,
)

DIMENSIONS = ["professionalism", "accuracy", "completeness", "practicality", "language_quality"]

JUDGE_PROMPT = """You are a professional evaluator for foreign-trade AI assistant responses.

Given one user query and two candidate responses A and B, rate each candidate
on the following five dimensions using a 1-5 Likert scale:

1. professionalism    - domain terminology accuracy and logical rigor
2. accuracy           - factual/policy/data correctness
3. completeness       - coverage of the key points the query implies
4. practicality       - actionability of recommendations
5. language_quality   - fluency, formatting, readability

Return only valid JSON:
{{"A": {{"professionalism": 1-5, "accuracy": 1-5, "completeness": 1-5, "practicality": 1-5, "language_quality": 1-5}},
  "B": {{"professionalism": 1-5, "accuracy": 1-5, "completeness": 1-5, "practicality": 1-5, "language_quality": 1-5}}}}

User query:
{query}

Response A:
{a}

Response B:
{b}
"""


async def _judge_pair(client: AsyncOpenAI, query: str, a: str, b: str) -> dict:
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(query=query, a=a, b=b)}],
        temperature=0.0,
    )
    try:
        return json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return {}


def _load_jsonl(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            out[record["case_id"]] = record
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-set", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--treatment", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cases = [json.loads(line) for line in Path(args.test_set).read_text().splitlines() if line.strip()]
    baseline = _load_jsonl(args.baseline)
    treatment = _load_jsonl(args.treatment)

    client_kwargs = {"api_key": OPENAI_API_KEY, "base_url": OPENAI_API_BASE}
    if PROXY_SERVER:
        client_kwargs["http_client"] = httpx.AsyncClient(proxy=PROXY_SERVER)
    client = AsyncOpenAI(**client_kwargs)

    scored: dict[str, list[float]] = {"baseline:" + d: [] for d in DIMENSIONS}
    scored.update({"treatment:" + d: [] for d in DIMENSIONS})

    for case in cases:
        cid = case["case_id"]
        if cid not in baseline or cid not in treatment:
            continue
        b_resp = baseline[cid].get("response", "")
        t_resp = treatment[cid].get("response", "")
        swap = random.random() < 0.5
        a, b = (t_resp, b_resp) if swap else (b_resp, t_resp)
        scores = await _judge_pair(client, case["query"], a, b)
        if not scores:
            continue
        # Unswap so "A" always refers to baseline, "B" to treatment
        baseline_scores = scores.get("B" if swap else "A", {})
        treatment_scores = scores.get("A" if swap else "B", {})
        for d in DIMENSIONS:
            if d in baseline_scores:
                scored["baseline:" + d].append(baseline_scores[d])
            if d in treatment_scores:
                scored["treatment:" + d].append(treatment_scores[d])
        print(f"{cid} scored")

    summary = {
        "baseline": {
            d: round(mean(scored["baseline:" + d]), 3) if scored["baseline:" + d] else None
            for d in DIMENSIONS
        },
        "treatment": {
            d: round(mean(scored["treatment:" + d]), 3) if scored["treatment:" + d] else None
            for d in DIMENSIONS
        },
    }
    summary["delta"] = {
        d: round(summary["treatment"][d] - summary["baseline"][d], 3)
        for d in DIMENSIONS
        if summary["baseline"][d] is not None and summary["treatment"][d] is not None
    }
    Path(args.output).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
