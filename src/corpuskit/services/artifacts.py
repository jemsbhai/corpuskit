"""Tenant-scoped artifact storage, retention, and replay manifests."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ArtifactKind,
    ArtifactState,
    ReplayComparison,
    RunManifest,
    artifact_storage_key,
    compare_replay,
    content_disposition,
    normalize_media_type,
    safe_download_filename,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.phon_rl import PhonRlPromptArtifact
from corpuskit.domain.platform import AuditAction, AuditResourceType
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.artifact_store import (
    ObjectDescriptor,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
    Membership,
    Project,
    Role,
    Run,
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager

WRITER_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.EDITOR})


@dataclass(frozen=True, slots=True)
class ArtifactActor:
    subject: str
    organization_id: UUID
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    id: UUID
    project_id: UUID
    run_id: UUID | None
    kind: ArtifactKind
    sha256: str
    size_bytes: int
    media_type: str
    filename: str
    state: ArtifactState
    retention_until: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactCreation:
    artifact: ArtifactSnapshot
    created: bool


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    chunks: AsyncIterator[bytes]
    size_bytes: int
    media_type: str
    filename: str
    content_disposition: str
    sha256: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class SignedArtifactDownload:
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scanned: int
    orphaned: int
    deleted: int
    delete_failures: int
    missing: int
    corrupt: int
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class PurgeReport:
    eligible: int
    deleted: int
    failed: int


class ArtifactService:
    """Coordinate authorization, immutable objects, relational metadata, and cleanup."""

    def __init__(self, database: Database, store: ObjectStore, settings: Settings) -> None:
        self.database = database
        self._store = store
        self._max_bytes = settings.artifact_max_bytes
        self._retention = timedelta(days=settings.artifact_retention_days)
        self._orphan_grace = timedelta(seconds=settings.artifact_orphan_grace_seconds)
        self._chunk_bytes = settings.artifact_download_chunk_bytes
        self._max_presign_seconds = settings.artifact_presign_seconds
        self._s3_endpoint = settings.artifact_s3_endpoint
        self._s3_bucket = settings.artifact_s3_bucket
        self._s3_path_style = settings.artifact_s3_path_style

    async def create(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        run_id: UUID | None,
        kind: ArtifactKind,
        content: bytes,
        expected_sha256: str,
        media_type: str,
        filename: str,
    ) -> ArtifactCreation:
        operation = "artifact.create"
        try:
            normalized_media_type = normalize_media_type(media_type)
            safe_filename = safe_download_filename(filename)
        except ValueError as exc:
            raise InvalidRequestError(operation) from exc
        if not content or len(content) > self._max_bytes:
            raise InvalidRequestError(operation)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if expected_sha256 != actual_sha256:
            raise InvalidRequestError(operation)
        if kind is ArtifactKind.RUN_MANIFEST and normalized_media_type != "application/json":
            raise InvalidRequestError(operation)
        if kind is ArtifactKind.PROMPT_SET:
            if normalized_media_type != "application/json":
                raise InvalidRequestError(operation)
            try:
                prompt_artifact = PhonRlPromptArtifact.model_validate_json(content, strict=True)
            except ValueError:
                raise InvalidRequestError(operation) from None
            if prompt_artifact.canonical_bytes() != content:
                raise InvalidRequestError(operation)
        scope_key = str(run_id) if run_id is not None else "project"

        try:
            async with self.database.session(_user_context(actor)) as session:
                user_id, _ = await self._authorize_scope(
                    session,
                    actor,
                    project_id=project_id,
                    run_id=run_id,
                    write=True,
                    operation=operation,
                )
                duplicate = await self._duplicate(
                    session,
                    actor=actor,
                    project_id=project_id,
                    scope_key=scope_key,
                    kind=kind,
                    sha256=actual_sha256,
                )
                if duplicate is not None and duplicate.state is not ArtifactState.ACTIVE:
                    raise ResourceConflictError(operation)
        except SQLAlchemyError as exc:
            raise DependencyUnavailableError(operation) from exc

        if duplicate is not None:
            try:
                await self._read_all(duplicate)
            except ObjectNotFoundError:
                # A previous metadata commit outlived its object. Re-run the immutable put
                # below so the caller's known-good bytes can repair the missing object.
                pass
            except (DependencyUnavailableError, ObjectStoreError, ValueError) as exc:
                # Never report an idempotent success for corrupt or unverifiable storage.
                raise DependencyUnavailableError(operation) from exc
            else:
                return ArtifactCreation(_snapshot(duplicate), created=False)

        key = artifact_storage_key(
            organization_id=actor.organization_id,
            project_id=project_id,
            run_id=run_id,
            kind=kind,
            sha256=actual_sha256,
        )
        try:
            await self._store.put(
                key=key,
                content=content,
                sha256=actual_sha256,
                media_type=normalized_media_type,
            )
        except (ObjectStoreError, ValueError) as exc:
            raise DependencyUnavailableError(operation) from exc

        now = datetime.now(UTC)
        artifact = Artifact(
            organization_id=actor.organization_id,
            project_id=project_id,
            run_id=run_id,
            created_by=user_id,
            scope_key=scope_key,
            kind=kind.value,
            sha256=actual_sha256,
            size_bytes=len(content),
            storage_key=key,
            media_type=normalized_media_type,
            filename=safe_filename,
            state=ArtifactState.ACTIVE,
            retention_until=now + self._retention,
        )
        try:
            async with self.database.session(_user_context(actor)) as session:
                await self._authorize_scope(
                    session,
                    actor,
                    project_id=project_id,
                    run_id=run_id,
                    write=True,
                    operation=operation,
                )
                await QuotaManager.consume_artifact(
                    session,
                    organization_id=actor.organization_id,
                    kind=kind,
                    size_bytes=len(content),
                )
                session.add(artifact)
                await session.flush()
                await AuditWriter.append(
                    session,
                    organization_id=actor.organization_id,
                    actor=AuditIdentity.user(user_id),
                    action=AuditAction.ARTIFACT_CREATED,
                    resource_type=AuditResourceType.ARTIFACT,
                    resource_id=artifact.id,
                    request_id=actor.request_id,
                    metadata={
                        "kind": kind.value,
                        "sha256": actual_sha256,
                        "size_bytes": len(content),
                    },
                )
                return ArtifactCreation(_snapshot(artifact), created=True)
        except IntegrityError:
            duplicate = await self._find_duplicate_after_race(
                actor,
                project_id=project_id,
                scope_key=scope_key,
                kind=kind,
                sha256=actual_sha256,
            )
            if duplicate is not None and duplicate.state is ArtifactState.ACTIVE:
                return ArtifactCreation(_snapshot(duplicate), created=False)
            # Do not delete here: a concurrent idempotent writer may already reference this
            # content address. The grace-period reconciler performs safe key-targeted cleanup.
            raise ResourceConflictError(operation) from None
        except ResourceNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise DependencyUnavailableError(operation) from exc

    async def get(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
    ) -> ArtifactSnapshot:
        async with self.database.session(_user_context(actor)) as session:
            await self._authorize_scope(
                session,
                actor,
                project_id=project_id,
                run_id=None,
                write=False,
                operation="artifact.get",
            )
            return _snapshot(
                await self._active_artifact(
                    session,
                    actor.organization_id,
                    project_id,
                    artifact_id,
                    "artifact.get",
                )
            )

    async def list(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[ArtifactSnapshot, ...]:
        async with self.database.session(_user_context(actor)) as session:
            await self._authorize_scope(
                session,
                actor,
                project_id=project_id,
                run_id=None,
                write=False,
                operation="artifact.list",
            )
            artifacts = await session.scalars(
                select(Artifact)
                .where(
                    Artifact.organization_id == actor.organization_id,
                    Artifact.project_id == project_id,
                    Artifact.state == ArtifactState.ACTIVE,
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return tuple(_snapshot(artifact) for artifact in artifacts)

    async def download(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
    ) -> ArtifactDownload:
        artifact = await self._artifact_model(actor, project_id, artifact_id, "artifact.download")
        try:
            opened = await self._store.open(artifact.storage_key, chunk_bytes=self._chunk_bytes)
            _verify_storage_metadata(opened.descriptor, artifact)
        except (ObjectStoreError, ValueError) as exc:
            raise DependencyUnavailableError("artifact.download") from exc
        digest_value = base64.b64encode(bytes.fromhex(artifact.sha256)).decode("ascii")
        return ArtifactDownload(
            chunks=self._verified_chunks(opened.chunks, artifact),
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
            filename=artifact.filename,
            content_disposition=content_disposition(artifact.filename),
            sha256=artifact.sha256,
            content_digest=f"sha-256=:{digest_value}:",
        )

    async def sign_download(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
        expires_seconds: int,
    ) -> SignedArtifactDownload:
        operation = "artifact.sign"
        if not 30 <= expires_seconds <= self._max_presign_seconds:
            raise InvalidRequestError(operation)
        artifact = await self._artifact_model(actor, project_id, artifact_id, operation)
        disposition = content_disposition(artifact.filename)
        try:
            descriptor = await self._store.stat(artifact.storage_key)
            _verify_storage_metadata(descriptor, artifact)
            url = await self._store.presign_get(
                artifact.storage_key,
                expires_seconds=expires_seconds,
                content_disposition=disposition,
            )
        except (ObjectStoreError, ValueError) as exc:
            raise DependencyUnavailableError(operation) from exc
        if url is None:
            raise ResourceConflictError(operation)
        if not self._valid_presigned_url(
            url,
            artifact.storage_key,
            expires_seconds,
            disposition,
        ):
            raise DependencyUnavailableError(operation)
        return SignedArtifactDownload(
            url=url,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_seconds),
        )

    async def tombstone(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
    ) -> None:
        operation = "artifact.delete"
        async with self.database.session(_user_context(actor)) as session:
            user_id, _ = await self._authorize_scope(
                session,
                actor,
                project_id=project_id,
                run_id=None,
                write=True,
                operation=operation,
            )
            artifact = await self._active_artifact(
                session,
                actor.organization_id,
                project_id,
                artifact_id,
                operation,
            )
            artifact.state = ArtifactState.TOMBSTONED
            artifact.tombstoned_at = datetime.now(UTC)
            await AuditWriter.append(
                session,
                organization_id=actor.organization_id,
                actor=AuditIdentity.user(user_id),
                action=AuditAction.ARTIFACT_TOMBSTONED,
                resource_type=AuditResourceType.ARTIFACT,
                resource_id=artifact.id,
                request_id=actor.request_id,
                metadata={"kind": artifact.kind, "size_bytes": artifact.size_bytes},
            )

    async def compare_manifest(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
        observed: RunManifest,
    ) -> ReplayComparison:
        operation = "manifest.compare"
        artifact = await self._artifact_model(actor, project_id, artifact_id, operation)
        if artifact.kind != ArtifactKind.RUN_MANIFEST.value:
            raise InvalidRequestError(operation)
        try:
            expected_bytes = await self._read_all(artifact)
            expected = RunManifest.model_validate_json(expected_bytes, strict=True)
        except (ObjectStoreError, ValueError) as exc:
            raise DependencyUnavailableError(operation) from exc
        return compare_replay(expected, observed)

    async def purge_due(self, *, now: datetime | None = None, limit: int = 1_000) -> PurgeReport:
        cutoff = (now or datetime.now(UTC)).astimezone(UTC)
        global_context = TenantContext.service(ServiceIdentity.MAINTENANCE)
        async with self.database.session(global_context) as session:
            rows = tuple(
                await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.state == ArtifactState.TOMBSTONED,
                        Artifact.retention_until <= cutoff,
                    )
                    .order_by(Artifact.retention_until, Artifact.id)
                    .limit(limit)
                )
            )
        deleted = 0
        failed = 0
        for artifact in rows:
            try:
                await self._store.delete(artifact.storage_key)
                context = TenantContext.service(
                    ServiceIdentity.MAINTENANCE, artifact.organization_id
                )
                async with self.database.session(context) as session:
                    updated_id = await session.scalar(
                        update(Artifact)
                        .where(
                            Artifact.id == artifact.id,
                            Artifact.state == ArtifactState.TOMBSTONED,
                        )
                        .values(state=ArtifactState.DELETED, deleted_at=cutoff)
                        .returning(Artifact.id)
                    )
                    if updated_id is not None:
                        await QuotaManager.release_artifact(
                            session,
                            organization_id=artifact.organization_id,
                            size_bytes=artifact.size_bytes,
                        )
                        await AuditWriter.append(
                            session,
                            organization_id=artifact.organization_id,
                            actor=AuditIdentity.service(ServiceIdentity.MAINTENANCE),
                            action=AuditAction.ARTIFACT_PURGED,
                            resource_type=AuditResourceType.ARTIFACT,
                            resource_id=artifact.id,
                            metadata={
                                "kind": artifact.kind,
                                "size_bytes": artifact.size_bytes,
                            },
                        )
                        deleted += 1
                    else:
                        failed += 1
            except (ObjectStoreError, SQLAlchemyError):
                failed += 1
        return PurgeReport(eligible=len(rows), deleted=deleted, failed=failed)

    async def reconcile_orphans(
        self,
        *,
        cursor: str | None = None,
        limit: int = 1_000,
        now: datetime | None = None,
    ) -> ReconciliationReport:
        """Reconcile an object page by exact key and defer young-orphan deletion."""

        try:
            keys = await self._store.list_keys("artifacts/v1", limit=limit, after=cursor)
            context = TenantContext.service(ServiceIdentity.MAINTENANCE)
            async with self.database.session(context) as session:
                audit_artifacts = tuple(
                    await session.scalars(
                        select(Artifact)
                        .where(
                            Artifact.state != ArtifactState.DELETED,
                            Artifact.storage_key > (cursor or ""),
                        )
                        .order_by(Artifact.storage_key)
                        .limit(limit)
                    )
                )
                next_cursor = _reconciliation_cursor(keys, audit_artifacts, limit)
                page_keys = tuple(key for key in keys if next_cursor is None or key <= next_cursor)
                page_artifacts = tuple(
                    artifact
                    for artifact in audit_artifacts
                    if next_cursor is None or artifact.storage_key <= next_cursor
                )
                referenced_keys = set(
                    await session.scalars(
                        select(Artifact.storage_key).where(Artifact.storage_key.in_(page_keys))
                    )
                )
        except ValueError as exc:
            raise InvalidRequestError("artifact.reconcile") from exc
        except (ObjectStoreError, SQLAlchemyError) as exc:
            raise DependencyUnavailableError("artifact.reconcile") from exc
        orphaned = deleted = delete_failures = corrupt = 0
        orphan_cutoff = (now or datetime.now(UTC)).astimezone(UTC) - self._orphan_grace
        for key in page_keys:
            if key in referenced_keys:
                continue
            try:
                descriptor = await self._store.stat(key)
                orphaned += 1
                if descriptor.modified_at > orphan_cutoff:
                    continue
                await self._store.delete(key)
                deleted += 1
            except ObjectStoreError:
                delete_failures += 1
        missing = 0
        for artifact in page_artifacts:
            try:
                _verify_storage_metadata(
                    await self._store.stat(artifact.storage_key),
                    artifact,
                )
            except ObjectNotFoundError:
                missing += 1
            except (ObjectStoreError, ValueError):
                corrupt += 1
        return ReconciliationReport(
            scanned=len(page_keys),
            orphaned=orphaned,
            deleted=deleted,
            delete_failures=delete_failures,
            missing=missing,
            corrupt=corrupt,
            next_cursor=next_cursor,
        )

    async def _artifact_model(
        self,
        actor: ArtifactActor,
        project_id: UUID,
        artifact_id: UUID,
        operation: str,
    ) -> Artifact:
        async with self.database.session(_user_context(actor)) as session:
            await self._authorize_scope(
                session,
                actor,
                project_id=project_id,
                run_id=None,
                write=False,
                operation=operation,
            )
            return await self._active_artifact(
                session,
                actor.organization_id,
                project_id,
                artifact_id,
                operation,
            )

    async def _read_all(self, artifact: Artifact) -> bytes:
        opened = await self._store.open(artifact.storage_key, chunk_bytes=self._chunk_bytes)
        _verify_storage_metadata(opened.descriptor, artifact)
        chunks: list[bytes] = []
        async for chunk in self._verified_chunks(opened.chunks, artifact):
            chunks.append(chunk)
        return b"".join(chunks)

    async def _verified_chunks(
        self,
        chunks: AsyncIterator[bytes],
        artifact: Artifact,
    ) -> AsyncIterator[bytes]:
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > artifact.size_bytes or size > self._max_bytes:
                    raise ObjectIntegrityError("artifact stream exceeds its declared size")
                digest.update(chunk)
                yield chunk
        except ObjectStoreError as exc:
            raise DependencyUnavailableError("artifact.download") from exc
        if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            raise DependencyUnavailableError("artifact.download")

    async def _find_duplicate_after_race(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        scope_key: str,
        kind: ArtifactKind,
        sha256: str,
    ) -> Artifact | None:
        try:
            async with self.database.session(_user_context(actor)) as session:
                return await self._duplicate(
                    session,
                    actor=actor,
                    project_id=project_id,
                    scope_key=scope_key,
                    kind=kind,
                    sha256=sha256,
                )
        except SQLAlchemyError:
            return None

    @staticmethod
    async def _duplicate(
        session: AsyncSession,
        *,
        actor: ArtifactActor,
        project_id: UUID,
        scope_key: str,
        kind: ArtifactKind,
        sha256: str,
    ) -> Artifact | None:
        artifact: Artifact | None = await session.scalar(
            select(Artifact).where(
                Artifact.organization_id == actor.organization_id,
                Artifact.project_id == project_id,
                Artifact.scope_key == scope_key,
                Artifact.kind == kind.value,
                Artifact.sha256 == sha256,
            )
        )
        return artifact

    @staticmethod
    async def _active_artifact(
        session: AsyncSession,
        organization_id: UUID,
        project_id: UUID,
        artifact_id: UUID,
        operation: str,
    ) -> Artifact:
        artifact = await session.scalar(
            select(Artifact).where(
                Artifact.id == artifact_id,
                Artifact.organization_id == organization_id,
                Artifact.project_id == project_id,
                Artifact.state == ArtifactState.ACTIVE,
            )
        )
        if artifact is None:
            raise ResourceNotFoundError(operation)
        return artifact

    @staticmethod
    async def _authorize_scope(
        session: AsyncSession,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        run_id: UUID | None,
        write: bool,
        operation: str,
    ) -> tuple[UUID, Role]:
        identity = (
            await session.execute(
                select(User.id, Membership.role)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    User.oidc_subject == actor.subject,
                    Membership.organization_id == actor.organization_id,
                )
            )
        ).one_or_none()
        project_statement = select(Project.id).where(
            Project.id == project_id,
            Project.organization_id == actor.organization_id,
            Project.lifecycle_state == ProjectLifecycle.ACTIVE,
        )
        if write:
            project_statement = project_statement.with_for_update()
        project_exists = await session.scalar(project_statement)
        if identity is None or project_exists is None:
            raise ResourceNotFoundError(operation)
        user_id, role = identity._tuple()
        if write and role not in WRITER_ROLES:
            raise ResourceNotFoundError(operation)
        if run_id is not None:
            run_exists = await session.scalar(
                select(Run.id).where(
                    Run.id == run_id,
                    Run.organization_id == actor.organization_id,
                    Run.project_id == project_id,
                )
            )
            if run_exists is None:
                raise ResourceNotFoundError(operation)
        return user_id, role

    def _valid_presigned_url(
        self,
        value: str,
        key: str,
        expires_seconds: int,
        disposition: str,
    ) -> bool:
        if self._s3_endpoint is None:
            return False
        try:
            endpoint = urlsplit(self._s3_endpoint)
            signed = urlsplit(value)
            endpoint_port = endpoint.port
            signed_port = signed.port
        except ValueError:
            return False
        expected_hosts = {endpoint.hostname}
        if not self._s3_path_style and endpoint.hostname is not None:
            expected_hosts.add(f"{self._s3_bucket}.{endpoint.hostname}")
        query = parse_qs(signed.query, keep_blank_values=True)
        expected_path = f"/{self._s3_bucket}/{key}" if self._s3_path_style else f"/{key}"
        required = {
            "X-Amz-Algorithm",
            "X-Amz-Credential",
            "X-Amz-Date",
            "X-Amz-Expires",
            "X-Amz-SignedHeaders",
            "X-Amz-Signature",
        }
        return (
            signed.scheme == endpoint.scheme
            and signed.hostname in expected_hosts
            and signed_port == endpoint_port
            and signed.username is None
            and signed.password is None
            and not signed.fragment
            and unquote(signed.path) == expected_path
            and required.issubset(query)
            and all(len(query[name]) == 1 and bool(query[name][0]) for name in required)
            and query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
            and query["X-Amz-Expires"] == [str(expires_seconds)]
            and query.get("response-content-disposition") == [disposition]
            and len(query["X-Amz-Signature"]) == 1
            and re.fullmatch(r"[0-9a-f]{64}", query["X-Amz-Signature"][0]) is not None
        )


def _reconciliation_cursor(
    keys: tuple[str, ...],
    artifacts: tuple[Artifact, ...],
    limit: int,
) -> str | None:
    """Advance by the lower full-page boundary across object and metadata streams."""

    boundaries: list[str] = []
    if len(keys) == limit:
        boundaries.append(keys[-1])
    if len(artifacts) == limit:
        boundaries.append(artifacts[-1].storage_key)
    return min(boundaries) if boundaries else None


def _verify_storage_metadata(descriptor: ObjectDescriptor, artifact: Artifact) -> None:
    if (
        descriptor.key != artifact.storage_key
        or descriptor.size_bytes != artifact.size_bytes
        or descriptor.sha256 != artifact.sha256
        or descriptor.media_type != artifact.media_type
    ):
        raise ObjectIntegrityError("stored artifact metadata mismatch")


def _user_context(actor: ArtifactActor) -> TenantContext:
    return TenantContext.user(actor.organization_id, actor.subject)


def _snapshot(artifact: Artifact) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        id=artifact.id,
        project_id=artifact.project_id,
        run_id=artifact.run_id,
        kind=ArtifactKind(artifact.kind),
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        filename=artifact.filename,
        state=artifact.state,
        retention_until=artifact.retention_until,
        created_at=artifact.created_at,
    )


__all__ = [
    "ArtifactActor",
    "ArtifactCreation",
    "ArtifactDownload",
    "ArtifactService",
    "ArtifactSnapshot",
    "PurgeReport",
    "ReconciliationReport",
    "SignedArtifactDownload",
]
