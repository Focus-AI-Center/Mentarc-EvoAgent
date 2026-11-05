---
name: skill-creator
description: Use when the user wants to create a skill, write a new skill, improve a skill description, organize skill content, or understand skill structure and progressive disclosure.
triggers:
  - create skill
  - save skill
  - write skill
  - improve skill
  - organize skill
required_tools:
  - skill
source: builtin
is_builtin: true
tags:
  - meta
  - skill-management
---

# Skill development guide

A skill packages domain knowledge, a reusable workflow and tool coordination. Create skills for recurring, structured tasks rather than isolated questions or single tool calls.

## Directory structure

```text
skill-name/
├── SKILL.md          # Required: YAML metadata and Markdown instructions
├── references/       # Detailed documents loaded on demand
├── scripts/          # Executable helpers run with execute_code
├── assets/           # Templates, images and other output resources
└── examples/         # Complete input/output examples
```

Loading proceeds from metadata (name, description and triggers), to the main document when matched, to supporting files when needed. Aim for a compact main document, usually 10-30 lines. Move long rules, templates, API details and complete examples into references; around 50 main-document lines is a useful signal to split by topic. Preserve useful details rather than mechanically truncating text.

The loaded skill includes its workspace-relative directory, such as `skills/skill-name/`. Resolve `scripts/`, `references/` and other relative paths from that directory. Run an existing helper using `execute_code(language="bash", code="python scripts/run.py", cwd="skills/skill-name")`. Template synchronization preserves resource paths and binary contents.

## Metadata

```yaml
---
name: write-seo-report
description: Search current sources and produce an SEO research report
triggers:
  - write report
  - generate SEO report
  - research report
required_tools:
  - websearch
  - fetchurl
tags: [writing, seo]
source: user_defined
---
```

Use two to four lowercase English words separated by hyphens for the name. Write a specific description with purpose and trigger conditions. Include three to five varied English trigger phrases. `required_tools` declares dependencies; configured tools are loaded when the skill activates. Available aliases include `websearch`, `fetchurl`, `imagegeneration`, `imageediting`, `execute_code`, `update_user_profile`, `memory` and `skill`.

## Instructions and supporting files

1. Use numbered steps with explicit actions, inputs, tools and expected outputs.
2. Explain significant reasons and constraints, such as searching current sources to avoid outdated information.
3. Specify output format and handle missing or insufficient inputs.
4. Reference every supporting file and explain when it must be read or executed.
5. Keep core instructions concise; avoid repeating complete reference contents.

`skill(action="get", name="skill-name")` loads the main text and lists references. Read a reference with `skill(action="get_ref", name="skill-name", ref_filename="rules.md")`. Scripts run through `execute_code`. Assets supply output resources; examples demonstrate complete usage.

## Creation workflow

1. Understand triggers, tools, their order, inputs, outputs and variable parameters. Clarify missing requirements when needed.
2. Identify reusable scripts, references, assets and examples.
3. Propose the skill with `skill(action="propose", operation="create", ...)`. The user sees the main document and every reference file on a card and confirms before anything is saved.

```python
skill(action="propose", operation="create", name="write-seo-report",
      description="Search current sources and produce an SEO research report",
      triggers=["write report", "generate SEO report", "research report"],
      instructions="1. Confirm the topic.\n2. Read references/report-template.md before drafting.\n3. Search sources and write the report.",
      references={"report-template.md": "# Report\n\nInclude a title, summary, findings and source links."},
      required_tools=["web_search", "fetch_url"], tags=["writing", "research"])
```

Reference keys are relative to `references/`, including nested paths. Provide complete text, not placeholders. Limits: at most 20 reference files, 64KB each and 256KB in total, instructions up to 30KB. Scripts and binary assets are created through code execution at their skill-relative paths; proposals carry text references only.

To change an existing skill, read the current text first, then propose:

```python
# One exact fragment (preferred for small corrections; the rest of the document is untouched).
skill(action="propose", operation="patch", name="write-seo-report",
      old_string="3. Search sources and write the report.",
      new_string="3. Search sources, verify citations and write the report.")
# The same, in a reference file.
skill(action="propose", operation="patch", name="write-seo-report", ref_filename="report-template.md",
      old_string="Include a title, summary, findings and source links.",
      new_string="Include a title, summary, findings, limitations and source links.")
# New content for chosen fields: supplied references are added or replaced, others are kept.
skill(action="propose", operation="update", name="write-seo-report",
      description="Search current sources, verify citations and produce an SEO research report")
```

`old_string` must be nonempty and occur exactly once in the target; include surrounding text if ambiguous. `new_string` is required; an explicit empty string deletes the fragment. A proposal is checked when you make it and again when the user confirms: if the skill changed in between, the confirmation is refused and you should read the skill again and propose again.

Creation details, usage history and omitted dependencies or handoff configuration are preserved by an update. Creating a name that already exists is rejected.

## Validation

Before saving, verify the name, specific description, varied triggers, declared tools, numbered steps, concise main document, explicit resource links and boundary behavior. Use `references/skill-creator-original.md` for the extended general guide when available.
