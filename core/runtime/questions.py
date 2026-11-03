"""Question identity, saved answer routing and SDK turn boundaries."""

import json

from agents.agent import ToolsToFinalOutputResult


class QuestionRoutingError(ValueError):
    """An answer cannot be associated with a pending question in this session."""


def question_tool_behavior():
    """Finish only the run that asked, after its current tool batch completes."""
    return _finish_on_question


def _finish_on_question(_context, results):
    for result in results:
        if result.tool.name != "ask_user_question":
            continue
        try:
            question = json.loads(result.output) if isinstance(result.output, str) else result.output
        except (ValueError, TypeError):
            continue
        if isinstance(question, dict) and question.get("type") == "ask_user" and question.get("question_id"):
            return ToolsToFinalOutputResult(is_final_output=True, final_output=result.output)
    return ToolsToFinalOutputResult(is_final_output=False)


def pending_questions(history):
    """Resolve identities from saved tool facts, never from submitted routing claims."""
    pending = {}
    for message in history:
        records = list(message.get("pending_questions", []))
        receipts = [] if "pending_questions" in message else list(message.get("delegation_receipts", []))
        if message.get("role") == "tool":
            try:
                data = json.loads(message.get("content") or "{}")
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                if data.get("type") == "ask_user":
                    records.append(data)
                elif data.get("type") == "sub_agent_receipt":
                    receipts.append(data)
        for receipt in receipts:
            owner, task_id = receipt.get("owner"), receipt.get("task_id")
            if (
                owner
                and task_id
                and (
                    receipt.get("status") in ("delivered", "waiting_user") or receipt.get("pending_questions")
                )
            ):
                for question_id, previous in list(pending.items()):
                    if previous.get("agent_type") == owner and previous.get("task_id") == task_id:
                        pending.pop(question_id)
            if not receipt.get("pending_questions"):
                continue
            for question in receipt.get("pending_questions", []):
                records.append(
                    dict(question, agent_type=receipt.get("owner"), task_id=receipt.get("task_id"))
                )
        for question in records:
            if isinstance(question, dict) and question.get("question_id"):
                pending[question["question_id"]] = question
        if message.get("role") == "user":
            answer = message.get("answer_to") or {}
            pending.pop(answer.get("question_id"), None)
    return pending


def resolve_answer(history, submitted):
    if not submitted:
        return None
    question = pending_questions(history).get(submitted.get("question_id"))
    if question is None:
        raise QuestionRoutingError("This question is no longer pending in the current session.")
    for key in ("agent_type", "task_id"):
        if submitted.get(key) and submitted[key] != question.get(key):
            raise QuestionRoutingError("The answer route does not match the saved question.")
    return question


def user_message(text, answer=None, attachments=None):
    message = {"role": "user", "content": text}
    if answer:
        message["answer_to"] = {
            key: answer[key]
            for key in ("question_id", "question", "agent_type", "task_id")
            if answer.get(key)
        }
    if attachments and any(attachments.values()):
        message["attachments"] = attachments
    return message


def model_user_content(message):
    content = message.get("content") or ""
    answer = message.get("answer_to")
    if message.get("role") != "user" or not isinstance(answer, dict) or not answer.get("question_id"):
        return content
    identity = {
        key: answer[key] for key in ("question_id", "question", "agent_type", "task_id") if answer.get(key)
    }
    return "[Answer to saved question]\n" + json.dumps(identity, ensure_ascii=False) + "\n\n" + str(content)
