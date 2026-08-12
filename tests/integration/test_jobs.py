"""Durable job idempotency, tenancy, lifecycle, and outbox integration contracts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from corpuskit.api.jobs import RunResponse
from corpuskit.config import Settings
from corpuskit.domain.corpora import CorpusImportLimits, CorpusImportRequest, prepare_corpus
from corpuskit.domain.errors import (
    InvalidRequestError,
    InvalidStateTransitionError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.jobs import (
    RunKind,
    RunState,
    normalize_result_summary,
    normalize_run_spec,
)
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedModelSelection,
    HostedPromptTemplatePolicy,
    SecretReference,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    AuditEvent,
    Corpus,
    CorpusVersion,
    Membership,
    Organization,
    OutboxMessage,
    OutboxState,
    Project,
    Role,
    Run,
    RunEvent,
    Sentence,
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.jobs import (
    DispatchMessage,
    JobActor,
    JobControlPlane,
    RunSubmission,
    TransactionalOutbox,
)
from corpuskit.services.platform import QuotaManager
from corpuskit.services.run_admission import ConfiguredRunAdmission


@pytest_asyncio.fixture
async def job_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    await database.create_schema()
    yield database
    await database.drop_schema()
    await database.dispose()


async def _identity(
    database: Database, suffix: str, role: Role = Role.OWNER
) -> tuple[JobActor, Project]:
    organization_id = uuid4()
    context = (
        TenantContext.service(ServiceIdentity.PLATFORM, organization_id)
        if database.engine.dialect.name == "postgresql"
        else None
    )
    async with database.session(context) as session:
        organization = Organization(
            id=organization_id,
            slug=f"org-{suffix}",
            name=f"Org {suffix}",
        )
        user = User(oidc_subject=f"oidc|{suffix}", display_name=suffix)
        session.add_all([organization, user])
        await session.flush()
        session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
        project = Project(
            organization_id=organization.id,
            created_by=user.id,
            name="Jobs",
            description="",
        )
        session.add(project)
        if database.engine.dialect.name == "postgresql":
            await QuotaManager.ensure_tenant(session, organization.id)
        await session.flush()
        return JobActor(user.oidc_subject, organization.id), project


def _submission(project: Project, **spec: object) -> RunSubmission:
    return RunSubmission(
        project_id=project.id,
        kind=RunKind.EVALUATE,
        spec={"language": "en-us", "source_ref": "corpus:v1", **spec},
    )


async def _corpus_version(
    database: Database,
    actor: JobActor,
    project: Project,
    *,
    language: str,
    sentences: tuple[str, ...],
) -> CorpusVersion:
    prepared = prepare_corpus(
        CorpusImportRequest(language=language, sentences=sentences),
        CorpusImportLimits(max_sentences=10_000, max_sentence_characters=2_000),
    )
    async with database.session() as session:
        user_id = await session.scalar(select(User.id).where(User.oidc_subject == actor.subject))
        assert user_id is not None
        corpus = Corpus(
            organization_id=actor.organization_id,
            project_id=project.id,
            created_by=user_id,
            name="Lineage corpus",
        )
        session.add(corpus)
        await session.flush()
        version = CorpusVersion(
            organization_id=actor.organization_id,
            corpus_id=corpus.id,
            created_by=user_id,
            version_number=1,
            language=prepared.language,
            sentence_count=len(prepared.sentences),
            content_sha256=prepared.content_sha256,
            corpusgen_version="0.1.7",
        )
        session.add(version)
        await session.flush()
        session.add_all(
            Sentence(
                organization_id=actor.organization_id,
                corpus_version_id=version.id,
                ordinal=item.ordinal,
                original_text=item.original_text,
                normalized_text=item.normalized_text,
            )
            for item in prepared.sentences
        )
        await session.flush()
        return version


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submission_is_idempotent_and_conflicting_reuse_is_rejected(
    job_database: Database,
) -> None:
    actor, project = await _identity(job_database, "idempotent")
    service = JobControlPlane(job_database)

    first = await service.submit(
        actor, _submission(project, options={"b": 2, "a": 1}), idempotency_key="job-1"
    )
    replay = await service.submit(
        actor, _submission(project, options={"a": 1, "b": 2}), idempotency_key="job-1"
    )

    assert first.created is True
    assert replay.created is False
    assert replay.run.id == first.run.id
    assert replay.run.state is RunState.QUEUED
    assert replay.run.outbox_state is OutboxState.PENDING
    with pytest.raises(ResourceConflictError):
        await service.submit(actor, _submission(project, changed=True), idempotency_key="job-1")
    with pytest.raises(InvalidRequestError):
        await service.submit(
            actor, _submission(project, api_key="must-not-persist"), idempotency_key="job-2"
        )
    with pytest.raises(InvalidRequestError):
        await service.submit(actor, _submission(project), idempotency_key="")

    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(RunEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1
        payload = await session.scalar(select(OutboxMessage.payload))
        assert payload is not None
        assert "source_ref" not in str(payload)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corpus_version_lineage_requires_identical_typed_inline_content(
    job_database: Database,
) -> None:
    actor, project = await _identity(job_database, "corpus-lineage")
    version = await _corpus_version(
        job_database,
        actor,
        project,
        language="en-us",
        sentences=("  Hello  ", "World", "World"),
    )
    service = JobControlPlane(job_database)
    target = {"mode": "derived", "phonemes": []}
    submissions = (
        RunSubmission(
            project_id=project.id,
            corpus_version_id=version.id,
            kind=RunKind.PHONEMIZE,
            spec={"texts": ["Hello", "World"], "language": "en-us"},
        ),
        RunSubmission(
            project_id=project.id,
            corpus_version_id=version.id,
            kind=RunKind.EVALUATE,
            spec={
                "sentences": ["Hello", "World"],
                "language": "en-us",
                "unit": "phoneme",
                "target": target,
            },
        ),
        RunSubmission(
            project_id=project.id,
            corpus_version_id=version.id,
            kind=RunKind.SELECT,
            spec={
                "candidates": ["Hello", "World"],
                "language": "en-us",
                "unit": "phoneme",
                "target": target,
            },
        ),
    )
    for index, submission in enumerate(submissions):
        result = await service.submit(
            actor,
            submission,
            idempotency_key=f"matching-lineage-{index}",
        )
        assert result.run.corpus_version_id == version.id

    for key, submission in (
        (
            "different-order",
            RunSubmission(
                project_id=project.id,
                corpus_version_id=version.id,
                kind=RunKind.EVALUATE,
                spec={
                    "sentences": ["World", "Hello"],
                    "language": "en-us",
                    "unit": "phoneme",
                    "target": target,
                },
            ),
        ),
        (
            "different-language",
            RunSubmission(
                project_id=project.id,
                corpus_version_id=version.id,
                kind=RunKind.PHONEMIZE,
                spec={"texts": ["Hello", "World"], "language": "en-gb"},
            ),
        ),
        (
            "unsupported-kind",
            RunSubmission(
                project_id=project.id,
                corpus_version_id=version.id,
                kind=RunKind.DISTRIBUTION,
                spec={"counts": [], "target_units": []},
            ),
        ),
    ):
        with pytest.raises(InvalidRequestError) as rejected:
            await service.submit(actor, submission, idempotency_key=key)
        assert rejected.value.operation == "run.corpus_lineage"

    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unimplemented_export_run_is_rejected_before_persistence(
    job_database: Database,
) -> None:
    actor, project = await _identity(job_database, "unsupported-export")
    service = JobControlPlane(job_database)

    with pytest.raises(InvalidRequestError) as caught:
        await service.submit(
            actor,
            RunSubmission(project_id=project.id, kind=RunKind.EXPORT, spec={}),
            idempotency_key="unsupported-export",
        )

    assert caught.value.operation == "run.kind.unsupported"
    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_advanced_admission_rejects_before_queue_and_preserves_idempotent_replay(
    job_database: Database,
) -> None:
    actor, project = await _identity(job_database, "advanced-admission")
    settings = Settings(
        environment="test",
        worker_hosted_model_policies=(
            HostedModelPolicy(
                provider="openai",
                model="openai/demo-model",
                connection_id="demo-provider",
                credential_ref=SecretReference(reference="secret://environment/provider-key"),
                input_cost_per_million_usd=Decimal("1"),
                output_cost_per_million_usd=Decimal("2"),
                max_output_tokens_per_request=128,
                prompt_templates=(
                    HostedPromptTemplatePolicy(
                        template_id="coverage-v1",
                        template_ref=SecretReference(
                            reference="secret://environment/prompt-template"
                        ),
                        sha256="b" * 64,
                        size_bytes=42,
                        max_rendered_bytes=512,
                    ),
                ),
            ),
        ),
    )
    configured = ConfiguredRunAdmission.from_settings(settings)

    class RevocableAdmission:
        enabled = True

        def validate(self, kind: RunKind, spec: Mapping[str, Any]) -> None:
            if not self.enabled:
                raise InvalidRequestError("run.advanced.allowlist")
            configured.validate(kind, spec)

    admission = RevocableAdmission()
    service = JobControlPlane(job_database, admission)
    request = HostedGenerationRequest(
        selection=HostedModelSelection(
            provider="openai",
            model="openai/demo-model",
            connection_id="demo-provider",
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=2,
        ),
        max_tokens_per_request=64,
        prompt_template_id="coverage-v1",
        external_processing_confirmed=True,
        activity_timeout_seconds=3,
    )
    submission = RunSubmission(
        project_id=project.id,
        kind=RunKind.GENERATE_LLM,
        spec=request.model_dump(mode="json"),
    )

    with pytest.raises(InvalidRequestError) as malformed:
        await service.submit(
            actor,
            RunSubmission(
                project_id=project.id,
                kind=RunKind.GENERATE_LLM,
                spec={"selection": {"provider": "unknown"}},
            ),
            idempotency_key="advanced-invalid",
        )
    assert malformed.value.operation == "run.submit"

    unauthorized_spec = request.model_dump(mode="json")
    unauthorized_spec["selection"] = {
        "provider": "anthropic",
        "model": "anthropic/demo-model",
        "connection_id": "unconfigured-provider",
    }
    with pytest.raises(InvalidRequestError) as unauthorized:
        await service.submit(
            actor,
            RunSubmission(
                project_id=project.id,
                kind=RunKind.GENERATE_LLM,
                spec=unauthorized_spec,
            ),
            idempotency_key="advanced-unauthorized",
        )
    assert unauthorized.value.operation == "model_runtime.hosted.allowlist"

    legacy_spec = request.model_dump(mode="json")
    legacy_spec.pop("prompt_template_id")
    legacy_spec["prompt_template"] = "RAW PRIVATE PROMPT"
    with pytest.raises(InvalidRequestError):
        await service.submit(
            actor,
            RunSubmission(
                project_id=project.id,
                kind=RunKind.GENERATE_LLM,
                spec=legacy_spec,
            ),
            idempotency_key="advanced-legacy-prompt",
        )

    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0

    first = await service.submit(actor, submission, idempotency_key="advanced-valid")
    fetched = await service.get(actor, first.run.id)
    listed = await service.list(actor, kind=RunKind.GENERATE_LLM)
    events = await service.events(actor, first.run.id)
    async with job_database.session() as session:
        audit_details = tuple(
            await session.scalars(
                select(AuditEvent.details).where(
                    AuditEvent.organization_id == actor.organization_id
                )
            )
        )
    serialized = json.dumps(
        {
            "created": RunResponse.model_validate(first.run).model_dump(mode="json"),
            "fetched": RunResponse.model_validate(fetched).model_dump(mode="json"),
            "listed": [RunResponse.model_validate(item).model_dump(mode="json") for item in listed],
            "events": [item.payload for item in events],
            "audit": audit_details,
        },
        sort_keys=True,
    )
    assert "coverage-v1" in serialized
    assert "RAW PRIVATE PROMPT" not in serialized
    assert "secret://" not in serialized
    admission.enabled = False
    same = await service.submit(actor, submission, idempotency_key="advanced-valid")
    assert first.created is True
    assert same.created is False
    assert same.run.id == first.run.id
    with pytest.raises(InvalidRequestError):
        await service.submit(actor, submission, idempotency_key="advanced-revoked")

    admission.enabled = True
    async with job_database.session() as session:
        await session.execute(
            update(Run).where(Run.id == first.run.id).values(state=RunState.FAILED)
        )
        assert await QuotaManager.release_run(
            session,
            organization_id=actor.organization_id,
            run_id=first.run.id,
        )
    retry = await service.retry(actor, first.run.id, idempotency_key="advanced-retry")
    admission.enabled = False
    same_retry = await service.retry(actor, first.run.id, idempotency_key="advanced-retry")
    assert retry.created is True
    assert same_retry.created is False
    assert same_retry.run.id == retry.run.id
    with pytest.raises(InvalidRequestError):
        await service.retry(actor, first.run.id, idempotency_key="advanced-retry-revoked")

    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 2
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demo_bootstrap_is_rejected_outside_development_and_test(
    job_database: Database,
) -> None:
    service = JobControlPlane(job_database)
    actor = JobActor("demo-user", UUID("00000000-0000-4000-8000-000000000001"))

    with pytest.raises(RuntimeError, match="development and test"):
        await service.bootstrap_demo(actor, environment="production")
    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demo_bootstrap_fails_atomically_on_conflicting_fixed_rows(
    job_database: Database,
) -> None:
    actor = JobActor("demo-user", UUID("00000000-0000-4000-8000-000000000001"))
    async with job_database.session() as session:
        session.add(
            Organization(
                id=actor.organization_id,
                slug="unexpected-demo",
                name="Unexpected",
            )
        )

    with pytest.raises(RuntimeError, match="conflicts"):
        await JobControlPlane(job_database).bootstrap_demo(actor, environment="development")

    async with job_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 1
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert await session.scalar(select(func.count()).select_from(Project)) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_sqlite_submission_converges_to_one_run(job_database: Database) -> None:
    actor, project = await _identity(job_database, "concurrent")
    service = JobControlPlane(job_database)

    results = await asyncio.gather(
        *[service.submit(actor, _submission(project), idempotency_key="same-key") for _ in range(8)]
    )

    assert len({result.run.id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    filtered = await service.list(
        actor,
        state=RunState.QUEUED,
        kind=RunKind.EVALUATE,
    )
    assert [item.id for item in filtered] == [results[0].run.id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_membership_role_and_project_scope_fail_closed(job_database: Database) -> None:
    viewer, viewer_project = await _identity(job_database, "viewer", Role.VIEWER)
    owner, _ = await _identity(job_database, "scoped-owner")
    service = JobControlPlane(job_database)

    with pytest.raises(ResourceNotFoundError):
        await service.submit(viewer, _submission(viewer_project), idempotency_key="viewer-write")
    with pytest.raises(ResourceNotFoundError):
        await service.submit(owner, _submission(viewer_project), idempotency_key="foreign-project")
    with pytest.raises(ResourceNotFoundError):
        await service.list(JobActor("missing-subject", owner.organization_id))


def test_run_spec_structural_and_size_limits_are_enforced() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(22):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(ValueError, match="structural"):
        normalize_run_spec(nested)
    with pytest.raises(ValueError, match="keys"):
        normalize_run_spec({1: "not-json"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="size"):
        normalize_run_spec({"value": "x" * (257 * 1024)})


@pytest.mark.parametrize(
    "key",
    [
        "provider_api_key",
        "providerApiKey",
        "APIKeyValue",
        "authorization_header",
        "client_secret_value",
        "password_hash",
        "refresh_token",
        "token",
    ],
)
def test_run_specs_and_results_reject_compound_credential_fields(key: str) -> None:
    with pytest.raises(ValueError, match="opaque secret_ref"):
        normalize_run_spec({key: "must-not-persist"})
    with pytest.raises(ValueError, match="must not contain credentials"):
        normalize_result_summary({key: "must-not-persist"})


def test_run_specs_allow_opaque_references_and_benign_token_budgets() -> None:
    value = {
        "credential_ref": {"reference": "provider/team/key"},
        "max_tokens": 100,
        "token_budget": 500,
    }

    normalized, _ = normalize_run_spec(value)

    assert normalized == value


def test_run_specs_and_results_reject_non_json_container_bypasses() -> None:
    hidden = ({"provider_api_key": "must-not-persist"},)

    with pytest.raises(ValueError, match="JSON-compatible"):
        normalize_run_spec({"nested": hidden})
    with pytest.raises(ValueError, match="JSON-compatible"):
        normalize_result_summary({"nested": hidden})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_isolation_cancellation_and_monotonic_event_polling(
    job_database: Database,
) -> None:
    owner, project = await _identity(job_database, "owner")
    intruder, _ = await _identity(job_database, "intruder")
    service = JobControlPlane(job_database)
    submitted = await service.submit(owner, _submission(project), idempotency_key="cancel-me")

    with pytest.raises(ResourceNotFoundError):
        await service.get(intruder, submitted.run.id)
    assert await service.list(intruder) == ()
    with pytest.raises(ResourceNotFoundError):
        await service.events(intruder, submitted.run.id)

    cancelled = await service.request_cancellation(owner, submitted.run.id)
    replay = await service.request_cancellation(owner, submitted.run.id)
    events = await service.events(owner, submitted.run.id)
    after_first = await service.events(owner, submitted.run.id, after=1)

    assert cancelled.state is RunState.CANCELLING
    assert replay.state is RunState.CANCELLING
    assert [event.sequence for event in events] == [1, 2]
    assert [event.sequence for event in after_first] == [2]
    assert events[-1].event_type == "run.cancellation_requested"
    with pytest.raises(InvalidStateTransitionError):
        await service.retry(owner, submitted.run.id, idempotency_key="too-soon")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_creates_new_attempt_with_immutable_lineage(job_database: Database) -> None:
    actor, project = await _identity(job_database, "retry")
    service = JobControlPlane(job_database)
    source = await service.submit(actor, _submission(project), idempotency_key="original")
    async with job_database.session() as session:
        await session.execute(
            update(Run).where(Run.id == source.run.id).values(state=RunState.FAILED)
        )

    retry = await service.retry(actor, source.run.id, idempotency_key="retry-1")
    replay = await service.retry(actor, source.run.id, idempotency_key="retry-1")

    assert retry.created is True
    assert replay.created is False
    assert retry.run.id != source.run.id
    assert retry.run.parent_run_id == source.run.id
    assert retry.run.attempt == 2
    assert retry.run.spec_sha256 == source.run.spec_sha256
    assert (await service.events(actor, retry.run.id))[0].event_type == "run.retry_submitted"


class RecordingDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[DispatchMessage] = []

    async def publish(self, message: DispatchMessage) -> None:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("provider-secret-must-not-be-recorded")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_leases_survive_restart_and_publish_at_least_once(
    job_database: Database,
) -> None:
    actor, project = await _identity(job_database, "outbox")
    service = JobControlPlane(job_database)
    submitted = await service.submit(actor, _submission(project), idempotency_key="dispatch")
    first_process = TransactionalOutbox(job_database)
    leased = await first_process.claim(worker_id="dispatcher-1")
    assert len(leased) == 1

    async with job_database.session() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == leased[0].id)
            .values(claimed_at=datetime.now(UTC) - timedelta(minutes=5))
        )

    restarted = TransactionalOutbox(job_database)
    with pytest.raises(ValueError, match="worker_id"):
        await restarted.claim(worker_id="")
    reclaimed = await restarted.claim(worker_id="dispatcher-2", lease_seconds=60)
    assert [message.id for message in reclaimed] == [leased[0].id]
    async with job_database.session() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == reclaimed[0].id)
            .values(state=OutboxState.PENDING, claimed_at=None, claimed_by=None)
        )
    dispatcher = RecordingDispatcher()
    result = await restarted.dispatch_batch(dispatcher, worker_id="dispatcher-3")

    assert result.published == 1
    assert dispatcher.messages[0].run_id == submitted.run.id
    assert "spec" not in dispatcher.messages[0].payload
    assert (await service.get(actor, submitted.run.id)).outbox_state is OutboxState.PUBLISHED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_failure_is_retriable_and_does_not_log_exception_details(
    job_database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    actor, project = await _identity(job_database, "outbox-failure")
    service = JobControlPlane(job_database)
    await service.submit(actor, _submission(project), idempotency_key="dispatch-failure")
    caplog.set_level(logging.DEBUG)

    result = await TransactionalOutbox(job_database).dispatch_batch(
        RecordingDispatcher(fail=True), worker_id="dispatcher-failing"
    )

    assert result.failed == 1
    assert "provider-secret-must-not-be-recorded" not in caplog.text
    async with job_database.session() as session:
        message = await session.scalar(select(OutboxMessage))
        assert message is not None
        assert message.state is OutboxState.PENDING
        assert message.last_error_code == "publisher_unavailable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_idempotency_when_ci_database_is_available() -> None:
    url = os.getenv("CORPUSKIT_TEST_POSTGRES_URL")
    if url is None:
        pytest.skip("CORPUSKIT_TEST_POSTGRES_URL is not configured")
    database = Database(url)
    suffix = uuid4().hex
    actor, project = await _identity(database, suffix)
    service = JobControlPlane(database)
    try:
        results = await asyncio.gather(
            *[
                service.submit(actor, _submission(project), idempotency_key=f"pg-{suffix}")
                for _ in range(12)
            ]
        )
        assert len({result.run.id for result in results}) == 1
        assert sum(result.created for result in results) == 1
    finally:
        # The real PostgreSQL acceptance database is disposable. Submitted runs append
        # immutable audit evidence, so cascading the tenant away would correctly trip the
        # audit mutation trigger and make the cleanup itself the test failure.
        await database.dispose()
