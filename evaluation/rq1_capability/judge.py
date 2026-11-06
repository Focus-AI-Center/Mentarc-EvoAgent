"""LLM-as-Judge scorer (5-dimension rubric) for two result files from ``run_eval.py``.

Five dimensions, each 1-5 (as defined in the paper): professionalism (domain terminology and logic),
accuracy (factual/policy correctness), completeness (coverage of key aspects), practicality
(actionability), language_quality (fluency, formatting, readability).

How it judges
- The two responses of a (case, repeat) are shown side by side in a seeded random order, together
  with the recent dialogue, and the scores are mapped back to the conditions. ``--both-orders``
  judges every pair twice with the order swapped, which removes position bias.
- The judge model must be named (``--judge-model``) and must differ from the model that produced
  the responses, because a model tends to prefer its own style (``--allow-same-model`` overrides).
- Pairs that cannot be scored (missing response, failed run, unparsable verdict) are counted by
  reason in the output; nothing is dropped silently.

Output (JSON): per-dimension means, differences with a 95% bootstrap interval over cases (repeats
of a case are averaged first), the number of pairs scored, and ``per_case`` scores. The summary
holds aggregates only; ``per_case`` is derived from your data, so keep it private.

Every call costs money: use ``--dry-run`` to see the plan; a real run needs ``--yes``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from configs.settings import OPENAI_API_BASE, OPENAI_API_KEY, PROXY_SERVER  # noqa: E402

DIMENSIONS = ["professionalism", "accuracy", "completeness", "practicality", "language_quality"]

JUDGE_PROMPT = """You are a professional evaluator for foreign-trade AI assistant responses.

Given the conversation so far, the user's final query and two candidate responses A and B,
rate each candidate on the following five dimensions using a 1-5 Likert scale:

1. professionalism    - domain terminology accuracy and logical rigor
2. accuracy           - factual/policy/data correctness
3. completeness       - coverage of the key points the query implies
4. practicality       - actionability of recommendations
5. language_quality   - fluency, formatting, readability

Return only valid JSON:
{{"A": {{"professionalism": 1-5, "accuracy": 1-5, "completeness": 1-5, "practicality": 1-5, "language_quality": 1-5}},
  "B": {{"professionalism": 1-5, "accuracy": 1-5, "completeness": 1-5, "practicality": 1-5, "language_quality": 1-5}}}}

Conversation so far (most recent turns):
{context}

User query:
{query}

Response A:
{a}

Response B:
{b}
"""


def format_context(turns: list[dict], count: int, limit: int = 1500) -> str:
    if count <= 0 or not turns:
        return "(none)"
    lines = []
    for turn in turns[-count:]:
        text = turn["content"]
        lines.append(
            f"{'User' if turn['role'] == 'user' else 'Assistant'}: {text[:limit]}{'...' if len(text) > limit else ''}"
        )
    return "\n".join(lines)


def parse_scores(text: str):
    """``{"A": {...}, "B": {...}}`` with scores in 1-5, or ``None`` if the verdict is unusable."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    parsed = {}
    for side in ("A", "B"):
        block = data.get(side) if isinstance(data, dict) else None
        if not isinstance(block, dict):
            return None
        for dimension in DIMENSIONS:
            value = block.get(dimension)
            if isinstance(value, bool) or not isinstance(value, int | float) or not 1 <= value <= 5:
                return None
        parsed[side] = {d: block[d] for d in DIMENSIONS}
    return parsed


def orders_for(seed: int, case_id: str, repeat: int, both_orders: bool) -> list[bool]:
    """Presentation orders to use; ``True`` means the treatment is shown as A."""
    return [False, True] if both_orders else [random.Random(f"{seed}:{case_id}:{repeat}").random() < 0.5]


def bootstrap_ci(values: list[float], n_boot: int = 2000, seed: int = 0):
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


async def ask(client, judge_model: str, prompt: str, temperature: float | None):
    kwargs = {"model": judge_model, "messages": [{"role": "user", "content": prompt}]}
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as error:  # noqa: BLE001 - some models reject a temperature
        if "temperature" not in kwargs or "temperature" not in str(error).lower():
            raise
        kwargs.pop("temperature")
        response = await client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def usable(row) -> bool:
    return bool(row) and not row.get("error") and bool((row.get("response") or "").strip())


