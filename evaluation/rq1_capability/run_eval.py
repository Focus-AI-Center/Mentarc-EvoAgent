"""Generate responses for the evaluation: one condition, one model.

--mode
  baseline          the model sees only the final query (the protocol used for the paper)
  baseline_context  the model also sees the persona prompt, the user's profile and memory and the
                    dialogue so far, but has no tools, skills, matching or compression
  evoagent          the full agent loop, started from the same dialogue

Comparing ``baseline_context`` with ``evoagent`` isolates what the harness adds beyond the
information it is given. To compare models, repeat the run with another ``--model``.

Privacy: external plugins (web search, page fetch, ...) are off by default, so case text only goes
to the model API; every run uses a throw-away workspace that is deleted afterwards. Keep the test
set and the outputs out of version control.

Every call costs money: use ``--dry-run`` to see the plan; a real run needs ``--yes``.

Test cases (JSONL, one object per line). Only ``query`` is required:
  {"case_id": "fc_001", "query": "Draft an initial reply email", "intent": "inquiry_response",
   "turns": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
   "profile": "text of USER.md", "memory": "text of MEMORY.md", "skills": {"name": "SKILL.md text"}}

Output (JSONL, one record per case and repeat):
  {case_id, repeat, mode, model, response, latency_ms, usage, error}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from configs.settings import MODEL, OPENAI_API_BASE, OPENAI_API_KEY, PROXY_SERVER  # noqa: E402

MODES = ("baseline", "baseline_context", "evoagent")
AGENT_ERROR_PREFIX = "Agent error:"  # what core.agent puts in the reply when a turn fails
USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")


def load_cases(path: str, limit: int | None = None) -> list[dict]:
    cases = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError(f"line {number}: 'query' must be a non-empty string")
        for turn in case.get("turns", []):
            if turn.get("role") not in ("user", "assistant") or not isinstance(turn.get("content"), str):
                raise ValueError(f"line {number}: every turn needs role user/assistant and a string content")
        case.setdefault("case_id", f"case_{len(cases):04d}")
        case.setdefault("turns", [])
        cases.append(case)
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("case_id values must be unique")
    return cases[:limit] if limit else cases


def seed_workspace(case: dict, root: str):
    """An isolated workspace holding this case's dialogue, profile, memory and skills."""
    from utils.storage_backend.localfs import LocalFSBackend
    from utils.workspace import Workspace

    safe = lambda text: re.sub(r"[^A-Za-z0-9_.-]", "_", text).strip("._") or "case"  # noqa: E731
    workspace = Workspace(
        safe(str(case.get("user_id") or f"eval_{case['case_id']}")), backend=LocalFSBackend(root=Path(root))
    )
    session_id = safe(f"eval_{case['case_id']}")
    turns = [{"role": t["role"], "content": t["content"]} for t in case["turns"]]
    workspace.history_save(session_id, {"conversation": turns})
    if case.get("profile"):
        workspace.write_prompt("USER", case["profile"])
    if case.get("memory"):
        workspace.write_prompt("MEMORY", case["memory"])
    for name, text in (case.get("skills") or {}).items():
        workspace.skill_set(name, text)
    return workspace, session_id


def build_messages(mode: str, case: dict, workspace=None) -> list[dict]:
    query = {"role": "user", "content": case["query"]}
    if mode == "baseline":
        return [query]
    from core.runtime.history import _prompt_body

    parts = [_prompt_body(workspace.read_prompt("SOUL"))]
    for title, name in (("User profile", "USER"), ("Memory", "MEMORY")):
        body = _prompt_body(workspace.read_prompt(name))
        if body:
            parts.append(f"## {title}\n{body}")
    turns = [{"role": t["role"], "content": t["content"]} for t in case["turns"]]
    return [{"role": "system", "content": "\n\n".join(parts)}, *turns, query]


