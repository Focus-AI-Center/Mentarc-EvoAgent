# Skill extraction and reference files

The session updater and `session-reviewer` use an LLM to identify reusable workflows. A proposal can contain a compact main document and several reference files. The user confirms the complete bundle before saving.

## When to split

Keep triggers, inputs, core steps, output requirements and reference-reading conditions in `SKILL.md`. Aim for 10-30 lines. Beyond roughly 50 lines, or for long rules, templates, examples and API details, move details into references by topic.

This is model guidance, not automatic truncation. Preserve reusable details. Short skills need no references. Each proposed file must include full text rather than a filename or placeholder.

## Proposal format

Use valid JSON between the proposal markers; escape newlines and quotes:

```text
[SKILL_PROPOSAL]
{
  "name": "buyer-lead-screening",
  "description": "Find potential buyers and screen purchasing signals",
  "triggers": ["find buyers", "screen leads", "buyer leads"],
  "required_tools": ["web_search", "fetch_url"],
  "instructions": "1. Confirm the product and market.\n2. Before screening, read references/screening-rules.md using skill(action=\"get_ref\", name=\"buyer-lead-screening\", ref_filename=\"screening-rules.md\").\n3. Search candidates and return a sourced lead table.",
  "references": {
    "screening-rules.md": "# Screening rules\n\nRequire a clear purchasing role and matching products. Mark insufficient evidence as unconfirmed.",
    "examples/report.md": "# Report format\n\nCompany | Purchasing signal | Source | Open questions"
  }
}
[/SKILL_PROPOSAL]
```

`references` maps paths to full text. Keys are relative to the skill's `references/` directory. Nested paths work; one leading `references/` is accepted and normalized. Absolute paths, parent traversal, empty bodies and duplicate normalized paths are rejected.

Legacy single-file text proposals remain supported. New proposals containing references must use JSON. Parsing failure is reported rather than silently dropping references.

## Confirmation and storage

Ordinary chat and "Update memory" recognize proposals. The card previews the main document and every reference. One confirmation submits the complete bundle to `/api/skills/<user_id>/create`.

Sub-agent proposals travel as separate SSE events with `code=12`, bypassing the 4,000-character ordinary sub-agent summary cap. The parent receives a brief report and need not repeat reference bodies. Incomplete output produces a retry notice and is not saved.

```text
<workspace>/<user_id>/skills/buyer-lead-screening/
├── SKILL.md
└── references/
    ├── screening-rules.md
    └── examples/report.md
```

Main-document metadata does not embed reference bodies. Extracted proposals preserve `source="extracted"` and the originating session in `extracted_from`; manual creation defaults to `user_defined`. Saving adds a short reference index if the main document lacks links to supplied references.

Do not call `save` or `ref_set` during extraction before confirmation. If saving is already explicitly authorized, the tool can submit both at once:

```python
skill(action="save", name="buyer-lead-screening", description="Screen buyer leads",
      triggers=["find buyers"], instructions="1. Confirm inputs.\n2. Read references/rules.md and screen leads.",
      references={"rules.md": "Complete screening rules"}, source="extracted")
```

## Cloud Agent reference access

`skill(get)`, `get_ref`, `save` and `ref_set` use the API process's workspace
storage backend. In MINIO mode they read and write objects directly through
the MinIO SDK. Ordinary reference access does not require a sandbox container,
Docker exec or bash. This avoids execution and filesystem overhead; use
`execute_code` when a script or file-processing operation actually needs to run.
See [Cloud Agent architecture and sandbox runtime](cloud-agent.md) for the
request paths, s3fs mapping and session-container lifecycle.

## Reading and updating

`skill(action="get", name="buyer-lead-screening")` returns the main text, skill directory and reference list without loading all reference bodies. Read details with `skill(action="get_ref", name="buyer-lead-screening", ref_filename="screening-rules.md")`.

Create, update and `skill(save)` accept reference maps. Matching files are replaced; omitted existing references remain. Delete an obsolete file with `ref_del`. All supplied paths are validated before creation.

## Scripts, assets and backup

Template synchronization preserves each original relative path: `scripts/run.py` stays under `scripts/`, `assets/` stays under `assets/`, and references remain under `references/`. Binary content is copied as bytes. Run existing helpers with `execute_code(language="bash", code="python scripts/run.py", cwd="skills/<skill-name>")`.

The sync layout version forces existing workspaces to refresh old misplaced `references/scripts/` copies while preserving user-owned prompt files. Skill snapshots retain main text, textual references and base64-encoded auxiliary resources, excluding nested skills from their parent's resource set. Older snapshots without the additive `resources` field remain importable.

Proposal `references` and `ref_set` still write text under `references/`; they do not create executable files under `scripts/`. Create scripts or binary assets through code execution when needed.

Implementation: [extraction prompt](../configs/prompts/SKILL_EXTRACT.md), [proposal UI](../app/static/index.html), [API schema](../app/schema/skill.py), [skill manager](../core/skills/manager.py), [tools](../core/plugins/internal.py) and [Workspace](../utils/workspace.py).

Tests: [proposal/reference integration](../tests/unit/test_skill_references.py) and [resource synchronization, execution and backup](../tests/unit/test_skill_resources.py).
