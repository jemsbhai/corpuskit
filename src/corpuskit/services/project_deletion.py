"""Retention-safe project deletion scheduling and maintenance finalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.artifacts import ArtifactState
from corpuskit.domain.errors import (
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.jobs import RunState
from corpuskit.domain.platform import (
    AuditAction,
    AuditResourceType,
    QuotaReservationState,
)
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.artifact_store import ObjectStore, ObjectStoreError
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
    Corpus,
    CorpusVersion,
    DatgIndexPublicationRecord,
    OutboxMessage,
    Project,
    QuotaReservation,
    Run,
    RunEvent,
    RunExecutionFact,
    RunReplay,
    Sentence,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager
from corpuskit.services.project_lifecycle import lock_project_lifecycle

MINIMUM_PROJECT_RETENTION = timedelta(days=30)
_NONTERMINAL_RUN_STATES = (
    RunState.DRAFT,
    RunState.QUEUED,
    RunState.PROVISIONING,
    RunState.RUNNING,
    RunState.CANCELLING,
)


@dataclass(frozen=True, slots=True)
class ProjectDeletionSnapshot:
    project_id: UUID
    state: ProjectLifecycle
    requested_at: datetime
    retention_until: datetime


@dataclass(frozen=True, slots=True)
class ProjectPurgeReport:
    eligible: int
    deleted: int
    deferred: int
    failed: int


class ProjectDeletionService:
    """Schedule deletion atomically without removing bytes or metadata in the request."""

    @staticmethod
    async def request(
        session: AsyncSession,
        *,
        organization_id: UUID,
        user_id: UUID,
        project_id: UUID,
        confirmation: str,
        request_id: str | None,
        retention: timedelta,
        now: datetime | None = None,
    ) -> ProjectDeletionSnapshot:
        operation = "project.delete"
        if retention < MINIMUM_PROJECT_RETENTION:
            raise ValueError("project deletion retention must be at least 30 days")
        requested_at = _utc(now or datetime.now(UTC))
        await lock_project_lifecycle(session, project_id)
        project = await session.scalar(
            select(Project)
            .where(
                Project.id == project_id,
                Project.organization_id == organization_id,
            )
            .with_for_update()
        )
        if project is None:
            raise ResourceNotFoundError(operation)
        if confirmation != f"DELETE {project.name}":
            raise InvalidRequestError(operation)
        if project.lifecycle_state is ProjectLifecycle.DELETION_PENDING:
            return _snapshot(project)

        active_run = await session.scalar(
            select(Run.id)
            .where(
                Run.organization_id == organization_id,
                Run.project_id == project_id,
                Run.state.in_(_NONTERMINAL_RUN_STATES),
            )
            .limit(1)
        )
        if active_run is not None:
            raise ResourceConflictError(operation)
        active_reservation = await session.scalar(
            select(QuotaReservation.id)
            .join(Run, Run.id == QuotaReservation.run_id)
            .where(
                QuotaReservation.organization_id == organization_id,
                QuotaReservation.state == QuotaReservationState.ACTIVE,
                Run.organization_id == organization_id,
                Run.project_id == project_id,
            )
            .limit(1)
        )
        if active_reservation is not None:
            raise ResourceConflictError(operation)

        corpus_sentences = int(
            await session.scalar(
                select(func.count(Sentence.id))
                .select_from(Sentence)
                .join(CorpusVersion, CorpusVersion.id == Sentence.corpus_version_id)
                .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
                .where(
                    Sentence.organization_id == organization_id,
                    CorpusVersion.organization_id == organization_id,
                    Corpus.organization_id == organization_id,
                    Corpus.project_id == project_id,
                )
            )
            or 0
        )
        artifacts = tuple(
            await session.scalars(
                select(Artifact)
                .where(
                    Artifact.organization_id == organization_id,
                    Artifact.project_id == project_id,
                    Artifact.state != ArtifactState.DELETED,
                )
                .with_for_update()
            )
        )
        retention_until = requested_at + retention
        for artifact in artifacts:
            artifact.state = ArtifactState.TOMBSTONED
            artifact.tombstoned_at = artifact.tombstoned_at or requested_at
            if _utc(artifact.retention_until) < retention_until:
                artifact.retention_until = retention_until

        project.lifecycle_state = ProjectLifecycle.DELETION_PENDING
        project.deletion_requested_at = requested_at
        project.deletion_retention_until = retention_until
        project.deletion_corpus_sentences = corpus_sentences
        await AuditWriter.append(
            session,
            organization_id=organization_id,
            actor=AuditIdentity.user(user_id),
            action=AuditAction.PROJECT_DELETION_REQUESTED,
            resource_type=AuditResourceType.PROJECT,
            resource_id=project.id,
            request_id=request_id,
            metadata={
                "artifact_count": len(artifacts),
                "corpus_sentences": corpus_sentences,
                "retention_until": retention_until.isoformat(),
            },
            occurred_at=requested_at,
        )
        await session.flush()
        return _snapshot(project)


class ProjectDeletionMaintenance:
    """Purge due projects only after database and object-store deletion preconditions hold."""

    def __init__(self, database: Database, store: ObjectStore) -> None:
        self._database = database
        self._store = store

    async def purge_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> ProjectPurgeReport:
        if not 1 <= limit <= 1_000:
            raise ValueError("project purge limit must be between 1 and 1000")
        cutoff = _utc(now or datetime.now(UTC))
        async with self._database.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            candidates = tuple(
                (
                    await session.execute(
                        select(Project.id, Project.organization_id)
                        .where(
                            Project.lifecycle_state == ProjectLifecycle.DELETION_PENDING,
                            Project.deletion_retention_until <= cutoff,
                        )
                        .order_by(Project.deletion_retention_until, Project.id)
                        .limit(limit)
                    )
                ).all()
            )

        deleted = deferred = failed = 0
        for project_id, organization_id in candidates:
            try:
                outcome = await self._purge_candidate(
                    project_id=project_id,
                    organization_id=organization_id,
                    cutoff=cutoff,
                )
            except (ObjectStoreError, SQLAlchemyError, RuntimeError, ValueError):
                failed += 1
            else:
                if outcome:
                    deleted += 1
                else:
                    deferred += 1
        return ProjectPurgeReport(
            eligible=len(candidates),
            deleted=deleted,
            deferred=deferred,
            failed=failed,
        )

    async def _purge_candidate(
        self,
        *,
        project_id: UUID,
        organization_id: UUID,
        cutoff: datetime,
    ) -> bool:
        context = TenantContext.service(ServiceIdentity.MAINTENANCE, organization_id)
        async with self._database.session(context) as session:
            await lock_project_lifecycle(session, project_id)
            project = await _due_project(session, project_id, organization_id, cutoff)
            if project is None or not await _database_preconditions(session, project):
                return False

        prefix = f"artifacts/v1/{organization_id.hex}/{project_id.hex}/"
        if await self._store.list_keys(prefix, limit=1):
            return False

        async with self._database.session(context) as session:
            await lock_project_lifecycle(session, project_id)
            project = await _due_project(session, project_id, organization_id, cutoff)
            if project is None or not await _database_preconditions(session, project):
                return False
            expected_sentences = project.deletion_corpus_sentences
            if expected_sentences is None:
                raise RuntimeError("project deletion accounting is unavailable")
            actual_sentences = await _corpus_sentence_count(
                session,
                organization_id=organization_id,
                project_id=project_id,
            )
            if actual_sentences != expected_sentences:
                raise RuntimeError("project corpus quota accounting is inconsistent")
            artifact_count = int(
                await session.scalar(
                    select(func.count(Artifact.id)).where(
                        Artifact.organization_id == organization_id,
                        Artifact.project_id == project_id,
                    )
                )
                or 0
            )
            await session.execute(
                delete(RunReplay).where(
                    RunReplay.organization_id == organization_id,
                    RunReplay.project_id == project_id,
                )
            )
            await session.execute(
                delete(RunExecutionFact).where(
                    RunExecutionFact.organization_id == organization_id,
                    RunExecutionFact.project_id == project_id,
                )
            )
            await session.execute(
                delete(DatgIndexPublicationRecord).where(
                    DatgIndexPublicationRecord.organization_id == organization_id,
                    DatgIndexPublicationRecord.project_id == project_id,
                )
            )
            await session.execute(
                delete(Artifact).where(
                    Artifact.organization_id == organization_id,
                    Artifact.project_id == project_id,
                    Artifact.state == ArtifactState.DELETED,
                )
            )
            run_ids = select(Run.id).where(
                Run.organization_id == organization_id,
                Run.project_id == project_id,
            )
            await session.execute(
                delete(QuotaReservation).where(
                    QuotaReservation.organization_id == organization_id,
                    QuotaReservation.run_id.in_(run_ids),
                )
            )
            await session.execute(
                delete(OutboxMessage).where(
                    OutboxMessage.organization_id == organization_id,
                    OutboxMessage.run_id.in_(run_ids),
                )
            )
            await session.execute(
                delete(RunEvent).where(
                    RunEvent.organization_id == organization_id,
                    RunEvent.run_id.in_(run_ids),
                )
            )
            await session.execute(
                delete(Run).where(
                    Run.organization_id == organization_id,
                    Run.project_id == project_id,
                )
            )
            version_ids = (
                select(CorpusVersion.id)
                .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
                .where(
                    CorpusVersion.organization_id == organization_id,
                    Corpus.organization_id == organization_id,
                    Corpus.project_id == project_id,
                )
            )
            await session.execute(
                delete(Sentence).where(
                    Sentence.organization_id == organization_id,
                    Sentence.corpus_version_id.in_(version_ids),
                )
            )
            await session.execute(
                delete(CorpusVersion).where(
                    CorpusVersion.organization_id == organization_id,
                    CorpusVersion.id.in_(version_ids),
                )
            )
            await session.execute(
                delete(Corpus).where(
                    Corpus.organization_id == organization_id,
                    Corpus.project_id == project_id,
                )
            )
            if expected_sentences:
                await QuotaManager.release_corpus_sentences(
                    session,
                    organization_id=organization_id,
                    sentence_count=expected_sentences,
                )
            await AuditWriter.append(
                session,
                organization_id=organization_id,
                actor=AuditIdentity.service(ServiceIdentity.MAINTENANCE),
                action=AuditAction.PROJECT_PURGED,
                resource_type=AuditResourceType.PROJECT,
                resource_id=project_id,
                metadata={
                    "artifact_count": artifact_count,
                    "corpus_sentences": expected_sentences,
                },
                occurred_at=cutoff,
            )
            await session.delete(project)
            await session.flush()
            return True


async def _due_project(
    session: AsyncSession,
    project_id: UUID,
    organization_id: UUID,
    cutoff: datetime,
) -> Project | None:
    project: Project | None = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.organization_id == organization_id,
            Project.lifecycle_state == ProjectLifecycle.DELETION_PENDING,
            Project.deletion_retention_until <= cutoff,
        )
    )
    return project


async def _database_preconditions(session: AsyncSession, project: Project) -> bool:
    remaining_artifact = await session.scalar(
        select(Artifact.id)
        .where(
            Artifact.organization_id == project.organization_id,
            Artifact.project_id == project.id,
            Artifact.state != ArtifactState.DELETED,
        )
        .limit(1)
    )
    active_run = await session.scalar(
        select(Run.id)
        .where(
            Run.organization_id == project.organization_id,
            Run.project_id == project.id,
            Run.state.in_(_NONTERMINAL_RUN_STATES),
        )
        .limit(1)
    )
    active_reservation = await session.scalar(
        select(QuotaReservation.id)
        .join(Run, Run.id == QuotaReservation.run_id)
        .where(
            QuotaReservation.organization_id == project.organization_id,
            QuotaReservation.state == QuotaReservationState.ACTIVE,
            Run.organization_id == project.organization_id,
            Run.project_id == project.id,
        )
        .limit(1)
    )
    return remaining_artifact is None and active_run is None and active_reservation is None


async def _corpus_sentence_count(
    session: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID,
) -> int:
    return int(
        await session.scalar(
            select(func.count(Sentence.id))
            .select_from(Sentence)
            .join(CorpusVersion, CorpusVersion.id == Sentence.corpus_version_id)
            .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
            .where(
                Sentence.organization_id == organization_id,
                CorpusVersion.organization_id == organization_id,
                Corpus.organization_id == organization_id,
                Corpus.project_id == project_id,
            )
        )
        or 0
    )


def _snapshot(project: Project) -> ProjectDeletionSnapshot:
    if (
        project.lifecycle_state is not ProjectLifecycle.DELETION_PENDING
        or project.deletion_requested_at is None
        or project.deletion_retention_until is None
    ):
        raise RuntimeError("project deletion state is inconsistent")
    return ProjectDeletionSnapshot(
        project_id=project.id,
        state=project.lifecycle_state,
        requested_at=_utc(project.deletion_requested_at),
        retention_until=_utc(project.deletion_retention_until),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "MINIMUM_PROJECT_RETENTION",
    "ProjectDeletionMaintenance",
    "ProjectDeletionService",
    "ProjectDeletionSnapshot",
    "ProjectPurgeReport",
]
