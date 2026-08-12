"""Tenant-scoped durable job persistence and at-least-once outbox dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.artifacts import ArtifactKind, ArtifactState
from corpuskit.domain.corpora import CorpusImportLimits, CorpusImportRequest, prepare_corpus
from corpuskit.domain.datg import DatgGuidedGenerationRequest
from corpuskit.domain.errors import (
    InvalidRequestError,
    InvalidStateTransitionError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.jobs import RunKind, RunState, ensure_transition, normalize_run_spec
from corpuskit.domain.model_runtime import LocalGenerationRequest
from corpuskit.domain.phon_rl import PhonRlStaticPromptSource, PhonRlTrainingRequest
from corpuskit.domain.platform import AuditAction, AuditResourceType, run_quota_class
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
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
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager
from corpuskit.services.run_admission import DenyAdvancedRunAdmission, RunAdmissionPolicy
from corpuskit.workflows.handlers import EvaluateRunSpec, PhonemizeRunSpec, SelectRunSpec

WRITER_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.EDITOR})
DEMO_USER_ID = UUID("00000000-0000-4000-8000-000000000002")
DEMO_PROJECT_ID = UUID("00000000-0000-4000-8000-000000000003")


@dataclass(frozen=True, slots=True)
class JobActor:
    subject: str
    organization_id: UUID
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunSubmission:
    project_id: UUID
    kind: RunKind
    spec: dict[str, Any]
    corpus_version_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    id: UUID
    organization_id: UUID
    project_id: UUID
    corpus_version_id: UUID | None
    parent_run_id: UUID | None
    kind: RunKind
    state: RunState
    attempt: int
    spec: dict[str, Any]
    spec_sha256: str
    outbox_state: OutboxState
    cancellation_requested_at: datetime | None
    created_at: datetime
    result_summary: dict[str, Any] | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    run: RunSnapshot
    created: bool


@dataclass(frozen=True, slots=True)
class DispatchMessage:
    id: UUID
    organization_id: UUID
    run_id: UUID
    event_type: str
    payload: Mapping[str, Any]
    attempt: int


@dataclass(frozen=True, slots=True)
class DispatchResult:
    claimed: int
    published: int
    failed: int


class Dispatcher(Protocol):
    """External workflow dispatcher; implementations must deduplicate by message ID."""

    async def publish(self, message: DispatchMessage) -> None: ...


class JobControlPlane:
    """Persist immutable runs and lifecycle requests without executing CorpusGen work."""

    def __init__(
        self,
        database: Database,
        admission_policy: RunAdmissionPolicy | None = None,
    ) -> None:
        self.database = database
        self._admission_policy = admission_policy or DenyAdvancedRunAdmission()

    async def close(self) -> None:
        await self.database.dispose()

    async def bootstrap_demo(self, actor: JobActor, *, environment: str) -> None:
        """Idempotently create the fixed local tenant; never available outside dev/test."""

        if environment not in {"development", "test"}:
            raise RuntimeError("demo bootstrap is limited to development and test")
        context = TenantContext.service(ServiceIdentity.PLATFORM, actor.organization_id)
        async with self.database.session(context) as session:
            dialect_name = session.get_bind().dialect.name
            rows = (
                (
                    Organization,
                    {
                        "id": actor.organization_id,
                        "slug": "corpuskit-demo",
                        "name": "CorpusKit Demo",
                    },
                ),
                (
                    User,
                    {
                        "id": DEMO_USER_ID,
                        "oidc_subject": actor.subject,
                        "display_name": "CorpusKit Demo User",
                    },
                ),
                (
                    Membership,
                    {
                        "id": UUID("00000000-0000-4000-8000-000000000004"),
                        "organization_id": actor.organization_id,
                        "user_id": DEMO_USER_ID,
                        "role": Role.OWNER,
                    },
                ),
                (
                    Project,
                    {
                        "id": DEMO_PROJECT_ID,
                        "organization_id": actor.organization_id,
                        "created_by": DEMO_USER_ID,
                        "name": "Demo project",
                        "description": "Local development workspace",
                    },
                ),
            )
            for model, values in rows:
                statement: Any
                if dialect_name == "postgresql":
                    statement = postgresql_insert(model).values(**values).on_conflict_do_nothing()
                elif dialect_name == "sqlite":
                    statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
                else:
                    raise RuntimeError("demo bootstrap requires PostgreSQL or SQLite")
                await session.execute(statement)
            await QuotaManager.ensure_tenant(session, actor.organization_id)
            organization = await session.get(Organization, actor.organization_id)
            user = await session.get(User, DEMO_USER_ID)
            membership = await session.scalar(
                select(Membership).where(
                    Membership.organization_id == actor.organization_id,
                    Membership.user_id == DEMO_USER_ID,
                    Membership.role == Role.OWNER,
                )
            )
            project = await session.scalar(
                select(Project).where(
                    Project.id == DEMO_PROJECT_ID,
                    Project.organization_id == actor.organization_id,
                )
            )
            if (
                organization is None
                or organization.slug != "corpuskit-demo"
                or organization.name != "CorpusKit Demo"
                or user is None
                or user.oidc_subject != actor.subject
                or user.display_name != "CorpusKit Demo User"
                or membership is None
                or project is None
                or project.created_by != DEMO_USER_ID
                or project.name != "Demo project"
            ):
                raise RuntimeError("demo identity conflicts with existing persisted data")

    async def submit(
        self,
        actor: JobActor,
        submission: RunSubmission,
        *,
        idempotency_key: str,
    ) -> SubmissionResult:
        key = _idempotency_key(idempotency_key)
        try:
            normalized_spec, spec_sha256 = normalize_run_spec(submission.spec)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError("run.submit") from exc
        async with self.database.session(_user_context(actor)) as session:
            user_id, role = await self._actor(session, actor)
            _require_writer(role, "run.submit")
            await self._submission_scope(session, actor.organization_id, submission)
            return await self._submit_in_session(
                session,
                actor=actor,
                user_id=user_id,
                submission=submission,
                normalized_spec=normalized_spec,
                spec_sha256=spec_sha256,
                idempotency_key=key,
                parent_run_id=None,
                attempt=1,
                initial_event_type="run.submitted",
                initial_event_payload={"state": RunState.QUEUED.value},
            )

    async def get(self, actor: JobActor, run_id: UUID) -> RunSnapshot:
        async with self.database.session(_user_context(actor)) as session:
            await self._actor(session, actor)
            run = await self._run(session, actor.organization_id, run_id)
            return await self._snapshot(session, run)

    async def list(
        self,
        actor: JobActor,
        *,
        state: RunState | None = None,
        kind: RunKind | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[RunSnapshot, ...]:
        async with self.database.session(_user_context(actor)) as session:
            await self._actor(session, actor)
            statement: Select[tuple[Run]] = (
                select(Run)
                .join(Project, Project.id == Run.project_id)
                .where(
                    Run.organization_id == actor.organization_id,
                    Project.organization_id == actor.organization_id,
                    Project.lifecycle_state == ProjectLifecycle.ACTIVE,
                )
            )
            if state is not None:
                statement = statement.where(Run.state == state)
            if kind is not None:
                statement = statement.where(Run.kind == kind)
            runs = (
                await session.scalars(
                    statement.order_by(Run.created_at.desc(), Run.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return tuple([await self._snapshot(session, run) for run in runs])

    async def events(
        self,
        actor: JobActor,
        run_id: UUID,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[EventSnapshot, ...]:
        async with self.database.session(_user_context(actor)) as session:
            await self._actor(session, actor)
            await self._run(session, actor.organization_id, run_id)
            events = (
                await session.scalars(
                    select(RunEvent)
                    .where(
                        RunEvent.organization_id == actor.organization_id,
                        RunEvent.run_id == run_id,
                        RunEvent.sequence > after,
                    )
                    .order_by(RunEvent.sequence)
                    .limit(limit)
                )
            ).all()
            return tuple(
                EventSnapshot(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=dict(event.payload),
                    occurred_at=event.occurred_at,
                )
                for event in events
            )

    async def request_cancellation(self, actor: JobActor, run_id: UUID) -> RunSnapshot:
        async with self.database.session(_user_context(actor)) as session:
            user_id, role = await self._actor(session, actor)
            _require_writer(role, "run.cancel")
            for _ in range(3):
                run = await self._run(session, actor.organization_id, run_id)
                if run.state is RunState.CANCELLING:
                    return await self._snapshot(session, run)
                ensure_transition(run.state, RunState.CANCELLING)
                requested_at = datetime.now(UTC)
                next_sequence = run.event_sequence + 1
                changed = await session.scalar(
                    update(Run)
                    .where(
                        Run.id == run.id,
                        Run.organization_id == actor.organization_id,
                        Run.state == run.state,
                        Run.event_sequence == run.event_sequence,
                    )
                    .values(
                        state=RunState.CANCELLING,
                        cancellation_requested_at=requested_at,
                        event_sequence=next_sequence,
                    )
                    .returning(Run.id)
                )
                if changed is None:
                    session.expire_all()
                    continue
                session.add(
                    RunEvent(
                        organization_id=actor.organization_id,
                        run_id=run.id,
                        sequence=next_sequence,
                        event_type="run.cancellation_requested",
                        payload={"state": RunState.CANCELLING.value},
                    )
                )
                session.add(_outbox(run, "run.cancel"))
                await AuditWriter.append(
                    session,
                    organization_id=actor.organization_id,
                    actor=AuditIdentity.user(user_id),
                    action=AuditAction.RUN_CANCELLATION_REQUESTED,
                    resource_type=AuditResourceType.RUN,
                    resource_id=run.id,
                    request_id=actor.request_id,
                    metadata={"prior_state": run.state.value},
                )
                await session.flush()
                session.expire_all()
                return await self._snapshot(
                    session, await self._run(session, actor.organization_id, run_id)
                )
            raise ResourceConflictError("run.cancel")

    async def retry(
        self,
        actor: JobActor,
        run_id: UUID,
        *,
        idempotency_key: str,
    ) -> SubmissionResult:
        key = _idempotency_key(idempotency_key)
        async with self.database.session(_user_context(actor)) as session:
            user_id, role = await self._actor(session, actor)
            _require_writer(role, "run.retry")
            source = await self._run(session, actor.organization_id, run_id)
            if source.state not in {RunState.FAILED, RunState.CANCELLED}:
                raise InvalidStateTransitionError("run.retry")
            submission = RunSubmission(
                project_id=source.project_id,
                corpus_version_id=source.corpus_version_id,
                kind=source.kind,
                spec=dict(source.spec),
            )
            return await self._submit_in_session(
                session,
                actor=actor,
                user_id=user_id,
                submission=submission,
                normalized_spec=dict(source.spec),
                spec_sha256=source.spec_sha256,
                idempotency_key=key,
                parent_run_id=source.id,
                attempt=source.attempt + 1,
                initial_event_type="run.retry_submitted",
                initial_event_payload={"source_run_id": str(source.id)},
            )

    async def _submit_in_session(
        self,
        session: AsyncSession,
        *,
        actor: JobActor,
        user_id: UUID,
        submission: RunSubmission,
        normalized_spec: dict[str, Any],
        spec_sha256: str,
        idempotency_key: str,
        parent_run_id: UUID | None,
        attempt: int,
        initial_event_type: str,
        initial_event_payload: dict[str, Any],
    ) -> SubmissionResult:
        existing = await self._idempotent_run(session, actor.organization_id, idempotency_key)
        if existing is not None:
            _same_submission(existing, submission, spec_sha256, parent_run_id)
            return SubmissionResult(await self._snapshot(session, existing), created=False)

        try:
            self._admission_policy.validate(submission.kind, normalized_spec)
        except (TypeError, ValueError) as exc:
            operation = "run.retry" if parent_run_id is not None else "run.submit"
            raise InvalidRequestError(operation) from exc
        await self._authorize_datg_index(
            session,
            actor.organization_id,
            submission,
            normalized_spec,
        )
        await self._authorize_advanced_artifact(
            session,
            actor.organization_id,
            submission,
            normalized_spec,
        )
        await self._authorize_corpus_lineage(
            session,
            actor.organization_id,
            submission,
            normalized_spec,
        )

        run_id = uuid4()
        values = {
            "id": run_id,
            "organization_id": actor.organization_id,
            "project_id": submission.project_id,
            "corpus_version_id": submission.corpus_version_id,
            "parent_run_id": parent_run_id,
            "created_by": user_id,
            "kind": submission.kind,
            "state": RunState.QUEUED,
            "idempotency_key": idempotency_key,
            "attempt": attempt,
            "event_sequence": 1,
            "spec": normalized_spec,
            "spec_sha256": spec_sha256,
        }
        dialect_name = session.get_bind().dialect.name
        insert_statement: Any
        if dialect_name == "postgresql":
            insert_statement = postgresql_insert(Run).values(**values)
        elif dialect_name == "sqlite":
            insert_statement = sqlite_insert(Run).values(**values)
        else:
            raise RuntimeError("durable job idempotency requires PostgreSQL or SQLite")
        inserted_id = await session.scalar(
            insert_statement.on_conflict_do_nothing(
                index_elements=[Run.organization_id, Run.idempotency_key]
            ).returning(Run.id)
        )
        if inserted_id is None:
            existing = await self._idempotent_run(session, actor.organization_id, idempotency_key)
            if existing is None:
                raise ResourceConflictError("run.submit")
            _same_submission(existing, submission, spec_sha256, parent_run_id)
            return SubmissionResult(await self._snapshot(session, existing), created=False)

        run = await self._run(session, actor.organization_id, inserted_id)
        await QuotaManager.reserve_run(
            session,
            organization_id=actor.organization_id,
            run=run,
        )
        session.add(
            RunEvent(
                organization_id=actor.organization_id,
                run_id=run.id,
                sequence=1,
                event_type=initial_event_type,
                payload=initial_event_payload,
            )
        )
        session.add(_outbox(run, "run.dispatch"))
        audit_action = (
            AuditAction.RUN_RETRY_SUBMITTED
            if parent_run_id is not None
            else AuditAction.RUN_SUBMITTED
        )
        metadata: dict[str, Any] = {
            "attempt": attempt,
            "kind": run.kind.value,
            "quota_class": run_quota_class(run.kind).value,
        }
        if parent_run_id is not None:
            metadata["source_run_id"] = str(parent_run_id)
        await AuditWriter.append(
            session,
            organization_id=actor.organization_id,
            actor=AuditIdentity.user(user_id),
            action=audit_action,
            resource_type=AuditResourceType.RUN,
            resource_id=run.id,
            request_id=actor.request_id,
            metadata=metadata,
        )
        await session.flush()
        return SubmissionResult(await self._snapshot(session, run), created=True)

    @staticmethod
    async def _authorize_advanced_artifact(
        session: AsyncSession,
        organization_id: UUID,
        submission: RunSubmission,
        normalized_spec: dict[str, Any],
    ) -> None:
        artifact_id: UUID | None = None
        expected_sha256: str | None = None
        expected_kind: ArtifactKind | None = None
        require_training_run = False
        try:
            if submission.kind is RunKind.TRAIN_PHON_RL:
                training_request = PhonRlTrainingRequest.model_validate(normalized_spec)
                if isinstance(training_request.prompt_source, PhonRlStaticPromptSource):
                    artifact_id = training_request.prompt_source.artifact_id
                    expected_sha256 = training_request.prompt_source.content_sha256
                    expected_kind = ArtifactKind.PROMPT_SET
            elif submission.kind is RunKind.GENERATE_LOCAL:
                local_request = LocalGenerationRequest.model_validate(normalized_spec)
                if local_request.phon_rl_adapter is not None:
                    artifact_id = local_request.phon_rl_adapter.artifact_id
                    expected_sha256 = local_request.phon_rl_adapter.artifact_sha256
                    expected_kind = ArtifactKind.RUN_RESULT
                    require_training_run = True
        except (TypeError, ValueError):
            raise InvalidRequestError("run.submit") from None
        if artifact_id is None or expected_sha256 is None or expected_kind is None:
            return
        statement = select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.organization_id == organization_id,
            Artifact.project_id == submission.project_id,
            Artifact.kind == expected_kind,
            Artifact.state == ArtifactState.ACTIVE,
            Artifact.sha256 == expected_sha256,
        )
        artifact = await session.scalar(statement)
        if artifact is None:
            raise ResourceNotFoundError("run.input_artifact")
        if require_training_run:
            if artifact.run_id is None:
                raise ResourceNotFoundError("run.input_artifact")
            source_run = await session.scalar(
                select(Run).where(
                    Run.id == artifact.run_id,
                    Run.organization_id == organization_id,
                    Run.project_id == submission.project_id,
                    Run.kind == RunKind.TRAIN_PHON_RL,
                    Run.state == RunState.SUCCEEDED,
                )
            )
            if source_run is None:
                raise ResourceNotFoundError("run.input_artifact")

    @staticmethod
    async def _authorize_datg_index(
        session: AsyncSession,
        organization_id: UUID,
        submission: RunSubmission,
        normalized_spec: dict[str, Any],
    ) -> None:
        if submission.kind is not RunKind.GENERATE_DATG:
            return
        try:
            request = DatgGuidedGenerationRequest.model_validate(normalized_spec)
        except (TypeError, ValueError):
            raise InvalidRequestError("run.submit") from None
        publication = await session.scalar(
            select(DatgIndexPublicationRecord.id).where(
                DatgIndexPublicationRecord.organization_id == organization_id,
                DatgIndexPublicationRecord.project_id == submission.project_id,
                DatgIndexPublicationRecord.cache_key_sha256 == request.index_cache_key_sha256,
                DatgIndexPublicationRecord.runtime_id == request.runtime_id,
                DatgIndexPublicationRecord.language == request.language,
                DatgIndexPublicationRecord.unit == request.unit.value,
            )
        )
        if publication is None:
            raise ResourceNotFoundError("run.datg_index")

    @staticmethod
    async def _authorize_corpus_lineage(
        session: AsyncSession,
        organization_id: UUID,
        submission: RunSubmission,
        normalized_spec: dict[str, Any],
    ) -> None:
        """Permit a corpus-version link only when the executable inline corpus is identical."""

        if submission.corpus_version_id is None:
            return
        try:
            if submission.kind is RunKind.PHONEMIZE:
                phonemize_request = PhonemizeRunSpec.model_validate(normalized_spec)
                sentences = (
                    (phonemize_request.text,)
                    if phonemize_request.text is not None
                    else phonemize_request.texts
                )
                language = phonemize_request.language
            elif submission.kind is RunKind.EVALUATE:
                evaluate_request = EvaluateRunSpec.model_validate(normalized_spec)
                sentences = evaluate_request.sentences
                language = evaluate_request.language
            elif submission.kind is RunKind.SELECT:
                select_request = SelectRunSpec.model_validate(normalized_spec)
                sentences = select_request.candidates
                language = select_request.language
            else:
                raise InvalidRequestError("run.corpus_lineage")
            prepared = prepare_corpus(
                CorpusImportRequest(language=language, sentences=sentences),
                CorpusImportLimits(max_sentences=10_000, max_sentence_characters=2_000),
            )
        except InvalidRequestError:
            raise
        except (TypeError, ValueError):
            raise InvalidRequestError("run.corpus_lineage") from None

        version = await session.scalar(
            select(CorpusVersion)
            .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
            .where(
                CorpusVersion.id == submission.corpus_version_id,
                CorpusVersion.organization_id == organization_id,
                Corpus.project_id == submission.project_id,
            )
        )
        if version is None:
            raise ResourceNotFoundError("run.submit")
        if (
            version.language != prepared.language
            or version.sentence_count != len(prepared.sentences)
            or version.content_sha256 != prepared.content_sha256
        ):
            raise InvalidRequestError("run.corpus_lineage")

    @staticmethod
    async def _actor(session: AsyncSession, actor: JobActor) -> tuple[UUID, Role]:
        row = (
            await session.execute(
                select(User.id, Membership.role)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    User.oidc_subject == actor.subject,
                    Membership.organization_id == actor.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ResourceNotFoundError("run.identity")
        return row._tuple()

    @staticmethod
    async def _submission_scope(
        session: AsyncSession,
        organization_id: UUID,
        submission: RunSubmission,
    ) -> None:
        if (
            await session.scalar(
                select(Project.id)
                .where(
                    Project.id == submission.project_id,
                    Project.organization_id == organization_id,
                    Project.lifecycle_state == ProjectLifecycle.ACTIVE,
                )
                .with_for_update()
            )
            is None
        ):
            raise ResourceNotFoundError("run.submit")
        if submission.corpus_version_id is not None and (
            await session.scalar(
                select(CorpusVersion.id)
                .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
                .where(
                    CorpusVersion.id == submission.corpus_version_id,
                    CorpusVersion.organization_id == organization_id,
                    Corpus.project_id == submission.project_id,
                )
            )
            is None
        ):
            raise ResourceNotFoundError("run.submit")

    @staticmethod
    async def _run(session: AsyncSession, organization_id: UUID, run_id: UUID) -> Run:
        run = await session.scalar(
            select(Run)
            .join(Project, Project.id == Run.project_id)
            .where(
                Run.id == run_id,
                Run.organization_id == organization_id,
                Project.organization_id == organization_id,
                Project.lifecycle_state == ProjectLifecycle.ACTIVE,
            )
        )
        if run is None:
            raise ResourceNotFoundError("run.get")
        return run

    @staticmethod
    async def _idempotent_run(
        session: AsyncSession, organization_id: UUID, idempotency_key: str
    ) -> Run | None:
        run: Run | None = await session.scalar(
            select(Run).where(
                Run.organization_id == organization_id,
                Run.idempotency_key == idempotency_key,
            )
        )
        return run

    @staticmethod
    async def _snapshot(session: AsyncSession, run: Run) -> RunSnapshot:
        outbox_state = await session.scalar(
            select(OutboxMessage.state)
            .where(OutboxMessage.run_id == run.id, OutboxMessage.event_type == "run.dispatch")
            .order_by(OutboxMessage.created_at.desc(), OutboxMessage.id.desc())
            .limit(1)
        )
        if outbox_state is None:
            raise RuntimeError("run has no durable dispatch intent")
        return RunSnapshot(
            id=run.id,
            organization_id=run.organization_id,
            project_id=run.project_id,
            corpus_version_id=run.corpus_version_id,
            parent_run_id=run.parent_run_id,
            kind=run.kind,
            state=run.state,
            attempt=run.attempt,
            spec=dict(run.spec),
            spec_sha256=run.spec_sha256,
            outbox_state=outbox_state,
            cancellation_requested_at=run.cancellation_requested_at,
            created_at=run.created_at,
            result_summary=(dict(run.result_summary) if run.result_summary is not None else None),
            failure_code=run.failure_code,
        )


class TransactionalOutbox:
    """Lease and publish committed intents without implying workflow execution."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        lease_seconds: int = 60,
    ) -> tuple[DispatchMessage, ...]:
        if not worker_id or len(worker_id) > 80:
            raise ValueError("worker_id must be between 1 and 80 characters")
        now = datetime.now(UTC)
        expired = now - timedelta(seconds=lease_seconds)
        claimed: list[DispatchMessage] = []
        context = TenantContext.service(ServiceIdentity.DISPATCHER)
        async with self.database.session(context) as session:
            candidate_ids = (
                await session.scalars(
                    select(OutboxMessage.id)
                    .where(
                        OutboxMessage.processed_at.is_(None),
                        OutboxMessage.available_at <= now,
                        or_(
                            OutboxMessage.state == OutboxState.PENDING,
                            and_(
                                OutboxMessage.state == OutboxState.CLAIMED,
                                OutboxMessage.claimed_at < expired,
                            ),
                        ),
                    )
                    .order_by(OutboxMessage.created_at, OutboxMessage.id)
                    .limit(limit)
                )
            ).all()
            for message_id in candidate_ids:
                message = await session.scalar(
                    update(OutboxMessage)
                    .where(
                        OutboxMessage.id == message_id,
                        OutboxMessage.processed_at.is_(None),
                        or_(
                            OutboxMessage.state == OutboxState.PENDING,
                            and_(
                                OutboxMessage.state == OutboxState.CLAIMED,
                                OutboxMessage.claimed_at < expired,
                            ),
                        ),
                    )
                    .values(
                        state=OutboxState.CLAIMED,
                        claimed_at=now,
                        claimed_by=worker_id,
                        attempts=OutboxMessage.attempts + 1,
                        last_error_code=None,
                    )
                    .returning(OutboxMessage)
                )
                if message is not None:
                    claimed.append(_dispatch_message(message))
        return tuple(claimed)

    async def dispatch_batch(
        self,
        dispatcher: Dispatcher,
        *,
        worker_id: str,
        limit: int = 20,
    ) -> DispatchResult:
        messages = await self.claim(worker_id=worker_id, limit=limit)
        published = 0
        failed = 0
        for message in messages:
            try:
                await dispatcher.publish(message)
            except Exception:
                failed += 1
                await self._release_failed(message.id, worker_id)
            else:
                published += 1
                await self._mark_published(message.id, worker_id)
        return DispatchResult(claimed=len(messages), published=published, failed=failed)

    async def _mark_published(self, message_id: UUID, worker_id: str) -> None:
        context = TenantContext.service(ServiceIdentity.DISPATCHER)
        async with self.database.session(context) as session:
            changed = await session.scalar(
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.state == OutboxState.CLAIMED,
                    OutboxMessage.claimed_by == worker_id,
                )
                .values(state=OutboxState.PUBLISHED, processed_at=datetime.now(UTC))
                .returning(OutboxMessage.id)
            )
            if changed is None:
                raise ResourceConflictError("outbox.acknowledge")

    async def _release_failed(self, message_id: UUID, worker_id: str) -> None:
        context = TenantContext.service(ServiceIdentity.DISPATCHER)
        async with self.database.session(context) as session:
            changed = await session.scalar(
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.state == OutboxState.CLAIMED,
                    OutboxMessage.claimed_by == worker_id,
                )
                .values(
                    state=OutboxState.PENDING,
                    claimed_at=None,
                    claimed_by=None,
                    available_at=datetime.now(UTC) + timedelta(seconds=5),
                    last_error_code="publisher_unavailable",
                )
                .returning(OutboxMessage.id)
            )
            if changed is None:
                raise ResourceConflictError("outbox.release")


