"""Real PostgreSQL acceptance for split-role manifest and replay transactions."""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ContentDigest,
    DeterminismClass,
    ReplayVerdict,
    StagedArtifactResult,
    staged_artifact_storage_key,
)
from corpuskit.domain.generation import (
    GenerationStoppingCriteria,
    GenerationStopReason,
    GenerationTarget,
)
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.model_runtime import (
    ImmutableModelPin,
    LocalGenerationRequest,
    LocalGenerationResult,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelExecutionManifest,
    ModelQuantization,
    ReproducibilityClass,
)
from corpuskit.domain.reproducibility import ReplayLifecycle, TrustedExecutionFacts
from corpuskit.persistence.artifact_store import InMemoryObjectStore
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import RunExecutionFact, RunReplay
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.artifact_adoption import ArtifactAdoptionService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.services.reproducibility import ReproducibilityActor, RunManifestService
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.store import DurableRunStore

OWNER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_OWNER_URL")
APP_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_APP_URL")
WORKER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_WORKER_URL")
ADOPTION_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_ADOPTION_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all((OWNER_URL, APP_URL, WORKER_URL, ADOPTION_URL)),
        reason="split PostgreSQL API/worker/adoption roles are not configured",
    ),
]


def _facts() -> TrustedExecutionFacts:
    return TrustedExecutionFacts(
        corpuskit_version="0.1.0a1",
        corpusgen_version="0.1.7",
        worker_profile="batch-cpu",
        worker_image_digest=f"sha256:{'a' * 64}",
        worker_policy=ContentDigest(name="worker-policy", sha256="b" * 64, size_bytes=128),
        determinism=DeterminismClass.EXACT,
    )


@pytest.mark.asyncio
async def test_split_roles_construct_and_replay_without_rls_bypass() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    assert WORKER_URL is not None
    assert ADOPTION_URL is not None
    owner = Database(OWNER_URL)
    api = Database(APP_URL)
    worker = Database(WORKER_URL)
    adoption = Database(ADOPTION_URL)
    settings = Settings(environment="test", database_url=APP_URL, _env_file=None)
    owner_jobs = JobControlPlane(owner)
    api_jobs = JobControlPlane(api)
    runs = DurableRunStore(worker)
    manifests = RunManifestService(
        api,
        InMemoryObjectStore(),
        settings,
        worker_database=worker,
        adoption_database=adoption,
        expected_corpuskit_version="0.1.0a1",
        expected_corpusgen_version="0.1.7",
    )
    job_actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    repro_actor = ReproducibilityActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    try:
        await owner_jobs.bootstrap_demo(job_actor, environment="test")
        source = await api_jobs.submit(
            job_actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.EVALUATE,
                spec={
                    "sentences": ["A valid replayable sentence."],
                    "language": "en-us",
                    "unit": "phoneme",
                    "target": {"mode": "derived", "phonemes": []},
                },
            ),
            idempotency_key=f"pg-source-{uuid4()}",
        )
        source_reference = RunWorkflowReference(
            str(DEMO_PRINCIPAL.organization_id),
            str(source.run.id),
            source.run.spec_sha256,
        )
        assert await runs.begin_execution(source_reference)
        assert await manifests.record_execution(source_reference, _facts())
        await runs.complete(source_reference, {"count": 1})
        source_manifest = await manifests.finalize(source_reference)

        submitted = await manifests.submit_replay(
            repro_actor,
            project_id=DEMO_PROJECT_ID,
            source_run_id=source.run.id,
            idempotency_key=f"pg-replay-{uuid4()}",
        )
        replay_reference = RunWorkflowReference(
            str(DEMO_PRINCIPAL.organization_id),
            str(submitted.replay.replay_run_id),
            source.run.spec_sha256,
        )
        assert await runs.begin_execution(replay_reference)
        assert await manifests.record_execution(replay_reference, _facts())
        await runs.complete(replay_reference, {"count": 1})
        replay_manifest = await manifests.finalize(replay_reference)
        status = await manifests.get_replay(repro_actor, submitted.replay.replay_run_id)

        assert source_manifest.artifact_id != replay_manifest.artifact_id
        assert status.lifecycle is ReplayLifecycle.COMPARED
        assert status.comparison is not None
        assert status.comparison.verdict is ReplayVerdict.EXACT_MATCH

        async def forge_execution_facts() -> None:
            async with api.session(
                TenantContext.user(
                    DEMO_PRINCIPAL.organization_id,
                    DEMO_PRINCIPAL.subject,
                )
            ) as session:
                session.add(
                    RunExecutionFact(
                        run_id=uuid4(),
                        organization_id=DEMO_PRINCIPAL.organization_id,
                        project_id=DEMO_PROJECT_ID,
                        facts={},
                        facts_sha256="f" * 64,
                        input_digests=[],
                    )
                )
                await session.flush()

        with pytest.raises(DBAPIError):
            await forge_execution_facts()
        async with api.session(
            TenantContext.user(DEMO_PRINCIPAL.organization_id, DEMO_PRINCIPAL.subject)
        ) as session:
            assert (
                await session.scalar(
                    select(RunExecutionFact).where(RunExecutionFact.run_id == source.run.id)
                )
                is not None
            )

        async def tamper_execution_facts() -> None:
            async with adoption.session(
                TenantContext.service(
                    ServiceIdentity.ADOPTION,
                    DEMO_PRINCIPAL.organization_id,
                )
            ) as session:
                await session.execute(
                    update(RunExecutionFact)
                    .where(RunExecutionFact.run_id == source.run.id)
                    .values(facts={"forged": True})
                )

        async def tamper_replay_comparison() -> None:
            async with adoption.session(
                TenantContext.service(
                    ServiceIdentity.ADOPTION,
                    DEMO_PRINCIPAL.organization_id,
                )
            ) as session:
                await session.execute(
                    update(RunReplay)
                    .where(RunReplay.replay_run_id == submitted.replay.replay_run_id)
                    .values(comparison={"forged": True})
                )

        with pytest.raises(DBAPIError):
            await tamper_execution_facts()
        with pytest.raises(DBAPIError):
            await tamper_replay_comparison()
    finally:
        await owner.dispose()
        await api.dispose()
        await worker.dispose()
        await adoption.dispose()


