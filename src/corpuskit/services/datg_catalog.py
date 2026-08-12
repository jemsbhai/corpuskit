"""Tenant-scoped catalog and authorized inspection of published DATG indexes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.datg import (
    DatgCoveredInspectionRequest,
    DatgFrequencyInspectionRequest,
    DatgIndexArtifact,
    DatgIndexPublication,
    DatgInspectionResult,
    DatgLogitDeltaPreviewRequest,
    DatgLogitDeltaPreviewResult,
    DatgLogitPreviewRequest,
    DatgLogitPreviewResult,
    DatgTargetInspectionRequest,
    DatgUnit,
)
from corpuskit.domain.errors import (
    ApplicationError,
    EngineUnavailableError,
    InvalidRequestError,
    ResourceNotFoundError,
)
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    DatgIndexPublicationRecord,
    Membership,
    Project,
    User,
)
from corpuskit.persistence.tenant_context import TenantContext
from corpuskit.services.datg import (
    DatgIndexCache,
    DatgInspectionService,
    DatgLogitPreviewEngine,
)


@dataclass(frozen=True, slots=True)
class DatgCatalogActor:
    subject: str
    organization_id: UUID


@dataclass(frozen=True, slots=True)
class _SingleArtifactCache:
    artifact: DatgIndexArtifact

    def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
        if cache_key_sha256 != self.artifact.identity.cache_key_sha256:
            return None
        return self.artifact


class DatgIndexCatalogService:
    """Expose only cache entries authorized to the actor's active project."""

    def __init__(
        self,
        database: Database,
        cache: DatgIndexCache,
        preview_engine: DatgLogitPreviewEngine | None = None,
    ) -> None:
        self._database = database
        self._cache = cache
        self._inspection = DatgInspectionService(cache)
        self._preview_engine = preview_engine

    async def list(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[DatgIndexPublication, ...]:
        async with self._database.session(_context(actor)) as session:
            await _authorize_project(session, actor, project_id, "datg.index.catalog")
            rows = tuple(
                await session.scalars(
                    select(DatgIndexPublicationRecord)
                    .where(
                        DatgIndexPublicationRecord.organization_id == actor.organization_id,
                        DatgIndexPublicationRecord.project_id == project_id,
                    )
                    .order_by(
                        DatgIndexPublicationRecord.created_at.desc(),
                        DatgIndexPublicationRecord.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
        publications: list[DatgIndexPublication] = []
        for row in rows:
            artifact = await asyncio.to_thread(self._cache.get, row.cache_key_sha256)
            if artifact is None:
                continue
            if artifact.content_sha256 != row.content_sha256:
                raise EngineUnavailableError("datg.index.catalog_integrity")
            publications.append(_snapshot(row))
        return tuple(publications)

    async def target(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgTargetInspectionRequest,
    ) -> DatgInspectionResult:
        artifact = await self._authorized_artifact(actor, project_id, request.cache_key_sha256)
        return await asyncio.to_thread(
            DatgInspectionService(_SingleArtifactCache(artifact)).target,
            request,
        )

    async def covered(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgCoveredInspectionRequest,
    ) -> DatgInspectionResult:
        artifact = await self._authorized_artifact(actor, project_id, request.cache_key_sha256)
        return await asyncio.to_thread(
            DatgInspectionService(_SingleArtifactCache(artifact)).covered,
            request,
        )

    async def frequency(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgFrequencyInspectionRequest,
    ) -> DatgInspectionResult:
        artifact = await self._authorized_artifact(actor, project_id, request.cache_key_sha256)
        return await asyncio.to_thread(
            DatgInspectionService(_SingleArtifactCache(artifact)).frequency,
            request,
        )

    async def preview_logits(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgLogitDeltaPreviewRequest,
    ) -> DatgLogitDeltaPreviewResult:
        """Authorize one immutable index and calculate bounded additive deltas."""

        if self._preview_engine is None:
            raise EngineUnavailableError("datg.logits.preview_unavailable")
        artifact = await self._authorized_artifact(
            actor,
            project_id,
            request.cache_key_sha256,
        )
        try:
            internal = DatgLogitPreviewRequest(
                artifact=artifact,
                target_phonemes=request.target_phonemes,
                target_units=request.target_units,
                coverage_sequences=request.coverage_sequences,
                guidance=request.guidance,
                logits=request.logits,
            )
        except ValidationError:
            raise InvalidRequestError("datg.logits.preview_units") from None
        try:
            raw = await asyncio.to_thread(self._preview_engine.preview_logits, internal)
            preview = DatgLogitPreviewResult.model_validate(raw.model_dump(mode="json"))
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.logits.preview_contract") from None
        if preview.original_logits != request.logits:
            raise EngineUnavailableError("datg.logits.preview_contract")
        try:
            return DatgLogitDeltaPreviewResult.from_preview(
                cache_key_sha256=artifact.identity.cache_key_sha256,
                preview=preview,
            )
        except ValidationError:
            raise EngineUnavailableError("datg.logits.preview_contract") from None

    async def _authorized_artifact(
        self,
        actor: DatgCatalogActor,
        project_id: UUID,
        cache_key_sha256: str,
    ) -> DatgIndexArtifact:
        content_sha256 = await self._authorize_key(actor, project_id, cache_key_sha256)
        artifact = await asyncio.to_thread(self._inspection.artifact, cache_key_sha256)
        if artifact.content_sha256 != content_sha256:
            raise EngineUnavailableError("datg.index.catalog_integrity")
        return artifact

    async def _authorize_key(
        self,
        actor: DatgCatalogActor,
        project_id: UUID,
        cache_key_sha256: str,
    ) -> str:
        async with self._database.session(_context(actor)) as session:
            await _authorize_project(session, actor, project_id, "datg.index.inspect")
            content_sha256 = await session.scalar(
                select(DatgIndexPublicationRecord.content_sha256).where(
                    DatgIndexPublicationRecord.organization_id == actor.organization_id,
                    DatgIndexPublicationRecord.project_id == project_id,
                    DatgIndexPublicationRecord.cache_key_sha256 == cache_key_sha256,
                )
            )
            if content_sha256 is None:
                raise ResourceNotFoundError("datg.index.inspect")
            return content_sha256


async def _authorize_project(
    session: AsyncSession,
    actor: DatgCatalogActor,
    project_id: UUID,
    operation: str,
) -> None:
    identity = await session.scalar(
        select(User.id)
        .join(Membership, Membership.user_id == User.id)
        .where(
            User.oidc_subject == actor.subject,
            Membership.organization_id == actor.organization_id,
        )
    )
    project = await session.scalar(
        select(Project.id).where(
            Project.id == project_id,
            Project.organization_id == actor.organization_id,
            Project.lifecycle_state == ProjectLifecycle.ACTIVE,
        )
    )
    if identity is None or project is None:
        raise ResourceNotFoundError(operation)


def _snapshot(row: DatgIndexPublicationRecord) -> DatgIndexPublication:
    created_at = row.created_at
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = created_at.astimezone(UTC)
    return DatgIndexPublication(
        build_run_id=row.build_run_id,
        cache_key_sha256=row.cache_key_sha256,
        content_sha256=row.content_sha256,
        runtime_id=row.runtime_id,
        language=row.language,
        unit=DatgUnit(row.unit),
        vocabulary_size=row.vocabulary_size,
        indexed_token_count=row.indexed_token_count,
        size_bytes=row.size_bytes,
        created_at=created_at,
    )


def _context(actor: DatgCatalogActor) -> TenantContext:
    return TenantContext.user(actor.organization_id, actor.subject)


__all__ = ["DatgCatalogActor", "DatgIndexCatalogService"]
