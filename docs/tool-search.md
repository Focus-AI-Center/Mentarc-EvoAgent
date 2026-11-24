# Tool search and deferred loading

`toolsearch` searches tools enabled for the current execution and exposes matching schemas for later steps in the same run. Main agents and sub-agents support it; it is enabled by default.

```python
# Load only the named tools when their exact names are known.
toolsearch(names=["execute_code"])

# Search by capability when the names are unknown.
toolsearch(query="run bash or Python", limit=1)
# After receiving the result, call the matched tool in a later model step.
execute_code(language="bash", code="ls")
```

The tool must already be enabled through plugin configuration, MCP configuration or the skill-loading workflow. Search does not install tools, enable unconfigured services or execute business operations.

For `.meta/mcp.json`, supported transports, filters and shared stdio startup, see
[MCP configuration and execution](mcp.md).

## Scope and ranking

Candidates include loaded internal tools, enabled plugins and configured MCP tools after include/exclude filtering. MCP tools retain full names such as `mcp__server__docs__lookup`; sandbox stdio tools use `mcp__sandbox__`.

Capability queries use local BM25F ranking over tool names, descriptions and
parameter schemas. Field weights are 4, 2 and 1 respectively; descriptions and
schemas are normalized by field length. This gives focused definitions more
weight than equally relevant text buried in a long definition. Matching tool
names have priority; remaining ties are resolved by tool name. snake_case and
camelCase names are split into terms, text is normalized with NFKC/case folding,
and CJK phrases use adjacent-character bigrams. Common query filler words do not
contribute to BM25F. Parameter-only capabilities can therefore match even if the
name and short description omit them.

This is local lexical retrieval, without embedding, Decisions or another model
request. It does not translate a query into a different language. Use the tool's
name or words appearing in its available metadata when a query finds no match.

| Parameter | Rule |
|---|---|
| `query` | Capability text, 1-512 characters after validation; mutually exclusive with `names` |
| `names` | 1-10 exact tool names, each 1-200 characters; mutually exclusive with `query` |
| `limit` | Query results: default 5, from 1 to 10; exact loading processes the requested names |

## Exact-name loading

```python
toolsearch(names=["fetch_url", "mcp__server__docs__lookup"])
```

Exact mode preserves separators and does not substitute fuzzy matches. It accepts
case/Unicode normalization and known historical aliases such as `update_memory`
for `memory`. Repeated canonical names are loaded once. If normalization matches
multiple names, the request must identify one unambiguously. Missing, disabled
or forbidden tools are not loaded; missing names can return up to three eligible
`suggestions`, which are never activated automatically. A request containing both
`query` and `names`, neither one, blank values or invalid limits fails before MCP
discovery.

Results retain the existing `tools` list and `loaded` / `newly_loaded` fields.
`loaded=true` means this search granted or reused access; `newly_loaded=true`
means it created a new independent search grant. Each matched row includes its
source, a short description and complete-definition `schema_chars`, without
returning the parameter schema in the search result body. Missing exact names
have `loaded=false` and `reason=not_found` or `ambiguous_name`. A capability query
sets `has_more=true` when additional ranked matches exist beyond its `limit`.
Full schemas appear in the next model call.

## Schema character budgets

Search adds complete function definitions only when they fit both budgets:

| Setting | Default | Scope |
|---|---:|---|
| `TOOLSEARCH_SEARCH_SCHEMA_CHARS` | 24000 | New independent search grants in one call |
| `TOOLSEARCH_ACTIVE_SCHEMA_CHARS` | 64000 | Cumulative independent search grants in one SDK run |

Both values must be positive integers. Measurement serializes the function's
name, description, parameters, strictness and type as compact Unicode JSON. This
is a character count, not a token count, byte count or total model-context limit.
The runtime never trims a schema to make it fit.

Rows that cannot receive a grant return `loaded=false` with one of:

- `schema_too_large`: the individual definition exceeds the single-search cap;
  the definition or deployment budget must change.
- `search_schema_budget`: the requested set exceeds this call's cap; retry fewer
  exact names in another search.
- `active_schema_budget`: another independent grant would exceed this run's cap;
  repeating the same search does not free capacity.

Other rows can succeed in the same call. The response's `schema_budget` reports
`unit`, `search_limit`, `search_used`, `active_limit` and `active_used`. Reusing a
resident or existing search grant costs no new capacity. Concurrent searches
cannot overcommit the cumulative budget.