@pytest.mark.asyncio
async def test_worker_reads_and_adoption_role_atomically_publishes_staged_result() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    assert WORKER_URL is not None
    assert ADOPTION_URL is not None
    owner = Database(OWNER_URL)
    api = Database(APP_URL)
    worker = Database(WORKER_URL)
    adoption = Database(ADOPTION_URL)
    objects = InMemoryObjectStore()
    settings = Settings(
        environment="test",
        database_url=APP_URL,
        worker_local_model_policies=(
            LocalModelPolicy(
                pin=ImmutableModelPin(model="acme/tiny-model", revision="a" * 40),
                artifact_sha256="b" * 64,
                allowed_devices=(ModelDevice.CPU,),
                allowed_quantizations=(ModelQuantization.NONE,),
            ),
        ),
        _env_file=None,
    )
    owner_jobs = JobControlPlane(owner)
    jobs = JobControlPlane(api, ConfiguredRunAdmission.from_settings(settings))
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    worker_runs = DurableRunStore(worker)
    adoption_runs = DurableRunStore(adoption)
    adopter = ArtifactAdoptionService(
        worker_runs,
        objects,
        settings,
        adoption_runs=adoption_runs,
    )
    request = LocalGenerationRequest(
        selection=LocalModelSelection(
            pin=ImmutableModelPin(model="acme/tiny-model", revision="a" * 40)
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1,
        ),
        activity_timeout_seconds=10,
    )
    result = LocalGenerationResult(
        model=ModelExecutionManifest(
            model="acme/tiny-model",
            revision="a" * 40,
            artifact_sha256="b" * 64,
            device=ModelDevice.CPU,
            quantization=ModelQuantization.NONE,
            sampling_enabled=False,
            seed=0,
        ),
        accepted=(),
        coverage=0,
        covered_units=(),
        missing_units=("p",),
        iterations=0,
        elapsed_seconds=0,
        stop_reason=GenerationStopReason.BACKEND_EXHAUSTED,
        reproducibility=ReproducibilityClass.BEST_EFFORT,
    )
    payload = result.model_dump_json().encode()
    digest = hashlib.sha256(payload).hexdigest()
    try:
        await owner_jobs.bootstrap_demo(actor, environment="test")
        submitted = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.GENERATE_LOCAL,
                spec=request.model_dump(mode="json"),
            ),
            idempotency_key=f"pg-adoption-{uuid4()}",
        )
        reference = RunWorkflowReference(
            str(actor.organization_id),
            str(submitted.run.id),
            submitted.run.spec_sha256,
        )
        assert await worker_runs.begin_execution(reference)
        await objects.put(
            key=staged_artifact_storage_key(digest),
            content=payload,
            sha256=digest,
            media_type="application/json",
        )
        claim = StagedArtifactResult(
            staged_artifact_ref=f"staged-artifact://sha256/{digest}",
            schema_id="corpuskit.local-generation-result.v1",
            artifact_type="run-result",
            media_type="application/json",
            size_bytes=len(payload),
        )

        committed = await adopter.adopt(reference, claim.model_dump(mode="json"))
        projected = await jobs.get(actor, submitted.run.id)

        assert committed.state is RunState.SUCCEEDED
        assert committed.created is True
        assert committed.artifact_id is not None
        assert projected.state is RunState.SUCCEEDED
        assert projected.result_summary is not None
        assert projected.result_summary["sha256"] == digest
    finally:
        await owner.dispose()
        await api.dispose()
        await worker.dispose()
        await adoption.dispose()
