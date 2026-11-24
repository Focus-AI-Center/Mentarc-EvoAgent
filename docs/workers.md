# File-producing workers and workspace agent definitions

Skills hold reusable methods. An internal worker executes one assigned task in
its own model context and writes files for the main agent. The main agent owns
user questions, verification, delivery and persistent-change proposals.
Interactive sub-agents remain available for direct user sessions, including
`session-reviewer`. When the main agent delegates to one, the runtime adds the
same internal-audience contract used by workers. Workers reuse `SubAgentRunner`,
history, compaction and usage accounting. Invocation context determines the
audience; it is not a property inferred from an ordinary agent's name.

## Agent definitions

The server seeds missing `configs/agents/*.md` templates into each user's
`agents/` workspace directory. `DEFAULT_AGENTS_DIR` changes the template source.
LocalFS stores these under `WORKSPACE_ROOT/<user_id>/agents/`; MinIO stores
`<user_id>/agents/<name>.md` in the configured bucket.

Workspace definitions override built-in defaults. Existing definitions are
preserved on template synchronization, including edits to seeded profiles.
Deleting a workspace definition exposes its built-in fallback, if any. Invalid
overrides are omitted from the catalog instead of silently reviving the default.
Unreadable or invalid default definitions are skipped individually; a malformed
UTF-8 template does not prevent other agents or user workspaces from loading.
Definitions must be UTF-8 Markdown with matching filename/name, instructions,
and optional YAML configuration:

```yaml
---
name: worker
description: Execute independent tasks and return workspace files.
execution_mode: worker
required_tools: [execute_code]
skills: []
max_steps: 30
timeout: 300
---
Complete the assigned task and save substantial outputs to the supplied directory.
```

`execution_mode` defaults to `interactive`. Worker profiles are invoked through
`worker`, not `/api/sub-agent/<type>/chat`. The main agent loads one effective
catalog per turn, uses its metadata in tool descriptions and passes that same
configuration to the child executor. Changes become visible on the next turn.
Full child instructions are not injected into the main model's context.

`GET /api/sub-agent/types?userId=alice` lists Alice's effective catalog, including
each profile's `execution_mode`. Omitting `userId` lists platform defaults only.
The built-in UI supplies its current user ID. Snapshots include agent definitions,
internal worker histories and binary worker files; older snapshots remain valid.

## Dispatch and collection

```python
worker(task="Research the three supplied sources and save a sourced comparison.",
       title="Source comparison", skills=["research-report"],
       task_id="comparison-a", background=True)
# Continue independent work, then collect results.
worker_status(task_ids=["comparison-a"], wait_seconds=30)
workspace_file(action="read", path="workers/<thread>/<attempt>/response.md")
```

The example skill must exist in the user's catalog. Specified skills load before
execution; the worker can discover other available skills as needed. Task input
contains the assignment and a snapshot of current user input, attachments and
prior resource references. It does not automatically copy the parent's transcript.

Worker tool capabilities start with the main agent's configured plugins, captured
when dispatching, plus the profile's `required_tools`. That field adds baseline
tools; it is not an allowlist. Plugins are instantiated separately with the
worker's output directory and the parent's execution session. Internal tools are
rebuilt for the worker, so mutable tool objects, todo lists and Tool Search grants
are not shared. Both agents resolve enabled MCP services from the same user's
configuration, using independent managers and discovery state. A worker loads
skills itself to obtain their instructions and additional tool grants; a skill
loaded only by the main agent does not automatically load in the worker. The
worker policy filters inherited, skill-mounted and discovered tools alike.

`background=true` is the default and returns a running receipt immediately.
`background=false` waits up to `WORKER_WAIT_SECONDS`, then returns either the
completed receipt or a running receipt. `worker_status` returns completed,
uncollected receipts plus progress for running tasks. Waiting does not cancel a
task; unknown task IDs return an error and do not consume other results.

Worker text, questions and proposal cards are not forwarded to the user. A
single expandable progress card per execution attempt reports the task, tool
activity and terminal file links. Clients receive
progress through existing code 5 frames named `worker_progress`, with
a stable per-attempt card ID and `streaming=true` until the terminal frame.
The dispatch receipt binds to the same card even when progress arrives first.
Completed calls, failures, timeouts and cancellation remain distinct; a reused
task ID gets a new card for its new output directory. Saved receipts reconstruct
these cards when history is restored. Worker text and raw tool results are not
included in progress frames.

File links download internal artifacts through
`GET /api/workspace/{user_id}/worker-files/{file_path}`. This read-only endpoint
accepts only paths below `workers/`, applies the Workspace path checks, supports
binary files on either storage backend, and forces attachment downloads with
`nosniff`. Worker-generated HTML does not execute in the chat page. The sidebar
labels worker and interactive profiles separately.
The main agent must collect and inspect results. If it returns early, the runtime waits for
workers and appends paired `worker_status` tool facts, then continues the model
without adding another user message. At most two such continuations are allowed.
A user question ends the current turn and cancels unfinished workers.

