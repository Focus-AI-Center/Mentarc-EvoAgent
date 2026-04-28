"""Factory tests for ``core.plugins.sandbox.make_sandbox_backend``.

The factory pairs storage and sandbox backends by ``MINIO_ENDPOINT``
(see plan §1.0). Cross-mode configurations are rejected at startup.
"""

from __future__ import annotations

import pytest

from core.plugins.sandbox import make_sandbox_backend


def test_local_mode_returns_subprocess(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    backend = make_sandbox_backend()
    assert type(backend).__name__ == "LocalSubprocessBackend"


def test_minio_mode_returns_docker(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "http://example.com:9000")
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    backend = make_sandbox_backend()
    assert type(backend).__name__ == "RemoteDockerBackend"


def test_minio_mode_explicit_docker(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "http://example.com:9000")
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    backend = make_sandbox_backend()
    assert type(backend).__name__ == "RemoteDockerBackend"


def test_local_mode_with_docker_request_fails(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    with pytest.raises(RuntimeError, match="requires MINIO_ENDPOINT"):
        make_sandbox_backend()


def test_local_mode_with_k8s_request_fails(monkeypatch):
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    monkeypatch.setenv("SANDBOX_BACKEND", "k8s")
    with pytest.raises(RuntimeError, match="requires MINIO_ENDPOINT"):
        make_sandbox_backend()


def test_minio_mode_k8s_reserved(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "http://example.com:9000")
    monkeypatch.setenv("SANDBOX_BACKEND", "k8s")
    with pytest.raises(RuntimeError, match="reserved for M5"):
        make_sandbox_backend()


def test_minio_mode_unknown_backend_fails(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "http://example.com:9000")
    monkeypatch.setenv("SANDBOX_BACKEND", "lambda")
    with pytest.raises(RuntimeError, match="Unknown SANDBOX_BACKEND"):
        make_sandbox_backend()
