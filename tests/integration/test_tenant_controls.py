"""SQLite semantic acceptance for quotas, audit evidence, and tenant predicates."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind
from corpuskit.domain.errors import InvalidRequestError, QuotaExceededError, ResourceNotFoundError
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
)
from corpuskit.domain.platform import (
    AuditAction,
    AuditResourceType,
    QuotaPolicyValues,
    QuotaReservationState,
)
from corpuskit.domain.workspaces import ManualCorpusInput, ProjectInput
from corpuskit.persistence.artifact_store import InMemoryObjectStore
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    AuditEvent,
    AuditHead,
    Membership,
    Organization,
    Project,
    QuotaReservation,
    QuotaUsage,
    Role,
    Run,
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext, TenantContextError
from corpuskit.services.artifacts import ArtifactActor, ArtifactService
from corpuskit.services.jobs import JobActor, JobControlPlane, RunSubmission
from corpuskit.services.platform import (
    AuditIdentity,
    AuditWriter,
    PlatformActor,
    PlatformService,
    QuotaManager,
)
from corpuskit.services.project_workspaces import ProjectWorkspaceService, WorkspaceActor
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.store import DurableRunStore


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'tenant-controls.db').as_posix()}")
    await database.create_schema()
    yield database
    await database.drop_schema()
    await database.dispose()


async def _identity(
    database: Database,
    suffix: str,
    *,
    role: Role = Role.OWNER,
) -> tuple[JobActor, Project, UUID]:
    async with database.session() as session:
        organization = Organization(slug=f"tenant-{suffix}", name=f"Tenant {suffix}")
        user = User(oidc_subject=f"oidc|{suffix}", display_name=suffix)
        session.add_all((organization, user))
        await session.flush()
        session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
        project = Project(
            organization_id=organization.id,
            created_by=user.id,
            name=f"Project {suffix}",
            description="",
        )
        session.add(project)
        await session.flush()
        return JobActor(user.oidc_subject, organization.id, f"request-{suffix}"), project, user.id


def _reference(run: object) -> RunWorkflowReference:
    from corpuskit.services.jobs import RunSnapshot

    if not isinstance(run, RunSnapshot):
        raise TypeError("run snapshot required")
    return RunWorkflowReference(
        organization_id=str(run.organization_id),
        run_id=str(run.id),
        spec_sha256=run.spec_sha256,
    )


def _rl_spec(*, deadline: float) -> dict[str, object]:
    return PhonRlTrainingRequest(
        runtime_id="rl-runtime",
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="seed", requested_prompts=1),
        parameters=PhonRlTrainingParameters(
            seed=1,
            num_steps=1,
            batch_size=1,
            max_new_tokens=1,
            activity_timeout_seconds=deadline,
        ),
    ).model_dump(mode="json")


def _rl_admission() -> ConfiguredRunAdmission:
    pin = PhonRlSnapshotPin(
        repository_id="acme/tiny-rl",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    return ConfiguredRunAdmission.from_settings(
        Settings(
            environment="test",
            worker_phon_rl_runtime_policies=(
                PhonRlRuntimePolicyEntry(
                    runtime_id="rl-runtime",
                    model=pin,
                    tokenizer=pin,
                    cache_root_id="models-ro",
                    cache_mount_read_only=True,
                    allowed_prompt_strategies=("seed",),
                ),
            ),
        )
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_job_quota_is_idempotent_terminally_released_and_tenant_independent(
    database: Database,
) -> None:
    actor, project, _ = await _identity(database, "alpha")
    other, other_project, _ = await _identity(database, "beta")
    jobs = JobControlPlane(database)
    created = []
    for index in range(3):
        created.append(
            await jobs.submit(
                actor,
                RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={"n": index}),
                idempotency_key=f"cpu-{index}",
            )
        )
    replay = await jobs.submit(
        actor,
        RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={"n": 0}),
        idempotency_key="cpu-0",
    )
    assert replay.created is False
    with pytest.raises(QuotaExceededError) as exhausted:
        await jobs.submit(
            actor,
            RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={}),
            idempotency_key="cpu-4",
        )
    assert exhausted.value.retry_after_seconds == 30

    independent = await jobs.submit(
        other,
        RunSubmission(project_id=other_project.id, kind=RunKind.EVALUATE, spec={}),
        idempotency_key="independent",
    )
    assert independent.created is True

    store = DurableRunStore(database)
    assert await store.fail(_reference(created[0].run), "execution_failed") is RunState.FAILED
    replacement = await jobs.submit(
        actor,
        RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={}),
        idempotency_key="cpu-replacement",
    )
    assert replacement.created is True

    platform = PlatformService(database)
    snapshot = await platform.quota(PlatformActor(actor.subject, actor.organization_id))
    assert snapshot.usage.active_cpu_jobs == 3
    assert snapshot.usage.active_expensive_jobs == 0
    page = await platform.audit_events(
        PlatformActor(actor.subject, actor.organization_id),
        limit=2,
    )
    assert len(page.events) == 2
    assert page.next_cursor == "2"
    assert all(event.request_id == "request-alpha" for event in page.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quota_mutations_roll_back_with_the_resource_transaction(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, project, _ = await _identity(database, "rollback")
    jobs = JobControlPlane(database)

    async def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit persistence failure")

    monkeypatch.setattr(AuditWriter, "append", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit"):
        await jobs.submit(
            actor,
            RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={}),
            idempotency_key="rollback",
        )
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(QuotaReservation)) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_long_deadline_lease_renews_and_stale_expiry_terminalizes_before_release(
    database: Database,
) -> None:
    actor, project, _ = await _identity(database, "leases")
    platform = PlatformService(database)
    await platform.replace_policy(
        TenantContext.service(ServiceIdentity.PLATFORM, actor.organization_id),
        organization_id=actor.organization_id,
        policy=QuotaPolicyValues(max_activity_deadline_seconds=7_200),
    )
    jobs = JobControlPlane(database, _rl_admission())
    long_run = await jobs.submit(
        actor,
        RunSubmission(
            project_id=project.id,
            kind=RunKind.TRAIN_PHON_RL,
            spec=_rl_spec(deadline=7_200),
        ),
        idempotency_key="long-rl",
    )
    with pytest.raises(QuotaExceededError):
        await jobs.submit(
            actor,
            RunSubmission(
                project_id=project.id,
                kind=RunKind.TRAIN_PHON_RL,
                spec=_rl_spec(deadline=7_200),
            ),
            idempotency_key="second-long-rl",
        )
    with pytest.raises(QuotaExceededError):
        await platform.replace_policy(
            TenantContext.service(ServiceIdentity.PLATFORM, actor.organization_id),
            organization_id=actor.organization_id,
            policy=QuotaPolicyValues(),
        )
    async with database.session() as session:
        reservation = await session.scalar(
            select(QuotaReservation).where(QuotaReservation.run_id == long_run.run.id)
        )
        assert reservation is not None
        assert reservation.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(hours=2)
        reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    store = DurableRunStore(database)
    assert await store.begin_execution(_reference(long_run.run)) is True
    cutoff = datetime.now(UTC)
    assert await QuotaManager.expire_stale(database, now=cutoff) == 0
    async with database.session() as session:
        renewed = await session.scalar(
            select(QuotaReservation).where(QuotaReservation.run_id == long_run.run.id)
        )
        assert renewed is not None
        assert renewed.expires_at.replace(tzinfo=UTC) > cutoff + timedelta(hours=2)

    queued = await jobs.submit(
        actor,
        RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={}),
        idempotency_key="abandoned",
    )
    async with database.session() as session:
        await session.execute(
            update(QuotaReservation)
            .where(QuotaReservation.run_id == queued.run.id)
            .values(expires_at=cutoff - timedelta(seconds=1))
        )
    assert await QuotaManager.expire_stale(database, now=cutoff) == 1
    assert (await jobs.get(actor, queued.run.id)).state is RunState.FAILED
    assert await store.begin_execution(_reference(queued.run)) is False
    async with database.session() as session:
        reservation = await session.scalar(
            select(QuotaReservation).where(QuotaReservation.run_id == queued.run.id)
        )
        assert reservation is not None
        assert reservation.state is QuotaReservationState.EXPIRED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_artifact_and_corpus_quotas_are_atomic_and_audited(database: Database) -> None:
    actor, _, user_id = await _identity(database, "storage")
    platform = PlatformService(database)
    policy = QuotaPolicyValues(
        max_artifact_bytes=8,
        max_artifact_count=1,
        max_corpus_sentences=2,
    )
    await platform.replace_policy(
        TenantContext.service(ServiceIdentity.PLATFORM, actor.organization_id),
        organization_id=actor.organization_id,
        policy=policy,
    )
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        artifact_max_bytes=1_024,
        artifact_retention_days=30,
        _env_file=None,
    )
    workspace = ProjectWorkspaceService(database, settings)
    workspace_actor = WorkspaceActor(actor.subject, actor.organization_id, "request-storage")
    project = await workspace.create_project(
        workspace_actor,
        ProjectInput(name="Quota workspace", description=""),
    )
    creation = await workspace.create_manual_corpus(
        workspace_actor,
        project.id,
        ManualCorpusInput(name="seed", language="en-us", sentences=("one", "two")),
    )
    assert creation.version.sentence_count == 2
    with pytest.raises(QuotaExceededError):
        await workspace.create_manual_corpus(
            workspace_actor,
            project.id,
            ManualCorpusInput(name="overflow", language="en-us", sentences=("three",)),
        )

    objects = InMemoryObjectStore()
    artifacts = ArtifactService(database, objects, settings)
    artifact_actor = ArtifactActor(actor.subject, actor.organization_id, "request-storage")
    content = b"12345678"
    created = await artifacts.create(
        artifact_actor,
        project_id=project.id,
        run_id=None,
        kind=ArtifactKind.CORPUS_TEXT,
        content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain",
        filename="seed.txt",
    )
    replay = await artifacts.create(
        artifact_actor,
        project_id=project.id,
        run_id=None,
        kind=ArtifactKind.CORPUS_TEXT,
        content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain",
        filename="seed.txt",
    )
    assert replay.created is False
    with pytest.raises(QuotaExceededError):
        await artifacts.create(
            artifact_actor,
            project_id=project.id,
            run_id=None,
            kind=ArtifactKind.CORPUS_TEXT,
            content=b"x",
            expected_sha256=hashlib.sha256(b"x").hexdigest(),
            media_type="text/plain",
            filename="other.txt",
        )
    await artifacts.tombstone(
        artifact_actor,
        project_id=project.id,
        artifact_id=created.artifact.id,
    )
    assert (
        await platform.quota(PlatformActor(actor.subject, actor.organization_id))
    ).usage.artifact_count == 1
    report = await artifacts.purge_due(now=datetime.now(UTC) + timedelta(days=31))
    assert (report.deleted, report.failed) == (1, 0)
    quota = await platform.quota(PlatformActor(actor.subject, actor.organization_id))
    assert quota.usage.artifact_count == 0
    assert quota.usage.artifact_bytes == 0
    assert quota.usage.corpus_sentences == 2
    assert user_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_chain_filters_detect_tampering_and_hide_from_non_admin(
    database: Database,
) -> None:
    owner, project, _ = await _identity(database, "audit")
    viewer, _, _ = await _identity(database, "viewer", role=Role.VIEWER)
    jobs = JobControlPlane(database)
    await jobs.submit(
        owner,
        RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={"prompt": "private"}),
        idempotency_key="audit-run",
    )
    platform = PlatformService(database)
    page = await platform.audit_events(
        PlatformActor(owner.subject, owner.organization_id),
        action=AuditAction.RUN_SUBMITTED,
        resource_type=AuditResourceType.RUN,
        limit=10,
    )
    assert len(page.events) == 1
    assert set(page.events[0].metadata) == {"attempt", "kind", "quota_class"}
    assert "prompt" not in repr(page.events[0].metadata)
    with pytest.raises(ResourceNotFoundError):
        await platform.audit_events(
            PlatformActor(viewer.subject, viewer.organization_id),
            limit=10,
        )

    context = TenantContext.user(owner.organization_id, owner.subject)
    async with database.session(context) as session:
        assert await AuditWriter.verify(session, owner.organization_id) is True
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.organization_id == owner.organization_id)
        )
        assert event is not None
        event.details = {"kind": "tampered"}
    async with database.session(context) as session:
        assert await AuditWriter.verify(session, owner.organization_id) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_platform_filter_validation_missing_identity_and_usage_floor(
    database: Database,
) -> None:
    owner, _, _ = await _identity(database, "platform-validation")
    service = PlatformService(database)
    actor = PlatformActor(owner.subject, owner.organization_id)
    aware = datetime.now(UTC)

    with pytest.raises(InvalidRequestError):
        await service.audit_events(actor, limit=0)
    with pytest.raises(InvalidRequestError):
        await service.audit_events(actor, cursor="0")
    with pytest.raises(InvalidRequestError):
        await service.audit_events(
            actor,
            occurred_from=datetime(2026, 1, 1),  # noqa: DTZ001 - intentional invalid input
        )
    with pytest.raises(InvalidRequestError):
        await service.audit_events(
            actor,
            occurred_to=datetime(2026, 1, 1),  # noqa: DTZ001 - intentional invalid input
        )
    with pytest.raises(InvalidRequestError):
        await service.audit_events(
            actor,
            occurred_from=aware + timedelta(seconds=1),
            occurred_to=aware,
        )
    page = await service.audit_events(
        actor,
        cursor="1",
        occurred_from=aware - timedelta(days=1),
        occurred_to=aware + timedelta(days=1),
    )
    assert page.next_cursor is None
    await service.quota(actor)

    with pytest.raises(ResourceNotFoundError):
        await service.quota(PlatformActor("oidc|missing", owner.organization_id))
    with pytest.raises(ResourceNotFoundError):
        await service.replace_policy(
            TenantContext.service(ServiceIdentity.WORKER, owner.organization_id),
            organization_id=owner.organization_id,
            policy=QuotaPolicyValues(),
        )

    async with database.session() as session:
        usage = await session.get(QuotaUsage, owner.organization_id)
        assert usage is not None
        usage.artifact_count = 2
    with pytest.raises(QuotaExceededError):
        await service.replace_policy(
            TenantContext.service(ServiceIdentity.PLATFORM, owner.organization_id),
            organization_id=owner.organization_id,
            policy=QuotaPolicyValues(max_artifact_count=1),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quota_and_audit_defensive_invariants(database: Database) -> None:
    actor, _, user_id = await _identity(database, "defensive")
    context = TenantContext.service(ServiceIdentity.PLATFORM, actor.organization_id)
    async with database.session() as session:
        await QuotaManager.ensure_tenant(session, actor.organization_id)
        assert await AuditWriter.verify(session, uuid4()) is False

    with pytest.raises(ValueError, match="cannot be a user"):
        AuditIdentity.service(ServiceIdentity.USER)
    with pytest.raises(ValueError, match="expiry limit"):
        await QuotaManager.expire_stale(database, limit=0)

    service = PlatformService(database)
    await service.replace_policy(
        context,
        organization_id=actor.organization_id,
        policy=QuotaPolicyValues(
            max_artifact_bytes=1,
            max_artifact_count=1,
            max_checkpoint_bytes=1,
        ),
    )

    async def consume(kind: ArtifactKind, size_bytes: int) -> None:
        async with database.session() as session:
            await QuotaManager.consume_artifact(
                session,
                organization_id=actor.organization_id,
                kind=kind,
                size_bytes=size_bytes,
            )

    with pytest.raises(ValueError, match="positive"):
        await consume(ArtifactKind.CORPUS_TEXT, 0)
    with pytest.raises(QuotaExceededError):
        await consume(ArtifactKind.CHECKPOINT, 2)
    with pytest.raises(QuotaExceededError):
        await consume(ArtifactKind.CORPUS_TEXT, 2)
    await consume(ArtifactKind.CORPUS_TEXT, 1)
    with pytest.raises(QuotaExceededError):
        await consume(ArtifactKind.CORPUS_TEXT, 1)

    with pytest.raises(RuntimeError, match="inconsistent"):
        async with database.session() as session:
            await QuotaManager.release_artifact(
                session,
                organization_id=actor.organization_id,
                size_bytes=2,
            )
    with pytest.raises(ValueError, match="positive"):
        async with database.session() as session:
            await QuotaManager.consume_corpus_sentences(
                session,
                organization_id=actor.organization_id,
                sentence_count=0,
            )
    async with database.session() as session:
        assert (
            await QuotaManager.release_run(
                session,
                organization_id=actor.organization_id,
                run_id=uuid4(),
            )
            is False
        )
        head = await session.get(AuditHead, actor.organization_id)
        assert head is not None
        head.last_hash = "z" * 64
    with pytest.raises(RuntimeError, match="chain head"):
        async with database.session() as session:
            await AuditWriter.append(
                session,
                organization_id=actor.organization_id,
                actor=AuditIdentity.user(user_id),
                action=AuditAction.PROJECT_CREATED,
                resource_type=AuditResourceType.PROJECT,
                resource_id=uuid4(),
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expiry_cancellation_terminal_and_redelivery_branches(database: Database) -> None:
    actor, project, _ = await _identity(database, "expiry-branches")
    jobs = JobControlPlane(database)
    cancelling = await jobs.submit(
        actor,
        RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={}),
        idempotency_key="cancelling",
    )
    await jobs.request_cancellation(actor, cancelling.run.id)
    cutoff = datetime.now(UTC)
    async with database.session() as session:
        reservation = await session.scalar(
            select(QuotaReservation).where(QuotaReservation.run_id == cancelling.run.id)
        )
        assert reservation is not None
        reservation.expires_at = cutoff - timedelta(seconds=1)
    assert await QuotaManager.expire_stale(database, now=cutoff) == 1
    assert (await jobs.get(actor, cancelling.run.id)).state is RunState.CANCELLED

    terminal = await jobs.submit(
        actor,
        RunSubmission(project_id=project.id, kind=RunKind.EVALUATE, spec={}),
        idempotency_key="already-terminal",
    )
    async with database.session() as session:
        persisted = await session.get(Run, terminal.run.id)
        reservation = await session.scalar(
            select(QuotaReservation).where(QuotaReservation.run_id == terminal.run.id)
        )
        assert persisted is not None
        assert reservation is not None
        persisted.state = RunState.FAILED
        persisted.failure_code = "execution_failed"
        reservation.expires_at = cutoff - timedelta(seconds=1)
    assert await QuotaManager.expire_stale(database, now=cutoff) == 1

    async with database.session() as session:
        persisted = await session.get(Run, terminal.run.id)
        assert persisted is not None
        assert (
            await QuotaManager.renew_run(
                session,
                organization_id=actor.organization_id,
                run=persisted,
            )
            is False
        )
        assert (
            await QuotaManager.release_run(
                session,
                organization_id=actor.organization_id,
                run_id=persisted.id,
            )
            is False
        )
        with pytest.raises(RuntimeError, match="inconsistent"):
            await QuotaManager.reserve_run(
                session,
                organization_id=actor.organization_id,
                run=persisted,
            )


def test_tenant_context_rejects_forged_service_and_invalid_scope() -> None:
    organization_id = uuid4()
    assert TenantContext.user(organization_id, "oidc|subject").organization_id == organization_id
    with pytest.raises(TenantContextError):
        TenantContext.user(organization_id, "bad\nsubject")
    with pytest.raises(TenantContextError):
        TenantContext.service(ServiceIdentity.USER, organization_id)
    with pytest.raises(TenantContextError):
        TenantContext.service(ServiceIdentity.WORKER)
    with pytest.raises(TenantContextError):
        TenantContext.service(ServiceIdentity.DISPATCHER, organization_id)
    forged = TenantContext(organization_id, ServiceIdentity.WORKER, "service:platform")
    with pytest.raises(TenantContextError):
        forged.validate()
    unsafe_service = TenantContext(organization_id, ServiceIdentity.WORKER, "bad\nactor")
    with pytest.raises(TenantContextError):
        unsafe_service.validate()
    missing_user_scope = TenantContext(None, ServiceIdentity.USER, "oidc|subject")
    with pytest.raises(TenantContextError):
        missing_user_scope.validate()
