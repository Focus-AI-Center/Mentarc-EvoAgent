"""Unit tests for ``utils.storage_backend.localfs.LocalFSBackend``.

Covers the full ``StorageBackend`` surface plus the LocalFS-specific
ETag-via-MD5 scheme and path-traversal guards.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from utils.storage_backend import (
    IF_MATCH_ABSENT,
    ETagMismatchError,
    KeyNotFoundError,
    StorageBackend,
)
from utils.storage_backend.localfs import LocalFSBackend


@pytest.fixture
def backend(tmp_path):
    return LocalFSBackend(root=tmp_path)


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ── Protocol conformance ─────────────────────────────────────────────────


def test_satisfies_protocol(backend):
    assert isinstance(backend, StorageBackend)


# ── read / write ─────────────────────────────────────────────────────────


async def test_write_then_read_text(backend):
    etag = await backend.write_text("alice/prompts/SOUL.md", "I am SOUL")
    assert etag == _md5_hex(b"I am SOUL")
    assert await backend.read_text("alice/prompts/SOUL.md") == "I am SOUL"


async def test_write_then_read_bytes(backend):
    payload = b"\x00\x01\x02\xff" * 256
    etag = await backend.write_bytes("alice/images/blob.bin", payload)
    assert etag == _md5_hex(payload)
    assert await backend.read_bytes("alice/images/blob.bin") == payload


async def test_read_missing_raises(backend):
    with pytest.raises(KeyNotFoundError):
        await backend.read_text("alice/prompts/missing.md")
    with pytest.raises(KeyNotFoundError):
        await backend.read_bytes("alice/images/missing.bin")


async def test_overwrite_replaces_atomically(backend):
    await backend.write_text("alice/prompts/X.md", "v1")
    await backend.write_text("alice/prompts/X.md", "v2")
    assert await backend.read_text("alice/prompts/X.md") == "v2"


async def test_unicode_text_roundtrip(backend):
    text = "测试 — émoji 🚀\n你好"
    etag = await backend.write_text("alice/prompts/zh.md", text)
    assert etag == _md5_hex(text.encode("utf-8"))
    assert await backend.read_text("alice/prompts/zh.md") == text


# ── head_etag ────────────────────────────────────────────────────────────


async def test_head_etag_present(backend):
    etag = await backend.write_text("alice/prompts/MEMORY.md", "abc")
    assert await backend.head_etag("alice/prompts/MEMORY.md") == etag


async def test_head_etag_missing(backend):
    assert await backend.head_etag("alice/prompts/nope.md") is None


# ── if_match: optimistic CAS ─────────────────────────────────────────────


async def test_if_match_success(backend):
    etag1 = await backend.write_text("alice/prompts/M.md", "v1")
    etag2 = await backend.write_text("alice/prompts/M.md", "v2", if_match=etag1)
    assert etag2 == _md5_hex(b"v2")
    assert await backend.read_text("alice/prompts/M.md") == "v2"


async def test_if_match_conflict(backend):
    await backend.write_text("alice/prompts/M.md", "v1")
    with pytest.raises(ETagMismatchError) as excinfo:
        await backend.write_text("alice/prompts/M.md", "v2", if_match="deadbeef")
    assert excinfo.value.key == "alice/prompts/M.md"
    assert excinfo.value.expected == "deadbeef"
    assert await backend.read_text("alice/prompts/M.md") == "v1"  # unchanged


async def test_if_match_absent_creates(backend):
    """if_match='' means the key must not exist (analog of If-None-Match: *)."""
    etag = await backend.write_text(
        "alice/prompts/new.md", "first", if_match=IF_MATCH_ABSENT
    )
    assert etag == _md5_hex(b"first")


async def test_if_match_absent_conflict_when_present(backend):
    await backend.write_text("alice/prompts/E.md", "exists")
    with pytest.raises(ETagMismatchError):
        await backend.write_text(
            "alice/prompts/E.md", "again", if_match=IF_MATCH_ABSENT
        )


async def test_if_match_against_missing_key(backend):
    """if_match=<etag> on a missing key → mismatch (current=None)."""
    with pytest.raises(ETagMismatchError):
        await backend.write_text(
            "alice/prompts/new.md", "x", if_match="cafef00d"
        )


# ── list_keys ────────────────────────────────────────────────────────────


async def test_list_keys_recursive(backend):
    await backend.write_text("alice/prompts/SOUL.md", "s")
    await backend.write_text("alice/prompts/USER.md", "u")
    await backend.write_text("alice/skills/code/SKILL.md", "c")
    await backend.write_text("alice/sessions/s1.json", "{}")

    keys = await backend.list_keys("alice/")
    assert keys == sorted(
        [
            "alice/prompts/SOUL.md",
            "alice/prompts/USER.md",
            "alice/skills/code/SKILL.md",
            "alice/sessions/s1.json",
        ]
    )


async def test_list_keys_partial_prefix(backend):
    await backend.write_text("alice/prompts/SOUL.md", "s")
    await backend.write_text("alice/prompts/USER.md", "u")
    await backend.write_text("alice/sessions/s1.json", "{}")

    assert await backend.list_keys("alice/prompts/") == [
        "alice/prompts/SOUL.md",
        "alice/prompts/USER.md",
    ]


async def test_list_keys_no_match(backend):
    await backend.write_text("alice/prompts/SOUL.md", "s")
    assert await backend.list_keys("bob/") == []
    assert await backend.list_keys("alice/nonexistent/") == []


# ── delete ───────────────────────────────────────────────────────────────


async def test_delete_single_key(backend):
    await backend.write_text("alice/prompts/SOUL.md", "s")
    assert await backend.delete("alice/prompts/SOUL.md") == 1
    assert await backend.head_etag("alice/prompts/SOUL.md") is None


async def test_delete_prefix_recursive(backend):
    await backend.write_text("alice/skills/foo/SKILL.md", "f")
    await backend.write_text("alice/skills/foo/references/a.md", "a")
    await backend.write_text("alice/skills/bar/SKILL.md", "b")

    assert await backend.delete("alice/skills/foo") == 2
    assert await backend.head_etag("alice/skills/foo/SKILL.md") is None
    assert await backend.head_etag("alice/skills/bar/SKILL.md") is not None  # untouched


async def test_delete_missing_returns_zero(backend):
    assert await backend.delete("alice/nope/none.md") == 0


# ── path traversal guards ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "/abs/path",
        "..",
        "../escape",
        "alice/../bob/leak.md",
        "alice/./prompts/X.md",
        "alice//prompts/X.md",
    ],
)
async def test_invalid_keys_rejected(backend, bad_key):
    with pytest.raises(ValueError):
        await backend.read_text(bad_key)
    with pytest.raises(ValueError):
        await backend.write_text(bad_key, "x")


# ── concurrency: asyncio.gather should fan out reads ────────────────────


async def test_concurrent_reads(backend):
    keys = [f"alice/many/k{i}.txt" for i in range(20)]
    for i, k in enumerate(keys):
        await backend.write_text(k, f"value-{i}")
    results = await asyncio.gather(*(backend.read_text(k) for k in keys))
    assert results == [f"value-{i}" for i in range(20)]


# ── isolation: distinct roots don't see each other ──────────────────────


async def test_distinct_roots_isolated(tmp_path):
    a = LocalFSBackend(root=tmp_path / "a")
    b = LocalFSBackend(root=tmp_path / "b")
    await a.write_text("user/x.md", "from-a")
    await b.write_text("user/x.md", "from-b")
    assert await a.read_text("user/x.md") == "from-a"
    assert await b.read_text("user/x.md") == "from-b"
