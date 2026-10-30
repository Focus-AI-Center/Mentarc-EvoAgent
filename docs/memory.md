# Memory storage and lifecycle

The project maintains a fixed user profile, a long-term memory index, on-demand memory bodies, conversation history and reusable skills. These layers have separate storage and loading rules.

## Cloud Agent storage path

Memory is persistent Agent state rather than container-local state. In MINIO
mode, `memory` reads and updates the workspace through the MinIO SDK in the API
process; it does not run bash for routine storage operations. The sandbox is
used to create or process independent files when execution is needed, or to
inspect additional lines from a long body. Both paths address the same objects.
See [Cloud Agent architecture and sandbox runtime](cloud-agent.md) for the
reasoning, path mapping, lifecycle and deployment configuration.

## Workspace layout

```text
<workspace>/<user_id>/
├── prompts/
│   ├── SOUL.md
│   ├── USER.md
│   ├── MEMORY.md
│   ├── MEMORY_user.md
│   ├── MEMORY_feedback.md
│   ├── MEMORY_project.md
│   └── MEMORY_reference.md
├── memory/
│   └── customer_acme.md
├── skills/<skill-name>/
│   ├── SKILL.md
│   └── references/
└── sessions/<session-id>.json
```

`memory/` is a convention for independent files, not a required directory. Paths in index links are relative to the user workspace root, including when the index lives under `prompts/`.

`Workspace` uses local files when `MINIO_ENDPOINT` is unset and MinIO object keys with the same user prefix when it is configured. In local execution, the sandbox starts in the user's workspace; Docker exposes that workspace at `/workspace`.

Templates live in [configs/prompts](../configs/prompts). `SOUL.md` is platform-owned and refreshed when the template sync version changes. `USER.md` and `MEMORY*.md` are seeded only when missing, preserving existing user content. The repository template files are not a user's live memory.

## Profile and durable memory

`USER.md` holds fixed English labels for basic information, business details, background and preferences. `update_user_profile` maps English tool arguments onto those fields. Only explicit self-descriptions or requested updates justify profile changes; temporary task content is not reliable evidence about the user.

Complete legacy profiles using the earlier localized fixed-field order retain
their values when rewritten with English labels. Partial or custom localized
schemas are rejected before writing and require explicit label migration.

Memory has four types:

| Type | Meaning |
|---|---|
| `user` | Background, expertise or preferences beyond the fixed profile |
| `feedback` | Collaboration rules, corrections and accepted approaches |
| `project` | Durable, non-obvious project decisions and constraints |
| `reference` | External systems and when to use them |

The files named `MEMORY_<type>.md` are optional grouped body storage for those types. They are not automatically loaded beside the index.

## Index formats

The index is Markdown with optional YAML frontmatter. Independent file links and grouped entries may coexist:

```markdown
---
name: MEMORY
description: Long-term memory index
---

- [project] [ACME background](memory/customer_acme.md) (2026-10-30) — Purchasing preferences
- [feedback] Concise responses (2026-10-30) — Lead with the result
```

An independent body can contain arbitrary useful UTF-8 text. A grouped body uses headings:

```markdown
## Concise responses (2026-10-30)

Lead with the result and expand when asked.

**Why:** The user prefers brief routine updates.
**How to apply:** Ordinary task reports and confirmations.
```

Several entries of one type share the corresponding grouped file. A file registration adds a link to `MEMORY.md`; it does not merge or copy the file into `MEMORY_<type>.md`. There is no fixed independent-file count limit. Titles should be unique within a type.

## Loading

At agent creation, the system prompt includes the profile and the memory index. Grouped bodies and independent files are read only when requested. Automatic index loading includes at most **200 lines or 25KB of UTF-8 bytes**, whichever comes first. This is a context budget, not a storage cap: the full index remains stored. A maintenance notice appears near the limit or when content is omitted.

`memory(action="read")` returns the full current index. `memory(action="get", title="ACME background")` resolves a registered file or grouped section. If a title appears under several types, specify `type`.

`get` returns at most **8,000 body characters** and identifies the source path when truncated. Read additional content with sandbox bash when necessary:

```python
execute_code(language="bash", code="cat -- memory/customer_acme.md")
execute_code(language="bash", code="sed -n '100,180p' memory/customer_acme.md")
```