def usage_of(response) -> dict:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    reasoning = getattr(usage, "completion_tokens_details", None)
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cached_input_tokens": int(getattr(details, "cached_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(reasoning, "reasoning_tokens", 0) or 0),
    }


_NO_TEMPERATURE: set[str] = set()


async def chat(client, model: str, messages: list[dict], temperature: float | None):
    """One chat completion; retried once without ``temperature`` if the model rejects it."""
    kwargs = {"model": model, "messages": messages}
    if temperature is not None and model not in _NO_TEMPERATURE:
        kwargs["temperature"] = temperature
    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as error:  # noqa: BLE001 - gateways word this error differently
        if "temperature" not in kwargs or "temperature" not in str(error).lower():
            raise
        _NO_TEMPERATURE.add(model)
        kwargs.pop("temperature")
        response = await client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or "", usage_of(response)


async def run_case(case, mode, model, client, temperature, plugins):
    with tempfile.TemporaryDirectory(prefix="evoagent_eval_") as tmp:
        if mode != "evoagent":
            workspace = seed_workspace(case, tmp)[0] if mode == "baseline_context" else None
            return await chat(client, model, build_messages(mode, case, workspace), temperature)
        from core.agent import EvoAgent

        workspace, session_id = seed_workspace(case, tmp)
        agent = EvoAgent(
            user_id=workspace.user_id,
            session_id=session_id,
            plugins=plugins,
            model=model,
            workspace=workspace,
        )
        parts, usage = [], {}
        async for event in agent.run_stream(case["query"]):
            if event.get("code") == 2:
                parts.append(str(event.get("content", "")))
            elif event.get("code") == 6:
                total = json.loads(event["content"]).get("usage") or {}
                usage = {field: total.get(field, 0) for field in USAGE_FIELDS}
        return "".join(parts), usage


async def generate(
    cases, *, mode, model, repeats=1, plugins=(), concurrency=4, temperature=0.5, client=None, on_result=None
):
    """Answer every (case, repeat) once. Failures are recorded, not raised."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case, repeat):
        async with semaphore:
            started = time.monotonic()
            error = None
            try:
                text, usage = await run_case(case, mode, model, client, temperature, list(plugins))
                if text.startswith(AGENT_ERROR_PREFIX):
                    error = text[:300]
            except Exception as exc:  # noqa: BLE001 - the failure is part of the result
                text, usage, error = "", {}, f"{type(exc).__name__}: {str(exc)[:300]}"
            return {
                "case_id": case["case_id"],
                "repeat": repeat,
                "mode": mode,
                "model": model,
                "response": text,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "usage": usage,
                "error": error,
            }

    records = []
    for finished in asyncio.as_completed([one(c, r) for r in range(repeats) for c in cases]):
        record = await finished
        records.append(record)
        if on_result:
            on_result(record)
    return sorted(records, key=lambda r: (r["repeat"], r["case_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--test-set", required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--model", default=MODEL, help="model under test (default: MODEL from settings)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None, help="only the first N cases (pilot run)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--temperature", type=float, default=0.5, help="plain calls only; dropped if the model rejects it"
    )
    parser.add_argument(
        "--plugins", default="", help="comma-separated external plugins to enable (default: none)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirm that this run may spend money")
    args = parser.parse_args()

    cases = load_cases(args.test_set, args.limit)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "repeats": args.repeats,
                "model_calls_at_least": len(cases) * args.repeats,
                "mode": args.mode,
                "model": args.model,
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
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:

        def save(record):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            print(
                f"{record['case_id']}#{record['repeat']}: {record['latency_ms']}ms{' ERROR' if record['error'] else ''}"
            )

        asyncio.run(
            generate(
                cases,
                mode=args.mode,
                model=args.model,
                repeats=args.repeats,
                concurrency=args.concurrency,
                temperature=args.temperature,
                plugins=[p.strip() for p in args.plugins.split(",") if p.strip()],
                client=AsyncOpenAI(**kwargs),
                on_result=save,
            )
        )


if __name__ == "__main__":
    main()
