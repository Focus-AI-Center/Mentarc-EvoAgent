# Task threads, live delegation and skill lifecycle

For example, a user asks for two reports and later requests a revision to the
first. Each report needs its own child history, live output and delivery status.
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

## Live output and handoff receipts

Child events are forwarded while the child is running, through a request-local
channel. Mutable task state belongs to each invocation; concurrent calls do not
share a result buffer. Forwarded events add `task_id`, `source_agent` and
`delivery_owner`. The parent owns the outer stream markers and metrics.

The built-in chat UI keeps text and tool cards separate for each task. API
consumers must also accumulate text by `(source_agent, task_id)` rather than
concatenate interleaved JSON or Markdown fragments into one response.

The model receives a JSON receipt rather than the full child transcript:

```json
{
  "type": "sub_agent_receipt",
  "task_id": "report-a",
  "owner": "worker",
  "session_id": "sa_worker_task_<hash>",
  "status": "delivered",
  "delivery_complete": true,
  "delivered_codes": [2],
  "artifact_refs": ["https://example.com/report.pdf"],
  "summary": "A bounded description of what was delivered",
  "delivery_id": "<unique delivery identifier>"
}
```

Statuses are `delivered`, `waiting_user` and `failed`. A waiting receipt also
contains `resume_agent`, `resume_task_id` and `pending_questions`.
`waiting_user` with `delivery_complete=true` means the question was delivered;
the business task is still pending. Partial failures retain delivery facts and
include `failures`; they must not be presented as successful completion.

The child owns its delivery. The parent should continue independent work, report
failures accurately and avoid repeating visible output or regenerating files.
For a revision, delegate again with the same owner and task ID. Question cards
include a question ID; answers carrying `answerTo` are resolved from saved history
and routed to the original worker before skill matching. Plain text follow-ups
retain model-directed routing. There are no merged question cards or separate
resume scheduler. See [runtime contracts](runtime-contracts.md).

Receipts also retain typed `artifacts` and per-call `tool_outcomes`. Only output
references that appear in visible body/preview events enter `artifact_refs`;
ordinary source links are not classified as generated artifacts. Tool errors
remain distinct from fatal run failures and visible delivery status.

Skill proposals are staged by the `skill` tool and delivered through code 12 as
structured events with the full text of every file, so they are not subject to
the summary cap and never appear in ordinary text streaming. A delegated agent's
proposal reaches the user directly; the parent receives only the receipt. User
confirmation is required before anything is saved.

Child history is saved before its end marker. Parent history stores receipts in
tool messages. Full display events are saved separately on those messages as
`delivery_events` for UI replay, and are excluded from model input and token
budget calculations. Context compression preserves recent receipt identities
in its asset index. Closing the stream cancels the SDK run and the event pump,
then saves interruption facts through the shared persistence component. Generated
child task IDs remain recoverable even if cancellation prevents a final receipt.
This does not provide durable execution or automatic recovery after a process
restart. See [runtime components](runtime-components.md).

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

They cover thread reuse/isolation, concurrent live delivery, receipts, partial
failure, proposal preservation, cancellation, automatic handoff, UI task buckets,
SDK schema updates, history restoration and compression/reload behavior.
