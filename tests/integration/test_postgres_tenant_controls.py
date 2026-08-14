"""Real PostgreSQL acceptance for forced RLS, role separation, and quota races."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ArtifactKind,
    ArtifactState,
    DeterminismClass,
    artifact_storage_key,
)
from corpuskit.domain.errors import QuotaExceededError, ResourceNotFoundError
from corpuskit.domain.jobs import RunKind, RunState, normalize_run_spec
from corpuskit.domain.platform import AuditAction, AuditResourceType
from corpuskit.domain.workspaces import (
    ManualCorpusInput,
    ManualCorpusVersionInput,
    ProjectDeletionInput,
    ProjectLifecycle,
)
from corpuskit.persistence.artifact_store import InMemoryObjectStore
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
    AuditEvent,
    Corpus,
    CorpusVersion,
    DatgIndexPublicationRecord,
    Membership,
    Organization,
    OutboxMessage,
    OutboxState,
    Project,
    Role,
    Run,
    RunEvent,
    RunExecutionFact,
    RunReplay,
    Sentence,
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext, TenantContextError
from corpuskit.services.jobs import (
    DispatchMessage,
    JobActor,
    JobControlPlane,
    RunSubmission,
    TransactionalOutbox,
)
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager
from corpuskit.services.project_deletion import ProjectDeletionMaintenance
from corpuskit.services.project_workspaces import ProjectWorkspaceService, WorkspaceActor

OWNER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_OWNER_URL")
APP_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_APP_URL")
DISPATCHER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_DISPATCHER_URL")
WORKER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_WORKER_URL")
ADOPTION_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_ADOPTION_URL")
MAINTENANCE_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_MAINTENANCE_URL")
PLATFORM_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_PLATFORM_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(
            (
                OWNER_URL,
                APP_URL,
                DISPATCHER_URL,
                WORKER_URL,
                ADOPTION_URL,
                MAINTENANCE_URL,
                PLATFORM_URL,
            )
        ),
        reason="separate PostgreSQL owner and service roles are not configured",
    ),
]

_PROTECTED_TABLES = (
    "organizations",
    "users",
    "memberships",
    "projects",
    "corpora",
    "corpus_versions",
    "sentences",
    "runs",
    "run_events",
    "outbox_messages",
    "artifacts",
    "api_rate_limit_windows",
    "datg_index_publications",
    "run_execution_facts",
    "run_replays",
    "quota_policies",
    "quota_usages",
    "quota_reservations",
    "audit_heads",
    "audit_events",
)


@dataclass(frozen=True, slots=True)
class SeededTenant:
    organization_id: UUID
    subject: str
    user_id: UUID
    project_id: UUID
    run_id: UUID


@pytest.mark.asyncio
async def test_concurrent_corpus_versions_serialize_parent_lineage() -> None:
    assert APP_URL is not None
    tenant = await _seed_tenant("corpus-version-race", full_graph=False)
    database = Database(APP_URL)
    service = ProjectWorkspaceService(
        database,
        Settings(environment="test", database_url=APP_URL, _env_file=None),
    )
    actor = WorkspaceActor(tenant.subject, tenant.organization_id, "pg-version-race")
    try:
        creation = await service.create_manual_corpus(
            actor,
            tenant.project_id,
            ManualCorpusInput(name="Concurrent corpus", sentences=("Initial",)),
        )
        await asyncio.gather(
            service.create_manual_version(
                actor,
                tenant.project_id,
                creation.corpus.id,
                ManualCorpusVersionInput(sentences=("Second candidate",)),
            ),
            service.create_manual_version(
                actor,
                tenant.project_id,
                creation.corpus.id,
                ManualCorpusVersionInput(sentences=("Third candidate",)),
            ),
        )

        versions = await service.list_versions(actor, tenant.project_id, creation.corpus.id)
        assert [version.version_number for version in versions] == [1, 2, 3]
        assert versions[1].parent_version_id == versions[0].id
        assert versions[2].parent_version_id == versions[1].id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_project_deletion_is_isolated_idempotent_and_maintenance_only() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    assert MAINTENANCE_URL is not None
    tenant, intruder = await asyncio.gather(
        _seed_tenant("project-delete", full_graph=False),
        _seed_tenant("project-delete-intruder", full_graph=False),
    )
    api = Database(APP_URL)
    maintenance = Database(MAINTENANCE_URL)
    owner = Database(OWNER_URL)
    settings = Settings(
        environment="test",
        database_url=APP_URL,
        artifact_retention_days=30,
        _env_file=None,
    )
    service = ProjectWorkspaceService(api, settings)
    actor = WorkspaceActor(tenant.subject, tenant.organization_id, "pg-project-delete")
    confirmation = ProjectDeletionInput(confirmation="DELETE Project project-delete")
    try:
        first, second = await asyncio.gather(
            service.request_project_deletion(actor, tenant.project_id, confirmation),
            service.request_project_deletion(actor, tenant.project_id, confirmation),
        )
        assert first == second
        assert first.state is ProjectLifecycle.DELETION_PENDING
        assert tenant.project_id not in {item.id for item in await service.list_projects(actor)}

        with pytest.raises(ResourceNotFoundError):
            await service.request_project_deletion(
                WorkspaceActor(intruder.subject, intruder.organization_id),
                tenant.project_id,
                confirmation,
            )

        async with owner.session(
            TenantContext.service(ServiceIdentity.PLATFORM, tenant.organization_id)
        ) as session:
            events = await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.organization_id == tenant.organization_id,
                    AuditEvent.action == AuditAction.PROJECT_DELETION_REQUESTED,
                    AuditEvent.resource_id == tenant.project_id,
                )
            )
            assert events == 1

        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Project).where(Project.id == tenant.project_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Project)
                    .where(Project.id == intruder.project_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT has_table_privilege(current_user, 'projects', 'UPDATE')")
                )
                is False
            )

        purge = await ProjectDeletionMaintenance(
            maintenance,
            InMemoryObjectStore(),
        ).purge_due(now=first.retention_until)
        assert purge.deleted >= 1
        async with owner.session(
            TenantContext.service(ServiceIdentity.PLATFORM, tenant.organization_id)
        ) as session:
            assert await session.get(Project, tenant.project_id) is None
            assert await AuditWriter.verify(session, tenant.organization_id) is True
    finally:
        await api.dispose()
        await maintenance.dispose()
        await owner.dispose()


async def _seed_tenant(label: str, *, full_graph: bool = True) -> SeededTenant:
    assert OWNER_URL is not None
    database = Database(OWNER_URL)
    organization_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    run_id = uuid4()
    subject = f"oidc|postgres-{label}-{uuid4()}"
    context = TenantContext.service(ServiceIdentity.PLATFORM, organization_id)
    try:
        async with database.session(context) as session:
            organization = Organization(
                id=organization_id,
                slug=f"pg-{label}-{organization_id.hex[:10]}",
                name=f"PG {label}",
            )
            user = User(id=user_id, oidc_subject=subject, display_name=label)
            session.add_all((organization, user))
            await session.flush()
            session.add(
                Membership(
                    organization_id=organization_id,
                    user_id=user_id,
                    role=Role.OWNER,
                )
            )
            project = Project(
                id=project_id,
                organization_id=organization_id,
                created_by=user_id,
                name=f"Project {label}",
                description="",
            )
            session.add(project)
            await QuotaManager.ensure_tenant(session, organization_id)
            if not full_graph:
                return SeededTenant(organization_id, subject, user_id, project_id, run_id)

            corpus = Corpus(
                organization_id=organization_id,
                project_id=project_id,
                created_by=user_id,
                name="Corpus",
            )
            session.add(corpus)
            await session.flush()
            version = CorpusVersion(
                organization_id=organization_id,
                corpus_id=corpus.id,
                created_by=user_id,
                version_number=1,
                language="en-us",
                sentence_count=1,
                content_sha256="a" * 64,
                corpusgen_version="0.1.7",
            )
            session.add(version)
            await session.flush()
            session.add(
                Sentence(
                    organization_id=organization_id,
                    corpus_version_id=version.id,
                    ordinal=0,
                    original_text="hello",
                    normalized_text="hello",
                )
            )
            normalized, spec_sha = normalize_run_spec({"runtime_id": "tiny-datg"})
            run = Run(
                id=run_id,
                organization_id=organization_id,
                project_id=project_id,
                corpus_version_id=version.id,
                created_by=user_id,
                kind=RunKind.BUILD_DATG_INDEX,
                state=RunState.RUNNING,
                idempotency_key=f"seed-{run_id}",
                attempt=1,
                event_sequence=1,
                spec=normalized,
                spec_sha256=spec_sha,
            )
            session.add(run)
            await session.flush()
            session.add(
                RunEvent(
                    organization_id=organization_id,
                    run_id=run_id,
                    sequence=1,
                    event_type="run.started",
                    payload={"state": "running"},
                )
            )
            session.add(
                OutboxMessage(
                    organization_id=organization_id,
                    run_id=run_id,
                    event_type="run.dispatch",
                    payload={"run_id": str(run_id)},
                    state=OutboxState.PENDING,
                    attempts=0,
                )
            )
            await QuotaManager.reserve_run(session, organization_id=organization_id, run=run)
            artifact_sha = "b" * 64
            artifact = Artifact(
                organization_id=organization_id,
                project_id=project_id,
                run_id=run_id,
                created_by=user_id,
                scope_key=str(run_id),
                kind=ArtifactKind.RUN_MANIFEST.value,
                sha256=artifact_sha,
                size_bytes=1,
                storage_key=artifact_storage_key(
                    organization_id=organization_id,
                    project_id=project_id,
                    run_id=run_id,
                    kind=ArtifactKind.RUN_MANIFEST,
                    sha256=artifact_sha,
                ),
                media_type="application/json",
                filename="result.json",
                state=ArtifactState.ACTIVE,
                retention_until=datetime.now(UTC) + timedelta(days=30),
            )
            session.add(artifact)
            await QuotaManager.consume_artifact(
                session,
                organization_id=organization_id,
                kind=ArtifactKind.RUN_MANIFEST,
                size_bytes=1,
            )
            session.add(
                RunExecutionFact(
                    run_id=run_id,
                    organization_id=organization_id,
                    project_id=project_id,
                    facts={"schema_version": "rls-seed"},
                    facts_sha256="c" * 64,
                    input_digests=[],
                    manifest_artifact_id=artifact.id,
                    manifest_sha256=artifact_sha,
                    finalized_at=datetime.now(UTC),
                )
            )
            session.add(
                RunReplay(
                    replay_run_id=run_id,
                    organization_id=organization_id,
                    project_id=project_id,
                    source_run_id=run_id,
                    source_manifest_artifact_id=artifact.id,
                    expected_manifest_sha256=artifact_sha,
                    classification=DeterminismClass.EXACT,
                    created_by=user_id,
                )
            )
            await QuotaManager.consume_corpus_sentences(
                session,
                organization_id=organization_id,
                sentence_count=1,
            )
            await AuditWriter.append(
                session,
                organization_id=organization_id,
                actor=AuditIdentity.user(user_id),
                action=AuditAction.RUN_SUBMITTED,
                resource_type=AuditResourceType.RUN,
                resource_id=run_id,
                metadata={"attempt": 1, "kind": "build-datg-index", "quota_class": "cpu"},
            )
        assert ADOPTION_URL is not None
        adoption = Database(ADOPTION_URL)
        try:
            async with adoption.session(
                TenantContext.service(ServiceIdentity.ADOPTION, organization_id)
            ) as session:
                session.add(
                    DatgIndexPublicationRecord(
                        organization_id=organization_id,
                        project_id=project_id,
                        build_run_id=run_id,
                        created_by=user_id,
                        cache_key_sha256="d" * 64,
                        content_sha256="e" * 64,
                        runtime_id="tiny-datg",
                        language="en-us",
                        unit="phoneme",
                        vocabulary_size=1,
                        indexed_token_count=1,
                        size_bytes=1,
                    )
                )
        finally:
            await adoption.dispose()
        return SeededTenant(organization_id, subject, user_id, project_id, run_id)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_nonowner_app_role_is_forced_through_rls_on_every_resource_table() -> None:
    assert APP_URL is not None
    first, second = await asyncio.gather(_seed_tenant("first"), _seed_tenant("second"))
    database = Database(APP_URL)
    try:
        context = TenantContext.user(first.organization_id, first.subject)
        async with database.session(context) as session:
            role = (
                await session.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = session_user")
                )
            ).one()
            assert role._tuple() == (False, False)
            groups = (
                await session.execute(
                    text(
                        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                        "rolreplication, rolbypassrls FROM pg_roles "
                        "WHERE rolname IN ('corpuskit_api', 'corpuskit_dispatcher', "
                        "'corpuskit_worker', 'corpuskit_adoption', "
                        "'corpuskit_maintenance', 'corpuskit_platform')"
                    )
                )
            ).all()
            assert len(groups) == 6
            assert all(not any(row[1:]) for row in groups)
            owned = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM pg_class "
                    "WHERE relname = ANY(:tables) "
                    "AND relowner = (SELECT oid FROM pg_roles WHERE rolname = session_user)"
                ),
                {"tables": list(_PROTECTED_TABLES)},
            )
            assert owned == 0
            flags = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = ANY(:tables)"
                    ),
                    {"tables": list(_PROTECTED_TABLES)},
                )
            ).all()
            assert {row[0] for row in flags} == set(_PROTECTED_TABLES)
            assert all(row[1] and row[2] for row in flags)
            for table in _PROTECTED_TABLES:
                count = await session.scalar(
                    text(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
                )
                expected = 0 if table == "api_rate_limit_windows" else 1
                assert count == expected, table

        other_context = TenantContext.user(second.organization_id, second.subject)
        async with database.session(other_context) as session:
            assert await session.scalar(select(func.count()).select_from(Project)) == 1
            assert (
                await session.scalar(
                    select(func.count()).select_from(Project).where(Project.id == first.project_id)
                )
                == 0
            )

        async with database.engine.begin() as connection:
            assert await connection.scalar(text("SELECT COUNT(*) FROM projects")) == 0
            await connection.execute(
                text(
                    "SELECT set_config('corpuskit.organization_id', :organization_id, true), "
                    "set_config('corpuskit.identity', 'worker', true), "
                    "set_config('corpuskit.actor_id', 'service:worker', true)"
                ),
                {"organization_id": str(second.organization_id)},
            )
            assert await connection.scalar(text("SELECT COUNT(*) FROM projects")) == 0

        with pytest.raises(TenantContextError):
            async with database.session():
                pass
        with pytest.raises(TenantContextError):
            TenantContext.service(ServiceIdentity.WORKER).validate()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pooled_connection_context_does_not_bleed_between_tenants() -> None:
    assert APP_URL is not None
    first, second = await asyncio.gather(
        _seed_tenant("pool-first", full_graph=False),
        _seed_tenant("pool-second", full_graph=False),
    )
    database = Database(APP_URL)
    try:
        async with database.session(
            TenantContext.user(first.organization_id, first.subject)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(Project)) == 1
        async with database.session(
            TenantContext.user(second.organization_id, second.subject)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(Project)) == 1
            assert (
                await session.scalar(
                    select(func.count()).select_from(Project).where(Project.id == first.project_id)
                )
                == 0
            )
        async with database.engine.connect() as connection:
            values = (
                await connection.execute(
                    text(
                        "SELECT current_setting('corpuskit.organization_id', true), "
                        "current_setting('corpuskit.identity', true), "
                        "current_setting('corpuskit.actor_id', true)"
                    )
                )
            ).one()
            assert values._tuple() in {(None, None, None), ("", "", "")}
            assert await connection.scalar(text("SELECT COUNT(*) FROM projects")) == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_n_plus_one_quota_and_cross_tenant_independence() -> None:
    assert APP_URL is not None
    first, second = await asyncio.gather(
        _seed_tenant("quota-first", full_graph=False),
        _seed_tenant("quota-second", full_graph=False),
    )
    database = Database(APP_URL)
    jobs = JobControlPlane(database)

    async def submit(tenant: SeededTenant, index: int) -> bool:
        try:
            result = await jobs.submit(
                JobActor(tenant.subject, tenant.organization_id),
                RunSubmission(
                    project_id=tenant.project_id,
                    kind=RunKind.EVALUATE,
                    spec={"index": index},
                ),
                idempotency_key=f"quota-{tenant.organization_id}-{index}",
            )
        except QuotaExceededError:
            return False
        return result.created

    try:
        first_results = await asyncio.gather(*(submit(first, index) for index in range(4)))
        second_results = await asyncio.gather(*(submit(second, index) for index in range(3)))
        assert sum(first_results) == 3
        assert sum(second_results) == 3
        async with database.session(
            TenantContext.user(first.organization_id, first.subject)
        ) as session:
            snapshot = await QuotaManager.snapshot(session, first.organization_id)
            assert snapshot.usage.active_cpu_jobs == 3
            assert await session.scalar(select(func.count()).select_from(Run)) == 3
    finally:
        await database.dispose()


class _Dispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[DispatchMessage] = []

    async def publish(self, message: DispatchMessage) -> None:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("provider details must not persist")


@pytest.mark.asyncio
async def test_dispatcher_global_role_can_claim_and_update_but_api_forgery_cannot() -> None:
    assert APP_URL is not None
    assert DISPATCHER_URL is not None
    first, second = await asyncio.gather(_seed_tenant("dispatch-a"), _seed_tenant("dispatch-b"))
    app_database = Database(APP_URL)
    dispatcher_database = Database(DISPATCHER_URL)
    try:
        async with app_database.engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT set_config('corpuskit.identity', 'dispatcher', true), "
                    "set_config('corpuskit.organization_id', '', true), "
                    "set_config('corpuskit.actor_id', 'service:dispatcher', true)"
                )
            )
            assert await connection.scalar(text("SELECT COUNT(*) FROM outbox_messages")) == 0

        outbox = TransactionalOutbox(dispatcher_database)
        success = _Dispatcher()
        result = await outbox.dispatch_batch(success, worker_id="dispatcher", limit=1)
        assert (result.claimed, result.published, result.failed) == (1, 1, 0)
        failure = _Dispatcher(fail=True)
        failed = await outbox.dispatch_batch(failure, worker_id="dispatcher", limit=100)
        assert failed.claimed >= 1
        assert failed.failed >= 1
        assert {
            first.organization_id,
            second.organization_id,
        } <= {message.organization_id for message in success.messages + failure.messages}
    finally:
        await app_database.dispose()
        await dispatcher_database.dispose()


@pytest.mark.asyncio
async def test_service_database_roles_are_narrow_and_context_identity_cannot_be_swapped() -> None:
    assert WORKER_URL is not None
    assert ADOPTION_URL is not None
    assert MAINTENANCE_URL is not None
    assert PLATFORM_URL is not None
    first, second = await asyncio.gather(_seed_tenant("service-a"), _seed_tenant("service-b"))
    worker = Database(WORKER_URL)
    adoption = Database(ADOPTION_URL)
    maintenance = Database(MAINTENANCE_URL)
    platform = Database(PLATFORM_URL)
    try:
        async with worker.session(
            TenantContext.service(ServiceIdentity.WORKER, first.organization_id)
        ) as session:
            assert (
                await session.scalar(
                    text("SELECT has_table_privilege(current_user, 'projects', 'UPDATE')")
                )
                is False
            )
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert (
                await session.scalar(
                    select(func.count()).select_from(Run).where(Run.id == second.run_id)
                )
                == 0
            )
        async with worker.session(
            TenantContext.service(ServiceIdentity.ADOPTION, first.organization_id)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
        async with adoption.session(
            TenantContext.service(ServiceIdentity.ADOPTION, first.organization_id)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 1

        async def insert_cross_tenant_publication() -> None:
            async with adoption.session(
                TenantContext.service(ServiceIdentity.ADOPTION, first.organization_id)
            ) as session:
                session.add(
                    DatgIndexPublicationRecord(
                        organization_id=first.organization_id,
                        project_id=first.project_id,
                        build_run_id=second.run_id,
                        created_by=first.user_id,
                        cache_key_sha256="1" * 64,
                        content_sha256="2" * 64,
                        runtime_id="tiny-datg",
                        language="en-us",
                        unit="phoneme",
                        vocabulary_size=1,
                        indexed_token_count=1,
                        size_bytes=1,
                    )
                )
                await session.flush()

        with pytest.raises(DBAPIError):
            await insert_cross_tenant_publication()
        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) >= 2
        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Project)
                    .where(Project.id.in_((first.project_id, second.project_id)))
                )
                == 0
            )
        async with platform.session(
            TenantContext.service(ServiceIdentity.PLATFORM, first.organization_id)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(Project)) == 1
    finally:
        await worker.dispose()
        await adoption.dispose()
        await maintenance.dispose()
        await platform.dispose()


@pytest.mark.asyncio
async def test_audit_rows_are_cross_tenant_hidden_and_immutable_even_to_owner() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    first, second = await asyncio.gather(_seed_tenant("audit-a"), _seed_tenant("audit-b"))
    app_database = Database(APP_URL)
    owner_database = Database(OWNER_URL)
    try:
        async with app_database.session(
            TenantContext.user(first.organization_id, first.subject)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.organization_id == second.organization_id)
                )
                == 0
            )
        with pytest.raises(DBAPIError):
            async with app_database.session(
                TenantContext.user(first.organization_id, first.subject)
            ) as session:
                await session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.organization_id == first.organization_id)
                    .values(actor_id="forged")
                )

        owner_context = TenantContext.service(ServiceIdentity.PLATFORM, first.organization_id)
        with pytest.raises(DBAPIError):
            async with owner_database.session(owner_context) as session:
                await session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.organization_id == first.organization_id)
                    .values(actor_id="forged")
                )

        async def delete_audit_event() -> None:
            async with owner_database.session(owner_context) as session:
                event = await session.scalar(
                    select(AuditEvent).where(AuditEvent.organization_id == first.organization_id)
                )
                if event is None:
                    raise AssertionError("seeded audit event is missing")
                await session.delete(event)
                await session.flush()

        with pytest.raises(DBAPIError):
            await delete_audit_event()
    finally:
        await app_database.dispose()
        await owner_database.dispose()
