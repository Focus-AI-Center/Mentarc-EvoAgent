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
2. Identify a new workflow or a meaningful improvement.
3. Output a valid JSON proposal between `[SKILL_PROPOSAL]` and `[/SKILL_PROPOSAL]`.
4. Do not call `save` or `ref_set` during extraction. The system saves the bundle after user confirmation.

## Proposal fields

- `name`: required; two to four lowercase English words separated by hyphens, such as `write-seo-report`.
- `description`: required; one specific sentence explaining purpose and trigger conditions. Prefer "Find buyer pain points in social posts and produce three sourced opportunities" over "Help with analysis".
- `triggers`: required; three to five varied formal and conversational English phrases.
- `instructions`: required; numbered steps identifying tools, necessary inputs and output format. Aim for 10-30 lines.
- `references`: optional; a JSON mapping from relative filenames to complete text, such as `{"rules.md": "Complete screening rules"}`. Paths are relative to the skill's `references/` directory; nested paths such as `examples/report.md` are supported.
- `required_tools`: optional; tool names such as `["web_search", "fetch_url"]`.
- `tags`: optional; category labels.

## Splitting long content

Keep triggers, inputs, core steps, output requirements and reference-reading conditions in `SKILL.md`. If the main text exceeds roughly 50 lines or contains long rules, templates, API details or complete examples, move those details into references by topic.

This is an organization guideline, not mechanical truncation. Preserve reusable details and avoid creating references for short skills. Each reference must contain its full text, not just a filename or placeholder.

Explain when to read each reference in `instructions`, for example: before screening, call `skill(action="get_ref", name="buyer-lead-screening", ref_filename="screening-rules.md")`. Markdown links use `references/screening-rules.md`.

Use valid JSON with escaped newlines (`\n`); do not use YAML block strings for reference maps. The user reviews and confirms the main document and references together. Never fabricate supporting content.
