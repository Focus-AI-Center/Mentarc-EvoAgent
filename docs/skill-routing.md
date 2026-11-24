# Skill routing and loading

Skills supply reusable methods; agent profiles define roles and execution
settings. Loading a skill does not inherently create another agent or container.

## Pre-matching and main-model fallback

At turn start, [EvoAgent](../core/agent.py) loads the top-level skill catalog and
passes it to [SkillMatcher](../core/skills/matcher.py):

1. Keyword matching selects the first skill with a trigger occurring in the
   request, ignoring case. Triggers are substring checks, not weighted rankings.
2. Without a keyword match, embedding matching compares the input with each
   skill's name, description and triggers. It selects the best score at or above
   `SKILL_MATCH_EMBEDDING_THRESHOLD`. Description embeddings are cached by skill
   name for the matcher instance; `invalidate_cache` can clear them.
3. Without a pre-match, the main agent proceeds with the skill catalog in its
   system instructions. It can choose and load a skill through `skill(get)`.

The matcher contains an `_llm_match` helper, but its normal `match()` path does
not call it. There is no additional dedicated matching-model call at step 3.
Embedding failures leave the main model able to continue with the catalog.
Requests recognized by `_is_skill_meta_request` bypass automatic matching.
Synchronous embedding-client calls run in worker threads so their network wait
does not block other agent turns or background workers on the event loop.
Cancelling that wait does not guarantee that an already issued provider request
has stopped.

## Executing a match

An ordinary match injects the original guide into history as a paired synthetic
`skill(get)` call and result. The [tool mounter](../core/skills/tool_mounting.py)
grants its `required_tools`. A history check avoids repeatedly injecting an
intact, successfully loaded guide. The main matching path increments
`usage_count` on this injection, not on every tool invocation or business result.

A skill with `requires_sub_agent` creates a child using `sub_agent_name`,
`sub_agent_instructions` and `sub_agent_tools`. It uses the same internal audience
contract as explicit main-agent delegation: the child returns files and a
receipt, then the main SDK loop resumes to read, verify and present the result.
`taskId` selects the automatic-handoff thread; ordinary tool calls use their own
`task_id`. See [agent coordination](agent-coordination.md).

## Manual and child loading

`skill(list)` discovers metadata; `skill(get)` loads the original guide;
`skill(get_ref)` reads a reference through the Workspace SDK. A reference-first
read also loads the main guide. Tool dependencies are granted when their guide
is loaded, subject to the current agent's policy and configured MCP services.
Loading definitions does not execute tools or install packages.

The runtime includes the skill's logical path and execution directory. In
LocalFS, helper scripts use the local user workspace. In MinIO mode, ordinary
reference reads use the storage SDK while helpers run in the mounted session
workspace through code execution. See [cloud execution](cloud-agent.md).

An internal worker sees the available top-level catalog and can preload skills
specified by its profile or dispatch call. It loads other skills itself. The
parent's loaded guide bodies and mutable Tool Search grants are not copied into
the child. See [workers](workers.md).

## Compaction and persistence

Original skill documents and their dynamic tool grants share one lifecycle.
Compression replaces guide bodies with reload pointers and unloads the
associated grants. Loading the guide again restores them. Resident tools and
independent searches retain their own grants. A prose summary alone does not
establish that a guide is loaded.

Workspace skill definitions persist independently of the conversation and its
compressed form. Reviews prepare changes; confirmation applies proposals to the
stored definitions. Later turns resolve the updated catalog. See
[context compression](context-compression.md) and [skill extraction](skill-extraction.md).

## Verification

`tests/unit/test_skill_matching.py` checks keyword bypass, unmatched fallback,
event-loop responsiveness and usage-context propagation during embedding waits.
Offline coverage for loading, dependency visibility, exact reloads, child roles
and automatic handoff lives in `tests/unit/test_skill_tool_lifecycle.py`,
`tests/unit/test_tool_search.py`, `tests/unit/test_delegation_audience.py` and
`tests/unit/test_agent_coordination.py`. Keyword/embedding model latency and
semantic match quality require separate evaluation with the configured provider.
