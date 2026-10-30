"""English repository policy and contracts affected by the language migration."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from core.plugins.internal import (
    _ALL_FIELDS,
    _BASIC_MULTI,
    _BASIC_SINGLE,
    _GENERAL_MULTI,
    _TRADE_SINGLE,
    UpdateUserProfileParams,
    UpdateUserProfileTool,
    _parse_user_fields,
)
from utils.extractors import extract_inquiry_analysis, extract_product_replication
from utils.storage_backend.localfs import LocalFSBackend
from utils.workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]")


def test_repository_source_docs_and_paths_have_no_han_text():
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("Policy scan requires a git checkout")
    files = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    violations = []
    for name in set(files):
        path = ROOT / name
        if not path.is_file():
            continue
        if HAN.search(name):
            violations.append(f"Non-English path: {name}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if HAN.search(line):
                violations.append(f"{name}:{number}")
    assert not violations, "Repository content must be English: " + ", ".join(sorted(violations))


@pytest.fixture
def workspace(tmp_path):
    return Workspace("alice", LocalFSBackend(tmp_path))


@pytest.mark.asyncio
async def test_english_profile_tool_preserves_fields_and_multiline_data(workspace):
    tool = UpdateUserProfileTool(user_id="alice", workspace=workspace)
    result = json.loads(
        await tool(
            UpdateUserProfileParams(
                action="update",
                name="Alice",
                company_name="ACME",
                main_products="solar panels",
                background="Engineer",
                preferences="Concise replies",
                social_accounts="https://example.com/alice",
            )
        )
    )
    assert result["success"] and result["fields"]["Company name"] == "ACME"
    await tool(
        UpdateUserProfileParams(
            action="append", main_products="solar panels, battery packs", preferences="Use tables"
        )
    )
    fields = _parse_user_fields(workspace.read_prompt("USER"))
    assert fields["Name"] == "Alice" and fields["Company name"] == "ACME"
    assert fields["Main products"] == "solar panels, battery packs"
    assert "Engineer" in fields["Background"]
    assert "Concise replies" in fields["Preferences"] and "Use tables" in fields["Preferences"]
    assert "https://example.com/alice" in fields["Social accounts"]


@pytest.mark.asyncio
async def test_profile_api_awaits_english_field_updates(workspace, monkeypatch):
    import app.api.routers.user as user_router
    import core.plugins.internal as internal

    monkeypatch.setattr(user_router, "Workspace", lambda _user_id: workspace)
    monkeypatch.setattr(internal, "Workspace", lambda _user_id: workspace)
    app = FastAPI()
    app.include_router(user_router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        updated = await client.put("/profile", json={"user_id": "alice", "company_name": "ACME"})
        profile = await client.post("/profile", json={"user_id": "alice"})
    assert updated.status_code == 200
    assert updated.json()["updated"] == ["company_name"]
    assert profile.json()["data"]["company_name"] == "ACME"


@pytest.mark.asyncio
async def test_legacy_profile_values_survive_english_label_migration(workspace):
    ordered = _BASIC_SINGLE + _BASIC_MULTI + _TRADE_SINGLE + _GENERAL_MULTI
    legacy = []
    for number, label in enumerate(ordered):
        # Generate localized legacy labels as data; repository text stays English.
        prefix = chr(0x4E00 + number) + chr(0xFF1A)
        legacy.append(
            prefix + (f"\nValue {number}" if label in _BASIC_MULTI + _GENERAL_MULTI else f" Value {number}")
        )
    workspace.write_prompt("USER", "\n".join(legacy))
    before = _parse_user_fields(workspace.read_prompt("USER"))
    assert all(before[label] == f"Value {i}" for i, label in enumerate(ordered))
    result = json.loads(
        await UpdateUserProfileTool(user_id="alice", workspace=workspace)(
            UpdateUserProfileParams(action="update", company_name="New company")
        )
    )
    assert result["success"]
    after = _parse_user_fields(workspace.read_prompt("USER"))
    assert after["Company name"] == "New company"
    assert all(after[label] == before[label] for label in _ALL_FIELDS if label != "Company name")
    assert not HAN.search(workspace.read_prompt("USER"))


def test_partial_legacy_profile_is_rejected_before_data_can_be_lost():
    with pytest.raises(ValueError, match="migrate its labels"):
        _parse_user_fields(chr(0x4E00) + chr(0xFF1A) + " Existing value")


@pytest.mark.asyncio
async def test_english_profile_keeps_user_supplied_unicode_content(workspace):
    text = chr(0x4E00) + chr(0xFF1A) + " User-provided value"
    tool = UpdateUserProfileTool(user_id="alice", workspace=workspace)
    result = json.loads(await tool(UpdateUserProfileParams(action="update", preferences=text)))
    assert result["success"]
    assert _parse_user_fields(workspace.read_prompt("USER"))["Preferences"] == text


def test_frontend_profile_parser_displays_english_fields():
    if not shutil.which("node"):
        pytest.skip("Node.js required for frontend checks")
    html = (ROOT / "app/static/index.html").read_text()
    function = (
        "function parseUserProfile"
        + html.split("function parseUserProfile", 1)[1].split("function renderProfileTable", 1)[0]
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            function
            + "\nprocess.stdout.write(JSON.stringify(parseUserProfile('---\\nname: USER\\n---\\n## Basic information\\nName: Alice\\nCompany name: ACME\\n(hint)')));",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        {"name": "Name", "value": "Alice"},
        {"name": "Company name", "value": "ACME"},
    ]


def test_english_report_and_request_extractors():
    analysis = extract_inquiry_analysis(
        "## Assessment summary\n**Inquiry subject**: Samples\n**Buyer information**: ACME\n**Core concerns**: Delivery"
    )
    assert analysis == {
        "type": "inquiry_analysis",
        "subject": "Samples",
        "buyer_info": "ACME",
        "core_concerns": "Delivery",
    }
    product = extract_product_replication(
        "Please analyze this product https://example.com/item", {"main_products": "Battery packs"}
    )
    assert product["product_name"] == ""
    assert product["my_product_info"] == "Battery packs"
    assert product["competitor_urls"] == ["https://example.com/item"]
