"""Tenant-scoped compare-and-swap persistence for durable execution activities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.artifacts import ArtifactKind, ArtifactState, artifact_storage_key
from corpuskit.domain.jobs import (
    RunKind,
    RunState,
    ensure_transition,
    is_terminal,
    normalize_result_summary,
    normalize_run_spec,
)
from corpuskit.domain.platform import AuditAction, AuditResourceType
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact, DatgIndexPublicationRecord, Run, RunEvent
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.progress import DurableRunProgress

_MAX_CAS_ATTEMPTS = 8


class RunStoreError(RuntimeError):
    """Safe persistence-contract failure without database or specification details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    organization_id: UUID
    project_id: UUID
    run_id: UUID
    created_by: UUID
    kind: RunKind
    state: RunState
    spec: dict[str, Any]
    spec_sha256: str


@dataclass(frozen=True, slots=True)
class AdoptedArtifact:
    sha256: str
    size_bytes: int
    storage_key: str
    media_type: str
    filename: str
    schema_id: str
    retention_until: datetime
    datg_index: AdoptedDatgIndex | None = None


@dataclass(frozen=True, slots=True)
class AdoptedDatgIndex:
    cache_key_sha256: str
    content_sha256: str
    runtime_id: str
    language: str
    unit: str
    vocabulary_size: int
    indexed_token_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactCommit:
    state: RunState
    artifact_id: UUID | None
    created: bool


