"""Collect delegated child work for the parent without user-facing child output."""

import asyncio
import json
from contextlib import aclosing

from core.runtime.interruption import _protected
from core.runtime.workers import WorkerJob
from utils.tool_results import tool_observation


async def run_internal_delegation(runner, task, context, task_id, emit, *, workspace, output_dir):
    job = WorkerJob(
        task_id,
        runner.config.name,
        output_dir,
        owner=runner.config.name,
        receipt_type="sub_agent_receipt",
        session_id=runner.session_id,
        state="running",
        progress="Executing",
        request={"task": task},
    )
    failures, questions, artifacts = [], [], {}

    async def progress():
        if emit:
            await emit(job.progress_event())

    try:
        await progress()
        async with aclosing(runner.run_stream(task, context)) as events:
            async for event in events:
                code = event.get("code")
                if code == 2:
                    job.text.append(str(event.get("content", "")))
                    continue
                if code == 0:
                    failures.append(str(event.get("content", "")))
                    continue
                if code not in (4, 5, 10, 12):
                    continue
                try:
                    data = json.loads(event.get("content") or "{}")
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if code == 10:
                    questions.append(data)
                elif code == 12:
                    failures.append(
                        "The child attempted a confirmation card. The main agent must create proposals."
                    )
                elif code == 4:
                    job.tool_calls.append(
                        {"tool": data.get("name"), "call_id": data.get("id"), "status": "running"}
                    )
                    job.tool_calls[:] = job.tool_calls[-100:]
                    job.progress = "Using " + str(data.get("name", "tool"))
                    await progress()
                else:
                    outcome = tool_observation(data if "status" in data else data.get("result"))
                    job.tool_outcomes.append({"tool": data.get("name"), "call_id": data.get("id"), **outcome})
                    for call in reversed(job.tool_calls):
                        if call["call_id"] == data.get("id"):
                            call["status"] = "failed" if outcome["is_error"] else "completed"
                            break
                    for artifact in outcome.get("artifacts", []):
                        if artifact.get("url"):
                            artifacts[artifact["url"]] = artifact | {"delivery_status": "produced"}
                    await progress()
        if getattr(runner, "failure", None):
            failures.append(runner.failure)
        run_status = getattr(runner, "run_status", "completed")
        if run_status != "completed":
            job.state = run_status
        elif failures:
            job.state = "failed"
        else:
            job.state = "needs_input" if questions else "completed"
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
    except Exception as exc:
        job.state = "failed"
        failures.append(str(exc))
    finally:
        await _protected(job.persist(workspace))
        job.finished = True
        job.receipt.update(
            delivery_complete=False,
            delivered_codes=[],
            artifact_refs=[],
            artifacts=list(artifacts.values())[-100:],
            agent_instruction=(
                "Internal child result; no child answer, question or confirmation card was delivered to the user. "
                "Read the result files, verify the findings, then compose your response. Ask any missing "
                "questions and present skill proposals yourself. Reuse owner and task_id for follow-ups."
            ),
        )
        if questions:
            job.receipt["questions_for_parent"] = questions
        if failures:
            job.receipt["failures"] = failures
        await progress()
    return job.receipt
