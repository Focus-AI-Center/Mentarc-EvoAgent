# Tool search and deferred loading

`toolsearch` searches tools enabled for the current execution and exposes matching schemas for later steps in the same run. Main agents and sub-agents support it; it is enabled by default.

```python
toolsearch(query="execute_code", limit=1)
# After receiving the result, call the matched tool in a later model step.
execute_code(language="bash", code="ls")
```

The tool must already be enabled through plugin configuration, MCP configuration or the skill-loading workflow. Search does not install tools, enable unconfigured services or execute business operations.

## Scope and ranking

Candidates include loaded internal tools, enabled plugins and configured MCP tools after include/exclude filtering. MCP tools retain full names such as `mcp__server__docs__lookup`; sandbox stdio tools use `mcp__sandbox__`.

Ranking uses local keyword matching over names and descriptions. Exact names rank highest, and name matches outweigh description matches. It is not embedding or LLM semantic retrieval. Try a precise name, capability or MCP service name when results are poor. Unicode queries are supported.

| Parameter | Rule |
|---|---|
| `query` | Required, nonempty, at most 512 characters |
| `limit` | Default 5, from 1 to 10 |

Results include `name`, a short `description`, `source`, `loaded` and `newly_loaded`. The full schema appears in the next model call rather than being repeated in the search result body.

## Loading sequence

1. Agent creation retains candidate tools on the server. Resident tools and `toolsearch` are immediately visible.
2. The first search discovers enabled MCP catalogs. Explicit resident or required MCP names trigger earlier discovery. Protocol adaptation, names and filters remain unchanged.
3. Search activates matching tools for this run.
4. The SDK recalculates enabled tools before the next model call and includes their full schemas.
5. Business tools run through their existing executor. Their original `is_enabled` conditions still apply.

Do not search and invoke a deferred tool in the same parallel model step. Repeated searches do not duplicate registration or execute business operations. Activated tools remain visible for the run; `limit` bounds each result, not the total active set.

## Discovery failures and retry

MCP discovery calls `list_tools`, which may connect to HTTP services or start configured stdio services. It does not call business tools.

HTTP and sandbox consumers propagate discovery failures. The manager records failed servers separately from successful results and caches successful catalogs, including genuinely empty catalogs, for the current run. Partial failure returns available tools plus a `discovery_error`; subsequent searches retry failed servers while reusing successful catalogs. Registry read failures also remain retryable. Discovery is complete only after all configured services succeed. Local search remains available during an outage.

This is application-side search implemented using dynamic tool visibility in the installed Agents SDK. It does not require a provider-native tool-search API or an embedding service.

## Configuration and lifetime

```dotenv
TOOLSEARCH_ENABLED=true
TOOLSEARCH_ALWAYS_LOADED=memory,skill,update_user_profile,todo_manager,ask_user_question,sub_agent,parent_history
```

`TOOLSEARCH_ALWAYS_LOADED` selects resident tools from existing candidates; it does not enable a plugin. MCP tools become candidates after discovery. Other tools, including `schedule_manager`, can be loaded by search.

Matched skills and sub-agent configurations preload `required_tools`, including supported aliases. Reading a skill manually does not install an unavailable dependency.

Visibility and MCP caches belong to one SDK run. They are not shared across users, parent/child agents or later requests. Saved tool history does not bypass the current configuration.

Set `TOOLSEARCH_ENABLED=false` to expose all current schemas before execution and discover MCP catalogs eagerly. The independent `SessionUpdater` continues to use its fixed profile, memory and skill tools.

## Maintenance and tests

Implementation: [search catalog](../core/tool_search.py), [MCP manager](../core/mcp/manager.py), [main agent](../core/agent.py), [sub-agent](../core/sub_agent.py).

[test_tool_search.py](../tests/unit/test_tool_search.py) covers deferred visibility, search-then-call through the real SDK loop in streaming and non-streaming modes, ranking, scope isolation, MCP filters, original enablement gates, partial failure/recovery, successful empty catalogs and disabling search.
