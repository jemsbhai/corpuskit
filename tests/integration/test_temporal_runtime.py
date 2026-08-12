"""Opt-in acceptance against a real local Temporal server and SDK worker."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from temporalio.client import Client, WorkflowFailureError

from corpuskit.config import Settings
from corpuskit.domain.datg import (
    DatgIndexBuildRequest,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
)
from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedModelSelection,
    SecretReference,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import OutboxMessage
from corpuskit.services.jobs import (
    DEMO_PROJECT_ID,
    DispatchMessage,
    JobActor,
    JobControlPlane,
    RunSubmission,
    TransactionalOutbox,
)
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.worker.runtime import build_worker, temporal_client_protocol
from corpuskit.workflows.contracts import RunWorkflowReference, workflow_id
from corpuskit.workflows.dispatcher import TemporalDispatcher
from corpuskit.workflows.store import DurableRunStore

DEMO_ORGANIZATION_ID = UUID("00000000-0000-4000-8000-000000000001")


def _datg_policy() -> DatgRuntimePolicyEntry:
    pin = DatgSnapshotPin(
        repository_id="corpuskit/temporal-cancellation-fixture",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    return DatgRuntimePolicyEntry(
        runtime_id="temporal-cancellation-fixture",
        model=pin,
        tokenizer=pin,
        allowed_quantizations=(DatgQuantization.NONE,),
    )


def _blocking_datg_request() -> DatgIndexBuildRequest:
    return DatgIndexBuildRequest(
        runtime_id="temporal-cancellation-fixture",
        max_vocabulary_size=1,
        activity_timeout_seconds=10,
    )


def _temporal_address() -> str:
    address = os.getenv("CORPUSKIT_TEST_TEMPORAL_ADDRESS")
    if address is None:
        pytest.skip("CORPUSKIT_TEST_TEMPORAL_ADDRESS is not configured")
    return address


async def _database(tmp_path: Path, name: str) -> Database:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")
    await database.create_schema()
    return database


async def _submit(
    database: Database,
    *,
    kind: RunKind,
    spec: dict[str, Any],
    key: str,
) -> tuple[JobActor, RunWorkflowReference]:
    actor = JobActor(subject="demo-user", organization_id=DEMO_ORGANIZATION_ID)
    admission = ConfiguredRunAdmission.from_settings(
        Settings(
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
                ),
            ),
            worker_datg_runtime_policies=(_datg_policy(),),
        )
    )
    jobs = JobControlPlane(database, admission)
    await jobs.bootstrap_demo(actor, environment="test")
    submitted = await jobs.submit(
        actor,
        RunSubmission(project_id=DEMO_PROJECT_ID, kind=kind, spec=spec),
        idempotency_key=key,
    )
    return actor, RunWorkflowReference(
        organization_id=str(actor.organization_id),
        run_id=str(submitted.run.id),
        spec_sha256=submitted.run.spec_sha256,
    )


def _hosted_spec() -> dict[str, Any]:
    return HostedGenerationRequest(
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
        max_tokens_per_request=16,
        external_processing_confirmed=True,
        activity_timeout_seconds=3,
    ).model_dump(mode="json")


def _settings(database: Database, address: str) -> Settings:
    return Settings(
        environment="test",
        auth_mode="demo",
        database_url=str(database.engine.url),
        job_backend="temporal",
        temporal_address=address,
        temporal_activity_heartbeat_seconds=0.5,
        worker_graceful_shutdown_seconds=5,
        _env_file=None,
    )


async def _wait_for_state(
    jobs: JobControlPlane,
    actor: JobActor,
    run_id: UUID,
    state: RunState,
) -> None:
    async with asyncio.timeout(20):
        while (await jobs.get(actor, run_id)).state is not state:  # noqa: ASYNC110
            await asyncio.sleep(0.05)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_temporal_executes_once_and_history_excludes_persisted_text(
    tmp_path: Path,
) -> None:
    address = _temporal_address()
    database = await _database(tmp_path, "temporal-real.db")
    actor, reference = await _submit(
        database,
        kind=RunKind.PHONEMIZE,
        spec={"language": "en-us", "text": "history-secret-sentence"},
        key="real-temporal-phonemize",
    )
    settings = _settings(database, address)
    client = await Client.connect(address)
    dispatcher = TemporalDispatcher(
        temporal_client_protocol(client),
        task_queue=settings.temporal_task_queue,
        terminal_probe=DurableRunStore(database),
    )
    worker = build_worker(client, database, settings)
    jobs = JobControlPlane(database)
    try:
        async with worker:
            dispatched = await TransactionalOutbox(database).dispatch_batch(
                dispatcher, worker_id="real-temporal-dispatcher"
            )
            assert dispatched.published == 1
            handle = client.get_workflow_handle(workflow_id(reference), result_type=str)
            assert await asyncio.wait_for(handle.result(), timeout=30) == reference.run_id

            async with database.session() as session:
                row = await session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.run_id == UUID(reference.run_id),
                        OutboxMessage.event_type == "run.dispatch",
                    )
                )
                assert row is not None
                duplicate = DispatchMessage(
                    id=row.id,
                    organization_id=row.organization_id,
                    run_id=row.run_id,
                    event_type=row.event_type,
                    payload=dict(row.payload),
                    attempt=row.attempts + 1,
                )
            await dispatcher.publish(duplicate)

            history = [event async for event in handle.fetch_history_events()]
            serialized_history = "\n".join(str(event) for event in history)
            assert "history-secret-sentence" not in serialized_history
            assert '"language"' not in serialized_history
            assert serialized_history.count("workflow_execution_started_event_attributes") == 1

        run = await jobs.get(actor, UUID(reference.run_id))
        assert run.state is RunState.SUCCEEDED
        assert run.result_summary is not None
        assert run.result_summary["item_count"] == 1
        assert [event.event_type for event in await jobs.events(actor, UUID(reference.run_id))] == [
            "run.submitted",
            "run.provisioning",
            "run.started",
            "run.succeeded",
        ]
    finally:
        await database.dispose()


@dataclass(frozen=True, slots=True)
class BlockingExtensionHandler:
    late_marker: Path
    kind: RunKind = RunKind.BUILD_DATG_INDEX

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert DatgIndexBuildRequest.model_validate(spec) == _blocking_datg_request()
        time.sleep(3)
        self.late_marker.write_text("handler was not terminated", encoding="utf-8")
        return {"artifact_count": 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_temporal_cancellation_signal_stops_commit_and_reaches_cancelled(
    tmp_path: Path,
) -> None:
    address = _temporal_address()
    database = await _database(tmp_path, "temporal-cancel.db")
    actor, reference = await _submit(
        database,
        kind=RunKind.BUILD_DATG_INDEX,
        spec=_blocking_datg_request().model_dump(mode="json"),
        key="real-temporal-cancel",
    )
    settings = _settings(database, address)
    client = await Client.connect(address)
    late_marker = tmp_path / "late-result.txt"
    worker = build_worker(
        client,
        database,
        settings,
        additional_handlers=(BlockingExtensionHandler(late_marker),),
    )
    dispatcher = TemporalDispatcher(
        temporal_client_protocol(client),
        task_queue=settings.temporal_task_queue,
        terminal_probe=DurableRunStore(database),
    )
    jobs = JobControlPlane(database)
    run_id = UUID(reference.run_id)
    try:
        async with worker:
            await TransactionalOutbox(database).dispatch_batch(
                dispatcher, worker_id="real-temporal-cancel-dispatcher"
            )
            await _wait_for_state(jobs, actor, run_id, RunState.RUNNING)
            await jobs.request_cancellation(actor, run_id)
            result = await TransactionalOutbox(database).dispatch_batch(
                dispatcher, worker_id="real-temporal-cancel-dispatcher"
            )
            assert result.published == 1
            await _wait_for_state(jobs, actor, run_id, RunState.CANCELLED)
            handle = client.get_workflow_handle(workflow_id(reference), result_type=str)
            assert await asyncio.wait_for(handle.result(), timeout=20) == reference.run_id
            await asyncio.sleep(3.25)
            assert not late_marker.exists()

        run = await jobs.get(actor, run_id)
        assert run.state is RunState.CANCELLED
        assert run.result_summary is None
        assert "run.succeeded" not in [
            event.event_type for event in await jobs.events(actor, run_id)
        ]
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_temporal_unsupported_kind_fails_closed_with_safe_projection(
    tmp_path: Path,
) -> None:
    address = _temporal_address()
    database = await _database(tmp_path, "temporal-unsupported.db")
    actor, reference = await _submit(
        database,
        kind=RunKind.GENERATE_LLM,
        spec=_hosted_spec(),
        key="real-temporal-unsupported",
    )
    settings = _settings(database, address)
    client = await Client.connect(address)
    worker = build_worker(client, database, settings)
    dispatcher = TemporalDispatcher(
        temporal_client_protocol(client),
        task_queue=settings.temporal_task_queue,
        terminal_probe=DurableRunStore(database),
    )
    jobs = JobControlPlane(database)
    try:
        async with worker:
            result = await TransactionalOutbox(database).dispatch_batch(
                dispatcher, worker_id="real-temporal-unsupported-dispatcher"
            )
            assert result.published == 1
            handle = client.get_workflow_handle(workflow_id(reference), result_type=str)
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=30)

        run = await jobs.get(actor, UUID(reference.run_id))
        assert run.state is RunState.FAILED
        assert run.failure_code == "unsupported_run_kind"
        assert run.result_summary is None
    finally:
        await database.dispose()
