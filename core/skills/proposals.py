"""Staged skill changes: an agent proposes, the user confirms, then the change is written.

Agents never write skills. ``stage`` validates a change and stores it under the user's
workspace; only ``commit`` (called from the confirmation endpoint) applies it, through
the same ``SkillManager`` write paths used for manual edits. A proposal is addressed by
id, so what the user confirmed is exactly what was staged.

Three operations are staged:

- ``create``: a new skill. Fails if the name already exists.
- ``update``: new content for an existing skill. Replaces the supplied fields and adds
  or replaces the supplied references; references not mentioned are kept.
- ``patch``: replace one exact fragment of the instructions or of one reference.

``update`` records the content it was based on and is rejected at confirmation if the
skill changed since, so a confirmation can never overwrite a newer edit. ``patch`` needs
no such record because its exact-once match and conditional write detect any change that
matters. The instructions stored for create/update already contain the reference links
that saving appends, so the preview is the text that will be saved.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.skills import Skill, skill_from_md
from core.skills.manager import SkillManager
from utils.skill_patching import patch_skill_text
from utils.skill_references import (
    normalize_reference_path,
    validate_reference_set,
    with_reference_links,
)
from utils.tool_names import canonical_tool_name, normalize_tool_references
from utils.workspace import _validate_segment

PROPOSAL_TTL = timedelta(days=7)
MAX_PENDING_PER_USER = 50
OPERATIONS = ("create", "update", "patch")
_EDITABLE = ("description", "triggers", "instructions", "required_tools", "tags")


class ProposalError(ValueError):
    """The proposal itself is invalid; the message tells the agent how to fix it."""


class ProposalNotFound(LookupError):
    """No pending proposal with this id (never staged, rejected, confirmed or expired)."""


class ProposalConflict(RuntimeError):
    """The skill changed after the proposal was staged; propose again from its current text."""


def _now() -> datetime:
    return datetime.now(UTC)


def _text_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def main_fingerprint(*, description, triggers, instructions, required_tools, tags) -> str:
    """Digest of the editable main-document content, ignoring usage counters and timestamps.

    Both sides go through the same normalization as loading a skill from disk, so a value
    computed from a proposal equals the one computed from the stored document.
    """
    return _text_digest(
        {
            "description": description or "",
            "triggers": list(triggers or []),
            "instructions": normalize_tool_references((instructions or "").strip()),
            "required_tools": [canonical_tool_name(name) for name in (required_tools or [])],
            "tags": list(tags or []),
        }
    )


def _stored_fingerprint(skill: Skill) -> str:
    return main_fingerprint(**{key: getattr(skill, key) for key in _EDITABLE})


def _digest(content: str | None) -> str | None:
    return None if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load(manager: SkillManager, user_id: str, name: str) -> tuple[Skill | None, dict[str, str]]:
    ws = manager._ws(user_id)
    raw = ws.skill_get(name)
    if raw is None:
        return None, {}
    return skill_from_md(raw, fallback_name=name), ws.skill_refs_get(name)


def _purge_expired(ws) -> list[dict]:
    live = []
    for record in ws.proposal_list():
        try:
            expired = datetime.fromisoformat(record["expires_at"]) <= _now()
        except (KeyError, ValueError, TypeError):
            expired = True
        if expired:
            ws.proposal_del(record["id"])
        else:
            live.append(record)
    return live


def list_pending(manager: SkillManager, user_id: str) -> list[dict]:
    return _purge_expired(manager._ws(user_id))


def reject(manager: SkillManager, user_id: str, proposal_id: str) -> None:
    if not manager._ws(user_id).proposal_del(proposal_id):
        raise ProposalNotFound(proposal_id)


def stage(
    manager: SkillManager,
    user_id: str,
    session_id: str,
    *,
    operation: str,
    name: str,
    fields: dict | None = None,
    old_string: str = "",
    new_string: str | None = None,
    ref_filename: str = "",
) -> dict:
    """Validate a change and store it for confirmation. Returns the staged record.

    ``fields`` holds only what the caller supplied (description, triggers, instructions,
    required_tools, tags, references); anything absent is left unchanged by an update.
    """
    if operation not in OPERATIONS:
        raise ProposalError(f"operation must be one of {', '.join(OPERATIONS)}.")
    if not name:
        raise ProposalError("propose requires a skill name.")
    try:
        _validate_segment(name, allow_slash=True)
    except ValueError as err:
        raise ProposalError(f"Invalid skill name: {err}") from err
    ws = manager._ws(user_id)
    live = _purge_expired(ws)
    if len(live) >= MAX_PENDING_PER_USER:
        raise ProposalError(
            f"{len(live)} proposals are already waiting for confirmation; ask the user to confirm or skip some."
        )
    skill, existing_refs = _load(manager, user_id, name)

    record = {
        "id": uuid4().hex,
        "operation": operation,
        "name": name,
        "session_id": session_id or None,
        "created_at": _now().isoformat(),
        "expires_at": (_now() + PROPOSAL_TTL).isoformat(),
    }
    if operation == "patch":
        record.update(_stage_patch(ws, skill, name, old_string, new_string, ref_filename))
    else:
        record.update(_stage_content(operation, skill, existing_refs, name, dict(fields or {})))

    if operation != "patch":
        # A newer proposal for the same skill and operation replaces the older one.
        for other in live:
            if other["name"] == name and other["operation"] == operation:
                ws.proposal_del(other["id"])
    ws.proposal_set(record["id"], record)
    return record


def _stage_patch(ws, skill, name, old_string, new_string, ref_filename) -> dict:
    if skill is None:
        raise ProposalError(f"Skill '{name}' does not exist; use operation=create.")
    if not old_string or new_string is None:
        raise ProposalError("A patch needs a nonempty old_string and an explicit new_string.")
    filename = ""
    if ref_filename:
        try:
            filename = normalize_reference_path(ref_filename)
        except ValueError as err:
            raise ProposalError(str(err)) from err
        content = ws.skill_refs_get(name).get(filename)
        if content is None:
            raise ProposalError(f"Reference '{filename}' does not exist in skill '{name}'.")
    else:
        content = ws.skill_get(name)
    try:
        patch_skill_text(content, old_string, new_string, main_document=not filename)
    except ValueError as err:
        raise ProposalError(str(err)) from err
    return {"ref_filename": filename, "old_string": old_string, "new_string": new_string}


def _stage_content(operation, skill, existing_refs, name, fields) -> dict:
    if operation == "create" and skill is not None:
        raise ProposalError(f"Skill '{name}' already exists; use operation=update or patch.")
    if operation == "update" and skill is None:
        raise ProposalError(f"Skill '{name}' does not exist; use operation=create.")
    unknown = set(fields) - {*_EDITABLE, "references"}
    if unknown:
        raise ProposalError(f"Unsupported fields: {', '.join(sorted(unknown))}.")
    if operation == "create":
        for key in ("description", "instructions"):
            if not fields.get(key):
                raise ProposalError(f"Creating a skill requires {key}.")
    elif not fields:
        raise ProposalError("An update must supply at least one field to change.")

    try:
        new_refs = validate_reference_set(
            fields.get("references") or {},
            instructions=fields.get("instructions") or "",
            existing_paths=existing_refs,
        )
    except ValueError as err:
        raise ProposalError(str(err)) from err

    current = {key: getattr(skill, key) for key in _EDITABLE} if skill else {}
    merged_refs = existing_refs | new_refs
    final = current | {key: value for key, value in fields.items() if key in _EDITABLE}
    final.setdefault("triggers", [])
    final.setdefault("required_tools", [])
    final.setdefault("tags", [])
    final["instructions"] = with_reference_links(final.get("instructions", ""), merged_refs)

    result = {"fields": {key: final[key] for key in _EDITABLE}, "references": new_refs}
    if operation == "update":
        # Only changed fields are sent to the update. Instructions are included whenever the
        # supplied text or newly added references changed them, with the links already in place.
        supplied = {key: final[key] for key in _EDITABLE if key in fields}
        if final["instructions"] != current.get("instructions"):
            supplied["instructions"] = final["instructions"]
        result["fields"] = supplied
        result["base_fingerprint"] = _stored_fingerprint(skill)
        result["base_references"] = {path: _digest(existing_refs.get(path)) for path in new_refs}
    result["result_fingerprint"] = main_fingerprint(**{key: final[key] for key in _EDITABLE})
    return result


def commit(manager: SkillManager, user_id: str, proposal_id: str) -> tuple[dict, Skill | None]:
    """Apply a staged proposal. Safe to call again after a partial failure.

    Returns the record and the resulting skill (None for a patch, which does not load it).
    """
    ws = manager._ws(user_id)
    record = ws.proposal_get(proposal_id)
    if record is None:
        raise ProposalNotFound(proposal_id)
    try:
        if datetime.fromisoformat(record["expires_at"]) <= _now():
            ws.proposal_del(proposal_id)
            raise ProposalNotFound(f"{proposal_id} (expired)")
    except (KeyError, ValueError, TypeError):
        ws.proposal_del(proposal_id)
        raise ProposalNotFound(proposal_id) from None

    name, operation = record["name"], record["operation"]
    skill, existing_refs = _load(manager, user_id, name)
    result_skill = None
    if operation == "patch":
        if skill is None:
            raise ProposalConflict(f"Skill '{name}' no longer exists.")
        try:
            manager.patch_user_skill(
                user_id, name, record["old_string"], record["new_string"], record.get("ref_filename", "")
            )
        except ValueError as err:
            raise ProposalConflict(f"The skill changed since this edit was proposed: {err}") from err
    elif operation == "create":
        result_skill = _commit_create(manager, user_id, record, skill, existing_refs)
    elif operation == "update":
        result_skill = _commit_update(manager, user_id, record, skill, existing_refs)
    else:
        raise ProposalNotFound(f"{proposal_id} (unknown operation)")
    ws.proposal_del(proposal_id)
    return record, result_skill


def _already_applied(record, skill, existing_refs) -> bool:
    return (
        skill is not None
        and _stored_fingerprint(skill) == record["result_fingerprint"]
        and all(existing_refs.get(path) == content for path, content in record["references"].items())
    )


def _commit_create(manager, user_id, record, skill, existing_refs) -> Skill:
    if skill is not None:
        if _already_applied(record, skill, existing_refs):
            return skill
        raise ProposalConflict(f"Skill '{record['name']}' already exists.")
    fields = record["fields"]
    created = Skill(
        name=record["name"],
        description=fields["description"],
        triggers=fields["triggers"],
        instructions=fields["instructions"],
        required_tools=fields["required_tools"],
        tags=fields["tags"],
        references=record["references"],
        source="extracted",
        extracted_from=record.get("session_id"),
        status="confirmed",
        created_by=user_id,
    )
    manager.save_user_skill(user_id, created)
    return created


def _commit_update(manager, user_id, record, skill, existing_refs) -> Skill:
    if skill is None:
        raise ProposalConflict(f"Skill '{record['name']}' no longer exists.")
    if _already_applied(record, skill, existing_refs):
        return skill
    stale = _stored_fingerprint(skill) != record["base_fingerprint"] or any(
        _digest(existing_refs.get(path)) not in (base, _digest(record["references"][path]))
        for path, base in record["base_references"].items()
    )
    if stale:
        raise ProposalConflict(
            f"Skill '{record['name']}' changed after this update was proposed; read it again and re-propose."
        )
    updates = dict(record["fields"])
    if record["references"]:
        updates["references"] = record["references"]
    updated = manager.update_user_skill(user_id, record["name"], updates)
    if updated is None:
        raise ProposalConflict(f"Skill '{record['name']}' no longer exists.")
    return updated


def proposal_event(record: dict) -> dict:
    """The user-facing view of a staged proposal: what a confirmation card needs to show."""
    event = {
        "type": "skill_proposal",
        "proposal_id": record["id"],
        "operation": record["operation"],
        "name": record["name"],
        "session_id": record.get("session_id"),
        "expires_at": record["expires_at"],
    }
    if record["operation"] == "patch":
        event.update(
            ref_filename=record.get("ref_filename", ""),
            old_string=record["old_string"],
            new_string=record["new_string"],
        )
    else:
        event.update(fields=record["fields"], references=record["references"])
    return event
