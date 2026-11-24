# Task threads, live delegation and skill lifecycle

Named agent profiles now resolve from the user's workspace `agents/` directory
before built-in defaults. `execution_mode: worker` selects internal file-producing
execution through `worker`/`worker_status`. Ordinary profiles support direct user
sessions and internal delegation, with the audience determined at invocation. See [workers and agent definitions](workers.md).

For example, a user asks for two reports and later requests a revision to the
first. Each report needs its own child history, output files and execution status.
The revision must reuse the first report's task ID rather than start over.

## Task identity

The explicit tool accepts:

```python
sub_agent(agent_type="session-reviewer", task="Review this conversation",
          context="Additional source material", task_id="review-a")
```

- Reuse `task_id` for follow-ups and revisions. Assign different IDs to independent
  tasks, including concurrent calls to the same agent type.
- Omitting the ID creates a new isolated task. Its receipt returns the generated
  ID; use that ID to resume it.
- IDs contain 1–64 ASCII letters, digits, underscores or hyphens, starting with a
  letter or digit. Invalid IDs are rejected rather than silently normalized.
- The child session key combines the agent type with a hash of the complete
  parent session ID and task ID. Workspace ownership supplies user isolation.
  Request IDs identify API requests and do not select child history.
- Calls do not acquire a session execution lock. Reuse a task ID after its
  previous call has finished; use different IDs for independent parallel work.
  Concurrent calls sharing a child session can overwrite each other's saved
  history snapshots.

Automatic `requires_sub_agent` handoff uses the same executor, event protocol and
receipts. Without an explicit ID it retains one stable task thread per skill in
the parent session. Clients can send `taskId` to `/api/agent/chat` to choose a
different automatic-handoff thread or resume a named one:

```json
{
  "userId": "alice",
  "sessionId": "research",
  "taskId": "report-a",
  "data": {"input": "Revise this report using the new source"}
}
```

`taskId` selects the automatic-handoff thread; explicit tool calls use their own
`task_id`. The independent `/api/sub-agent/{type}/chat` endpoint continues to use
its supplied session ID. Skill-backed workers can also be resumed through the
tool using the owner and task ID from their receipt.

## Invocation audience and internal results

The invocation route determines who communicates with the user:

| Route | Audience and behavior |
|---|---|
| Direct `/api/sub-agent/{type}/chat` | The selected agent replies, asks questions and presents skill proposals to the user |
| Main-agent `sub_agent(...)` | The child reports internal findings and files; the main agent verifies and delivers |
| Automatic `requires_sub_agent` handoff | The child runs first; the main agent resumes to verify and deliver |
| Internal `worker(...)` | Background file-producing work is collected by the main agent |

The same ordinary profile can serve either audience. The API uses the runner's
normal direct mode; `SubAgentTool` explicitly passes `delegated=True`. Workers
also use the delegated audience contract. The runtime appends this instruction
without modifying or copying the stored agent definition:

> Only the main agent communicates with the user in this delegation chain.

The child receives further instructions to return findings, file paths and gaps
to the parent. In delegated mode, user-question and delivery tools are removed,
and `skill` permits only `list`, `get` and `get_ref`. The policy applies to initial
schemas, skill tool mounts and Tool Search, including eager discovery. Analytical
agents retain their existing profile and memory tools; workers have the stricter
policy described in [workers](workers.md#limits-and-lifecycle). Direct sessions
retain their original tools and user interaction behavior.

Delegated text, preview, question and proposal events do not become user-facing
child responses. A request-local channel sends only sanitized task progress,
tool activity and final file links, using the same `worker_progress` cards as
background workers. Mutable task state belongs to each invocation. Direct user
sessions keep their existing event protocol.

The parent receives a bounded receipt, while the complete child response is
stored in the workspace:

```json
{
  "type": "sub_agent_receipt",
  "task_id": "report-a",
  "owner": "analyst",
  "session_id": "sa_analyst_task_<hash>",
  "status": "completed",
  "delivery_complete": false,
  "delivered_codes": [],
  "output_dir": "workers/task_<hash>/<attempt>",
  "files": ["workers/task_<hash>/<attempt>/response.md"],
  "artifact_refs": [],
  "summary": "A bounded description of the internal result"
}
```

`completed` describes child execution, not user delivery or verified correctness.
The parent reads the files with `workspace_file`, checks sources and tool outcomes,
then writes its own response. Produced artifacts remain `produced` until the main
agent delivers them. Missing requirements are reported to the parent. A legacy
or custom child question is captured as `questions_for_parent` with status
`needs_input`; it is not a pending question shown to the user. The main agent
asks any necessary question itself. Failures, cancellation and timeout remain
explicit, and partial text is retained when storage succeeds.

Child skill proposals are prepared as candidate files for the main agent to
review and propose. Confirmation cards originate from the main agent in this
chain. Direct child sessions can still use `skill(propose)` and receive the
normal confirmation flow. Existing saved child questions remain answerable for
history compatibility, after which the main agent handles presentation.

Automatic handoff appends one paired `sub_agent` call/result and continues the
main SDK loop without adding another user message. Parent history saves internal
receipts and sanitized progress in `delivery_events` for UI replay; that metadata
is excluded from model input and token budgets. Child histories remain separate.
Closing the stream cancels execution and preserves interruption facts and partial
files when storage succeeds. Generated task IDs and output directories remain
available for follow-ups. This does not provide durable execution or automatic
recovery after a process restart. See [runtime components](runtime-components.md).

## Skill guides and tool grants

One `SkillToolMounter` serves each main or child agent. It handles matched skills,
configured child skills, explicit `skill(get)` and history restoration:

1. Load the original guide and grant its `required_tools` to the loading agent.
2. Initialize missing plugin instances with the current user, session and
   workspace. Configured MCP requirements use the existing MCP catalog.
3. Update SDK tool visibility for the next model step. Search and ordinary tool
   calls retain their existing authorization and enablement checks.
4. Restore grants on later turns only from intact, successful skill tool pairs;
   a prose summary or failed lookup does not prove that instructions are loaded.

Reading a reference first also includes the main guide, so dependencies are not
granted with the reference alone. Reference text still comes directly from the
workspace, without a sandbox invocation.

When history compression actually changes context, original skill and reference
texts are replaced with reload instructions before any LLM summarization. The
mounter removes skill-introduced tools, clears skill-specific visibility grants
and resets the skill tool's loaded-document cache. Resident tools and independent
tool-search grants remain available. A later `skill(get)` returns the exact
guide again and restores its tools. Disabled compression or a failed summary
leaves the original guides and grants intact.

Compression runs at turn boundaries and before SDK model requests, after the
preceding parallel tool batch has completed. The same compressor and lifecycle
callbacks serve both paths. SDK schema visibility is recalculated after tools
have been unloaded, without restarting the run or replaying completed calls.
See [context compression](context-compression.md) for budgets and failure behavior.

## Validation

Offline tests use local workspaces and scripted models through the real SDK:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p asyncio \
  tests/unit/test_agent_coordination.py tests/unit/test_skill_tool_lifecycle.py \
  tests/unit/test_tool_search.py tests/unit/test_skill_references.py -q
```

They cover thread reuse/isolation, concurrent internal progress and result collection, receipts, partial
failure, proposal preservation, cancellation, automatic handoff, UI task buckets,
SDK schema updates, history restoration and compression/reload behavior.
