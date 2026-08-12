"""Artifact service acceptance tests across database and object-store boundaries."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.persistence.artifact_store import InMemoryObjectStore, PutResult
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact, Project
from corpuskit.services.artifacts import ArtifactActor, ArtifactService
from corpuskit.services.jobs import (
    DEMO_PROJECT_ID,
    JobActor,
    JobControlPlane,
    RunSubmission,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'artifacts.db').as_posix()}",
        "artifact_max_bytes": 1_024,
        "max_upload_bytes": 1_024,
        "artifact_download_chunk_bytes": 16 * 1_024,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _actor() -> ArtifactActor:
    return ArtifactActor(
        subject=DEMO_PRINCIPAL.subject,
        organization_id=DEMO_PRINCIPAL.organization_id,
    )


async def _stack(
    tmp_path: Path,
    *,
    store: InMemoryObjectStore | None = None,
    settings: Settings | None = None,
) -> tuple[Database, ArtifactService, InMemoryObjectStore, UUID]:
    resolved = settings or _settings(tmp_path)
    database = Database(resolved.database_url)
    await database.create_schema()
    jobs = JobControlPlane(database)
    await jobs.bootstrap_demo(
        JobActor(subject=DEMO_PRINCIPAL.subject, organization_id=DEMO_PRINCIPAL.organization_id),
        environment="test",
    )
    submitted = await jobs.submit(
        JobActor(subject=DEMO_PRINCIPAL.subject, organization_id=DEMO_PRINCIPAL.organization_id),
        RunSubmission(
            project_id=DEMO_PROJECT_ID,
            kind=RunKind.EVALUATE,
            spec={"language": "en-us"},
        ),
        idempotency_key="artifact-run",
    )
    object_store = store or InMemoryObjectStore()
    return (
        database,
        ArtifactService(database, object_store, resolved),
        object_store,
        submitted.run.id,
    )


async def _create(
    service: ArtifactService,
    run_id: UUID,
    *,
    content: bytes = b'{"ok":true}',
    filename: str = "result.json",
) -> Any:
    return await service.create(
        _actor(),
        project_id=DEMO_PROJECT_ID,
        run_id=run_id,
        kind=ArtifactKind.CORPUS_TEXT,
        content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        media_type="application/json",
        filename=filename,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_is_scoped_immutable_idempotent_and_stream_verified(tmp_path: Path) -> None:
    database, service, _, run_id = await _stack(tmp_path)
    try:
        first = await _create(service, run_id)
        duplicate = await _create(service, run_id)
        listed = await service.list(_actor(), project_id=DEMO_PROJECT_ID)
        fetched = await service.get(
            _actor(), project_id=DEMO_PROJECT_ID, artifact_id=first.artifact.id
        )
        download = await service.download(
            _actor(), project_id=DEMO_PROJECT_ID, artifact_id=first.artifact.id
        )

        assert first.created is True
        assert duplicate.created is False
        assert duplicate.artifact.id == first.artifact.id
        assert listed == (fetched,)
        assert b"".join([chunk async for chunk in download.chunks]) == b'{"ok":true}'
        assert download.content_disposition.startswith('attachment; filename="result.json"')
        assert download.content_digest.startswith("sha-256=:")
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_rehydrates_missing_object_and_fails_closed_on_corruption(
    tmp_path: Path,
) -> None:
    database, service, store, run_id = await _stack(tmp_path)
    try:
        created = await _create(service, run_id)
        async with database.session() as session:
            key = await session.scalar(
                select(Artifact.storage_key).where(Artifact.id == created.artifact.id)
            )
        assert key is not None

        await store.delete(key)
        repaired = await _create(service, run_id)
        assert repaired.created is False
        assert repaired.artifact.id == created.artifact.id
        assert await store.stat(key)

        store.corrupt(key, b'{"corrupt":true}')
        with pytest.raises(DependencyUnavailableError):
            await _create(service, run_id)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_reads_deletes_and_signs_are_indistinguishable(tmp_path: Path) -> None:
    database, service, _, run_id = await _stack(tmp_path)
    try:
        created = await _create(service, run_id)
        attacker = ArtifactActor(subject="other", organization_id=uuid4())

        for operation in (
            lambda: service.get(
                attacker,
                project_id=DEMO_PROJECT_ID,
                artifact_id=created.artifact.id,
            ),
            lambda: service.tombstone(
                attacker,
                project_id=DEMO_PROJECT_ID,
                artifact_id=created.artifact.id,
            ),
            lambda: service.sign_download(
                attacker,
                project_id=DEMO_PROJECT_ID,
                artifact_id=created.artifact.id,
                expires_seconds=30,
            ),
        ):
            with pytest.raises(ResourceNotFoundError) as raised:
                await operation()
            assert str(raised.value) == "The requested resource was not found."
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checksum_oversize_media_and_manifest_type_fail_closed(tmp_path: Path) -> None:
    database, service, _, run_id = await _stack(tmp_path)
    try:
        with pytest.raises(InvalidRequestError):
            await service.create(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                run_id=run_id,
                kind=ArtifactKind.EXPORT,
                content=b"value",
                expected_sha256="0" * 64,
                media_type="text/plain",
                filename="value.txt",
            )
        with pytest.raises(InvalidRequestError):
            await _create(service, run_id, content=b"x" * 1_025)
        with pytest.raises(InvalidRequestError):
            await service.create(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                run_id=run_id,
                kind=ArtifactKind.RUN_MANIFEST,
                content=b"x",
                expected_sha256=hashlib.sha256(b"x").hexdigest(),
                media_type="text/plain",
                filename="manifest.json",
            )
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_corruption_is_detected_at_end_of_full_read(tmp_path: Path) -> None:
    database, service, store, run_id = await _stack(tmp_path)
    try:
        created = await _create(service, run_id)
        async with database.session() as session:
            key = await session.scalar(
                select(Artifact.storage_key).where(Artifact.id == created.artifact.id)
            )
        assert key is not None
        store.corrupt(key, b'{"no":true}')
        download = await service.download(
            _actor(), project_id=DEMO_PROJECT_ID, artifact_id=created.artifact.id
        )
        with pytest.raises(DependencyUnavailableError):
            _ = b"".join([chunk async for chunk in download.chunks])
    finally:
        await database.dispose()


class DeletingStore(InMemoryObjectStore):
    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        sha256: str,
        media_type: str,
    ) -> PutResult:
        result = await super().put(
            key=key,
            content=content,
            sha256=sha256,
            media_type=media_type,
        )
        async with self.database.session() as session:
            await session.execute(delete(Project).where(Project.id == DEMO_PROJECT_ID))
        return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scope_disappearing_after_upload_triggers_compensation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    store = DeletingStore(database)
    _, service, _, run_id = await _stack(tmp_path, store=store, settings=settings)
    try:
        with pytest.raises(ResourceNotFoundError):
            await _create(service, run_id)
        assert len(await store.list_keys("artifacts/v1", limit=10)) == 1
        deferred = await service.reconcile_orphans(now=datetime.now(UTC))
        assert (deferred.orphaned, deferred.deleted) == (1, 0)
        reconciled = await service.reconcile_orphans(now=datetime.now(UTC) + timedelta(hours=2))
        assert (reconciled.orphaned, reconciled.deleted) == (1, 1)
    finally:
        await database.dispose()


class BarrierStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.arrivals = 0
        self.ready = asyncio.Event()

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        sha256: str,
        media_type: str,
    ) -> PutResult:
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await self.ready.wait()
        return await super().put(
            key=key,
            content=content,
            sha256=sha256,
            media_type=media_type,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_idempotent_adoption_never_deletes_committed_object(
    tmp_path: Path,
) -> None:
    store = BarrierStore()
    database, service, _, run_id = await _stack(tmp_path, store=store)
    try:
        first, second = await asyncio.gather(
            _create(service, run_id),
            _create(service, run_id),
        )
        assert first.artifact.id == second.artifact.id
        assert sorted((first.created, second.created)) == [False, True]
        assert len(await store.list_keys("artifacts/v1", limit=10)) == 1
        download = await service.download(
            _actor(), project_id=DEMO_PROJECT_ID, artifact_id=first.artifact.id
        )
        assert b"".join([chunk async for chunk in download.chunks]) == b'{"ok":true}'
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_uses_exact_returned_keys_above_page_limit(tmp_path: Path) -> None:
    database, service, store, run_id = await _stack(tmp_path)
    try:
        await _create(service, run_id, content=b'{"one":1}')
        await _create(service, run_id, content=b'{"two":2}')

        report = await service.reconcile_orphans(
            limit=1, now=datetime.now(UTC) + timedelta(hours=2)
        )

        assert report.orphaned == 0
        assert report.deleted == 0
        assert len(await store.list_keys("artifacts/v1", limit=10)) == 2
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_cursor_pages_union_without_orphan_or_missing_object_starvation(
    tmp_path: Path,
) -> None:
    database, service, store, run_id = await _stack(tmp_path)
    try:
        first = await _create(service, run_id, content=b'{"one":1}')
        second = await _create(service, run_id, content=b'{"two":2}')
        async with database.session() as session:
            rows = tuple(
                await session.scalars(
                    select(Artifact).where(Artifact.id.in_((first.artifact.id, second.artifact.id)))
                )
            )
        missing = max(rows, key=lambda artifact: artifact.storage_key)
        await store.delete(missing.storage_key)

        orphan_content = b"late orphan"
        orphan_digest = hashlib.sha256(orphan_content).hexdigest()
        orphan_key = f"artifacts/v1/zzzz/{orphan_digest}"
        await store.put(
            key=orphan_key,
            content=orphan_content,
            sha256=orphan_digest,
            media_type="text/plain",
        )

        cursor: str | None = None
        totals = {"missing": 0, "orphaned": 0, "deleted": 0}
        pages = 0
        while True:
            report = await service.reconcile_orphans(
                cursor=cursor,
                limit=1,
                now=datetime.now(UTC) + timedelta(hours=2),
            )
            pages += 1
            totals["missing"] += report.missing
            totals["orphaned"] += report.orphaned
            totals["deleted"] += report.deleted
            cursor = report.next_cursor
            if cursor is None:
                break
            assert pages < 10

        assert pages >= 3
        assert totals == {"missing": 1, "orphaned": 1, "deleted": 1}
        assert orphan_key not in await store.list_keys("artifacts/v1", limit=10)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_rejects_untrusted_cursor(tmp_path: Path) -> None:
    database, service, _, _ = await _stack(tmp_path)
    try:
        with pytest.raises(InvalidRequestError):
            await service.reconcile_orphans(cursor="../tenant/private", limit=1)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_failure_is_safe_and_orphan_reconciliation_is_retryable(tmp_path: Path) -> None:
    database, service, store, run_id = await _stack(tmp_path)
    try:
        store.fail_put = True
        with pytest.raises(DependencyUnavailableError):
            await _create(service, run_id)
        store.fail_put = False
        orphan_content = b"orphan"
        orphan_digest = hashlib.sha256(orphan_content).hexdigest()
        orphan_key = f"artifacts/v1/{'f' * 32}/{orphan_digest}"
        await store.put(
            key=orphan_key,
            content=orphan_content,
            sha256=orphan_digest,
            media_type="text/plain",
        )
        store.fail_delete = True
        failed = await service.reconcile_orphans(now=datetime.now(UTC) + timedelta(hours=2))
        assert (failed.orphaned, failed.deleted, failed.delete_failures) == (1, 0, 1)
        store.fail_delete = False
        recovered = await service.reconcile_orphans(now=datetime.now(UTC) + timedelta(hours=2))
        assert (recovered.orphaned, recovered.deleted) == (1, 1)
    finally:
        await database.dispose()


class SigningStore(InMemoryObjectStore):
    def __init__(self, *, hostile: bool = False) -> None:
        super().__init__()
        self.hostile = hostile

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        content_disposition: str,
    ) -> str | None:
        await self.stat(key)
        host = "attacker.invalid" if self.hostile else "minio"
        query = urlencode(
            {
                "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                "X-Amz-Credential": "local/20260811/us-east-1/s3/aws4_request",
                "X-Amz-Date": "20260811T120000Z",
                "X-Amz-Expires": str(expires_seconds),
                "X-Amz-SignedHeaders": "host",
                "X-Amz-Signature": "c" * 64,
                "response-content-disposition": content_disposition,
            }
        )
        return f"http://{host}:9000/corpuskit-artifacts/{key}?{query}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_presign_is_scoped_bounded_and_endpoint_pinned(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_backend="s3",
        artifact_s3_endpoint="http://minio:9000",
        artifact_s3_path_style=True,
    )
    store = SigningStore()
    database, service, _, run_id = await _stack(tmp_path, store=store, settings=settings)
    try:
        created = await _create(service, run_id)
        signed = await service.sign_download(
            _actor(),
            project_id=DEMO_PROJECT_ID,
            artifact_id=created.artifact.id,
            expires_seconds=30,
        )
        assert signed.url.startswith("http://minio:9000/corpuskit-artifacts/")
        assert signed.expires_at > datetime.now(UTC)
        with pytest.raises(InvalidRequestError):
            await service.sign_download(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                artifact_id=created.artifact.id,
                expires_seconds=901,
            )
        store.hostile = True
        with pytest.raises(DependencyUnavailableError):
            await service.sign_download(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                artifact_id=created.artifact.id,
                expires_seconds=30,
            )
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tombstone_blocks_access_and_purge_waits_for_retention(tmp_path: Path) -> None:
    database, service, store, run_id = await _stack(tmp_path)
    try:
        created = await _create(service, run_id)
        await service.tombstone(
            _actor(), project_id=DEMO_PROJECT_ID, artifact_id=created.artifact.id
        )
        with pytest.raises(ResourceNotFoundError):
            await service.get(_actor(), project_id=DEMO_PROJECT_ID, artifact_id=created.artifact.id)
        early = await service.purge_due(now=datetime.now(UTC))
        assert early.eligible == 0
        due = await service.purge_due(now=datetime.now(UTC) + timedelta(days=31))
        assert (due.eligible, due.deleted, due.failed) == (1, 1, 0)
        assert await store.list_keys("artifacts/v1", limit=10) == ()
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filesystem_signing_is_explicitly_unavailable(tmp_path: Path) -> None:
    database, service, _, run_id = await _stack(tmp_path)
    try:
        created = await _create(service, run_id)
        with pytest.raises(ResourceConflictError):
            await service.sign_download(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                artifact_id=created.artifact.id,
                expires_seconds=30,
            )
    finally:
        await database.dispose()
