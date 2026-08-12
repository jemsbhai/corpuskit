"""Opt-in end-to-end manifest/replay acceptance across production adapters.

The driver runs outside the worker image.  The caller must start a worker from
the image whose inspected digest is supplied through ``CORPUSKIT_TEST_WORKER_IMAGE_DIGEST``.
That keeps this test from claiming image provenance for an in-process test worker.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from temporalio.client import Client

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind, ReplayVerdict, RunManifest
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.reproducibility import ReplayLifecycle
from corpuskit.domain.selection import CorpusSelectionArtifactV1, SelectionAlgorithm
from corpuskit.persistence.artifact_store import ObjectStore, build_object_store
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact, OutboxMessage, RunExecutionFact
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.jobs import (
    DEMO_PROJECT_ID,
    DispatchMessage,
    JobActor,
    JobControlPlane,
    RunSubmission,
    TransactionalOutbox,
)
from corpuskit.services.reproducibility import ReproducibilityActor, RunManifestService
from corpuskit.worker.runtime import temporal_client_protocol
from corpuskit.workflows.contracts import RunWorkflowReference, workflow_id
from corpuskit.workflows.dispatcher import TemporalDispatcher
from corpuskit.workflows.store import DurableRunStore

_OWNER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_OWNER_URL")
_APP_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_APP_URL")
_DISPATCHER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_DISPATCHER_URL")
_WORKER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_WORKER_URL")
_ADOPTION_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_ADOPTION_URL")
_TEMPORAL_ADDRESS = os.getenv("CORPUSKIT_TEST_TEMPORAL_ADDRESS")
_S3_ENDPOINT = os.getenv("CORPUSKIT_TEST_S3_ENDPOINT")
_WORKER_IMAGE_DIGEST = os.getenv("CORPUSKIT_TEST_WORKER_IMAGE_DIGEST")

_REQUIRED_ENVIRONMENT = {
    "CORPUSKIT_TEST_POSTGRES_OWNER_URL": _OWNER_URL,
    "CORPUSKIT_TEST_POSTGRES_APP_URL": _APP_URL,
    "CORPUSKIT_TEST_POSTGRES_DISPATCHER_URL": _DISPATCHER_URL,
    "CORPUSKIT_TEST_POSTGRES_WORKER_URL": _WORKER_URL,
    "CORPUSKIT_TEST_POSTGRES_ADOPTION_URL": _ADOPTION_URL,
    "CORPUSKIT_TEST_TEMPORAL_ADDRESS": _TEMPORAL_ADDRESS,
    "CORPUSKIT_TEST_S3_ENDPOINT": _S3_ENDPOINT,
    "CORPUSKIT_TEST_WORKER_IMAGE_DIGEST": _WORKER_IMAGE_DIGEST,
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(_REQUIRED_ENVIRONMENT.values()),
        reason="combined PostgreSQL/Temporal/MinIO runtime is not configured",
    ),
]


def _settings() -> Settings:
    assert _WORKER_URL is not None
    assert _ADOPTION_URL is not None
    assert _TEMPORAL_ADDRESS is not None
    assert _S3_ENDPOINT is not None
    assert _WORKER_IMAGE_DIGEST is not None
    return Settings(
        environment="test",
        auth_mode="demo",
        database_url=_WORKER_URL,
        adoption_database_url=_ADOPTION_URL,
        job_backend="temporal",
        temporal_address=_TEMPORAL_ADDRESS,
        temporal_activity_heartbeat_seconds=0.5,
        artifact_backend="s3",
        artifact_s3_endpoint=_S3_ENDPOINT,
        artifact_s3_bucket=os.getenv("CORPUSKIT_TEST_S3_BUCKET", "corpuskit-combined-artifacts"),
        artifact_s3_region="us-east-1",
        artifact_s3_access_key_id=os.getenv("CORPUSKIT_TEST_S3_ACCESS_KEY", "corpuskit-combined"),
        artifact_s3_secret_access_key=os.getenv(
            "CORPUSKIT_TEST_S3_SECRET_KEY", "combined-minio-only"
        ),
        artifact_s3_path_style=True,
        worker_profile="batch-cpu",
        worker_image_digest=_WORKER_IMAGE_DIGEST,
        _env_file=None,
    )


def _spec() -> dict[str, Any]:
    return {
        "candidates": [
            "A deterministic combined-stack selection candidate.",
            "Replay must preserve this exact seeded recipe.",
            "Sphinx of black quartz, judge my vow.",
        ],
        "language": "en-us",
        "unit": "phoneme",
        "target": {"mode": "derived", "phonemes": []},
        "options": {
            "algorithm": "stochastic",
            "max_sentences": 2,
            "target_coverage": 1.0,
            "epsilon": 0.2,
            "seed": 424242,
        },
    }


async def _dispatch_message(owner: Database, run_id: UUID) -> DispatchMessage:
    async with owner.session(
        TenantContext.service(ServiceIdentity.PLATFORM, DEMO_PRINCIPAL.organization_id)
    ) as session:
        row = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.run_id == run_id,
                OutboxMessage.event_type == "run.dispatch",
            )
        )
    assert row is not None
    return DispatchMessage(
        id=row.id,
        organization_id=row.organization_id,
        run_id=row.run_id,
        event_type=row.event_type,
        payload=dict(row.payload),
        attempt=row.attempts + 1,
    )


async def _dispatch_and_wait(
    *,
    owner: Database,
    outbox: TransactionalOutbox,
    dispatcher: TemporalDispatcher,
    client: Client,
    reference: RunWorkflowReference,
    worker_id: str,
) -> str:
    message = await _dispatch_message(owner, UUID(reference.run_id))
    result = await outbox.dispatch_batch(dispatcher, worker_id=worker_id)
    assert (result.claimed, result.published, result.failed) == (1, 1, 0)

    # Simulate a dispatcher crash after Temporal accepted the request but before
    # the outbox acknowledgement became durable.  The duplicate must converge on
    # the same deterministic workflow rather than execute a second run.
    await dispatcher.publish(message)
    handle = client.get_workflow_handle(workflow_id(reference), result_type=str)
    assert await asyncio.wait_for(handle.result(), timeout=60) == reference.run_id

    history = [event async for event in handle.fetch_history_events()]
    serialized = "\n".join(str(event) for event in history)
    assert "combined-stack selection candidate" not in serialized.lower()
    assert '"candidates"' not in serialized
    assert serialized.count("workflow_execution_started_event_attributes") == 1
    return serialized


async def _artifact_payload(store: ObjectStore, artifact: Artifact) -> bytes:
    descriptor = await store.stat(artifact.storage_key)
    assert descriptor.sha256 == artifact.sha256
    assert descriptor.size_bytes == artifact.size_bytes
    opened = await store.open(artifact.storage_key, chunk_bytes=257)
    payload = b"".join([chunk async for chunk in opened.chunks])
    assert len(payload) == artifact.size_bytes
    return payload


async def _assert_private(endpoint: str, bucket: str, key: str) -> None:
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        response = await client.get(f"{endpoint.rstrip('/')}/{bucket}/{key}")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_seeded_selection_manifest_replay_crosses_postgres_temporal_and_private_minio() -> (
    None
):
    assert _OWNER_URL is not None
    assert _APP_URL is not None
    assert _DISPATCHER_URL is not None
    assert _WORKER_URL is not None
    assert _ADOPTION_URL is not None
    assert _TEMPORAL_ADDRESS is not None
    assert _S3_ENDPOINT is not None
    assert _WORKER_IMAGE_DIGEST is not None

    settings = _settings()
    owner = Database(_OWNER_URL)
    api = Database(_APP_URL)
    dispatcher_database = Database(_DISPATCHER_URL)
    worker_probe_database = Database(_WORKER_URL)
    store = build_object_store(settings)
    jobs = JobControlPlane(api)
    owner_jobs = JobControlPlane(owner)
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    reproduction_actor = ReproducibilityActor(
        DEMO_PRINCIPAL.subject,
        DEMO_PRINCIPAL.organization_id,
        request_id=f"combined-{uuid4()}",
    )
    manifests = RunManifestService(api, store, settings)
    temporal = await Client.connect(_TEMPORAL_ADDRESS)
    dispatcher = TemporalDispatcher(
        temporal_client_protocol(temporal),
        task_queue="batch-cpu",
        terminal_probe=DurableRunStore(worker_probe_database),
    )
    outbox = TransactionalOutbox(dispatcher_database)

    try:
        await owner_jobs.bootstrap_demo(actor, environment="test")
        source = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.SELECT,
                spec=_spec(),
            ),
            idempotency_key=f"combined-source-{uuid4()}",
        )
        source_reference = RunWorkflowReference(
            organization_id=str(actor.organization_id),
            run_id=str(source.run.id),
            spec_sha256=source.run.spec_sha256,
        )
        await _dispatch_and_wait(
            owner=owner,
            outbox=outbox,
            dispatcher=dispatcher,
            client=temporal,
            reference=source_reference,
            worker_id="combined-source-dispatcher",
        )
        assert (await jobs.get(actor, source.run.id)).state is RunState.SUCCEEDED

        replay = await manifests.submit_replay(
            reproduction_actor,
            project_id=DEMO_PROJECT_ID,
            source_run_id=source.run.id,
            idempotency_key=f"combined-replay-{uuid4()}",
        )
        replay_reference = RunWorkflowReference(
            organization_id=str(actor.organization_id),
            run_id=str(replay.replay.replay_run_id),
            spec_sha256=source.run.spec_sha256,
        )
        await _dispatch_and_wait(
            owner=owner,
            outbox=outbox,
            dispatcher=dispatcher,
            client=temporal,
            reference=replay_reference,
            worker_id="combined-replay-dispatcher",
        )

        status = await manifests.get_replay(reproduction_actor, replay.replay.replay_run_id)
        assert status.lifecycle is ReplayLifecycle.COMPARED
        assert status.comparison is not None
        assert status.comparison.verdict is ReplayVerdict.EXACT_MATCH
        assert status.comparison.replay_inputs_match is True
        assert status.comparison.outputs_match is True

        async with owner.session(
            TenantContext.service(ServiceIdentity.PLATFORM, actor.organization_id)
        ) as session:
            facts = tuple(
                await session.scalars(
                    select(RunExecutionFact)
                    .where(
                        RunExecutionFact.run_id.in_((source.run.id, replay.replay.replay_run_id))
                    )
                    .order_by(RunExecutionFact.run_id)
                )
            )
            artifacts = tuple(
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.run_id.in_((source.run.id, replay.replay.replay_run_id)))
                    .order_by(Artifact.kind, Artifact.run_id)
                )
            )

        assert len(facts) == 2
        assert all(item.facts["worker_image_digest"] == _WORKER_IMAGE_DIGEST for item in facts)
        manifest_artifacts = tuple(
            item for item in artifacts if item.kind == ArtifactKind.RUN_MANIFEST.value
        )
        result_artifacts = tuple(
            item for item in artifacts if item.kind == ArtifactKind.RUN_RESULT.value
        )
        assert len(manifest_artifacts) == 2
        assert len(result_artifacts) == 2

        parsed: list[RunManifest] = []
        for artifact in manifest_artifacts:
            payload = await _artifact_payload(store, artifact)
            manifest = RunManifest.model_validate_json(payload, strict=True)
            assert manifest.canonical_bytes() == payload
            assert manifest.sha256 == artifact.sha256
            assert manifest.worker_image_digest == _WORKER_IMAGE_DIGEST
            assert manifest.unit == "phoneme"
            assert manifest.seed == 424242
            assert manifest.parameters["unit"] == "phoneme"
            assert manifest.parameters["options"]["algorithm"] == "stochastic"
            assert manifest.parameters["options"]["seed"] == 424242
            parsed.append(manifest)
            await _assert_private(
                _S3_ENDPOINT,
                settings.artifact_s3_bucket,
                artifact.storage_key,
            )

        result_payloads: list[bytes] = []
        for artifact in result_artifacts:
            payload = await _artifact_payload(store, artifact)
            selection = CorpusSelectionArtifactV1.model_validate_json(payload, strict=True)
            assert selection.canonical_bytes() == payload
            assert selection.algorithm is SelectionAlgorithm.STOCHASTIC
            assert selection.unit.value == "phoneme"
            assert selection.metadata.seed == 424242
            assert selection.selected_sentences
            result_payloads.append(payload)
            await _assert_private(
                _S3_ENDPOINT,
                settings.artifact_s3_bucket,
                artifact.storage_key,
            )

        assert parsed[0].run_id != parsed[1].run_id
        assert parsed[0].parameters == parsed[1].parameters
        assert parsed[0].input_digests == parsed[1].input_digests
        assert parsed[0].output_digests == parsed[1].output_digests
        assert result_payloads[0] == result_payloads[1]
    finally:
        await owner.dispose()
        await api.dispose()
        await dispatcher_database.dispose()
        await worker_probe_database.dispose()
