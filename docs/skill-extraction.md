# Skill extraction and reference files

The session updater and `session-reviewer` use an LLM to identify reusable workflows. Agents never write skills: the `skill` tool offers `list`, `get`, `get_ref` and `propose`. A proposal is staged on the server, shown to the user as a card with every file, and applied only when the user confirms. Extraction rules (criteria, fields, when to split into references) live in one place, [SKILL_EXTRACT](../configs/prompts/SKILL_EXTRACT.md); the session updater appends it and `session-reviewer` points to it.

## When to split

Keep triggers, inputs, core steps, output requirements and reference-reading conditions in `SKILL.md`. Aim for 10-30 lines. Beyond roughly 50 lines, or for long rules, templates, examples and API details, move details into references by topic.

This is model guidance, not automatic truncation. Preserve reusable details. Short skills need no references. Each proposed file must include full text rather than a filename or placeholder.

## Proposing a change

```python
# A new skill (the default operation).
skill(action="propose", operation="create", name="buyer-lead-screening",
      description="Find potential buyers and screen purchasing signals",
      triggers=["find buyers", "screen leads"], required_tools=["web_search", "fetch_url"],
      instructions="1. Confirm the product and market.\n2. Read references/screening-rules.md.\n3. Return a sourced lead table.",
      references={"screening-rules.md": "# Screening rules\n\nRequire a clear purchasing role."})

# One exact fragment of the main text (or, with ref_filename, of a reference).
skill(action="propose", operation="patch", name="buyer-lead-screening",
      old_string="3. Return a sourced lead table.", new_string="3. Verify sources and return a lead table.")

# New content for chosen fields; supplied references are added or replaced, others are kept.
skill(action="propose", operation="update", name="buyer-lead-screening", description="...")
```

The call returns `pending_confirmation` and a `proposal_id`; it saves nothing. `references` maps paths relative to `references/` (nested paths allowed; one leading `references/` is accepted) to full text. Paths with absolute, parent or empty segments, empty bodies, and names that differ only by case or Unicode form are rejected, as are these limits: at most 20 references, 64KB each, 256KB in total, and 30KB of instructions. A rejected proposal returns the reason to the agent so it can correct and propose again. A pending proposal expires after 7 days; at most 50 can wait per user, and a newer create/update for the same skill replaces the older one.

The text that will be saved is what the card shows: when references are supplied, the reference links that saving appends to the instructions are already in the staged text.

## Confirmation and storage

A proposal is stored under `.meta/proposals/<id>.json` in the user's workspace (not under `skills/`, so it is never listed, exported or loaded as a skill) and its full content is sent to the client as a code 12 event: `{"type": "skill_proposal", "proposal_id", "operation", "name", "fields", "references", ...}` or, for a patch, `old_string` / `new_string` / `ref_filename`. Sub-agent proposals travel the same way, so the 4,000-character cap on a sub-agent's summary does not apply. Proposals that were not decided survive a page reload through `GET /api/skills/<user_id>/proposals`.

- `POST /api/skills/<user_id>/proposals/<id>/confirm` applies the staged copy; the request carries no content, so what is saved is exactly what was shown. 404 means the proposal is gone or expired; 409 means the skill changed since it was proposed.
- `DELETE /api/skills/<user_id>/proposals/<id>` discards it.
- `create` is rejected if the name already exists. `update` records the content it was based on and is refused if the skill changed since (usage counters and timestamps do not count). A `patch` is checked against the current text at confirmation: it must still match exactly once, and the write is conditional on the stored version. After a conflict the agent should read the skill again and re-propose.
- Confirming is safe to repeat after a partial failure: the main document is written last and an already-applied proposal is recognized.

Confirmed proposals save through the same `SkillManager` paths as manual edits. A new skill records `source="extracted"` and the originating session in `extracted_from`; an update preserves creation and ownership details, usage count, quality statistics and omitted metadata such as tool dependencies and handoff configuration, and advances `version` and `updated_at`. Omitted `triggers`, `required_tools` and `tags` stay as they are; explicit empty lists clear them. Reference bundles are not a multi-file transaction.

The manual API (`POST /api/skills/<user_id>/create`, `PUT` and `DELETE` on `/<skill_name>`) remains for edits made by the user directly.

## Previous version and undo

Before a skill is replaced, patched or deleted, its current `SKILL.md` and references are copied to `.meta/skill_prev/` (one previous version per skill). `POST /api/skills/<user_id>/undo` with `{"name": ...}` restores it, including a deleted skill; the state being replaced becomes the new previous version, so an undo can be undone. A patch that fails validation or hits a conflict leaves the previous version untouched. Usage counting does not create versions.

## Reading

`skill(action="get", name="buyer-lead-screening")` returns the main text, skill directory and reference list without loading all reference bodies. Read details with `skill(action="get_ref", name="buyer-lead-screening", ref_filename="screening-rules.md")`.

If the main guide has not been loaded, a reference-first read includes it before the reference body. Loading a guide also grants its required tools to that agent. After history compression unloads a guide, it can be read again with its original text and tool grants. See [agent coordination](agent-coordination.md).

### Exact patches

The text rules live in [skill_patching.py](../utils/skill_patching.py); [Workspace.skill_patch](../utils/workspace.py) owns storage and conditional writes. Both the confirmation of a patch proposal and manual tooling use them.

- `old_string` must be nonempty and match exactly once, including whitespace and case. Missing, ambiguous (including overlapping) and unchanged replacements fail without writing.
- `new_string` must be supplied. An explicit empty string deletes the matched fragment.
- Without `ref_filename`, only the Markdown instructions are editable; YAML frontmatter, metadata, unrelated text and other files are preserved exactly (a patch does not advance `version`). With `ref_filename`, only that text reference is edited.
- The workspace reads the target and ETag together and writes with `If-Match`, so a concurrent edit rejects the patch instead of being overwritten. There is no automatic retry.

## Scripts, assets and backup

Template synchronization preserves each original relative path: `scripts/run.py` stays under `scripts/`, `assets/` stays under `assets/`, and references remain under `references/`. Binary content is copied as bytes. Run existing helpers with `execute_code(language="bash", code="python scripts/run.py", cwd="skills/<skill-name>")`.

The sync layout version forces existing workspaces to refresh old misplaced `references/scripts/` copies while preserving user-owned prompt files. Skill snapshots retain main text, textual references and base64-encoded auxiliary resources, excluding nested skills from their parent's resource set. Older snapshots without the additive `resources` field remain importable.

Proposal `references` write text under `references/`; they do not create executable files under `scripts/`. Create scripts or binary assets through code execution when needed.

Implementation: [extraction prompt](../configs/prompts/SKILL_EXTRACT.md), [proposal staging and commit](../core/skills/proposals.py), [reference validation](../utils/skill_references.py), [skill tool](../core/plugins/internal.py), [skill manager](../core/skills/manager.py), [endpoints](../app/api/routers/skills.py), [proposal card](../app/static/index.html) and [Workspace](../utils/workspace.py).

Tests: [staging, confirmation and undo](../tests/unit/test_skill_proposals.py), [exact patches and concurrent writes](../tests/unit/test_skill_patch.py), [references](../tests/unit/test_skill_references.py) and [resource synchronization, execution and backup](../tests/unit/test_skill_resources.py).