async def judge_all(
    cases,
    baseline_rows,
    treatment_rows,
    *,
    client,
    judge_model,
    seed=0,
    both_orders=False,
    context_turns=6,
    concurrency=4,
    temperature=0.0,
):
    """One record per judgment: scores per condition, or the reason the pair was not scored."""
    by_case = {c["case_id"]: c for c in cases}
    base = {(r["case_id"], r.get("repeat", 0)): r for r in baseline_rows}
    treat = {(r["case_id"], r.get("repeat", 0)): r for r in treatment_rows}
    repeats = sorted({key[1] for key in [*base, *treat]}) or [0]
    keys = sorted(set(base) | set(treat) | {(case_id, repeat) for case_id in by_case for repeat in repeats})
    semaphore = asyncio.Semaphore(concurrency)
    records, jobs = [], []

    async def one(key, swapped):
        case_id, repeat = key
        a, b = (treat[key], base[key]) if swapped else (base[key], treat[key])
        prompt = JUDGE_PROMPT.format(
            context=format_context(by_case[case_id]["turns"], context_turns),
            query=by_case[case_id]["query"],
            a=a["response"],
            b=b["response"],
        )
        record = {"case_id": case_id, "repeat": repeat, "swapped": swapped}
        async with semaphore:
            try:
                parsed = parse_scores(await ask(client, judge_model, prompt, temperature))
            except Exception as exc:  # noqa: BLE001
                return {**record, "reason": f"judge call failed: {type(exc).__name__}"}
        if parsed is None:
            return {**record, "reason": "unparsable verdict"}
        first, second = (parsed["B"], parsed["A"]) if swapped else (parsed["A"], parsed["B"])
        return {**record, "baseline": first, "treatment": second}

    for key in keys:
        case_id, repeat = key
        if case_id not in by_case:
            records.append({"case_id": case_id, "repeat": repeat, "reason": "case not in test set"})
        elif not (usable(base.get(key)) and usable(treat.get(key))):
            missing = "baseline" if not usable(base.get(key)) else "treatment"
            records.append(
                {"case_id": case_id, "repeat": repeat, "reason": f"no usable response ({missing})"}
            )
        else:
            jobs += [one(key, swapped) for swapped in orders_for(seed, case_id, repeat, both_orders)]
    return records + list(await asyncio.gather(*jobs))


def summarize(records: list[dict], judge_model: str, n_boot: int = 2000, seed: int = 0) -> dict:
    scored = [r for r in records if "baseline" in r]
    per_pair = defaultdict(list)
    for r in scored:
        per_pair[(r["case_id"], r["repeat"])].append(r)
    per_case = defaultdict(list)
    for (case_id, _), rows in per_pair.items():
        per_case[case_id].append(
            {
                side: {d: mean(r[side][d] for r in rows) for d in DIMENSIONS}
                for side in ("baseline", "treatment")
            }
        )
    cases = {
        cid: {
            side: {d: mean(rep[side][d] for rep in reps) for d in DIMENSIONS}
            for side in ("baseline", "treatment")
        }
        for cid, reps in per_case.items()
    }
    summary = {
        "judge_model": judge_model,
        "judgments": len(records),
        "cases_scored": len(cases),
        "not_scored": dict(Counter(r["reason"] for r in records if "reason" in r)),
    }
    if cases:
        rows = list(cases.values())
        for side in ("baseline", "treatment"):
            summary[side] = {d: round(mean(c[side][d] for c in rows), 3) for d in DIMENSIONS}
        deltas = {d: [c["treatment"][d] - c["baseline"][d] for c in rows] for d in DIMENSIONS}
        deltas["overall"] = [mean(c["treatment"][d] - c["baseline"][d] for d in DIMENSIONS) for c in rows]
        summary["delta"] = {d: round(mean(v), 3) for d, v in deltas.items()}
        summary["delta_ci95"] = {
            d: [round(x, 3) for x in bootstrap_ci(v, n_boot, seed)] for d, v in deltas.items()
        }
        summary["overall_wins_ties_losses"] = [
            sum(x > 0 for x in deltas["overall"]),
            sum(x == 0 for x in deltas["overall"]),
            sum(x < 0 for x in deltas["overall"]),
        ]
    return {"summary": summary, "per_case": cases}


def load_jsonl(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--test-set", required=True)
    parser.add_argument("--baseline", required=True, help="results of the baseline condition")
    parser.add_argument(
        "--treatment", required=True, help="results of the condition being tested (e.g. evoagent)"
    )
    parser.add_argument(
        "--judge-model", required=True, help="must differ from the model that produced the responses"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--both-orders", action="store_true")
    parser.add_argument("--context-turns", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--temperature", type=float, default=0.0, help="dropped if the judge model rejects it"
    )
    parser.add_argument("--allow-same-model", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirm that this run may spend money")
    args = parser.parse_args()

    from evaluation.rq1_capability.run_eval import load_cases

    cases = load_cases(args.test_set)
    baseline_rows, treatment_rows = load_jsonl(args.baseline), load_jsonl(args.treatment)
    produced_by = {r.get("model") for r in [*baseline_rows, *treatment_rows]}
    if args.judge_model in produced_by and not args.allow_same_model:
        sys.exit(
            f"The judge {args.judge_model!r} also produced responses being judged; choose another model (or --allow-same-model)."
        )
    pairs = len(
        {(r["case_id"], r.get("repeat", 0)) for r in baseline_rows}
        & {(r["case_id"], r.get("repeat", 0)) for r in treatment_rows}
    )
    print(
        json.dumps(
            {
                "pairs": pairs,
                "judge_calls_at_most": pairs * (2 if args.both_orders else 1),
                "judge_model": args.judge_model,
            }
        )
    )
    if args.dry_run:
        return
    if not args.yes:
        sys.exit("This run calls a paid API: re-run with --yes (or --dry-run to only see the plan).")

    kwargs = {"api_key": OPENAI_API_KEY, "base_url": OPENAI_API_BASE}
    if PROXY_SERVER:
        kwargs["http_client"] = httpx.AsyncClient(proxy=PROXY_SERVER)
    records = asyncio.run(
        judge_all(
            cases,
            baseline_rows,
            treatment_rows,
            client=AsyncOpenAI(**kwargs),
            judge_model=args.judge_model,
            seed=args.seed,
            both_orders=args.both_orders,
            context_turns=args.context_turns,
            concurrency=args.concurrency,
            temperature=args.temperature,
        )
    )
    result = summarize(records, args.judge_model, seed=args.seed)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
