"""Object-store adapter integrity and failure contract tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from corpuskit.config import Settings
from corpuskit.domain.artifacts import staged_artifact_storage_key
from corpuskit.domain.jobs import RunKind
from corpuskit.persistence.artifact_store import (
    ConfiguredStagedArtifactWriter,
    FilesystemObjectStore,
    InMemoryObjectStore,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStoreError,
    S3ObjectStore,
    build_object_store,
)

KEY = f"artifacts/v1/{'1' * 32}/{'2' * 32}/project/export/aa/{'a' * 64}"
CONTENT = b"artifact payload"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


async def _collect(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


@pytest.mark.asyncio
async def test_memory_store_is_idempotent_streaming_and_deletable() -> None:
    store = InMemoryObjectStore()

    first = await store.put(
        key=KEY,
        content=CONTENT,
        sha256=DIGEST,
        media_type="application/json",
    )
    second = await store.put(
        key=KEY,
        content=CONTENT,
        sha256=DIGEST,
        media_type="application/json",
    )

    assert first.created is True
    assert second.created is False
    assert await _collect((await store.open(KEY, chunk_bytes=3)).chunks) == CONTENT
    assert await store.list_keys("artifacts/v1", limit=1) == (KEY,)
    assert await store.presign_get(KEY, expires_seconds=30, content_disposition="x") is None
    await store.delete(KEY)
    with pytest.raises(ObjectNotFoundError):
        await store.stat(KEY)


@pytest.mark.asyncio
async def test_memory_store_rejects_hash_collision_invalid_limits_and_failures() -> None:
    store = InMemoryObjectStore()
    with pytest.raises((ObjectIntegrityError, ValueError)):
        await store.put(key=KEY, content=CONTENT, sha256="0" * 64, media_type="text/plain")
    with pytest.raises(ValueError, match="key"):
        await store.put(
            key="../escape",
            content=CONTENT,
            sha256=DIGEST,
            media_type="text/plain",
        )
    await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    with pytest.raises(ObjectIntegrityError):
        await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="application/json")
    with pytest.raises(ValueError, match="chunk"):
        await store.open(KEY, chunk_bytes=0)
    store.fail_put = True
    with pytest.raises(ObjectStoreError):
        await store.put(
            key=KEY.replace("/aa/", "/bb/"),
            content=CONTENT,
            sha256=DIGEST,
            media_type="text/plain",
        )
    store.fail_delete = True
    with pytest.raises(ObjectStoreError):
        await store.delete(KEY)


@pytest.mark.asyncio
async def test_filesystem_store_roundtrip_detects_corruption_and_removes_sidecar(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    created = await store.put(
        key=KEY,
        content=CONTENT,
        sha256=DIGEST,
        media_type="text/plain",
    )

    assert created.created is True
    assert (
        await store.put(
            key=KEY,
            content=CONTENT,
            sha256=DIGEST,
            media_type="text/plain",
        )
    ).created is False
    assert await _collect((await store.open(KEY, chunk_bytes=4)).chunks) == CONTENT
    assert await store.list_keys("artifacts/v1", limit=10) == (KEY,)
    assert await store.presign_get(KEY, expires_seconds=30, content_disposition="x") is None

    object_path = tmp_path / "objects" / Path(*KEY.split("/"))
    object_path.write_bytes(b"bad")
    with pytest.raises(ObjectIntegrityError, match="size"):
        await store.stat(KEY)
    object_path.write_bytes(CONTENT)
    await store.delete(KEY)
    assert not object_path.exists()
    assert not object_path.with_name(f"{object_path.name}.metadata.json").exists()


@pytest.mark.asyncio
async def test_filesystem_store_rejects_bad_metadata_and_absent_root(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")
    assert await store.list_keys("artifacts/v1", limit=10) == ()
    with pytest.raises(ObjectNotFoundError):
        await store.stat(KEY)
    with pytest.raises(ValueError, match="prefix"):
        await store.list_keys("../", limit=1)
    with pytest.raises(ValueError, match="chunk"):
        await store.open(KEY, chunk_bytes=-1)

    await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    object_path = tmp_path / "objects" / Path(*KEY.split("/"))
    object_path.with_name(f"{object_path.name}.metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ObjectIntegrityError, match="metadata"):
        await store.stat(KEY)


class FakeBody:
    def __init__(self, content: bytes, *, fail: bool = False) -> None:
        self.content = content
        self.position = 0
        self.fail = fail
        self.closed = False

    def read(self, amount: int) -> bytes:
        if self.fail:
            raise OSError("sdk detail")
        value = self.content[self.position : self.position + amount]
        self.position += len(value)
        return value

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, Any]]] = {}
        self.last_put: dict[str, Any] = {}
        self.fail_operation: str | None = None
        self.bad_checksum = False
        self.bad_continuation = False
        self.body_fail = False
        self.transport_failure = False

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail("put_object")
        self.last_put = kwargs
        key = str(kwargs["Key"])
        if key in self.objects:
            raise _client_error("PreconditionFailed", 412, "PutObject")
        self.objects[key] = (bytes(kwargs["Body"]), kwargs)
        return {"ChecksumSHA256": "bad" if self.bad_checksum else kwargs["ChecksumSHA256"]}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail("head_object")
        try:
            content, request = self.objects[str(kwargs["Key"])]
        except KeyError as exc:
            raise _client_error("NoSuchKey", 404, "HeadObject") from exc
        return {
            "ContentLength": len(content),
            "ContentType": request["ContentType"],
            "Metadata": request["Metadata"],
            "ServerSideEncryption": request.get("ServerSideEncryption"),
            "SSEKMSKeyId": request.get("SSEKMSKeyId"),
            "LastModified": datetime(2026, 8, 11, tzinfo=UTC),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail("get_object")
        try:
            content, request = self.objects[str(kwargs["Key"])]
        except KeyError as exc:
            raise _client_error("404", 404, "GetObject") from exc
        return {
            "Body": FakeBody(content, fail=self.body_fail),
            "ContentLength": len(content),
            "ContentType": request["ContentType"],
            "ChecksumSHA256": ("bad" if self.bad_checksum else request["ChecksumSHA256"]),
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail("delete_object")
        self.objects.pop(str(kwargs["Key"]), None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._fail("list_objects_v2")
        keys = sorted(key for key in self.objects if key.startswith(str(kwargs["Prefix"])))
        if self.bad_continuation:
            return {"Contents": [{"Key": keys[0]}], "IsTruncated": True}
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str:
        self._fail("generate_presigned_url")
        del args
        key = kwargs["Params"]["Key"]
        return f"http://minio:9000/corpuskit-artifacts/{key}?X-Amz-Expires={kwargs['ExpiresIn']}"

    def _fail(self, operation: str) -> None:
        if self.fail_operation == operation:
            if self.transport_failure:
                raise EndpointConnectionError(endpoint_url="https://objects.invalid")
            raise _client_error("InternalError", 500, operation)


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


@pytest.mark.asyncio
async def test_s3_store_uses_private_integrity_and_encryption_contract() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(
        client,
        bucket="corpuskit-artifacts",
        server_side_encryption="AES256",
        kms_key_id=None,
    )

    first = await store.put(
        key=KEY,
        content=CONTENT,
        sha256=DIGEST,
        media_type="application/json",
    )
    duplicate = await store.put(
        key=KEY,
        content=CONTENT,
        sha256=DIGEST,
        media_type="application/json",
    )
    opened = await store.open(KEY, chunk_bytes=4)

    assert first.created is True
    assert duplicate.created is False
    assert await _collect(opened.chunks) == CONTENT
    assert client.last_put["IfNoneMatch"] == "*"
    assert client.last_put["ServerSideEncryption"] == "AES256"
    assert client.last_put["ChecksumSHA256"] == base64.b64encode(bytes.fromhex(DIGEST)).decode()
    assert "ACL" not in client.last_put
    assert await store.list_keys("artifacts/v1", limit=10) == (KEY,)
    assert "X-Amz-Expires=30" in await store.presign_get(
        KEY,
        expires_seconds=30,
        content_disposition="attachment",
    )
    await store.delete(KEY)


@pytest.mark.asyncio
async def test_s3_store_maps_failures_and_detects_invalid_responses() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(client, bucket="bucket", server_side_encryption=None, kms_key_id=None)
    with pytest.raises(ObjectNotFoundError):
        await store.stat(KEY)
    client.fail_operation = "put_object"
    with pytest.raises(ObjectStoreError, match="write"):
        await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    client.fail_operation = None
    client.bad_checksum = True
    with pytest.raises(ObjectIntegrityError, match="checksum"):
        await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    client.bad_checksum = False
    client.objects.clear()
    await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    client.bad_checksum = True
    with pytest.raises(ObjectIntegrityError, match="checksum"):
        await store.open(KEY, chunk_bytes=4)
    client.bad_checksum = False
    client.body_fail = True
    opened = await store.open(KEY, chunk_bytes=4)
    with pytest.raises(ObjectStoreError, match="stream"):
        await _collect(opened.chunks)
    client.body_fail = False
    client.fail_operation = "delete_object"
    with pytest.raises(ObjectStoreError, match="delete"):
        await store.delete(KEY)
    client.fail_operation = "list_objects_v2"
    with pytest.raises(ObjectStoreError, match="listing"):
        await store.list_keys("artifacts/v1", limit=10)


@pytest.mark.asyncio
async def test_s3_listing_rejects_missing_continuation_and_kms_is_sent() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(
        client,
        bucket="bucket",
        server_side_encryption="aws:kms",
        kms_key_id="kms-key",
    )
    await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    assert client.last_put["SSEKMSKeyId"] == "kms-key"
    client.bad_continuation = True
    with pytest.raises(ObjectIntegrityError, match="continuation"):
        await store.list_keys("artifacts/v1", limit=2)


@pytest.mark.asyncio
async def test_s3_transport_failures_are_safely_mapped_for_every_operation() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(client, bucket="bucket", server_side_encryption=None, kms_key_id=None)
    await store.put(key=KEY, content=CONTENT, sha256=DIGEST, media_type="text/plain")
    client.transport_failure = True

    operations = (
        (
            "put_object",
            lambda: store.put(
                key=KEY.replace("/aa/", "/bb/"),
                content=CONTENT,
                sha256=DIGEST,
                media_type="text/plain",
            ),
        ),
        ("head_object", lambda: store.stat(KEY)),
        ("get_object", lambda: store.open(KEY, chunk_bytes=4)),
        ("delete_object", lambda: store.delete(KEY)),
        (
            "generate_presigned_url",
            lambda: store.presign_get(
                KEY,
                expires_seconds=30,
                content_disposition="attachment",
            ),
        ),
        ("list_objects_v2", lambda: store.list_keys("artifacts/v1", limit=10)),
    )
    for sdk_operation, operation in operations:
        client.fail_operation = sdk_operation
        with pytest.raises(ObjectStoreError):
            await operation()


def test_store_factory_uses_fixed_backend_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = build_object_store(Settings(environment="test", artifact_root=tmp_path, _env_file=None))
    assert isinstance(local, FilesystemObjectStore)
    with pytest.raises(ValueError, match="settings"):
        S3ObjectStore.from_settings(Settings(environment="test", _env_file=None))

    client = FakeS3Client()
    captured: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> FakeS3Client:
        captured["service"] = service
        captured.update(kwargs)
        return client

    monkeypatch.setattr("corpuskit.persistence.artifact_store.boto3.client", fake_client)
    remote = build_object_store(
        Settings(
            environment="test",
            artifact_backend="s3",
            artifact_s3_endpoint="http://127.0.0.1:9000",
            artifact_s3_bucket="fixed-bucket",
            artifact_s3_region="us-test-1",
            artifact_s3_access_key_id="access",
            artifact_s3_secret_access_key="secret",
            artifact_s3_session_token="session",
            artifact_s3_path_style=True,
            artifact_s3_sse="aws:kms",
            artifact_s3_kms_key_id="kms-key",
            artifact_s3_connect_timeout_seconds=2,
            artifact_s3_read_timeout_seconds=9,
            artifact_s3_max_attempts=4,
            _env_file=None,
        )
    )

    assert isinstance(remote, S3ObjectStore)
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "http://127.0.0.1:9000"
    assert captured["region_name"] == "us-test-1"
    assert captured["aws_access_key_id"] == "access"
    assert captured["aws_secret_access_key"] == "secret"
    assert captured["aws_session_token"] == "session"
    config = captured["config"]
    assert config.signature_version == "s3v4"
    assert config.connect_timeout == 2
    assert config.read_timeout == 9
    assert config.retries == {"mode": "standard", "total_max_attempts": 4}
    assert config.s3 == {"addressing_style": "path", "payload_signing_enabled": True}


def test_configured_child_writer_exposes_only_digest_staging(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        artifact_root=tmp_path / "objects",
        _env_file=None,
    )
    writer = ConfiguredStagedArtifactWriter.from_settings(settings)
    reference = writer.stage_model_result(
        kind=RunKind.GENERATE_LOCAL,
        payload=CONTENT,
        content_sha256=DIGEST,
    )

    assert reference == f"staged-artifact://sha256/{DIGEST}"
    descriptor = asyncio.run(
        FilesystemObjectStore(settings.artifact_root).stat(staged_artifact_storage_key(DIGEST))
    )
    assert descriptor.sha256 == DIGEST
    assert descriptor.media_type == "application/json"
    with pytest.raises(ObjectIntegrityError):
        writer.stage_model_result(
            kind=RunKind.GENERATE_LOCAL,
            payload=CONTENT,
            content_sha256="0" * 64,
        )


def test_configured_child_writer_excludes_parent_database_authority(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+asyncpg://worker:worker-canary@db.test/corpuskit",
        adoption_database_url=("postgresql+asyncpg://adoption:adoption-canary@db.test/corpuskit"),
        artifact_root=tmp_path / "objects",
        _env_file=None,
    )

    encoded = ForkingPickler.dumps(ConfiguredStagedArtifactWriter.from_settings(settings))

    assert b"worker-canary" not in encoded
    assert b"adoption-canary" not in encoded