def _idempotency_key(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise InvalidRequestError("run.idempotency")
    return value


def _user_context(actor: JobActor) -> TenantContext:
    return TenantContext.user(actor.organization_id, actor.subject)


def _require_writer(role: Role, operation: str) -> None:
    if role not in WRITER_ROLES:
        raise ResourceNotFoundError(operation)


def _same_submission(
    run: Run,
    submission: RunSubmission,
    spec_sha256: str,
    parent_run_id: UUID | None,
) -> None:
    if (
        run.kind != submission.kind
        or run.project_id != submission.project_id
        or run.corpus_version_id != submission.corpus_version_id
        or run.parent_run_id != parent_run_id
        or run.spec_sha256 != spec_sha256
    ):
        raise ResourceConflictError("run.idempotency")


def _outbox(run: Run, event_type: str) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        organization_id=run.organization_id,
        run_id=run.id,
        event_type=event_type,
        payload={
            "run_id": str(run.id),
            "kind": run.kind.value,
            "spec_sha256": run.spec_sha256,
        },
        state=OutboxState.PENDING,
        attempts=0,
    )


def _dispatch_message(message: OutboxMessage) -> DispatchMessage:
    return DispatchMessage(
        id=message.id,
        organization_id=message.organization_id,
        run_id=message.run_id,
        event_type=message.event_type,
        payload=dict(message.payload),
        attempt=message.attempts,
    )


__all__ = [
    "DEMO_PROJECT_ID",
    "DispatchMessage",
    "DispatchResult",
    "Dispatcher",
    "EventSnapshot",
    "JobActor",
    "JobControlPlane",
    "RunSnapshot",
    "RunSubmission",
    "SubmissionResult",
    "TransactionalOutbox",
]
