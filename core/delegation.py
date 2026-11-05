"""Shared task identity, live delivery and receipts for both delegation routes."""

import asyncio
import hashlib
import json
from contextlib import aclosing
from uuid import uuid4

from utils.logger import logger
from utils.tool_results import tool_observation

RECEIPT_TYPE = "sub_agent_receipt"
TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


def task_session_base(parent_session_id: str, task_id: str) -> str:
    """Hash complete identifiers so paths and long prefixes cannot collide."""
    digest = hashlib.sha256(json.dumps([parent_session_id, task_id]).encode()).hexdigest()[:32]
    return f"task_{digest}"


def automatic_task_id(skill_name: str) -> str:
    return "skill-" + hashlib.sha256(skill_name.encode()).hexdigest()[:16]


def new_task_id() -> str:
    return f"task-{uuid4().hex}"


async def stream_handoff(tool, params, skill, history):
    """Automatic handoff has the same receipts, including an interrupted thread."""
    call_id = f"handoff_{uuid4().hex}"
    record = {"role": "tool", "name": "sub_agent", "tool_call_id": call_id, "delivery_events": []}
    receipt = {
        "type": RECEIPT_TYPE,
        "owner": params.agent_type,
        "task_id": params.task_id,
        "status": "interrupted",
        "delivery_complete": False,
        "resume_agent": params.agent_type,
        "resume_task_id": params.task_id,
        "instruction": "Execution outcome is unconfirmed. Verify before retrying this task.",
    }
    record["content"] = json.dumps(receipt, ensure_ascii=False)
    history.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "sub_agent", "arguments": params.model_dump_json()},
                    }
                ],
            },
            record,
        ]
    )
    queue, done = asyncio.Queue(), object()

    async def execute():
        try:
            return await tool.delegate(params, skill=skill, emit=queue.put)
        finally:
            await queue.put(done)

    task = asyncio.create_task(execute())
    try:
        while (event := await queue.get()) is not done:
            record["delivery_events"].append(dict(event))
            yield event
        receipt = await task
        if receipt.get("status") == "failed" and not set(receipt.get("delivered_codes", [])) & {2, 8, 10, 12}:
            frame = {
                "code": 2,
                "content": "The delegated task failed. Please try again.",
                "task_id": params.task_id,
                "source_agent": params.agent_type,
                "delivery_owner": params.agent_type,
            }
            record["delivery_events"].append(frame)
            yield frame
    except Exception as exc:
        logger.exception("Sub-agent handoff failed")
        receipt = {**receipt, "status": "failed", "failures": [str(exc)]}
        frame = {
            "code": 2,
            "content": "The delegated task failed. Please try again.",
            "task_id": params.task_id,
            "source_agent": params.agent_type,
        }
        record["delivery_events"].append(frame)
        yield frame
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        record["content"] = json.dumps(receipt, ensure_ascii=False)


async def run_delegation(runner, task, context, task_id, emit=None):
    """Keep all mutable delivery state local to one invocation."""
    codes, produced = set(), {}
    delivered_frames, preview_frames, questions = [], [], []
    tool_outcomes = []
    text_parts, failures = [], []
    asked = False

    async def deliver(event):
        if event.get("code") in (3, 6):
            return
        frame = dict(event)
        frame.setdefault("task_id", task_id)
        frame.setdefault("source_agent", runner.config.name)
        frame.setdefault("delivery_owner", runner.config.name)
        codes.add(frame.get("code"))
        if frame.get("code") == 2:
            delivered_frames.append(str(frame.get("content", "")))
        elif frame.get("code") == 8:
            preview_frames.append(str(frame.get("content", "")))
        if frame.get("code") == 5:
            try:
                data = json.loads(frame.get("content") or "{}")
            except (ValueError, TypeError):
                data = {}
            if isinstance(data, dict):
                observation = data if "status" in data else tool_observation(data.get("result"))
                tool_name = data.get("name") or "unknown"
                outcome = {
                    "tool": tool_name,
                    "call_id": data.get("id", ""),
                    "status": observation.get("status", "unknown"),
                }
                if observation.get("is_error"):
                    outcome["error"] = str(observation.get("error") or "Tool reported failure")[:1000]
                tool_outcomes.append(outcome)
                for artifact in observation.get("artifacts", []):
                    if isinstance(artifact, dict) and artifact.get("url"):
                        produced[artifact["url"]] = dict(artifact, tool=tool_name, call_id=data.get("id", ""))
        if emit:
            await emit(frame)

    try:
        async with aclosing(runner.run_stream(task, context)) as events:
            async for event in events:
                if event.get("code") == 2:
                    text_parts.append(str(event.get("content", "")))
                    await deliver(event)
                else:
                    asked = asked or event.get("code") == 10
                    if event.get("code") == 10:
                        try:
                            question = json.loads(event.get("content", ""))
                        except (ValueError, TypeError):
                            question = {"content": str(event.get("content", ""))[:1000]}
                        questions.append(question)
                    if event.get("code") == 0:
                        failures.append(str(event.get("content", "")))
                    await deliver(event)
    except Exception as exc:
        failures.append(str(exc))
    if getattr(runner, "failure", None):
        failures.append(runner.failure)
    summary = "".join(text_parts).strip()
    delivered_text = "".join(delivered_frames) + "\n" + "\n".join(preview_frames)
    artifacts = [
        dict(artifact, delivery_status="delivered" if url in delivered_text else "produced")
        for url, artifact in produced.items()
    ]
    references = [artifact["url"] for artifact in artifacts if artifact["delivery_status"] == "delivered"]
    visible_delivery = bool(codes & {2, 8, 10, 12})
    status = "failed" if failures or not visible_delivery else "waiting_user" if asked else "delivered"
    if not visible_delivery and not failures:
        failures.append("Sub-agent ended without a user-visible delivery.")
    receipt = {
        "type": RECEIPT_TYPE,
        "task_id": task_id,
        "owner": runner.config.name,
        "session_id": runner.session_id,
        "status": status,
        "delivery_complete": visible_delivery and not failures,
        "delivered_codes": sorted(codes),
        "artifact_refs": references[:20],
        "artifacts": artifacts[:100],
        "tool_outcomes": tool_outcomes[-100:],
        "summary": summary[:1000] + ("\n...\n" + summary[-500:] if len(summary) > 1500 else summary[1000:]),
        "agent_instruction": (
            "The child output was streamed directly to the user. Do not repeat it or regenerate delivered artifacts. "
            "Continue any other independent tasks. To revise or resume this task, delegate to the same owner "
            "with this exact task_id. waiting_user means the question was delivered, not that the task finished. "
            "Skill proposals require user confirmation before saving. Report failures accurately."
        ),
    }
    if asked:
        receipt.update(resume_agent=runner.config.name, resume_task_id=task_id, pending_questions=questions)
    if failures:
        receipt["failures"] = failures
    return receipt
