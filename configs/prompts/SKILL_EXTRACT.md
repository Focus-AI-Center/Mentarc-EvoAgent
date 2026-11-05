---
name: SKILL_EXTRACT
description: Guidance for extracting reusable skills and updating existing workflows
variables: []
---

## Extraction criteria

Extract a skill only when all criteria hold:

1. Multiple steps: at least two tool calls, or an explicit processing workflow.
2. Reusable: likely to recur, rather than a one-off task.
3. Structured: a clear, generalizable input -> processing -> output pattern.
4. New value: distinct from existing skills, or a substantive improvement.

Skip simple questions, casual conversation and isolated tool calls. Do not invent a workflow.

## Procedure

1. Call `skill(action="list")` to avoid duplicates.
2. Identify a new workflow or a meaningful improvement. For an improvement, read the existing skill with `get` / `get_ref` first.
3. Call `skill(action="propose", ...)` once per change. The user sees each proposal as a card and decides; nothing is saved by the call, and `propose` is the only way you change skills.

## Choosing the operation

- `operation="create"`: a new skill. Requires `name`, `description` and `instructions`; `triggers`, `required_tools`, `tags` and `references` are optional.
- `operation="update"`: new content for an existing skill. Supply only the fields that change. `instructions` replaces the whole text; supplied `references` are added or replaced and the others are kept.
- `operation="patch"`: replace one exact fragment. Give `old_string` copied from `get` / `get_ref` (it must match exactly once) and `new_string`; set `ref_filename` to edit a reference instead of the main text. Prefer `patch` for a small correction so the rest of the document is untouched.

## Proposal fields

- `name`: required; two to four lowercase English words separated by hyphens, such as `write-seo-report`.
- `description`: one specific sentence explaining purpose and trigger conditions. Prefer "Find buyer pain points in social posts and produce three sourced opportunities" over "Help with analysis".
- `triggers`: three to five varied formal and conversational English phrases.
- `instructions`: numbered steps identifying tools, necessary inputs and output format. Aim for 10-30 lines.
- `references`: optional mapping from relative filenames to complete text, such as `{"rules.md": "Complete screening rules"}`. Nested paths such as `examples/report.md` are supported. Limits: at most 20 files, 64KB each, 256KB in total; instructions up to 30KB.
- `required_tools`: optional plugin tool names such as `["web_search", "fetch_url"]`.
- `tags`: optional category labels.

## Splitting long content

Keep triggers, inputs, core steps, output requirements and reference-reading conditions in `SKILL.md`. If the main text exceeds roughly 50 lines or contains long rules, templates, API details or complete examples, move those details into references by topic.

This is an organization guideline, not mechanical truncation. Preserve reusable details and avoid creating references for short skills. Each reference must contain its full text, not just a filename or placeholder.

Explain when to read each reference in `instructions`, for example: before screening, call `skill(action="get_ref", name="buyer-lead-screening", ref_filename="screening-rules.md")`. Markdown links use `references/screening-rules.md`.

The user reviews the main document and references together before anything is saved. Never fabricate supporting content.