There are no pagination parameters. File bodies are read fresh on each `get`, so editing an independent file does not require another registration.

## Creating an independent memory

Create a UTF-8 file in the workspace, then register it:

```python
execute_code(language="python", code="from pathlib import Path; Path('memory').mkdir(exist_ok=True); Path('memory/customer_acme.md').write_text('ACME purchasing preferences', encoding='utf-8')")
memory(action="append", type="project", title="ACME background",
       file_path="memory/customer_acme.md", summary="Purchasing preferences")
```

The tool checks that the path stays within the user's workspace and that the file exists, is UTF-8 and is nonempty. Absolute paths, parent traversal and cross-user local symlinks are rejected. Registration requires `summary` and excludes `content`. Repeated registration of the same file does not add another link.

The body remains at its original path. Update it using code execution. Its index date and summary do not automatically change after a body edit.

## Grouped compatibility operations

Without `file_path`, append writes a grouped section and an index line:

```python
memory(action="append", type="feedback", title="Concise responses",
       content="Lead with the result.\n\n**Why:** Explicit preference.\n**How to apply:** Routine updates.",
       summary="Lead with the result")
```

A summary should be a single line of at most 40 characters. If omitted for grouped append, the tool uses the first content line.

`memory(action="rewrite", type="feedback", content="## Concise responses (2026-10-30)\nUpdated rule")` replaces that entire grouped file and rebuilds the index. Preserve the dated heading structure. `memory(action="rebuild_index")` rebuilds grouped entries and preserves independent-file links, including links whose files are currently missing. It cannot recover an independent link that has already been lost or discover an unregistered file.

A rebuild derives grouped summaries from each section's first body line, so a custom summary may change.

## Concurrent updates and recovery

Memory appends and index rebuilds use conditional read/modify/write operations. Text and ETag are obtained from one read; a competing write causes a fresh read and recomputation. Local storage checks the condition and publishes the file under a per-key process-shared file lock, using unique temporary files. MinIO sends `If-Match` or `If-None-Match` with the PUT; the server must support conditional writes. Failed preconditions are retried, while unrelated storage errors are reported.

This preserves distinct entries from concurrent Agent instances. Duplicate checks run again against the latest index. A whole-file `rewrite` rejects a body change detected during its update instead of overwriting a concurrent append.

Creating an independent file, writing grouped content and updating the index are **not a single transaction across files**. After an interrupted grouped update, rebuild the index. A created but unregistered independent file must be registered again. A missing file is reported by `get` while its link remains available for repair. Avoid competing operations on the same logical entry across different storage forms.

## History, skills and session review

Session JSON stores conversation messages and tool activity. History compression replaces older messages with a continuation summary and an asset index; it is separate from durable memory. Skills package reusable procedures under `skills/`, with metadata, main text and optional supporting files. See [skill extraction](skill-extraction.md).

During a conversation, the main Agent records individual useful signals with `memory`. The "Update memory" action runs `SessionUpdater`; explicit analytical review requests can use the `session-reviewer` sub-agent. Both compare existing state, update reliable profile/memory facts and propose skills for user confirmation.

Do not retain temporary task state, public knowledge, profile duplicates or facts more authoritatively represented by current source and git history.

## Backup and compatibility

`Workspace.export_snapshot()` stores profile, index and grouped files under `prompts`, and indexed independent files that still exist under `memory_files`. `import_snapshot()` restores both; its default `replace=True` resets the workspace first. Unregistered files are not automatically included as memory.

The LLM-facing tools are `memory` and `skill`. Legacy Python aliases remain importable, and saved tool references are migrated when loaded without rewriting resource paths such as `scripts/update_memory.py`.

Implementation: [MemoryTool](../core/plugins/internal.py), [index parser](../utils/memory_index.py), [context budgets](../utils/context_assets.py), [Workspace](../utils/workspace.py) and [storage backends](../utils/storage_backend).

Regression coverage: [memory operations](../tests/unit/test_memory_tool.py), [forced concurrent updates](../tests/unit/test_memory_concurrency.py), [conditional storage writes](../tests/unit/test_storage_conditional_writes.py) and [context boundaries](../tests/unit/test_context_assets.py).
