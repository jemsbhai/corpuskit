"""Database-backed durable activity lifecycle and fault tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from corpuskit.domain.artifacts import ContentDigest, DeterminismClass
from corpuskit.domain.errors import DependencyUnavailableError
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.reproducibility import TrustedExecutionFacts
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Run, RunEvent
from corpuskit.services.jobs import (
    DEMO_PROJECT_ID,
    JobActor,
    JobControlPlane,
    RunSubmission,
)
from corpuskit.services.reproducibility import ReproducibilityError
from corpuskit.workflows.activities import CoreRunActivities
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.progress import DurableRunProgress, RunProgressPhase
from corpuskit.workflows.store import DurableRunStore, RunStoreError

DEMO_ORGANIZATION_ID = UUID("00000000-0000-4000-8000-000000000001")


@dataclass(slots=True)
class StubHandler:
    kind: RunKind = RunKind.PHONEMIZE
    summary: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_retryable: bool = False
    calls: int = 0

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        self.calls += 1
        assert spec == {"language": "en-us", "text": "hello"}
        if self.failure_code is not None:
            raise RunExecutionError(
                self.failure_code,
                retryable=self.failure_retryable,
            )
        return self.summary or {"item_count": 1, "phoneme_count": 4}


@dataclass(slots=True)
class ProgressStubHandler:
    kind: RunKind = RunKind.PHONEMIZE

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        del spec
        raise AssertionError("progress-aware dispatch must use execute_with_progress")

    def execute_with_progress(
        self,
        spec: Mapping[str, Any],
        emit: Any,
    ) -> dict[str, Any]:
        assert spec == {"language": "en-us", "text": "hello"}
        emit(
            DurableRunProgress(
                sequence=0,
                phase=RunProgressPhase.GENERATING,
                completed=1,
                total=2,
            )
        )
        time.sleep(0.5)
        emit(
            DurableRunProgress(
                sequence=1,
                phase=RunProgressPhase.FINISHED,
                completed=2,
                total=2,
            )
        )
        return {"item_count": 1, "phoneme_count": 4}


@dataclass(slots=True)
class StubExecutionFacts:
    calls: list[tuple[RunKind, dict[str, Any]]]

    def for_run(
        self,
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> TrustedExecutionFacts:
        self.calls.append((kind, dict(spec)))
        return TrustedExecutionFacts(
            corpuskit_version="0.1.0a1",
            corpusgen_version="0.1.7",
            worker_profile="batch-cpu",
            worker_image_digest=f"sha256:{'a' * 64}",
            worker_policy=ContentDigest(
                name="worker-policy",
                sha256="b" * 64,
                size_bytes=16,
            ),
            determinism=DeterminismClass.EXACT,
        )


@dataclass(slots=True)
class StubManifestRecorder:
    events: list[str]
    record_error: Exception | None = None
    finalize_error: Exception | None = None

    async def record_execution(
        self,
        reference: RunWorkflowReference,
        facts: TrustedExecutionFacts,
    ) -> bool:
        assert reference.run_id
        assert facts.worker_profile == "batch-cpu"
        self.events.append("record")
        if self.record_error is not None:
            raise self.record_error
        return True

    async def finalize(self, reference: RunWorkflowReference) -> object:
        assert reference.run_id
        self.events.append("finalize")
        if self.finalize_error is not None:
            raise self.finalize_error
        return object()


@pytest_asyncio.fixture
async def temporal_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'temporal.db').as_posix()}")
    await database.create_schema()
    yield database
    await database.dispose()


async def _submitted(database: Database, suffix: str) -> tuple[JobActor, RunWorkflowReference]:
    actor = JobActor(subject="demo-user", organization_id=DEMO_ORGANIZATION_ID)
    jobs = JobControlPlane(database)
    await jobs.bootstrap_demo(actor, environment="test")
    result = await jobs.submit(
        actor,
        RunSubmission(
            project_id=DEMO_PROJECT_ID,
            kind=RunKind.PHONEMIZE,
            spec={"language": "en-us", "text": "hello"},
        ),
        idempotency_key=f"temporal-{suffix}",
    )
    return actor, RunWorkflowReference(
        organization_id=str(actor.organization_id),
        run_id=str(result.run.id),
        spec_sha256=result.run.spec_sha256,
    )


def _activities(
    database: Database,
    handler: StubHandler,
) -> CoreRunActivities:
    return CoreRunActivities(
        DurableRunStore(database),
        HandlerRegistry((handler,)),
        heartbeat_seconds=0.01,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_activity_success_is_monotonic_and_redelivery_is_idempotent(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "success")
    handler = StubHandler()
    activities = _activities(temporal_database, handler)
    environment = ActivityEnvironment()
    heartbeats: list[tuple[object, ...]] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)

    await environment.run(activities.prepare_run, reference)
    await environment.run(activities.execute_run, reference)
    await environment.run(activities.execute_run, reference)

    jobs = JobControlPlane(temporal_database)
    run = await jobs.get(actor, UUID(reference.run_id))
    events = await jobs.events(actor, UUID(reference.run_id))
    assert run.state is RunState.SUCCEEDED
    assert run.result_summary == {"item_count": 1, "phoneme_count": 4}
    assert run.failure_code is None
    assert [event.event_type for event in events] == [
        "run.submitted",
        "run.provisioning",
        "run.started",
        "run.succeeded",
    ]
    assert all(details == (reference,) for details in heartbeats)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_progress_is_queryable_before_completion_and_reconnects_in_order(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "progress-live")
    jobs = JobControlPlane(temporal_database)
    activities = CoreRunActivities(
        DurableRunStore(temporal_database),
        HandlerRegistry((ProgressStubHandler(),)),
        heartbeat_seconds=0.02,
    )
    environment = ActivityEnvironment()
    await environment.run(activities.prepare_run, reference)
    execution = asyncio.create_task(environment.run(activities.execute_run, reference))

    first_progress = None
    for _ in range(500):
        events = await jobs.events(actor, UUID(reference.run_id))
        first_progress = next(
            (event for event in events if event.event_type == "run.progress"),
            None,
        )
        if first_progress is not None:
            break
        await asyncio.sleep(0.01)
    assert first_progress is not None
    assert not execution.done()
    assert (await jobs.get(actor, UUID(reference.run_id))).state is RunState.RUNNING
    assert first_progress.payload == {
        "accepted_count": None,
        "activity_attempt": 1,
        "completed": 1,
        "coverage": None,
        "phase": "generating",
        "schema_version": 1,
        "sequence": 0,
        "total": 2,
    }
    assert not {
        "api_key",
        "credential",
        "prompt",
        "secret",
        "source_id",
        "text",
        "token",
    }.intersection(first_progress.payload)

    await execution
    all_events = await jobs.events(actor, UUID(reference.run_id), limit=100)
    reconnected = await jobs.events(
        actor,
        UUID(reference.run_id),
        after=first_progress.sequence,
        limit=100,
    )
    assert [event.sequence for event in all_events] == list(range(1, len(all_events) + 1))
    assert [event.event_type for event in all_events] == [
        "run.submitted",
        "run.provisioning",
        "run.started",
        "run.progress",
        "run.progress",
        "run.succeeded",
    ]
    assert [event.sequence for event in reconnected] == [
        first_progress.sequence + 1,
        first_progress.sequence + 2,
    ]

    progress_count = sum(event.event_type == "run.progress" for event in all_events)
    await environment.run(activities.execute_run, reference)
    redelivered = await jobs.events(actor, UUID(reference.run_id), limit=100)
    assert sum(event.event_type == "run.progress" for event in redelivered) == progress_count


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_never_appends_progress_after_cancellation(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "progress-cancel")
    store = DurableRunStore(temporal_database)
    jobs = JobControlPlane(temporal_database)
    assert await store.begin_execution(reference) is True
    assert await store.record_progress(
        reference,
        DurableRunProgress(sequence=0, phase=RunProgressPhase.TRAINING),
        activity_attempt=1,
    )
    await jobs.request_cancellation(actor, UUID(reference.run_id))
    assert not await store.record_progress(
        reference,
        DurableRunProgress(sequence=1, phase=RunProgressPhase.TRAINING),
        activity_attempt=1,
    )
    await store.acknowledge_cancellation(reference)

    events = await jobs.events(actor, UUID(reference.run_id))
    progress_events = [event for event in events if event.event_type == "run.progress"]
    assert [event.payload["sequence"] for event in progress_events] == [0]
    cancellation_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "run.cancellation_requested"
    )
    assert all(event.event_type != "run.progress" for event in events[cancellation_index + 1 :])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_deduplicates_stale_progress_and_accepts_monotonic_retry_attempt(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "progress-attempt-order")
    store = DurableRunStore(temporal_database)
    assert await store.begin_execution(reference) is True
    first = DurableRunProgress(sequence=0, phase=RunProgressPhase.TRAINING)
    second = DurableRunProgress(sequence=1, phase=RunProgressPhase.TRAINING)
    assert await store.record_progress(reference, first, activity_attempt=1)
    assert not await store.record_progress(reference, first, activity_attempt=1)
    assert await store.record_progress(reference, second, activity_attempt=1)
    assert await store.record_progress(reference, first, activity_attempt=2)
    assert not await store.record_progress(reference, second, activity_attempt=1)

    progress = [
        event
        for event in await JobControlPlane(temporal_database).events(
            actor,
            UUID(reference.run_id),
        )
        if event.event_type == "run.progress"
    ]
    assert [
        (event.payload["activity_attempt"], event.payload["sequence"]) for event in progress
    ] == [(1, 0), (1, 1), (2, 0)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_facts_are_parent_authored_before_compute_and_manifest_finalizes_once(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "manifest-lifecycle")
    facts = StubExecutionFacts([])
    recorder = StubManifestRecorder([])
    activities = CoreRunActivities(
        DurableRunStore(temporal_database),
        HandlerRegistry((StubHandler(),)),
        heartbeat_seconds=0.01,
        execution_facts=facts,
        manifest_recorder=recorder,
    )
    environment = ActivityEnvironment()

    await environment.run(activities.prepare_run, reference)
    await environment.run(activities.execute_run, reference)

    assert facts.calls == [(RunKind.PHONEMIZE, {"language": "en-us", "text": "hello"})]
    assert recorder.events == ["record", "finalize"]
    assert (
        await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))
    ).state is RunState.SUCCEEDED

    await environment.run(activities.execute_run, reference)
    assert len(facts.calls) == 1
    assert recorder.events == ["record", "finalize", "finalize"]


def test_execution_facts_and_manifest_recorder_are_an_atomic_configuration_pair() -> None:
    facts = StubExecutionFacts([])
    recorder = StubManifestRecorder([])
    store = cast(DurableRunStore, object())
    handlers = HandlerRegistry((StubHandler(),))

    with pytest.raises(ValueError, match="configured together"):
        CoreRunActivities(
            store,
            handlers,
            heartbeat_seconds=0.01,
            execution_facts=facts,
        )
    with pytest.raises(ValueError, match="configured together"):
        CoreRunActivities(
            store,
            handlers,
            heartbeat_seconds=0.01,
            manifest_recorder=recorder,
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    [
        (ReproducibilityError("execution_facts_conflict"), "execution_facts_conflict", False),
        (
            DependencyUnavailableError("manifest.storage"),
            "manifest_storage_unavailable",
            True,
        ),
    ],
)
async def test_execution_facts_failures_are_sanitized_and_never_run_the_handler(
    temporal_database: Database,
    error: Exception,
    expected_code: str,
    retryable: bool,
) -> None:
    actor, reference = await _submitted(
        temporal_database,
        f"manifest-error-{expected_code}",
    )
    recorder = StubManifestRecorder([], record_error=error)
    activities = CoreRunActivities(
        DurableRunStore(temporal_database),
        HandlerRegistry((StubHandler(),)),
        heartbeat_seconds=0.01,
        execution_facts=StubExecutionFacts([]),
        manifest_recorder=recorder,
    )

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities.execute_run, reference)

    assert raised.value.type == expected_code
    assert raised.value.non_retryable is not retryable
    run = await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))
    assert run.state is (RunState.RUNNING if retryable else RunState.FAILED)
    assert run.result_summary is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transient_failure_retries_then_commits_safe_terminal_code(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "retry")
    handler = StubHandler(
        failure_code="engine_unavailable",
        failure_retryable=True,
    )
    activities = _activities(temporal_database, handler)
    environment = ActivityEnvironment()
    await environment.run(activities.prepare_run, reference)

    with pytest.raises(ApplicationError) as first:
        await environment.run(activities.execute_run, reference)
    assert first.value.type == "engine_unavailable"
    assert first.value.non_retryable is False
    assert (await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))).state is (
        RunState.RUNNING
    )

    environment.info = replace(environment.info, attempt=3)
    with pytest.raises(ApplicationError) as last:
        await environment.run(activities.execute_run, reference)
    run = await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))
    assert last.value.type == "engine_unavailable"
    assert last.value.non_retryable is True
    assert run.state is RunState.FAILED
    assert run.failure_code == "engine_unavailable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_retryable_failure_and_finalizer_do_not_overwrite_first_failure(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "invalid")
    handler = StubHandler(failure_code="invalid_run_spec")
    activities = _activities(temporal_database, handler)
    environment = ActivityEnvironment()
    await environment.run(activities.prepare_run, reference)

    with pytest.raises(ApplicationError) as error:
        await environment.run(activities.execute_run, reference)
    await environment.run(activities.finalize_failure, reference)

    run = await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))
    assert error.value.non_retryable is True
    assert run.state is RunState.FAILED
    assert run.failure_code == "invalid_run_spec"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancellation_wins_before_execution_and_is_idempotent(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "cancel")
    jobs = JobControlPlane(temporal_database)
    await jobs.request_cancellation(actor, UUID(reference.run_id))
    handler = StubHandler()
    activities = _activities(temporal_database, handler)
    environment = ActivityEnvironment()

    await environment.run(activities.prepare_run, reference)
    await environment.run(activities.execute_run, reference)
    await environment.run(activities.finalize_cancellation, reference)

    run = await jobs.get(actor, UUID(reference.run_id))
    assert run.state is RunState.CANCELLED
    assert [event.event_type for event in await jobs.events(actor, UUID(reference.run_id))] == [
        "run.submitted",
        "run.cancellation_requested",
        "run.cancelled",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_and_spec_hash_mismatch_fail_closed_without_state_change(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "scope")
    activities = _activities(temporal_database, StubHandler())
    environment = ActivityEnvironment()
    foreign = RunWorkflowReference(
        organization_id=str(uuid4()),
        run_id=reference.run_id,
        spec_sha256=reference.spec_sha256,
    )
    tampered = RunWorkflowReference(
        organization_id=reference.organization_id,
        run_id=reference.run_id,
        spec_sha256="f" * 64,
    )

    with pytest.raises(ApplicationError) as hidden:
        await environment.run(activities.prepare_run, foreign)
    with pytest.raises(ApplicationError) as integrity:
        await environment.run(activities.prepare_run, tampered)

    assert hidden.value.type == "run_not_found"
    assert integrity.value.type == "spec_integrity_violation"
    run = await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))
    assert run.state is RunState.QUEUED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_spec_tampering_is_detected_before_handler_execution(
    temporal_database: Database,
) -> None:
    actor, reference = await _submitted(temporal_database, "tamper")
    async with temporal_database.session() as session:
        await session.execute(
            update(Run)
            .where(Run.id == UUID(reference.run_id))
            .values(spec={"language": "en-us", "text": "changed"})
        )
    handler = StubHandler()
    environment = ActivityEnvironment()
    activities = _activities(temporal_database, handler)

    with pytest.raises(ApplicationError) as error:
        await environment.run(activities.prepare_run, reference)

    assert error.value.type == "spec_integrity_violation"
    async with temporal_database.session() as session:
        events = (
            await session.scalars(select(RunEvent).where(RunEvent.run_id == UUID(reference.run_id)))
        ).all()
    assert len(events) == 1
    assert (await JobControlPlane(temporal_database).get(actor, UUID(reference.run_id))).state is (
        RunState.QUEUED
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_activity_persistence_failures_expose_only_stable_safe_codes(
    temporal_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, reference = await _submitted(temporal_database, "persistence-errors")
    store = DurableRunStore(temporal_database)
    activities = CoreRunActivities(store, HandlerRegistry((StubHandler(),)), heartbeat_seconds=0.01)
    environment = ActivityEnvironment()

    async def unavailable(_reference: RunWorkflowReference, *_args: object) -> RunState:
        raise OSError("database details must not escape")

    async def invalid_state(_reference: RunWorkflowReference, *_args: object) -> RunState:
        raise RunStoreError("invalid_run_state")

    monkeypatch.setattr(store, "prepare", unavailable)
    with pytest.raises(ApplicationError) as prepare_unavailable:
        await environment.run(activities.prepare_run, reference)
    assert prepare_unavailable.value.type == "persistence_unavailable"
    assert prepare_unavailable.value.non_retryable is False

    monkeypatch.setattr(store, "fail", unavailable)
    with pytest.raises(ApplicationError) as failure_unavailable:
        await environment.run(activities.finalize_failure, reference)
    assert failure_unavailable.value.type == "persistence_unavailable"

    monkeypatch.setattr(store, "acknowledge_cancellation", unavailable)
    with pytest.raises(ApplicationError) as cancellation_unavailable:
        await environment.run(activities.finalize_cancellation, reference)
    assert cancellation_unavailable.value.type == "persistence_unavailable"

    monkeypatch.setattr(store, "fail", invalid_state)
    with pytest.raises(ApplicationError) as failure_contract:
        await environment.run(activities.finalize_failure, reference)
    assert failure_contract.value.type == "invalid_run_state"
    assert failure_contract.value.non_retryable is True

    monkeypatch.setattr(store, "acknowledge_cancellation", invalid_state)
    with pytest.raises(ApplicationError) as cancellation_contract:
        await environment.run(activities.finalize_cancellation, reference)
    assert cancellation_contract.value.type == "invalid_run_state"
    assert cancellation_contract.value.non_retryable is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_edge_states_are_monotonic_and_first_terminal_result_wins(
    temporal_database: Database,
) -> None:
    actor, successful = await _submitted(temporal_database, "store-success")
    store = DurableRunStore(temporal_database)

    assert await store.begin_execution(successful) is True
    assert await store.prepare(successful) is RunState.RUNNING
    assert await store.complete(successful, {"count": 1}) is RunState.SUCCEEDED
    assert await store.prepare(successful) is RunState.SUCCEEDED
    assert await store.complete(successful, {"count": 2}) is RunState.SUCCEEDED
    assert await store.fail(successful, "late_failure") is RunState.SUCCEEDED
    assert await store.request_cancellation(successful) is RunState.SUCCEEDED
    assert await store.acknowledge_cancellation(successful) is RunState.SUCCEEDED
    assert await store.is_terminal(successful) is True
    assert (
        await JobControlPlane(temporal_database).get(actor, UUID(successful.run_id))
    ).result_summary == {"count": 1}

    _, invalid_state = await _submitted(temporal_database, "store-draft")
    async with temporal_database.session() as session:
        await session.execute(
            update(Run).where(Run.id == UUID(invalid_state.run_id)).values(state=RunState.DRAFT)
        )
    with pytest.raises(RunStoreError, match="invalid_run_state"):
        await store.prepare(invalid_state)

    _, invalid_failure = await _submitted(temporal_database, "store-failure")
    assert await store.prepare(invalid_failure) is RunState.PROVISIONING
    assert await store.complete(invalid_failure, {"unused": True}) is RunState.PROVISIONING
    assert await store.fail(invalid_failure, "NOT SAFE") is RunState.FAILED
    failed = await JobControlPlane(temporal_database).get(actor, UUID(invalid_failure.run_id))
    assert failed.failure_code == "execution_failed"

    _, cancellation = await _submitted(temporal_database, "store-cancel-race")
    assert await store.begin_execution(cancellation) is True
    assert await store.request_cancellation(cancellation) is RunState.CANCELLING
    assert await store.begin_execution(cancellation) is False
    assert await store.complete(cancellation, {"must_not_commit": True}) is RunState.CANCELLED

    _, malformed = await _submitted(temporal_database, "store-malformed")
    async with temporal_database.session() as session:
        await session.execute(
            update(Run)
            .where(Run.id == UUID(malformed.run_id))
            .values(spec={"token": "credential-like-value"})
        )
    with pytest.raises(RunStoreError, match="spec_integrity_violation"):
        await store.execution_record(malformed)
