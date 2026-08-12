"""Private content-addressed object-store adapters."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from pydantic import SecretStr

from corpuskit.config import Settings
from corpuskit.domain.artifacts import staged_artifact_reference, staged_artifact_storage_key
from corpuskit.domain.jobs import RunKind

_SAFE_KEY = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,511}$", flags=re.ASCII)


class ObjectStoreError(RuntimeError):
    """Non-sensitive object-store failure."""


class ObjectNotFoundError(ObjectStoreError):
    """Object key was absent."""


class ObjectIntegrityError(ObjectStoreError):
    """Stored bytes or metadata do not match their immutable declaration."""


@dataclass(frozen=True, slots=True)
class ObjectDescriptor:
    key: str
    size_bytes: int
    sha256: str
    media_type: str
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class PutResult:
    descriptor: ObjectDescriptor
    created: bool


@dataclass(frozen=True, slots=True)
class ObjectStream:
    descriptor: ObjectDescriptor
    chunks: AsyncIterator[bytes]


class ObjectStore(Protocol):
    async def put(
        self,
        *,
        key: str,
        content: bytes,
        sha256: str,
        media_type: str,
    ) -> PutResult: ...

    async def stat(self, key: str) -> ObjectDescriptor: ...

    async def open(self, key: str, *, chunk_bytes: int) -> ObjectStream: ...

    async def delete(self, key: str) -> None: ...

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        content_disposition: str,
    ) -> str | None: ...

    async def list_keys(
        self,
        prefix: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> tuple[str, ...]: ...


class InMemoryObjectStore:
    """Deterministic non-networked adapter for unit and contract tests."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, ObjectDescriptor]] = {}
        self.fail_put = False
        self.fail_delete = False

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        sha256: str,
        media_type: str,
    ) -> PutResult:
        _validate_put(key, content, sha256, media_type)
        if self.fail_put:
            raise ObjectStoreError("object write failed")
        descriptor = ObjectDescriptor(key, len(content), sha256, media_type, datetime.now(UTC))
        existing = self._objects.get(key)
        if existing is not None:
            _verify_descriptor(existing[1], descriptor)
            if existing[0] != content:
                raise ObjectIntegrityError("content address collision")
            return PutResult(existing[1], created=False)
        self._objects[key] = (bytes(content), descriptor)
        return PutResult(descriptor, created=True)

    async def stat(self, key: str) -> ObjectDescriptor:
        _validate_key(key)
        try:
            return self._objects[key][1]
        except KeyError as exc:
            raise ObjectNotFoundError("object not found") from exc

    async def open(self, key: str, *, chunk_bytes: int) -> ObjectStream:
        if chunk_bytes <= 0:
            raise ValueError("chunk size must be positive")
        descriptor = await self.stat(key)
        content = self._objects[key][0]

        async def chunks() -> AsyncIterator[bytes]:
            for offset in range(0, len(content), chunk_bytes):
                yield content[offset : offset + chunk_bytes]

        return ObjectStream(descriptor, chunks())

    async def delete(self, key: str) -> None:
        _validate_key(key)
        if self.fail_delete:
            raise ObjectStoreError("object delete failed")
        self._objects.pop(key, None)

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        content_disposition: str,
    ) -> str | None:
        del expires_seconds, content_disposition
        await self.stat(key)
        return None

    async def list_keys(
        self,
        prefix: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> tuple[str, ...]:
        _validate_listing(prefix, limit, after)
        return tuple(
            sorted(
                key
                for key in self._objects
                if key.startswith(prefix) and (after is None or key > after)
            )[:limit]
        )

    def corrupt(self, key: str, content: bytes) -> None:
        """Test-only corruption hook that intentionally preserves declared metadata."""

        descriptor = self._objects[key][1]
        self._objects[key] = (content, descriptor)


class FilesystemObjectStore:
    """Atomic local adapter for development; object names remain generated and opaque."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        sha256: str,
        media_type: str,
    ) -> PutResult:
        _validate_put(key, content, sha256, media_type)
        return await asyncio.to_thread(self._put_sync, key, content, sha256, media_type)

    def _put_sync(self, key: str, content: bytes, sha256: str, media_type: str) -> PutResult:
        path = self._path(key)
        descriptor = ObjectDescriptor(key, len(content), sha256, media_type, datetime.now(UTC))
        if path.exists():
            existing = self._stat_sync(key)
            _verify_descriptor(existing, descriptor)
            if _hash_path(path) != sha256:
                raise ObjectIntegrityError("stored object hash is invalid")
            return PutResult(existing, created=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".upload-", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            temporary = None
            self._write_metadata(path, descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return PutResult(descriptor, created=True)

    async def stat(self, key: str) -> ObjectDescriptor:
        _validate_key(key)
        return await asyncio.to_thread(self._stat_sync, key)

    def _stat_sync(self, key: str) -> ObjectDescriptor:
        path = self._path(key)
        metadata_path = self._metadata_path(path)
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
            descriptor = ObjectDescriptor(
                key=key,
                size_bytes=int(value["size_bytes"]),
                sha256=str(value["sha256"]),
                media_type=str(value["media_type"]),
                modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            )
        except FileNotFoundError as exc:
            raise ObjectNotFoundError("object not found") from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObjectIntegrityError("object metadata is invalid") from exc
        if not path.is_file() or path.stat().st_size != descriptor.size_bytes:
            raise ObjectIntegrityError("stored object size is invalid")
        _validate_descriptor(descriptor)
        return descriptor

    async def open(self, key: str, *, chunk_bytes: int) -> ObjectStream:
        if chunk_bytes <= 0:
            raise ValueError("chunk size must be positive")
        descriptor = await self.stat(key)
        path = self._path(key)

        async def chunks() -> AsyncIterator[bytes]:
            file = await asyncio.to_thread(path.open, "rb")
            try:
                while chunk := await asyncio.to_thread(file.read, chunk_bytes):
                    yield chunk
            finally:
                await asyncio.to_thread(file.close)

        return ObjectStream(descriptor, chunks())

    async def delete(self, key: str) -> None:
        _validate_key(key)
        path = self._path(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        await asyncio.to_thread(self._metadata_path(path).unlink, missing_ok=True)

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        content_disposition: str,
    ) -> str | None:
        del expires_seconds, content_disposition
        await self.stat(key)
        return None

    async def list_keys(
        self,
        prefix: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> tuple[str, ...]:
        _validate_listing(prefix, limit, after)

        def scan() -> tuple[str, ...]:
            if not self._root.exists():
                return ()
            keys = (
                path.relative_to(self._root).as_posix()
                for path in self._root.rglob("*")
                if path.is_file()
                and not path.name.endswith(".metadata.json")
                and not path.name.startswith(".upload-")
            )
            return tuple(
                sorted(
                    key for key in keys if key.startswith(prefix) and (after is None or key > after)
                )[:limit]
            )

        return await asyncio.to_thread(scan)

    def _path(self, key: str) -> Path:
        _validate_key(key)
        candidate = (self._root / Path(*key.split("/"))).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError("object key escapes configured root")
        return candidate

    @staticmethod
    def _metadata_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.metadata.json")

    def _write_metadata(self, path: Path, descriptor: ObjectDescriptor) -> None:
        value = json.dumps(
            {
                "media_type": descriptor.media_type,
                "sha256": descriptor.sha256,
                "size_bytes": descriptor.size_bytes,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        metadata_path = self._metadata_path(path)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=".metadata-",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                file.write(value)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, metadata_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class _StreamingBody(Protocol):
    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class _S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...

    def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str: ...


class S3ObjectStore:
    """S3-compatible private store with bounded SDK policy and integrity metadata."""

    def __init__(
        self,
        client: _S3Client,
        *,
        bucket: str,
        server_side_encryption: str | None,
        kms_key_id: str | None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._sse = server_side_encryption
        self._kms_key_id = kms_key_id

    @classmethod
    def from_settings(
        cls,
        settings: Settings | _StagedArtifactStoreConfig,
    ) -> S3ObjectStore:
        if settings.artifact_backend != "s3" or settings.artifact_s3_endpoint is None:
            raise ValueError("S3 artifact settings are required")
        config = Config(
            signature_version="s3v4",
            connect_timeout=settings.artifact_s3_connect_timeout_seconds,
            read_timeout=settings.artifact_s3_read_timeout_seconds,
            retries={"mode": "standard", "total_max_attempts": settings.artifact_s3_max_attempts},
            s3={
                "addressing_style": "path" if settings.artifact_s3_path_style else "virtual",
                "payload_signing_enabled": True,
            },
        )
        credentials: dict[str, str] = {}
        if settings.artifact_s3_access_key_id is not None:
            assert settings.artifact_s3_secret_access_key is not None
            credentials = {
                "aws_access_key_id": settings.artifact_s3_access_key_id.get_secret_value(),
                "aws_secret_access_key": settings.artifact_s3_secret_access_key.get_secret_value(),
            }
            if settings.artifact_s3_session_token is not None:
                credentials["aws_session_token"] = (
                    settings.artifact_s3_session_token.get_secret_value()
                )
        client = cast(
            _S3Client,
            boto3.client(
                "s3",
                endpoint_url=settings.artifact_s3_endpoint,
                region_name=settings.artifact_s3_region,
                config=config,
                **credentials,
            ),
        )
        kms_key_id = (
            settings.artifact_s3_kms_key_id.get_secret_value()
            if settings.artifact_s3_kms_key_id is not None
            else None
        )
        return cls(
            client,
            bucket=settings.artifact_s3_bucket,
            server_side_encryption=settings.artifact_s3_sse,
            kms_key_id=kms_key_id,
        )

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        sha256: str,
        media_type: str,
    ) -> PutResult:
        _validate_put(key, content, sha256, media_type)
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": content,
            "ContentLength": len(content),
            "ContentType": media_type,
            "ChecksumSHA256": checksum,
            "IfNoneMatch": "*",
            "Metadata": {
                "corpuskit-sha256": sha256,
                "corpuskit-media-type": media_type,
            },
        }
        if self._sse is not None:
            request["ServerSideEncryption"] = self._sse
        if self._kms_key_id is not None:
            request["SSEKMSKeyId"] = self._kms_key_id
        created = True
        try:
            response = await asyncio.to_thread(self._client.put_object, **request)
            returned_checksum = response.get("ChecksumSHA256")
            if returned_checksum is not None and returned_checksum != checksum:
                raise ObjectIntegrityError("object store returned an invalid checksum")
        except ClientError as exc:
            if not _is_precondition_failure(exc):
                raise ObjectStoreError("object write failed") from exc
            created = False
        except BotoCoreError as exc:
            raise ObjectStoreError("object write failed") from exc
        descriptor = await self.stat(key)
        expected = ObjectDescriptor(
            key,
            len(content),
            sha256,
            media_type,
            descriptor.modified_at,
        )
        _verify_descriptor(descriptor, expected)
        return PutResult(descriptor, created=created)

    async def stat(self, key: str) -> ObjectDescriptor:
        _validate_key(key)
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=key,
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError("object not found") from exc
            raise ObjectStoreError("object metadata read failed") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("object metadata read failed") from exc
        metadata = response.get("Metadata", {})
        try:
            descriptor = ObjectDescriptor(
                key=key,
                size_bytes=int(response["ContentLength"]),
                sha256=str(metadata["corpuskit-sha256"]),
                media_type=str(metadata["corpuskit-media-type"]),
                modified_at=response["LastModified"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ObjectIntegrityError("object metadata is invalid") from exc
        _validate_descriptor(descriptor)
        if response.get("ContentType") != descriptor.media_type:
            raise ObjectIntegrityError("object media type is invalid")
        if self._sse is not None and response.get("ServerSideEncryption") != self._sse:
            raise ObjectIntegrityError("object encryption policy is invalid")
        if self._kms_key_id is not None and response.get("SSEKMSKeyId") != self._kms_key_id:
            raise ObjectIntegrityError("object KMS key policy is invalid")
        return descriptor

    async def open(self, key: str, *, chunk_bytes: int) -> ObjectStream:
        if chunk_bytes <= 0:
            raise ValueError("chunk size must be positive")
        expected = await self.stat(key)
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError("object not found") from exc
            raise ObjectStoreError("object read failed") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("object read failed") from exc
        returned = ObjectDescriptor(
            key=key,
            size_bytes=int(response.get("ContentLength", -1)),
            sha256=expected.sha256,
            media_type=str(response.get("ContentType", "")),
            modified_at=expected.modified_at,
        )
        _verify_descriptor(returned, expected)
        checksum = response.get("ChecksumSHA256")
        expected_checksum = base64.b64encode(bytes.fromhex(expected.sha256)).decode("ascii")
        if checksum is not None and checksum != expected_checksum:
            raise ObjectIntegrityError("object response checksum is invalid")
        body = cast(_StreamingBody, response["Body"])

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while chunk := await asyncio.to_thread(body.read, chunk_bytes):
                    yield chunk
            except Exception as exc:
                raise ObjectStoreError("object stream failed") from exc
            finally:
                await asyncio.to_thread(body.close)

        return ObjectStream(expected, chunks())

    async def delete(self, key: str) -> None:
        _validate_key(key)
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise ObjectStoreError("object delete failed") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("object delete failed") from exc

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        content_disposition: str,
    ) -> str | None:
        await self.stat(key)
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ResponseContentDisposition": content_disposition,
                },
                ExpiresIn=expires_seconds,
                HttpMethod="GET",
            )
        except ClientError as exc:
            raise ObjectStoreError("download signing failed") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("download signing failed") from exc

    async def list_keys(
        self,
        prefix: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> tuple[str, ...]:
        _validate_listing(prefix, limit, after)
        keys: list[str] = []
        continuation: str | None = None
        try:
            while len(keys) < limit:
                request: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Prefix": prefix,
                    "MaxKeys": min(1_000, limit - len(keys)),
                }
                if continuation is None and after is not None:
                    request["StartAfter"] = after
                if continuation is not None:
                    request["ContinuationToken"] = continuation
                response = await asyncio.to_thread(self._client.list_objects_v2, **request)
                keys.extend(
                    str(item["Key"])
                    for item in response.get("Contents", ())
                    if isinstance(item, dict) and "Key" in item
                )
                if not response.get("IsTruncated"):
                    break
                continuation = response.get("NextContinuationToken")
                if not isinstance(continuation, str) or not continuation:
                    raise ObjectIntegrityError("object listing continuation is invalid")
        except ClientError as exc:
            raise ObjectStoreError("object listing failed") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("object listing failed") from exc
        return tuple(keys[:limit])


@dataclass(frozen=True, slots=True)
class _StagedArtifactStoreConfig:
    """Pickle-safe artifact-only configuration; parent/database secrets are excluded."""

    artifact_root: Path
    artifact_backend: str
    artifact_max_bytes: int
    artifact_s3_endpoint: str | None
    artifact_s3_bucket: str
    artifact_s3_region: str
    artifact_s3_access_key_id: SecretStr | None
    artifact_s3_secret_access_key: SecretStr | None
    artifact_s3_session_token: SecretStr | None
    artifact_s3_path_style: bool
    artifact_s3_sse: str | None
    artifact_s3_kms_key_id: SecretStr | None
    artifact_s3_connect_timeout_seconds: float
    artifact_s3_read_timeout_seconds: float
    artifact_s3_max_attempts: int

    @classmethod
    def from_settings(cls, settings: Settings) -> _StagedArtifactStoreConfig:
        return cls(
            artifact_root=settings.artifact_root,
            artifact_backend=settings.artifact_backend,
            artifact_max_bytes=settings.artifact_max_bytes,
            artifact_s3_endpoint=settings.artifact_s3_endpoint,
            artifact_s3_bucket=settings.artifact_s3_bucket,
            artifact_s3_region=settings.artifact_s3_region,
            artifact_s3_access_key_id=settings.artifact_s3_access_key_id,
            artifact_s3_secret_access_key=settings.artifact_s3_secret_access_key,
            artifact_s3_session_token=settings.artifact_s3_session_token,
            artifact_s3_path_style=settings.artifact_s3_path_style,
            artifact_s3_sse=settings.artifact_s3_sse,
            artifact_s3_kms_key_id=settings.artifact_s3_kms_key_id,
            artifact_s3_connect_timeout_seconds=settings.artifact_s3_connect_timeout_seconds,
            artifact_s3_read_timeout_seconds=settings.artifact_s3_read_timeout_seconds,
            artifact_s3_max_attempts=settings.artifact_s3_max_attempts,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredStagedArtifactWriter:
    """Pickle-safe child adapter exposing only content-addressed staging writes."""

    settings: _StagedArtifactStoreConfig

    @classmethod
    def from_settings(cls, settings: Settings) -> ConfiguredStagedArtifactWriter:
        """Copy only the child-required artifact configuration from parent settings."""

        return cls(_StagedArtifactStoreConfig.from_settings(settings))

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        del kind
        if not payload or len(payload) > self.settings.artifact_max_bytes:
            raise ObjectIntegrityError("staged artifact size is invalid")
        key = staged_artifact_storage_key(content_sha256)
        store = build_object_store(self.settings)
        asyncio.run(
            store.put(
                key=key,
                content=payload,
                sha256=content_sha256,
                media_type="application/json",
            )
        )
        return staged_artifact_reference(content_sha256)


def build_object_store(settings: Settings | _StagedArtifactStoreConfig) -> ObjectStore:
    if settings.artifact_backend == "s3":
        return S3ObjectStore.from_settings(settings)
    return FilesystemObjectStore(settings.artifact_root)


def _validate_put(key: str, content: bytes, sha256: str, media_type: str) -> None:
    _validate_key(key)
    actual = hashlib.sha256(content).hexdigest()
    if sha256 != actual:
        raise ObjectIntegrityError("upload checksum is invalid")
    _validate_descriptor(ObjectDescriptor(key, len(content), sha256, media_type, datetime.now(UTC)))


def _validate_descriptor(descriptor: ObjectDescriptor) -> None:
    if descriptor.size_bytes < 0 or re.fullmatch(r"[0-9a-f]{64}", descriptor.sha256) is None:
        raise ObjectIntegrityError("object metadata is invalid")
    if not descriptor.media_type or len(descriptor.media_type) > 160:
        raise ObjectIntegrityError("object media type is invalid")
    if (
        not isinstance(descriptor.modified_at, datetime)
        or descriptor.modified_at.tzinfo is None
        or descriptor.modified_at.utcoffset() is None
    ):
        raise ObjectIntegrityError("object modification timestamp is invalid")


def _verify_descriptor(actual: ObjectDescriptor, expected: ObjectDescriptor) -> None:
    _validate_descriptor(actual)
    if (
        actual.key != expected.key
        or actual.size_bytes != expected.size_bytes
        or actual.sha256 != expected.sha256
        or actual.media_type != expected.media_type
    ):
        raise ObjectIntegrityError("object metadata does not match its content address")


def _validate_key(key: str) -> None:
    if _SAFE_KEY.fullmatch(key) is None or "//" in key or "/../" in f"/{key}/":
        raise ValueError("invalid object key")


def _validate_prefix(prefix: str) -> None:
    if not prefix or _SAFE_KEY.fullmatch(prefix.rstrip("/")) is None or "//" in prefix:
        raise ValueError("invalid object prefix")


def _validate_listing(prefix: str, limit: int, after: str | None) -> None:
    _validate_prefix(prefix)
    if not 1 <= limit <= 10_000:
        raise ValueError("object listing limit is invalid")
    if after is not None:
        _validate_key(after)
        if not after.startswith(prefix):
            raise ValueError("object listing cursor is invalid")


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_precondition_failure(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "412"} or status == 412


def _is_not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404


__all__ = [
    "ConfiguredStagedArtifactWriter",
    "FilesystemObjectStore",
    "InMemoryObjectStore",
    "ObjectDescriptor",
    "ObjectIntegrityError",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "ObjectStream",
    "PutResult",
    "S3ObjectStore",
    "build_object_store",
]
