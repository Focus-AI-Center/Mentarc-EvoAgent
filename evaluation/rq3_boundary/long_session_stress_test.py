"""RQ3: Capability boundary — long-session stress test.

Runs a single session for up to --max-turns conversation rounds, driving the
agent with a scripted prompt generator. Tracks per-turn latency, compression
triggers, and error count; reports the totals when done (or on first error).

Usage:
    python evaluation/rq3_boundary/long_session_stress_test.py \
        --max-turns 420 --user-id stress_user
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

from core.agent import EvoAgent

PROMPTS = [
    "帮我整理一下目前的客户清单",
    "写一封询盘回复邮件，重点说明交付周期",
    "给这个产品写 3 个卖点",
    "把上一轮的卖点翻译成英文",
    "列出下周需要跟进的事项",
    "帮我分析一下这个市场近期的趋势",
    "写一封跟进邮件，语气专业一些",
    "给一个报价建议，考虑运费和关税",
    "复盘本周的客户沟通情况",
    "整理 FAQ 的前 5 个问题",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-turns", type=int, default=420)
    parser.add_argument("--user-id", default=f"stress_{uuid.uuid4().hex[:6]}")
    parser.add_argument("--output", default="results/long_session.json")
    args = parser.parse_args()

    session_id = f"stress_{int(time.time())}"
    agent = EvoAgent(user_id=args.user_id, session_id=session_id)

    latencies: list[int] = []
    compression_events = 0
    errors: list[dict] = []

    # Hook into the compressor to count triggers
    original_compress = agent._compressor.compress_history

    async def counting_compress(history):
        nonlocal compression_events
        compression_events += 1
        return await original_compress(history)

    agent._compressor.compress_history = counting_compress

    for turn in range(1, args.max_turns + 1):
        query = PROMPTS[turn % len(PROMPTS)]
        t0 = time.monotonic()
        try:
            async for _ in agent.run_stream(query):
                pass
            latencies.append(int((time.monotonic() - t0) * 1000))
        except Exception as e:
            errors.append({"turn": turn, "error": str(e)})
            print(f"turn {turn}: ERROR {e}")
            break
        if turn % 20 == 0:
            print(f"turn {turn}: latency={latencies[-1]}ms compactions={compression_events}")

    report = {
        "user_id": args.user_id,
        "session_id": session_id,
        "completed_turns": len(latencies),
        "compression_events": compression_events,
        "errors": errors,
        "latency_ms_avg": sum(latencies) // len(latencies) if latencies else 0,
        "latency_ms_first_40": sum(latencies[:40]) // max(1, min(40, len(latencies))),
        "latency_ms_after_120": (
            sum(latencies[120:]) // max(1, len(latencies) - 120) if len(latencies) > 120 else 0
        ),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    import json

    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
