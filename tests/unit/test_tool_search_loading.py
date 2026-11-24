"""Exact-name loading, local field ranking and bounded search grants."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from agents import Agent
from agents.run_context import RunContextWrapper

from configs import settings
from core.runtime.tool_policy import WORKER_POLICY
from core.tool_search import configure_agent_tools, schema_chars
from tests.unit.test_tool_search import make_tool, search, visible


@pytest.fixture(autouse=True)
def search_settings(monkeypatch):
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOLSEARCH_ALWAYS_LOADED", ("memory", "skill"))
    monkeypatch.setattr(settings, "TOOLSEARCH_SEARCH_SCHEMA_CHARS", 24000)
    monkeypatch.setattr(settings, "TOOLSEARCH_ACTIVE_SCHEMA_CHARS", 64000)


async def invoke(agent, params, context=None):
    tool = next(tool for tool in agent.tools if tool.name == "toolsearch")
    result = await tool.on_invoke_tool(RunContextWrapper(context=context), json.dumps(params))
    return json.loads(result)


@pytest.mark.asyncio
async def test_exact_names_never_load_nearby_tools_and_missing_names_only_suggest():
    agent = Agent(name="test", tools=[make_tool("read_report"), make_tool("read_reports")])
    await configure_agent_tools(agent)
    result = await invoke(agent, {"names": ["read-reprot"]})
    row = result["tools"][0]
    assert row["loaded"] is False and row["reason"] == "not_found"
    assert "read_report" in row["suggestions"]
    assert await visible(agent) == {"toolsearch"}
    result = await invoke(agent, {"names": ["read_report", "read_report"]})
    assert [row["name"] for row in result["tools"]] == ["read_report"]
    assert await visible(agent) == {"toolsearch", "read_report"}


@pytest.mark.asyncio
async def test_exact_names_normalize_case_and_known_legacy_aliases():
    agent = Agent(name="test", tools=[make_tool("memory"), make_tool("execute_code")])
    await configure_agent_tools(agent)
    result = await invoke(agent, {"names": ["update_memory", "MEMORY", " EXECUTE_CODE "]})
    assert [row["name"] for row in result["tools"]] == ["memory", "execute_code"]
    assert result["tools"][0]["newly_loaded"] is False
    assert result["tools"][1]["newly_loaded"] is True


@pytest.mark.asyncio
async def test_exact_names_respect_original_enablement_and_worker_policy():
    async def allowed(ctx, _agent):
        return ctx.context == "owner"

    agent = Agent(
        name="test",
        tools=[
            make_tool("read_public"),
            make_tool("read_private", enabled=allowed),
            make_tool("ask_user_question"),
        ],
    )
    await configure_agent_tools(agent, policy=WORKER_POLICY)
    result = await invoke(agent, {"names": ["read_private", "ask_user_question"]}, "visitor")
    assert all(row["loaded"] is False for row in result["tools"])
    assert all("read_private" not in row["suggestions"] for row in result["tools"])
    assert await visible(agent, "visitor") == {"toolsearch"}
    assert (await invoke(agent, {"names": ["read_private"]}, "owner"))["tools"][0]["loaded"]
    assert "read_private" not in await visible(agent, "visitor")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"query": "read", "names": ["read"]},
        {"names": []},
        {"names": [" "]},
        {"names": ["a" * 201]},
        {"names": ["read"] * 11},
        {"names": [None]},
        {"query": None, "names": None},
        {"query": " "},
        {"names": ["read"], "limit": 0},
    ],
)
async def test_invalid_modes_and_names_do_not_discover_or_change_state(params):
    loader = AsyncMock(side_effect=AssertionError("No discovery for invalid input"))
    agent = Agent(name="test")
    await configure_agent_tools(agent, loader=loader)
    assert "error" in await invoke(agent, params)
    loader.assert_not_awaited()
    assert await visible(agent) == {"toolsearch"}


@pytest.mark.asyncio
async def test_schema_too_large_stays_hidden_and_its_definition_is_not_truncated(monkeypatch):
    tool = make_tool("large", "Detailed operation " * 100)
    original = deepcopy(tool.params_json_schema)
    monkeypatch.setattr(settings, "TOOLSEARCH_SEARCH_SCHEMA_CHARS", schema_chars(tool) - 1)
    agent = Agent(name="test", tools=[tool, make_tool("small")])
    await configure_agent_tools(agent)
    result = await invoke(agent, {"names": ["large"]})
    assert result["tools"][0]["reason"] == "schema_too_large"
    assert result["schema_budget"]["search_used"] == 0
    assert result["schema_budget"]["active_used"] == 0
    assert "large" not in await visible(agent)
    assert next(t for t in agent.tools if t.name == "large").params_json_schema == original
    assert (await invoke(agent, {"names": ["small"]}))["tools"][0]["loaded"]


@pytest.mark.asyncio
async def test_single_search_budget_allows_partial_loading_and_separate_retry(monkeypatch):
    tools = [make_tool("first"), make_tool("second")]
    sizes = [schema_chars(tool) for tool in tools]
    monkeypatch.setattr(settings, "TOOLSEARCH_SEARCH_SCHEMA_CHARS", max(sizes))
    agent = Agent(name="test", tools=tools)
    await configure_agent_tools(agent)
    first = await invoke(agent, {"names": ["first", "second"]})
    assert first["tools"][0]["loaded"]
    assert first["tools"][1]["reason"] == "search_schema_budget"
    assert first["schema_budget"]["search_used"] == sizes[0]
    assert await visible(agent) == {"toolsearch", "first"}
    second = await invoke(agent, {"names": ["second"]})
    assert second["tools"][0]["loaded"]
    assert second["schema_budget"]["active_used"] == sum(sizes)


@pytest.mark.asyncio
async def test_cumulative_budget_blocks_new_grants_and_does_not_charge_repeats(monkeypatch):
    tools = [make_tool("first"), make_tool("second")]
    monkeypatch.setattr(settings, "TOOLSEARCH_ACTIVE_SCHEMA_CHARS", schema_chars(tools[0]))
    agent = Agent(name="test", tools=tools)
    await configure_agent_tools(agent)
    first = await invoke(agent, {"names": ["first"]})
    denied = await invoke(agent, {"names": ["second"]})
    assert denied["tools"][0]["reason"] == "active_schema_budget"
    again = await invoke(agent, {"names": ["first", "first"]})
    assert again["tools"][0]["loaded"] and not again["tools"][0]["newly_loaded"]
    assert again["schema_budget"]["search_used"] == 0
    assert again["schema_budget"]["active_used"] == first["schema_budget"]["active_used"]
    assert "second" not in await visible(agent)


@pytest.mark.asyncio
async def test_resident_and_skill_grants_have_their_original_loading_rules(monkeypatch):
    monkeypatch.setattr(settings, "TOOLSEARCH_SEARCH_SCHEMA_CHARS", 1)
    monkeypatch.setattr(settings, "TOOLSEARCH_ACTIVE_SCHEMA_CHARS", 1)
    resident, skill = make_tool("memory", "Large resident guide " * 100), make_tool("skill_helper")
    agent = Agent(name="test", tools=[resident])
    catalog = await configure_agent_tools(agent)
    await catalog.mount_skill_tools(["skill_helper"], [skill])
    assert await visible(agent) == {"toolsearch", "memory", "skill_helper"}
    result = await invoke(agent, {"names": ["memory"]})
    assert result["tools"][0]["loaded"]
    assert result["schema_budget"]["active_used"] == 0


@pytest.mark.asyncio
async def test_removed_skill_tools_release_their_search_budget_and_ranking_metadata(monkeypatch):
    tool = make_tool("skill_helper")
    monkeypatch.setattr(settings, "TOOLSEARCH_ACTIVE_SCHEMA_CHARS", schema_chars(tool))
    agent = Agent(name="test", tools=[make_tool("next_tool")])
    catalog = await configure_agent_tools(agent)
    await catalog.mount_skill_tools(["skill_helper"], [tool])
    assert (await invoke(agent, {"names": ["skill_helper"]}))["schema_budget"]["active_used"] > 0
    catalog.unmount_skill_tools({"skill_helper"})
    result = await invoke(agent, {"names": ["next_tool"]})
    assert result["tools"][0]["loaded"] and "skill_helper" not in catalog.names


@pytest.mark.asyncio
async def test_budget_measures_unicode_characters_not_utf8_bytes(monkeypatch):
    from agents.models.openai_responses import Converter

    tool = make_tool("unicode_tool", "\u00e9" * 100)
    wire_definition, _ = Converter._convert_tool(tool)
    serialized = json.dumps(wire_definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert schema_chars(tool) == len(serialized) < len(serialized.encode("utf-8"))
    monkeypatch.setattr(settings, "TOOLSEARCH_SEARCH_SCHEMA_CHARS", len(serialized))
    monkeypatch.setattr(settings, "TOOLSEARCH_ACTIVE_SCHEMA_CHARS", len(serialized))
    agent = Agent(name="test", tools=[tool])
    await configure_agent_tools(agent)
    assert (await invoke(agent, {"names": ["unicode_tool"]}))["tools"][0]["loaded"]


@pytest.mark.asyncio
async def test_parallel_searches_cannot_overcommit_the_run_budget(monkeypatch):
    tools = [make_tool("first"), make_tool("second")]
    monkeypatch.setattr(settings, "TOOLSEARCH_ACTIVE_SCHEMA_CHARS", max(schema_chars(tool) for tool in tools))
    agent = Agent(name="test", tools=tools)
    await configure_agent_tools(agent)
    results = await asyncio.gather(*(invoke(agent, {"names": [tool.name]}) for tool in tools))
    assert sum(row["loaded"] for result in results for row in result["tools"]) == 1
    assert len(await visible(agent)) == 2


@pytest.mark.asyncio
async def test_invalid_discovered_metadata_does_not_poison_healthy_search():
    broken = make_tool("broken")
    broken.params_json_schema = {"type": "object", "default": object()}
    loader = AsyncMock(return_value=[broken])
    agent = Agent(name="test", tools=[make_tool("healthy")])
    catalog = await configure_agent_tools(agent, loader=loader)
    for _ in range(2):
        result = await invoke(agent, {"names": ["healthy"]})
        assert result["tools"][0]["loaded"] and "discovery_error" in result
    assert "broken" not in catalog.names


@pytest.mark.asyncio
async def test_bm25f_weights_names_descriptions_and_parameter_fields():
    name = make_tool("invoice_lookup", "Generic operation")
    description = make_tool("description_tool", "Invoice")
    parameter = make_tool("parameter_tool", "Generic operation")
    parameter.params_json_schema = {"type": "object", "properties": {"invoice": {"type": "string"}}}
    agent = Agent(name="test", tools=[parameter, description, name])
    await configure_agent_tools(agent)
    result = await search(agent, "invoice")
    assert [tool["name"] for tool in result["tools"]] == [name.name, description.name, parameter.name]


@pytest.mark.asyncio
async def test_bm25f_finds_camel_case_names_and_parameter_only_capabilities():
    name = make_tool("mcp__server__tax__getHTTPInvoice", "Generic operation")
    parameter = make_tool("shipping_tool", "Generic operation")
    parameter.params_json_schema = {"type": "object", "properties": {"postalCode": {"type": "string"}}}
    agent = Agent(name="test", tools=[name, parameter])
    await configure_agent_tools(agent)
    assert (await search(agent, "HTTP invoice"))["tools"][0]["name"] == name.name
    assert (await search(agent, "postal"))["tools"][0]["name"] == parameter.name


@pytest.mark.asyncio
async def test_bm25f_prefers_focused_descriptions_and_deduplicates_query_terms():
    focused = make_tool("focused", "Reconcile invoices")
    verbose = make_tool("verbose", "Reconcile " + "unrelated " * 200)
    agent = Agent(name="test", tools=[verbose, focused])
    await configure_agent_tools(agent)
    one = await search(agent, "reconcile", limit=1)
    repeated = await search(agent, "reconcile reconcile", limit=1)
    assert one["tools"][0]["name"] == repeated["tools"][0]["name"] == focused.name
    assert one["has_more"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["TOOLSEARCH_SEARCH_SCHEMA_CHARS", "TOOLSEARCH_ACTIVE_SCHEMA_CHARS"])
async def test_enabled_discovery_requires_positive_budgets_and_eager_mode_bypasses_them(monkeypatch, setting):
    monkeypatch.setattr(settings, setting, 0)
    with pytest.raises(ValueError, match="must be positive"):
        await configure_agent_tools(Agent(name="test"))
    monkeypatch.setattr(settings, "TOOLSEARCH_ENABLED", False)
    agent = Agent(name="test", tools=[make_tool("business")])
    assert await configure_agent_tools(agent) is None
    assert await visible(agent) == {"business"}
