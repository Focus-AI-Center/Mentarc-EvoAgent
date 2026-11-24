"""Snapshot current user material separately from historical references and model notes."""

import json
from copy import deepcopy

from utils.context_recovery import collect_assets

DELEGATED_AGENT_INSTRUCTION = (
    "Only the main agent communicates with the user in this delegation chain. "
    "You are an internal sub-agent: return your findings, output file paths and missing information "
    "to the main agent. Do not address or question the user, deliver user-facing content, or present "
    "confirmation cards. The main agent verifies your work and owns user questions, final delivery "
    "and skill proposals. Prepare candidate skill changes for the main agent instead of proposing them."
)

MAIN_AGENT_DELEGATION_INSTRUCTION = (
    "Only you communicate with the user in a delegation chain. Sub-agents and workers return internal "
    "material to you; their execution is not user delivery. Read and verify their result files, handle "
    "missing information, and compose the user-facing response yourself. You own questions and "
    "confirmation cards. Progress cards describe execution only."
)


def request_input(text, images=(), files=()):
    if images:
        text += "\n\n[User-uploaded images]\n" + "\n".join(images)
    if files:
        text += "\n\n[User-uploaded files]\n" + json.dumps(files, ensure_ascii=False)
    return text


def delegation_snapshot(user_input, history, attachments=None):
    attachments = attachments or {}
    return {
        "current_user_input": user_input,
        "current_attachments": deepcopy({key: attachments.get(key, []) for key in ("images", "files")}),
        "prior_assets": collect_assets(history)[-100:],
    }


def render_delegation_context(snapshot, notes=""):
    """Keep reference material in user-role input; it is not a system instruction."""
    if not snapshot:
        return notes
    return (
        "Delegation source material (current input and prior resources are separate; "
        "prior results are not evidence of work completed for this new task):\n"
        + json.dumps(snapshot, ensure_ascii=False)
        + ("\n\nParent model notes:\n" + notes if notes else "")
    )


def delegated_input(task, context):
    return task + ("\n\n[Delegation source material]\n" + context if context else "")
