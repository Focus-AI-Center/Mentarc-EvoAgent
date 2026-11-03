"""Snapshot current user material separately from historical references and model notes."""

import json
from copy import deepcopy

from utils.context_recovery import collect_assets


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