Resident tools and direct Skill grants retain their original loading rules and
are not limited by these search caps. Searching a tool currently granted only by
a Skill creates an independent search grant and is accounted for normally;
if that grant is refused, the Skill's existing visibility is unchanged. These
budgets cover neither all resident/Skill schemas nor history and system text.
Skill-owned tools removed during compaction release any associated search budget;
independent grants to configured tools retain their existing run lifetime.

## Loading sequence

1. Agent creation retains candidate tools on the server. Resident tools and `toolsearch` are immediately visible.
2. The first search discovers enabled MCP catalogs. Explicit resident or required MCP names trigger earlier discovery. Protocol adaptation, names and filters remain unchanged.
3. Search activates matching tools for this run.
4. The SDK recalculates enabled tools before the next model call and includes their full schemas.
5. Business tools run through their existing executor. Their original `is_enabled` conditions still apply.

Do not search and invoke a deferred tool in the same parallel model step. Repeated searches do not duplicate registration or execute business operations. Activated tools retain their run lifetime; `limit` bounds each query result, while the schema budgets bound new independent search grants.

## Discovery failures and retry

MCP discovery calls `list_tools`, which may connect to HTTP services or start configured stdio services. It does not call business tools.

HTTP and sandbox consumers propagate discovery failures. The manager records failed servers separately from successful results and caches successful catalogs, including genuinely empty catalogs, for the current run. Partial failure returns available tools plus a `discovery_error`; subsequent searches retry failed servers while reusing successful catalogs. Registry read failures also remain retryable. Discovery is complete only after all configured services succeed. Local search remains available during an outage.

This is application-side search implemented using dynamic tool visibility in the installed Agents SDK. It does not require a provider-native tool-search API or an embedding service.

## Configuration and lifetime

```dotenv
TOOLSEARCH_ENABLED=true
TOOLSEARCH_SEARCH_SCHEMA_CHARS=24000
TOOLSEARCH_ACTIVE_SCHEMA_CHARS=64000
TOOLSEARCH_ALWAYS_LOADED=memory,skill,update_user_profile,todo_manager,ask_user_question,sub_agent,parent_history,worker,worker_status,workspace_file
```

`TOOLSEARCH_ALWAYS_LOADED` selects resident tools from existing candidates; it does not enable a plugin. MCP tools become candidates after discovery. Other tools, including `schedule_manager`, can be loaded by search.

Internal workers apply one tool policy to initial schemas, skill grants and MCP
discovery. Denied tools never enter their catalog, and read-only `skill`/`memory`
actions remain enforced at invocation. See [worker boundaries](workers.md#limits-and-lifecycle).

Workers inherit the main agent's configured plugin pool at dispatch and add their
profile's `required_tools`. Each worker creates independent plugin instances and
resolves the same user's enabled MCP services using the parent execution session.
It maintains its own search visibility and loads its own skill guides; the
parent's existing search and skill grants do not automatically load child schemas.

Matched skills, configured child skills and manual `skill(get)` calls grant
their `required_tools`, including supported aliases. Missing built-in plugin
instances are initialized for the current identity; MCP requirements still use
configured services. A reference-first read includes its main guide. This does
not install software packages or enable an unconfigured MCP service.

Skill-specific visibility grants are tracked separately from resident schemas
and independent search results. Compression removes skill-introduced tools and
grants together with their original instructions; reloading the guide restores
them. See [agent coordination](agent-coordination.md).

Visibility and MCP caches belong to one SDK run. They are not shared across users, parent/child agents or later requests. Saved tool history does not bypass the current configuration.

Set `TOOLSEARCH_ENABLED=false` to expose all current schemas before execution and discover MCP catalogs eagerly; search-specific budgets then do not apply. The independent `SessionUpdater` continues to use its fixed profile, memory and skill tools.

## Maintenance and tests

Implementation: [search catalog](../core/tool_search.py), [local BM25F ranking](../core/tool_search_ranking.py), [MCP manager](../core/mcp/manager.py), [main agent](../core/agent.py), [sub-agent](../core/sub_agent.py).

[test_tool_search.py](../tests/unit/test_tool_search.py) covers deferred visibility, search-then-call through the real SDK loop in streaming and non-streaming modes, ranking, scope isolation, MCP filters, original enablement gates, partial failure/recovery, successful empty catalogs and disabling search.

[test_tool_search_loading.py](../tests/unit/test_tool_search_loading.py) covers exact
loading without substitutions, validation before discovery, context and worker
restrictions, per-call/cumulative limits, concurrent grants, repeated loads,
resident and Skill behavior, Unicode character accounting against the SDK's wire
definition, metadata cleanup, parameter-only matching and local field ranking.
The real SDK search-then-call tests cover both query and exact modes.
