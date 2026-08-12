"""Retention, isolation, race, audit, and quota acceptance for project erasure."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind, ArtifactState
from corpuskit.domain.errors import (
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.platform import AuditAction
from corpuskit.domain.workspaces import (
    ManualCorpusInput,
    ProjectDeletionInput,
    ProjectInput,
    ProjectLifecycle,
)
from corpuskit.persistence.artifact_store import InMemoryObjectStore, PutResult
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
    AuditEvent,
    Membership,
    Organization,
    Project,
    QuotaUsage,
    Role,
    User,
)
from corpuskit.services.artifacts import ArtifactActor, ArtifactService
from corpuskit.services.jobs import JobActor, JobControlPlane, RunSubmission
from corpuskit.services.platform import AuditWriter
from corpuskit.services.project_deletion import ProjectDeletionMaintenance
from corpuskit.services.project_workspaces import ProjectWorkspaceService, WorkspaceActor


class PausingObjectStore(InMemoryObjectStore):
    """Write bytes, then pause so deletion can win before metadata persistence."""

    def __init__(self) -> None:
        super().__init__()
        self.put_completed = asyncio.Event()
        self.release_put = asyncio.Event()

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
        self.put_completed.set()
        await self.release_put.wait()
        return result


@dataclass(frozen=True, slots=True)
class Stack:
    database: Database
    settings: Settings
    workspace: ProjectWorkspaceService
    artifacts: ArtifactService
    store: InMemoryObjectStore
    actor: WorkspaceActor
    user_id: UUID


async def _stack(
    tmp_path: Path,
    *,
    store: InMemoryObjectStore | None = None,
) -> Stack:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'deletion.db').as_posix()}",
        api_docs_enabled=True,
        artifact_retention_days=30,
        artifact_orphan_grace_seconds=60,
        artifact_max_bytes=1_024,
        max_upload_bytes=1_024,
        _env_file=None,
    )
    database = Database(settings.database_url)
    await database.create_schema()
    jobs = JobControlPlane(database)
    actor = WorkspaceActor(
        subject=DEMO_PRINCIPAL.subject,
        organization_id=DEMO_PRINCIPAL.organization_id,
        request_id="project-delete-test",
    )
    await jobs.bootstrap_demo(
        JobActor(subject=actor.subject, organization_id=actor.organization_id),
        environment="test",
    )
    object_store = store or InMemoryObjectStore()
    return Stack(
        database=database,
        settings=settings,
        workspace=ProjectWorkspaceService(database, settings),
        artifacts=ArtifactService(database, object_store, settings),
        store=object_store,
        actor=actor,
        user_id=UUID("00000000-0000-4000-8000-000000000002"),
    )


async def _artifact(stack: Stack, project_id: UUID, content: bytes = b"project export") -> UUID:
    creation = await stack.artifacts.create(
        ArtifactActor(
            subject=stack.actor.subject,
            organization_id=stack.actor.organization_id,
            request_id="artifact-before-delete",
        ),
        project_id=project_id,
        run_id=None,
        kind=ArtifactKind.EXPORT,
        content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain",
        filename="export.txt",
    )
    return creation.artifact.id


async def _usage(database: Database, organization_id: UUID) -> tuple[int, int, int]:
    async with database.session() as session:
        usage = await session.get(QuotaUsage, organization_id)
        assert usage is not None
        return usage.artifact_count, usage.artifact_bytes, usage.corpus_sentences


@pytest.mark.integration
@pytest.mark.asyncio
async def test_request_hides_project_tombstones_artifacts_and_is_idempotent(
    tmp_path: Path,
) -> None:
    stack = await _stack(tmp_path)
    try:
        project = await stack.workspace.create_project(
            stack.actor,
            ProjectInput(name="Erase me", description="Sensitive workspace"),
        )
        corpus = await stack.workspace.create_manual_corpus(
            stack.actor,
            project.id,
            ManualCorpusInput(name="Seed", sentences=("one", "two")),
        )
        artifact_id = await _artifact(stack, project.id)
        usage_before = await _usage(stack.database, stack.actor.organization_id)

        with pytest.raises(InvalidRequestError):
            await stack.workspace.request_project_deletion(
                stack.actor,
                project.id,
                ProjectDeletionInput(confirmation="delete Erase me"),
            )
        first = await stack.workspace.request_project_deletion(
            stack.actor,
            project.id,
            ProjectDeletionInput(confirmation="DELETE Erase me"),
        )
        second = await stack.workspace.request_project_deletion(
            stack.actor,
            project.id,
            ProjectDeletionInput(confirmation="DELETE Erase me"),
        )

        assert second == first
        assert first.state is ProjectLifecycle.DELETION_PENDING
        assert first.retention_until - first.requested_at == timedelta(days=30)
        assert project.id not in {
            item.id for item in await stack.workspace.list_projects(stack.actor)
        }
        for operation in (
            stack.workspace.list_corpora(stack.actor, project.id),
            stack.workspace.list_versions(stack.actor, project.id, corpus.corpus.id),
            stack.workspace.list_sentences(
                stack.actor,
                project.id,
                corpus.corpus.id,
                corpus.version.id,
                offset=0,
                limit=10,
            ),
            stack.artifacts.list(
                ArtifactActor(stack.actor.subject, stack.actor.organization_id),
                project_id=project.id,
            ),
            stack.artifacts.get(
                ArtifactActor(stack.actor.subject, stack.actor.organization_id),
                project_id=project.id,
                artifact_id=artifact_id,
            ),
        ):
            with pytest.raises(ResourceNotFoundError):
                await operation

        assert await _usage(stack.database, stack.actor.organization_id) == usage_before
        async with stack.database.session() as session:
            persisted = await session.get(Project, project.id)
            artifact = await session.get(Artifact, artifact_id)
            events = tuple(
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.organization_id == stack.actor.organization_id,
                        AuditEvent.action == AuditAction.PROJECT_DELETION_REQUESTED,
                        AuditEvent.resource_id == project.id,
                    )
                )
            )
        assert persisted is not None
        assert persisted.lifecycle_state is ProjectLifecycle.DELETION_PENDING
        assert persisted.deletion_corpus_sentences == 2
        assert artifact is not None
        assert artifact.state is ArtifactState.TOMBSTONED
        artifact_retention = artifact.retention_until
        if artifact_retention.tzinfo is None:
            artifact_retention = artifact_retention.replace(tzinfo=UTC)
        assert artifact_retention >= first.retention_until
        assert len(events) == 1
        assert events[0].details["artifact_count"] == 1
        assert events[0].details["corpus_sentences"] == 2
    finally:
        await stack.database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deletion_is_admin_only_non_enumerating_and_rejects_active_runs(
    tmp_path: Path,
) -> None:
    stack = await _stack(tmp_path)
    try:
        project = await stack.workspace.create_project(
            stack.actor,
            ProjectInput(name="Busy project"),
        )
        async with stack.database.session() as session:
            editor = User(oidc_subject="oidc|editor", display_name="Editor")
            other_org = Organization(slug="other-delete", name="Other")
            intruder = User(oidc_subject="oidc|intruder-delete", display_name="Intruder")
            session.add_all((editor, other_org, intruder))
            await session.flush()
            session.add_all(
                (
                    Membership(
                        organization_id=stack.actor.organization_id,
                        user_id=editor.id,
                        role=Role.EDITOR,
                    ),
                    Membership(
                        organization_id=other_org.id,
                        user_id=intruder.id,
                        role=Role.OWNER,
                    ),
                )
            )
            await session.flush()
            other_org_id = other_org.id

        editor_actor = WorkspaceActor("oidc|editor", stack.actor.organization_id)
        intruder_actor = WorkspaceActor("oidc|intruder-delete", other_org_id)
        for actor in (editor_actor, intruder_actor):
            with pytest.raises(ResourceNotFoundError):
                await stack.workspace.request_project_deletion(
                    actor,
                    project.id,
                    ProjectDeletionInput(confirmation="DELETE Busy project"),
                )

        jobs = JobControlPlane(stack.database)
        await jobs.submit(
            JobActor(stack.actor.subject, stack.actor.organization_id),
            RunSubmission(
                project_id=project.id,
                kind=RunKind.EVALUATE,
                spec={"language": "en-us"},
            ),
            idempotency_key="busy-project-run",
        )
        with pytest.raises(ResourceConflictError):
            await stack.workspace.request_project_deletion(
                stack.actor,
                project.id,
                ProjectDeletionInput(confirmation="DELETE Busy project"),
            )
        assert project.id in {item.id for item in await stack.workspace.list_projects(stack.actor)}
    finally:
        await stack.database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purge_waits_for_retention_and_object_success_then_releases_exact_quota(
    tmp_path: Path,
) -> None:
    stack = await _stack(tmp_path)
    try:
        project = await stack.workspace.create_project(
            stack.actor,
            ProjectInput(name="Retention project"),
        )
        await stack.workspace.create_manual_corpus(
            stack.actor,
            project.id,
            ManualCorpusInput(name="Seed", sentences=("one", "two", "three")),
        )
        await _artifact(stack, project.id, b"retained bytes")
        deletion = await stack.workspace.request_project_deletion(
            stack.actor,
            project.id,
            ProjectDeletionInput(confirmation="DELETE Retention project"),
        )
        maintenance = ProjectDeletionMaintenance(stack.database, stack.store)

        early_artifacts = await stack.artifacts.purge_due(
            now=deletion.retention_until - timedelta(microseconds=1)
        )
        early_project = await maintenance.purge_due(
            now=deletion.retention_until - timedelta(microseconds=1)
        )
        assert early_artifacts.eligible == 0
        assert early_project.eligible == 0

        stack.store.fail_delete = True
        failed_artifacts = await stack.artifacts.purge_due(now=deletion.retention_until)
        deferred_project = await maintenance.purge_due(now=deletion.retention_until)
        assert failed_artifacts.failed == 1
        assert deferred_project.deferred == 1
        assert await _usage(stack.database, stack.actor.organization_id) == (
            1,
            len(b"retained bytes"),
            3,
        )

        stack.store.fail_delete = False
        purged_artifacts = await stack.artifacts.purge_due(now=deletion.retention_until)
        purged_project = await maintenance.purge_due(now=deletion.retention_until)
        assert purged_artifacts.deleted == 1
        assert purged_project.deleted == 1
        assert await _usage(stack.database, stack.actor.organization_id) == (0, 0, 0)
        async with stack.database.session() as session:
            assert await session.get(Project, project.id) is None
            assert (
                await session.scalar(
                    select(func.count(Artifact.id)).where(Artifact.project_id == project.id)
                )
                == 0
            )
            purge_event = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.organization_id == stack.actor.organization_id,
                    AuditEvent.action == AuditAction.PROJECT_PURGED,
                    AuditEvent.resource_id == project.id,
                )
            )
            assert purge_event is not None
            assert purge_event.details == {"artifact_count": 1, "corpus_sentences": 3}
            assert await AuditWriter.verify(session, stack.actor.organization_id) is True
    finally:
        await stack.database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inflight_artifact_cannot_commit_after_deletion_and_orphan_blocks_purge(
    tmp_path: Path,
) -> None:
    store = PausingObjectStore()
    stack = await _stack(tmp_path, store=store)
    try:
        project = await stack.workspace.create_project(
            stack.actor,
            ProjectInput(name="Race project"),
        )
        content = b"in-flight payload"
        upload = asyncio.create_task(
            stack.artifacts.create(
                ArtifactActor(stack.actor.subject, stack.actor.organization_id),
                project_id=project.id,
                run_id=None,
                kind=ArtifactKind.EXPORT,
                content=content,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                media_type="text/plain",
                filename="race.txt",
            )
        )
        await asyncio.wait_for(store.put_completed.wait(), timeout=5)
        deletion = await stack.workspace.request_project_deletion(
            stack.actor,
            project.id,
            ProjectDeletionInput(confirmation="DELETE Race project"),
        )
        store.release_put.set()
        with pytest.raises(ResourceNotFoundError):
            await asyncio.wait_for(upload, timeout=5)

        async with stack.database.session() as session:
            active = await session.scalar(
                select(func.count(Artifact.id)).where(
                    Artifact.project_id == project.id,
                    Artifact.state == ArtifactState.ACTIVE,
                )
            )
        assert active == 0
        assert await _usage(stack.database, stack.actor.organization_id) == (0, 0, 0)
        prefix = f"artifacts/v1/{stack.actor.organization_id.hex}/{project.id.hex}/"
        assert len(await store.list_keys(prefix, limit=10)) == 1

        maintenance = ProjectDeletionMaintenance(stack.database, store)
        deferred = await maintenance.purge_due(now=deletion.retention_until)
        assert deferred.deferred == 1
        reconciliation = await stack.artifacts.reconcile_orphans(
            now=deletion.retention_until,
        )
        assert reconciliation.orphaned == 1
        assert reconciliation.deleted == 1
        purged = await maintenance.purge_due(now=deletion.retention_until)
        assert purged.deleted == 1
    finally:
        store.release_put.set()
        await stack.database.dispose()