class DurableRunStore:
    """Own lifecycle transitions for activities; PostgreSQL remains the projection source."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def execution_record(self, reference: RunWorkflowReference) -> ExecutionRecord:
        organization_id, run_id = _ids(reference)
        context = TenantContext.service(ServiceIdentity.WORKER, organization_id)
        async with self.database.session(context) as session:
            run = await self._run(session, organization_id, run_id)
            self._verify_spec(run, reference.spec_sha256)
            return ExecutionRecord(
                organization_id=run.organization_id,
                project_id=run.project_id,
                run_id=run.id,
                created_by=run.created_by,
                kind=run.kind,
                state=run.state,
                spec=dict(run.spec),
                spec_sha256=run.spec_sha256,
            )

    async def commit_adopted_result(
        self,
        reference: RunWorkflowReference,
        adopted: AdoptedArtifact,
    ) -> ArtifactCommit:
        """Atomically publish one authoritative artifact and the terminal success event."""

        organization_id, run_id = _ids(reference)
        context = TenantContext.service(ServiceIdentity.ADOPTION, organization_id)
        async with self.database.session(context) as session:
            run = await self._run(session, organization_id, run_id, for_update=True)
            self._verify_spec(run, reference.spec_sha256)
            expected_key = artifact_storage_key(
                organization_id=run.organization_id,
                project_id=run.project_id,
                run_id=run.id,
                kind=ArtifactKind.RUN_RESULT,
                sha256=adopted.sha256,
            )
            if adopted.storage_key != expected_key:
                raise RunStoreError("artifact_integrity_violation")
            if run.state in {RunState.CANCELLING, RunState.CANCELLED} or (
                run.cancellation_requested_at is not None
            ):
                cancelled = await self._cancel_locked(session, run)
                if cancelled:
                    await self._record_terminal_audit(
                        session,
                        run,
                        action=AuditAction.RUN_CANCELLED,
                        identity=ServiceIdentity.ADOPTION,
                    )
                return ArtifactCommit(RunState.CANCELLED, None, created=False)
            if run.state is RunState.SUCCEEDED:
                existing = await session.scalar(
                    select(Artifact).where(
                        Artifact.organization_id == run.organization_id,
                        Artifact.project_id == run.project_id,
                        Artifact.run_id == run.id,
                        Artifact.kind == ArtifactKind.RUN_RESULT.value,
                        Artifact.sha256 == adopted.sha256,
                    )
                )
                if existing is None or (
                    existing.created_by != run.created_by
                    or existing.scope_key != str(run.id)
                    or existing.size_bytes != adopted.size_bytes
                    or existing.storage_key != adopted.storage_key
                    or existing.media_type != adopted.media_type
                    or existing.filename != adopted.filename
                    or existing.state is not ArtifactState.ACTIVE
                    or run.result_summary
                    != normalize_result_summary(_artifact_result_summary(existing.id, adopted))
                ):
                    raise RunStoreError("artifact_integrity_violation")
                await self._verify_existing_datg_publication(session, run, adopted)
                return ArtifactCommit(run.state, existing.id, created=False)
            if run.state is not RunState.RUNNING:
                raise RunStoreError("invalid_run_state")

            artifact_id = uuid4()
            result_summary = _artifact_result_summary(artifact_id, adopted)
            normalized = normalize_result_summary(result_summary)
            sequence = run.event_sequence + 1
            session.add(
                Artifact(
                    id=artifact_id,
                    organization_id=run.organization_id,
                    project_id=run.project_id,
                    run_id=run.id,
                    created_by=run.created_by,
                    scope_key=str(run.id),
                    kind=ArtifactKind.RUN_RESULT.value,
                    sha256=adopted.sha256,
                    size_bytes=adopted.size_bytes,
                    storage_key=adopted.storage_key,
                    media_type=adopted.media_type,
                    filename=adopted.filename,
                    state=ArtifactState.ACTIVE,
                    retention_until=adopted.retention_until,
                )
            )
            await self._commit_datg_publication(session, run, adopted)
            await QuotaManager.consume_artifact(
                session,
                organization_id=run.organization_id,
                kind=ArtifactKind.RUN_RESULT,
                size_bytes=adopted.size_bytes,
            )
            ensure_transition(run.state, RunState.SUCCEEDED)
            run.state = RunState.SUCCEEDED
            run.event_sequence = sequence
            run.result_summary = normalized
            run.failure_code = None
            session.add(
                RunEvent(
                    organization_id=run.organization_id,
                    run_id=run.id,
                    sequence=sequence,
                    event_type="run.succeeded",
                    payload={"result_summary": normalized, "state": RunState.SUCCEEDED.value},
                )
            )
            await AuditWriter.append(
                session,
                organization_id=run.organization_id,
                actor=AuditIdentity.service(ServiceIdentity.ADOPTION),
                action=AuditAction.ARTIFACT_ADOPTED,
                resource_type=AuditResourceType.ARTIFACT,
                resource_id=artifact_id,
                metadata={
                    "kind": ArtifactKind.RUN_RESULT.value,
                    "schema_id": adopted.schema_id,
                    "sha256": adopted.sha256,
                    "size_bytes": adopted.size_bytes,
                },
            )
            await self._record_terminal_audit(
                session,
                run,
                action=AuditAction.RUN_SUCCEEDED,
                identity=ServiceIdentity.ADOPTION,
            )
            await session.flush()
            return ArtifactCommit(run.state, artifact_id, created=True)

    @staticmethod
    async def _commit_datg_publication(
        session: AsyncSession,
        run: Run,
        adopted: AdoptedArtifact,
    ) -> None:
        publication = adopted.datg_index
        if publication is None:
            return
        if run.kind is not RunKind.BUILD_DATG_INDEX:
            raise RunStoreError("artifact_integrity_violation")
        values = {
            "id": uuid4(),
            "organization_id": run.organization_id,
            "project_id": run.project_id,
            "build_run_id": run.id,
            "created_by": run.created_by,
            "cache_key_sha256": publication.cache_key_sha256,
            "content_sha256": publication.content_sha256,
            "runtime_id": publication.runtime_id,
            "language": publication.language,
            "unit": publication.unit,
            "vocabulary_size": publication.vocabulary_size,
            "indexed_token_count": publication.indexed_token_count,
            "size_bytes": publication.size_bytes,
        }
        dialect_name = session.get_bind().dialect.name
        statement: Any
        if dialect_name == "postgresql":
            statement = postgresql_insert(DatgIndexPublicationRecord).values(**values)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(DatgIndexPublicationRecord).values(**values)
        else:
            raise RunStoreError("persistence_unavailable")
        inserted = await session.scalar(
            statement.on_conflict_do_nothing().returning(DatgIndexPublicationRecord.id)
        )
        if inserted is not None:
            return
        existing = await session.scalar(
            select(DatgIndexPublicationRecord).where(
                DatgIndexPublicationRecord.organization_id == run.organization_id,
                DatgIndexPublicationRecord.project_id == run.project_id,
                DatgIndexPublicationRecord.cache_key_sha256 == publication.cache_key_sha256,
            )
        )
        if existing is None or not _same_datg_publication(existing, publication):
            raise RunStoreError("artifact_integrity_violation")

    @staticmethod
    async def _verify_existing_datg_publication(
        session: AsyncSession,
        run: Run,
        adopted: AdoptedArtifact,
    ) -> None:
        publication = adopted.datg_index
        if publication is None:
            return
        existing = await session.scalar(
            select(DatgIndexPublicationRecord).where(
                DatgIndexPublicationRecord.organization_id == run.organization_id,
                DatgIndexPublicationRecord.project_id == run.project_id,
                DatgIndexPublicationRecord.cache_key_sha256 == publication.cache_key_sha256,
            )
        )
        if existing is None or not _same_datg_publication(existing, publication):
            raise RunStoreError("artifact_integrity_violation")

    async def prepare(self, reference: RunWorkflowReference) -> RunState:
        """Idempotently move a queued run into provisioning or acknowledge cancellation."""

        state = await self.state(reference)
        if state is RunState.QUEUED:
            return await self._transition(
                reference,
                expected=frozenset({RunState.QUEUED}),
                target=RunState.PROVISIONING,
                event_type="run.provisioning",
                payload={"state": RunState.PROVISIONING.value},
            )
        if state is RunState.CANCELLING:
            return await self.acknowledge_cancellation(reference)
        if state in {
            RunState.PROVISIONING,
            RunState.RUNNING,
            RunState.CANCELLED,
            RunState.SUCCEEDED,
            RunState.FAILED,
        }:
            return state
        raise RunStoreError("invalid_run_state")

    async def begin_execution(self, reference: RunWorkflowReference) -> bool:
        """Move provisioning to running; return false when no execution should occur."""

        state = await self.state(reference)
        if state is RunState.QUEUED:
            state = await self.prepare(reference)
        if state is RunState.PROVISIONING:
            state = await self._transition(
                reference,
                expected=frozenset({RunState.PROVISIONING}),
                target=RunState.RUNNING,
                event_type="run.started",
                payload={"state": RunState.RUNNING.value},
            )
        if state is RunState.CANCELLING:
            await self.acknowledge_cancellation(reference)
            return False
        return state is RunState.RUNNING

    async def complete(
        self,
        reference: RunWorkflowReference,
        summary: Mapping[str, Any],
    ) -> RunState:
        """Atomically commit a bounded result projection and success event."""

        normalized = normalize_result_summary(dict(summary))
        state = await self.state(reference)
        if state is RunState.CANCELLING:
            return await self.acknowledge_cancellation(reference)
        if state in {RunState.CANCELLED, RunState.SUCCEEDED, RunState.FAILED}:
            return state
        return await self._transition(
            reference,
            expected=frozenset({RunState.RUNNING}),
            target=RunState.SUCCEEDED,
            event_type="run.succeeded",
            payload={"result_summary": normalized, "state": RunState.SUCCEEDED.value},
            result_summary=normalized,
        )

    async def record_progress(
        self,
        reference: RunWorkflowReference,
        progress: DurableRunProgress,
        *,
        activity_attempt: int,
    ) -> bool:
        """Append progress only while running, with monotonic per-attempt child sequence."""

        if (
            isinstance(activity_attempt, bool)
            or not isinstance(activity_attempt, int)
            or not 1 <= activity_attempt <= 100
        ):
            raise RunStoreError("invalid_progress")
        organization_id, run_id = _ids(reference)
        payload = {
            "activity_attempt": activity_attempt,
            **progress.model_dump(mode="json"),
        }
        for _ in range(_MAX_CAS_ATTEMPTS):
            context = TenantContext.service(ServiceIdentity.WORKER, organization_id)
            async with self.database.session(context) as session:
                run = await self._run(session, organization_id, run_id)
                self._verify_spec(run, reference.spec_sha256)
                if run.state is not RunState.RUNNING:
                    return False
                latest = await session.scalar(
                    select(RunEvent)
                    .where(
                        RunEvent.organization_id == organization_id,
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "run.progress",
                    )
                    .order_by(RunEvent.sequence.desc())
                    .limit(1)
                )
                if latest is not None:
                    latest_attempt = latest.payload.get("activity_attempt")
                    latest_progress_sequence = latest.payload.get("sequence")
                    if (
                        isinstance(latest_attempt, bool)
                        or not isinstance(latest_attempt, int)
                        or isinstance(latest_progress_sequence, bool)
                        or not isinstance(latest_progress_sequence, int)
                    ):
                        raise RunStoreError("progress_integrity_violation")
                    if activity_attempt < latest_attempt:
                        return False
                    if (
                        activity_attempt == latest_attempt
                        and progress.sequence <= latest_progress_sequence
                    ):
                        return False
                event_sequence = run.event_sequence + 1
                changed = await session.scalar(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.organization_id == organization_id,
                        Run.spec_sha256 == reference.spec_sha256,
                        Run.state == RunState.RUNNING,
                        Run.event_sequence == run.event_sequence,
                    )
                    .values(event_sequence=event_sequence)
                    .returning(Run.id)
                )
                if changed is None:
                    continue
                session.add(
                    RunEvent(
                        organization_id=organization_id,
                        run_id=run_id,
                        sequence=event_sequence,
                        event_type="run.progress",
                        payload=payload,
                    )
                )
                await session.flush()
                return True
        raise RunStoreError("state_conflict")

    async def fail(self, reference: RunWorkflowReference, code: str) -> RunState:
        """Persist one safe failure code unless cooperative cancellation won the race."""

        if not _safe_code(code):
            code = "execution_failed"
        state = await self.state(reference)
        if state is RunState.CANCELLING:
            return await self.acknowledge_cancellation(reference)
        if state in {RunState.CANCELLED, RunState.SUCCEEDED, RunState.FAILED}:
            return state
        return await self._transition(
            reference,
            expected=frozenset({RunState.QUEUED, RunState.PROVISIONING, RunState.RUNNING}),
            target=RunState.FAILED,
            event_type="run.failed",
            payload={"failure_code": code, "state": RunState.FAILED.value},
            failure_code=code,
        )

    async def request_cancellation(self, reference: RunWorkflowReference) -> RunState:
        """Internal cancellation CAS used when a Temporal signal precedes API projection."""

        state = await self.state(reference)
        if state in {RunState.QUEUED, RunState.PROVISIONING, RunState.RUNNING}:
            return await self._transition(
                reference,
                expected=frozenset({state}),
                target=RunState.CANCELLING,
                event_type="run.cancellation_observed",
                payload={"state": RunState.CANCELLING.value},
            )
        return state

    async def acknowledge_cancellation(self, reference: RunWorkflowReference) -> RunState:
        """Cooperatively reach cancelled through legal monotonic transitions."""

        state = await self.request_cancellation(reference)
        if state is RunState.CANCELLING:
            return await self._transition(
                reference,
                expected=frozenset({RunState.CANCELLING}),
                target=RunState.CANCELLED,
                event_type="run.cancelled",
                payload={"state": RunState.CANCELLED.value},
            )
        return state

    async def cancellation_requested(self, reference: RunWorkflowReference) -> bool:
        return (await self.state(reference)) in {RunState.CANCELLING, RunState.CANCELLED}

    async def is_terminal(self, reference: RunWorkflowReference) -> bool:
        return is_terminal(await self.state(reference))

    async def state(self, reference: RunWorkflowReference) -> RunState:
        organization_id, run_id = _ids(reference)
        context = TenantContext.service(ServiceIdentity.WORKER, organization_id)
        async with self.database.session(context) as session:
            run = await self._run(session, organization_id, run_id)
            self._verify_spec(run, reference.spec_sha256)
            return run.state

    async def _transition(
        self,
        reference: RunWorkflowReference,
        *,
        expected: frozenset[RunState],
        target: RunState,
        event_type: str,
        payload: dict[str, Any],
        result_summary: dict[str, Any] | None = None,
        failure_code: str | None = None,
    ) -> RunState:
        organization_id, run_id = _ids(reference)
        for _ in range(_MAX_CAS_ATTEMPTS):
            context = TenantContext.service(ServiceIdentity.WORKER, organization_id)
            async with self.database.session(context) as session:
                run = await self._run(session, organization_id, run_id)
                self._verify_spec(run, reference.spec_sha256)
                if run.state is target:
                    return target
                if is_terminal(run.state):
                    return run.state
                if run.state not in expected:
                    return run.state
                ensure_transition(run.state, target)
                sequence = run.event_sequence + 1
                changed = await session.scalar(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.organization_id == organization_id,
                        Run.spec_sha256 == reference.spec_sha256,
                        Run.state == run.state,
                        Run.event_sequence == run.event_sequence,
                    )
                    .values(
                        state=target,
                        event_sequence=sequence,
                        result_summary=result_summary,
                        failure_code=failure_code,
                    )
                    .returning(Run.id)
                )
                if changed is None:
                    continue
                session.add(
                    RunEvent(
                        organization_id=organization_id,
                        run_id=run_id,
                        sequence=sequence,
                        event_type=event_type,
                        payload=payload,
                    )
                )
                await self._apply_transition_controls(
                    session,
                    run,
                    target=target,
                    failure_code=failure_code,
                )
                await session.flush()
                return target
        raise RunStoreError("state_conflict")

    @staticmethod
    async def _run(
        session: AsyncSession,
        organization_id: UUID,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> Run:
        statement = select(Run).where(
            Run.id == run_id,
            Run.organization_id == organization_id,
        )
        if for_update:
            statement = statement.with_for_update()
        run = await session.scalar(statement)
        if run is None:
            raise RunStoreError("run_not_found")
        return run

    @staticmethod
    async def _cancel_locked(session: AsyncSession, run: Run) -> bool:
        now = datetime.now(UTC)
        sequence = run.event_sequence
        cancelled = False
        if run.state not in {RunState.CANCELLING, RunState.CANCELLED}:
            ensure_transition(run.state, RunState.CANCELLING)
            sequence += 1
            session.add(
                RunEvent(
                    organization_id=run.organization_id,
                    run_id=run.id,
                    sequence=sequence,
                    event_type="run.cancellation_observed",
                    payload={"state": RunState.CANCELLING.value},
                )
            )
            run.state = RunState.CANCELLING
        if run.state is RunState.CANCELLING:
            ensure_transition(run.state, RunState.CANCELLED)
            sequence += 1
            session.add(
                RunEvent(
                    organization_id=run.organization_id,
                    run_id=run.id,
                    sequence=sequence,
                    event_type="run.cancelled",
                    payload={"state": RunState.CANCELLED.value},
                )
            )
            run.state = RunState.CANCELLED
            cancelled = True
        run.event_sequence = sequence
        run.cancellation_requested_at = run.cancellation_requested_at or now
        run.result_summary = None
        run.failure_code = None
        await session.flush()
        return cancelled

    @staticmethod
    async def _apply_transition_controls(
        session: AsyncSession,
        run: Run,
        *,
        target: RunState,
        failure_code: str | None,
    ) -> None:
        if target is RunState.RUNNING:
            renewed = await QuotaManager.renew_run(
                session,
                organization_id=run.organization_id,
                run=run,
            )
            if not renewed:
                raise RuntimeError("active run has no quota reservation")
            return
        if target is RunState.CANCELLING:
            await AuditWriter.append(
                session,
                organization_id=run.organization_id,
                actor=AuditIdentity.service(ServiceIdentity.WORKER),
                action=AuditAction.RUN_CANCELLATION_REQUESTED,
                resource_type=AuditResourceType.RUN,
                resource_id=run.id,
                metadata={"prior_state": run.state.value},
            )
            return
        action = {
            RunState.SUCCEEDED: AuditAction.RUN_SUCCEEDED,
            RunState.FAILED: AuditAction.RUN_FAILED,
            RunState.CANCELLED: AuditAction.RUN_CANCELLED,
        }.get(target)
        if action is None:
            return
        await QuotaManager.release_run(
            session,
            organization_id=run.organization_id,
            run_id=run.id,
        )
        metadata: dict[str, Any] = {"kind": run.kind.value}
        if action is AuditAction.RUN_FAILED:
            metadata["failure_code"] = failure_code or "execution_failed"
        await AuditWriter.append(
            session,
            organization_id=run.organization_id,
            actor=AuditIdentity.service(ServiceIdentity.WORKER),
            action=action,
            resource_type=AuditResourceType.RUN,
            resource_id=run.id,
            metadata=metadata,
        )

    @staticmethod
    async def _record_terminal_audit(
        session: AsyncSession,
        run: Run,
        *,
        action: AuditAction,
        identity: ServiceIdentity,
    ) -> None:
        await QuotaManager.release_run(
            session,
            organization_id=run.organization_id,
            run_id=run.id,
        )
        await AuditWriter.append(
            session,
            organization_id=run.organization_id,
            actor=AuditIdentity.service(identity),
            action=action,
            resource_type=AuditResourceType.RUN,
            resource_id=run.id,
            metadata={"kind": run.kind.value},
        )

    @staticmethod
    def _verify_spec(run: Run, expected_hash: str) -> None:
        if run.spec_sha256 != expected_hash:
            raise RunStoreError("spec_integrity_violation")
        try:
            _, actual_hash = normalize_run_spec(dict(run.spec))
        except (TypeError, ValueError):
            raise RunStoreError("spec_integrity_violation") from None
        if actual_hash != expected_hash:
            raise RunStoreError("spec_integrity_violation")


def _ids(reference: RunWorkflowReference) -> tuple[UUID, UUID]:
    try:
        reference.validate()
        return UUID(reference.organization_id), UUID(reference.run_id)
    except ValueError:
        raise RunStoreError("invalid_workflow_reference") from None


def _artifact_result_summary(artifact_id: UUID, adopted: AdoptedArtifact) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact_id),
        "artifact_type": ArtifactKind.RUN_RESULT.value,
        "media_type": adopted.media_type,
        "schema_id": adopted.schema_id,
        "sha256": adopted.sha256,
        "size_bytes": adopted.size_bytes,
    }


def _same_datg_publication(
    existing: DatgIndexPublicationRecord,
    expected: AdoptedDatgIndex,
) -> bool:
    return (
        existing.cache_key_sha256 == expected.cache_key_sha256
        and existing.content_sha256 == expected.content_sha256
        and existing.runtime_id == expected.runtime_id
        and existing.language == expected.language
        and existing.unit == expected.unit
        and existing.vocabulary_size == expected.vocabulary_size
        and existing.indexed_token_count == expected.indexed_token_count
        and existing.size_bytes == expected.size_bytes
    )


def _safe_code(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 80
        and all(
            character.islower() or character.isdigit() or character == "_" for character in value
        )
    )


__all__ = [
    "AdoptedArtifact",
    "AdoptedDatgIndex",
    "ArtifactCommit",
    "DurableRunStore",
    "ExecutionRecord",
    "RunStoreError",
]
