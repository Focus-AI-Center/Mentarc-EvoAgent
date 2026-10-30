"""Atomic conditional writes across processes, plus the real minio SDK adapter."""

import hashlib
import multiprocessing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from minio.error import S3Error

from utils.storage_backend import IF_MATCH_ABSENT, ETagMismatchError, KeyNotFoundError, StorageError
from utils.storage_backend.localfs import LocalFSBackend
from utils.storage_backend.minio_sdk import MinIOSDKBackend


def conditional_writer(root, barrier, queue, etag, value):
    backend = LocalFSBackend(root)
    try:
        barrier.wait(timeout=10)
        backend.write_text_sync("alice/note.md", value, if_match=etag)
        queue.put("written")
    except ETagMismatchError:
        queue.put("conflict")
    except Exception as exc:
        queue.put(repr(exc))


@pytest.mark.parametrize("create", [False, True])
def test_only_one_process_can_publish_against_the_same_etag(tmp_path, create):
    backend = LocalFSBackend(tmp_path)
    etag = IF_MATCH_ABSENT if create else backend.write_text_sync("alice/note.md", "original")
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(4), context.Queue()
    workers = [
        context.Process(target=conditional_writer, args=(tmp_path, barrier, queue, etag, str(i)))
        for i in range(4)
    ]
    try:
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert sorted(results) == ["conflict"] * 3 + ["written"]
        assert backend.list_keys_sync("alice/") == ["alice/note.md"]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        queue.close()


def test_local_read_pairs_etag_with_exact_bytes(tmp_path):
    backend = LocalFSBackend(tmp_path)
    backend.write_text_sync("alice/note.md", "Unicode🚀")
    text, etag = backend.read_text_with_etag_sync("alice/note.md")
    assert etag == hashlib.md5(text.encode()).hexdigest()


@pytest.fixture
def minio_backend():
    # No network: exercise the installed SDK's actual _put_object method, mocking
    # only its HTTP transport. This catches SDK signature/header regressions.
    backend = MinIOSDKBackend("http://localhost:9000", "test-access", "test-secret")
    backend._client._execute = Mock(return_value=SimpleNamespace(headers={"etag": '"next"'}))
    return backend


@pytest.mark.parametrize("expected,header,value", [("abc", "If-Match", '"abc"'), ("", "If-None-Match", "*")])
def test_minio_put_sends_condition_in_the_same_request(minio_backend, expected, header, value):
    assert minio_backend.write_text_sync("alice/note.md", "Unicode", if_match=expected) == "next"
    call = minio_backend._client._execute.call_args
    assert call.args[0] == "PUT"
    assert call.kwargs["headers"][header] == value
    assert call.kwargs["body"] == b"Unicode"


@pytest.mark.parametrize(
    "code", ["PreconditionFailed", "ConditionalRequestConflict", "NoSuchKey", "AccessDenied"]
)
def test_minio_translates_only_precondition_errors_into_conflicts(minio_backend, code):
    minio_backend._client._execute.side_effect = S3Error(
        response=None, code=code, message="failed", resource="note", request_id="request", host_id="host"
    )
    error = StorageError if code == "AccessDenied" else ETagMismatchError
    with pytest.raises(error):
        minio_backend.write_text_sync("alice/note.md", "new", if_match="old")


def test_minio_read_gets_content_and_etag_from_one_response(minio_backend):
    response = Mock(headers={"etag": '"multipart-2"'})
    response.read.return_value = b"body"
    minio_backend._client.get_object = Mock(return_value=response)
    assert minio_backend.read_text_with_etag_sync("alice/note.md") == ("body", "multipart-2")
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_minio_missing_read_remains_create_only(minio_backend):
    minio_backend._client.get_object = Mock(
        side_effect=S3Error(
            response=None,
            code="NoSuchKey",
            message="missing",
            resource="note",
            request_id="request",
            host_id="host",
        )
    )
    with pytest.raises(KeyNotFoundError):
        minio_backend.read_text_with_etag_sync("alice/note.md")