## Files and execution

Every invocation receives a fresh output directory:

```text
workers/task_<parent-and-task-hash>/<attempt-id>/
├── comparison.csv
├── sources.md
└── response.md
```

`workspace_file` reads, lists and writes UTF-8 text directly through the storage
backend. Worker writes through this tool are confined to their supplied output
directory; main-agent instances of the tool are read-only. Writes accept up to
1 MiB; reads expose up to 20,000 characters with a truncation flag. Use code
execution for larger selections, scripts or binary files. The runtime saves the
complete worker response as `response.md` (reserved for the runtime), or
`partial-response.md` after interruption, and lists stored files in the receipt.
File lists contain at most 100 paths; `output_dir` allows listing the remainder.

The receipt contains task/profile identity, status, output directory, file paths,
a summary of up to 1,500 characters, elapsed time and individual tool outcomes.
Files are internal material until the main agent verifies and delivers them.
`completed` describes execution with stored output, not a verification of its
correctness; individual failed tool calls and unresolved gaps still need review.
Task identity and the output directory survive context compression independently
of the model-generated summary.

Both deployment modes expose the same worker tools:

| Mode | Files | Code execution |
|---|---|---|
| LocalFS | User workspace on disk | Host Bash/Python subprocess |
| MinIO | User-prefix objects through the SDK | Existing session Docker backend |

Starting a worker does not create a container. Only code execution or stdio MCP
needs the sandbox. The code tool defaults to the worker output directory, while
explicit skill-script working directories remain supported. Workers share the
parent's execution session and sandbox when needed, while their conversation
histories remain independent. A task directory
is an output convention, not a dedicated container or OS security boundary.
Code retains the existing backend's filesystem and network scope.

## Limits and lifecycle

| Setting | Default | Scope |
|---|---:|---|
| `WORKER_ENABLED` | true | Expose worker dispatch and collection tools in both modes |
| `WORKER_MAX_PER_TURN` | 8 | Total dispatches in one request |
| `WORKER_MAX_CONCURRENCY` | 4 | Active workers in one request; excess tasks queue |
| `WORKER_TURN_TIMEOUT_SECONDS` | 540 | Request budget, measured from turn start |
| `WORKER_REPLY_RESERVE_SECONDS` | 60 | Time reserved for the main agent and final storage |
| `WORKER_WAIT_SECONDS` | 30 | Initial wait when `background=false` |
| `WORKER_CANCEL_TIMEOUT_SECONDS` | 20 | Maximum wait for worker cleanup |

A job's timeout is capped at 300 seconds and the remaining worker budget;
queueing time counts. Worker profiles cannot exceed 30 SDK steps. The main SDK
stream observes the request deadline once a worker is dispatched. Final storage
and cancellation cleanup have their existing separate bounded waits, so the
request budget is not a hard deadline for every remote side effect.

Worker tool policy blocks user questions, further delegation, profile updates,
scheduling and delivery tools. `memory` allows only `read`/`get`; `skill` allows
only `list`/`get`/`get_ref`. These restrictions apply to initial tools, skill
mounts and Tool Search/MCP discovery, including eager discovery. They govern
tool access and do not make arbitrary code or external services read-only.
Creating a skill in a worker means preparing candidate files for the main agent;
the normal proposal/confirmation flow still owns installation.

Tasks belong to the active request. Disconnect, cancellation or normal final
cleanup cancels unfinished model tasks, preserves child histories when storage
succeeds and replaces uncollected running receipts with terminal facts. Existing
code-execution side effects may outlive local cancellation; verify before retrying.
Workers share the parent's usage collector, and final metrics follow cleanup.

Reuse the same parent session, worker profile and `task_id` to continue saved
child history in a later turn. Each attempt gets new output files. Task IDs are
1-64 ASCII letters, digits, underscores or hyphens, starting with a letter/digit.
Same-request active duplicates are rejected; no session execution lock is added.
Independent concurrent requests are not serialized.

This is request-local scheduling, without Redis or a durable job queue. A process
crash cannot automatically resume live tasks. Status queries do not address an
earlier request; file artifacts and saved history remain available independently.

The default worker profile declares `execute_code`, independently of the main
agent's `DEFAULT_PLUGINS`. Set `WORKER_ENABLED=false` to remove worker dispatch
and collection tools when this capability is unwanted. Other configured agents,
skills and tool dependencies retain their existing behavior.

## Verification

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p asyncio \
  tests/unit/test_agent_registry.py tests/unit/test_workers.py \
  tests/unit/test_worker_policy.py -q
```

Offline coverage uses real SDK loops with scripted models, LocalFS and the MinIO
SDK with mocked object transport. It covers catalog precedence and snapshots,
background progress, tool inheritance, file output and downloads, collection, deadlines, cancellation, tool
policy and history continuation. It does not measure live-model quality or
Docker/MinIO deployment performance.
