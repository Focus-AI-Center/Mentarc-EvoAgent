# MCP configuration and execution

MCP adds configured external tool services to the same SDK tool catalog as
built-in tools. [Tool Search](tool-search.md) controls schema visibility; the
MCP registry controls which services exist and which remote tools are eligible.

## Per-user registry

The optional registry is stored at `.meta/mcp.json` inside the user's Workspace:

- LocalFS: `WORKSPACE_ROOT/<user_id>/.meta/mcp.json`.
- MinIO: `<user_id>/.meta/mcp.json` in the configured bucket.

It is not seeded by default. An operator supplies the JSON through the storage
backend; there is no MCP management API. A missing, malformed or empty registry
provides no MCP services. Storage read failures propagate so discovery can retry.
An HTTP example:

```json
{
  "version": 1,
  "servers": {
    "docs": {
      "transport": "streamable-http",
      "enabled": true,
      "url": "https://mcp.example.com/mcp",
      "timeout": 30,
      "toolFilter": {
        "include": ["search*", "read*"],
        "exclude": ["delete*"]
      }
    }
  }
}
```

Replace the example URL with the deployed service. Optional `headers` are passed
to that HTTP service. `http` is an alias for `streamable-http`; `sse` is also
supported. Timeouts default to 30 seconds and are clamped to 1-120 seconds.
`enabled=false` excludes a configured service. Filters use shell-style patterns
against the original remote tool name; exclusions win.

## HTTP and sandbox stdio

| Transport | Execution | Deployment modes |
|---|---|---|
| `streamable-http`, `http`, `sse` | The API process connects to the configured URL | LocalFS and MinIO |
| `stdio` | A JSON-RPC process runs in the existing session Docker container | MinIO only |

For a stdio service, use `command`, an `args` array and an optional `env` mapping
instead of a URL. For example, a preinstalled Python service could use:

```json
{
  "transport": "stdio",
  "command": "python",
  "args": ["/workspace/skills/source-reader/scripts/server.py"],
  "timeout": 30
}
```

That example service must exist with its dependencies installed. Discovery does
not install packages. The process starts in `/workspace`; command arguments and
environment values are passed through the sandbox backend. The registry removes
its blocked interpreter/environment override variables. This is configuration
validation, not an OS or network isolation boundary.

stdio sessions are owned by the API process and keyed by user, execution session
and MCP server name. The runtime shares the parent's execution session with
workers, so code execution and stdio MCP can reuse the same Docker container.
Concurrent discovery waits for one initialization before receiving the session.
Failed or cancelled initialization closes and removes that session, unblocks
waiters, and allows a later discovery to retry. This wait applies only to startup;
it does not serialize independent business tool calls.

Idle MCP-session cleanup closes its connection separately from container cleanup.
Existing stdio processes can outlive an individual manager or model run; changing
a server's command or credentials does not itself replace the process cached by
that session key. Recreate or close the session when applying such changes.
Multiple API processes do not share the in-memory ownership registry. See
[cloud lifecycle and ownership](cloud-agent.md#persistence-and-api-process-ownership).

## Discovery, names and results

The [manager](../core/mcp/manager.py) discovers enabled services, applies filters
and converts remote schemas through the [adapter](../core/mcp/adapter.py):

- HTTP tool `lookup` on `docs` becomes `mcp__server__docs__lookup`.
- stdio tool `read` on `files` becomes `mcp__sandbox__files__read`.

With Tool Search enabled, the first search normally discovers catalogs and
activates matching schemas. A required or resident MCP name can trigger earlier
discovery. With Tool Search disabled, catalogs load eagerly. Successful catalogs
are cached for the manager's lifetime; failed discovery remains retryable while
successful services remain available.

Use `toolsearch(names=["mcp__server__docs__lookup"])` when a full name is known.
Exact requests never load approximate substitutes. Newly search-granted MCP
definitions obey the same per-call and cumulative schema character budgets as
plugins; oversized definitions remain intact and return an explicit refusal.
See [schema budgets](tool-search.md#schema-character-budgets).

Calling a matched tool uses its original remote name and arguments. Timeouts and
error fields remain explicit tool outcomes. Tool policy also applies to discovered
schemas: delegated children cannot regain forbidden user-facing tools through
search. Arbitrary external MCP capabilities retain the permissions of their
configured service; internal tool policies do not make every remote API read-only.

## Verification

`tests/unit/test_mcp_registry.py` covers registry parsing and filters.
`tests/unit/test_mcp_session_lifecycle.py` checks concurrent initialization,
failure, cancellation and retry without Docker. `tests/unit/test_tool_search.py`
and `tests/unit/test_worker_policy.py` cover discovery and role restrictions.
These tests do not validate a live external MCP service or Docker transport.
