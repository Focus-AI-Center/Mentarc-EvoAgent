"""Unit tests for ``utils.storage_backend.minio_sdk.MinIOSDKBackend``.

These tests hit a real MinIO server. They auto-skip when the
``MINIO_TEST_ENDPOINT`` env var is not set so the suite stays green in CI
without MinIO. Each test scopes its keys under a unique ``run-XXXX/`` prefix
and cleans up at the end.

To run locally:

    export MINIO_TEST_ENDPOINT=http://minio.example.com:9000
    export MINIO_TEST_ACCESS_KEY=your-access-key
    export MINIO_TEST_SECRET_KEY=your-secret-key
    export MINIO_TEST_BUCKET=evoagent
    pytest tests/unit/test_minio_backend.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

import pytest

from utils.storage_backend import (
    IF_MATCH_ABSENT,
    ETagMismatchError,
    KeyNotFoundError,
    StorageBackend,
)

ENDPOINT = os.getenv("MINIO_TEST_ENDPOINT", "").strip()
ACCESS_KEY = os.getenv("MINIO_TEST_ACCESS_KEY", "").strip()
SECRET_KEY = os.getenv("MINIO_TEST_SECRET_KEY", "").strip()
BUCKET = os.getenv("MINIO_TEST_BUCKET", "evoagent").strip()

pytestmark = pytest.mark.skipif(
    not (ENDPOINT and ACCESS_KEY and SECRET_KEY),
    reason="MINIO_TEST_ENDPOINT / ACCESS_KEY / SECRET_KEY not set",
)


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def run_prefix():
    """Per-test random prefix to avoid collisions across parallel runs."""
    return f"_test/run-{secrets.token_hex(4)}"


@pytest.fixture
def backend():
    from utils.storage_backend.minio_sdk import MinIOSDKBackend

    return MinIOSDKBackend(
        endpoint=ENDPOINT,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        bucket=BUCKET,
    )


@pytest.fixture(autouse=True)
async def _cleanup(backend, run_prefix):
    yield
    # Best-effort cleanup; if the bucket has versioning, this leaves
    # delete-markers (cheap, lifecycle handles them).
    try:
        await backend.delete(run_prefix + "/")
    except Exception:
        pass


# ── Protocol conformance ─────────────────────────────────────────────────


def test_satisfies_protocol(backend):
    assert isinstance(backend, StorageBackend)


# ── read / write ─────────────────────────────────────────────────────────


async def test_write_then_read_text(backend, run_prefix):
    key = f"{run_prefix}/SOUL.md"
    etag = await backend.write_text(key, "I am SOUL")
    assert etag == _md5_hex(b"I am SOUL")  # MinIO non-multipart ETag = MD5
    assert await backend.read_text(key) == "I am SOUL"


async def test_write_then_read_bytes(backend, run_prefix):
    key = f"{run_prefix}/blob.bin"
    payload = b"\x00\x01\x02\xff" * 256
    etag = await backend.write_bytes(key, payload)
    assert etag == _md5_hex(payload)
    assert await backend.read_bytes(key) == payload


async def test_read_missing_raises(backend, run_prefix):
    with pytest.raises(KeyNotFoundError):
        await backend.read_text(f"{run_prefix}/missing.md")


async def test_unicode_text_roundtrip(backend, run_prefix):
    text = "测试 — émoji 🚀\n你好"
    key = f"{run_prefix}/zh.md"
    await backend.write_text(key, text)
    assert await backend.read_text(key) == text


# ── head_etag ────────────────────────────────────────────────────────────


async def test_head_etag_present(backend, run_prefix):
    key = f"{run_prefix}/MEMORY.md"
    etag = await backend.write_text(key, "abc")
    assert await backend.head_etag(key) == etag


async def test_head_etag_missing(backend, run_prefix):
    assert await backend.head_etag(f"{run_prefix}/nope.md") is None


# ── if_match: optimistic CAS ─────────────────────────────────────────────


async def test_if_match_success(backend, run_prefix):
    key = f"{run_prefix}/M.md"
    etag1 = await backend.write_text(key, "v1")
    etag2 = await backend.write_text(key, "v2", if_match=etag1)
    assert etag2 == _md5_hex(b"v2")
    assert await backend.read_text(key) == "v2"


async def test_if_match_conflict(backend, run_prefix):
    key = f"{run_prefix}/M.md"
    await backend.write_text(key, "v1")
    with pytest.raises(ETagMismatchError):
        await backend.write_text(key, "v2", if_match="deadbeef")
    assert await backend.read_text(key) == "v1"  # unchanged


async def test_if_match_absent_creates(backend, run_prefix):
    key = f"{run_prefix}/new.md"
    etag = await backend.write_text(key, "first", if_match=IF_MATCH_ABSENT)
    assert etag == _md5_hex(b"first")


async def test_if_match_absent_conflict_when_present(backend, run_prefix):
    key = f"{run_prefix}/E.md"
    await backend.write_text(key, "exists")
    with pytest.raises(ETagMismatchError):
        await backend.write_text(key, "again", if_match=IF_MATCH_ABSENT)


async def test_if_match_against_missing_key(backend, run_prefix):
    with pytest.raises(ETagMismatchError):
        await backend.write_text(
            f"{run_prefix}/new.md", "x", if_match="cafef00d"
        )


# ── list_keys ────────────────────────────────────────────────────────────


async def test_list_keys_recursive(backend, run_prefix):
    keys = [
        f"{run_prefix}/prompts/SOUL.md",
        f"{run_prefix}/prompts/USER.md",
        f"{run_prefix}/skills/code/SKILL.md",
        f"{run_prefix}/sessions/s1.json",
    ]
    for k in keys:
        await backend.write_text(k, "x")
    listed = await backend.list_keys(f"{run_prefix}/")
    assert sorted(listed) == sorted(keys)


async def test_list_keys_partial_prefix(backend, run_prefix):
    await backend.write_text(f"{run_prefix}/prompts/SOUL.md", "s")
    await backend.write_text(f"{run_prefix}/prompts/USER.md", "u")
    await backend.write_text(f"{run_prefix}/sessions/s1.json", "{}")
    keys = await backend.list_keys(f"{run_prefix}/prompts/")
    assert sorted(keys) == [
        f"{run_prefix}/prompts/SOUL.md",
        f"{run_prefix}/prompts/USER.md",
    ]


async def test_list_keys_no_match(backend, run_prefix):
    assert await backend.list_keys(f"{run_prefix}/nope/") == []


# ── delete ───────────────────────────────────────────────────────────────


async def test_delete_single_key(backend, run_prefix):
    key = f"{run_prefix}/SOUL.md"
    await backend.write_text(key, "s")
    assert await backend.delete(key) == 1
    assert await backend.head_etag(key) is None


async def test_delete_prefix_recursive(backend, run_prefix):
    await backend.write_text(f"{run_prefix}/skills/foo/SKILL.md", "f")
    await backend.write_text(f"{run_prefix}/skills/foo/refs/a.md", "a")
    await backend.write_text(f"{run_prefix}/skills/bar/SKILL.md", "b")

    deleted = await backend.delete(f"{run_prefix}/skills/foo")
    assert deleted == 2
    assert await backend.head_etag(f"{run_prefix}/skills/foo/SKILL.md") is None
    assert await backend.head_etag(f"{run_prefix}/skills/bar/SKILL.md") is not None


async def test_delete_missing_returns_zero(backend, run_prefix):
    assert await backend.delete(f"{run_prefix}/none.md") == 0


# ── concurrency: asyncio.gather should fan out network calls ────────────


async def test_concurrent_reads(backend, run_prefix):
    keys = [f"{run_prefix}/many/k{i}.txt" for i in range(10)]
    for i, k in enumerate(keys):
        await backend.write_text(k, f"value-{i}")
    results = await asyncio.gather(*(backend.read_text(k) for k in keys))
    assert results == [f"value-{i}" for i in range(10)]
