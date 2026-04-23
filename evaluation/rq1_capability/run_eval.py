"""RQ1: Does EvoAgent improve a backbone LLM on domain dialogues?

Runs each test case in two modes:
- `baseline`:   raw LLM call, single user message, no skills/memory
- `evoagent`:   full EvoAgent loop (skill matching + memory + compression)

Outputs a JSONL file with one record per test case:
{case_id, mode, model, response, latency_ms, error?}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from configs.settings import (
    MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    PROXY_SERVER,
)
from core.agent import EvoAgent


async def run_baseline(client: AsyncOpenAI, query: str) -> dict:
    t0 = time.monotonic()
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": query}],
            temperature=0.0,
        )
        return {
            "response": resp.choices[0].message.content or "",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:
        return {
            "response": "",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": str(e),
        }


async def run_evoagent(case: dict) -> dict:
    t0 = time.monotonic()
    agent = EvoAgent(
        user_id=case["user_id"],
        session_id=case["session_id"],
    )
    try:
        parts: list[str] = []
        async for event in agent.run_stream(case["query"]):
            if event.get("code") == 2:
                parts.append(str(event.get("content", "")))
        return {
            "response": "".join(parts),
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:
        return {
            "response": "",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": str(e),
        }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-set", required=True, help="Path to test_cases.jsonl")
    parser.add_argument("--mode", choices=["baseline", "evoagent"], required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cases = [json.loads(line) for line in Path(args.test_set).read_text().splitlines() if line.strip()]

    client_kwargs = {"api_key": OPENAI_API_KEY, "base_url": OPENAI_API_BASE}
    if PROXY_SERVER:
        client_kwargs["http_client"] = httpx.AsyncClient(proxy=PROXY_SERVER)
    baseline_client = AsyncOpenAI(**client_kwargs) if args.mode == "baseline" else None

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for case in cases:
            if args.mode == "baseline":
                result = await run_baseline(baseline_client, case["query"])
            else:
                result = await run_evoagent(case)
            record = {"case_id": case["case_id"], "mode": args.mode, "model": MODEL, **result}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{case['case_id']}: {result['latency_ms']}ms")


if __name__ == "__main__":
    asyncio.run(main())
