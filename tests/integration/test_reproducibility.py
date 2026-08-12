"""Authoritative manifest construction and replay coordinator acceptance tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ArtifactKind,
    ArtifactState,
    ContentDigest,
    DeterminismClass,
    ReplayVerdict,
    artifact_storage_key,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.platform import AuditAction
from corpuskit.domain.reproducibility import ReplayLifecycle, TrustedExecutionFacts
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.artifact_store import InMemoryObjectStore
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
    AuditEvent,
    Corpus,
    CorpusVersion,
    Membership,
    Project,
    Role,
    Run,
    RunEvent,
    RunExecutionFact,
    RunReplay,
    Sentence,
    User,
)
from corpuskit.services.jobs import (
    DEMO_PROJECT_ID,
    JobActor,
    JobControlPlane,
    RunSubmission,
)
from corpuskit.services.reproducibility import (
    ReproducibilityActor,
    ReproducibilityError,
    RunManifestService,
    _artifact_record,
)
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.store import DurableRunStore

KIT_VERSION = "0.1.0a1"
GEN_VERSION = "0.1.7"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'replay.db').as_posix()}",
        artifact_max_bytes=1024 * 1024,
        artifact_download_chunk_bytes=16 * 1024,
        _env_file=None,
    )


def _actor() -> ReproducibilityActor:
    return ReproducibilityActor(
        DEMO_PRINCIPAL.subject,
        DEMO_PRINCIPAL.organization_id,
        request_id="request-replay-1",
    )


def _facts(
    classification: DeterminismClass = DeterminismClass.EXACT,
) -> TrustedExecutionFacts:
    return TrustedExecutionFacts(
        corpuskit_version=KIT_VERSION,
        corpusgen_version=GEN_VERSION,
        worker_profile="batch-cpu",
        worker_image_digest=f"sha256:{'a' * 64}",
        worker_policy=ContentDigest(
            name="worker-policy",
            sha256="b" * 64,
            size_bytes=128,
        ),
        determinism=classification,
    )


async def _stack(
    tmp_path: Path,
) -> tuple[Database, JobControlPlane, DurableRunStore, RunManifestService, InMemoryObjectStore]:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    jobs = JobControlPlane(database)
    await jobs.bootstrap_demo(
        JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id),
        environment="test",
    )
    store = InMemoryObjectStore()
    manifests = RunManifestService(
        database,
        store,
        settings,
        expected_corpuskit_version=KIT_VERSION,
        expected_corpusgen_version=GEN_VERSION,
    )
    return database, jobs, DurableRunStore(database), manifests, store


async def _submitted(
    jobs: JobControlPlane,
    *,
    key: str,
    corpus_version_id: UUID | None = None,
) -> tuple[UUID, RunWorkflowReference]:
    result = await jobs.submit(
        JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id),
        RunSubmission(
            project_id=DEMO_PROJECT_ID,
            corpus_version_id=corpus_version_id,
            kind=RunKind.EVALUATE,
            spec={
                "sentences": ["A valid replayable sentence."],
                "language": "en-us",
                "unit": "phoneme",
                "target": {"mode": "derived", "phonemes": []},
            },
        ),
        idempotency_key=key,
    )
    return result.run.id, RunWorkflowReference(
        str(DEMO_PRINCIPAL.organization_id),
        str(result.run.id),
        result.run.spec_sha256,
    )


async def _persist_artifact(
    database: Database,
    store: InMemoryObjectStore,
    *,
    kind: ArtifactKind,
    content: bytes,
    run_id: UUID | None,
) -> UUID:
    digest = hashlib.sha256(content).hexdigest()
    key = artifact_storage_key(
        organization_id=DEMO_PRINCIPAL.organization_id,
        project_id=DEMO_PROJECT_ID,
        run_id=run_id,
        kind=kind,
        sha256=digest,
    )
    await store.put(key=key, content=content, sha256=digest, media_type="application/json")
    artifact_id = uuid4()
    async with database.session() as session:
        created_by = await session.scalar(
            select(User.id).where(User.oidc_subject == DEMO_PRINCIPAL.subject)
        )
        assert created_by is not None
        session.add(
            Artifact(
                id=artifact_id,
                organization_id=DEMO_PRINCIPAL.organization_id,
                project_id=DEMO_PROJECT_ID,
                run_id=run_id,
                created_by=created_by,
                scope_key=str(run_id) if run_id is not None else "project",
                kind=kind.value,
                sha256=digest,
                size_bytes=len(content),
                storage_key=key,
                media_type="application/json",
                filename=f"{kind.value}.json",
                state=ArtifactState.ACTIVE,
                retention_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
    return artifact_id


async def _finish(
    runs: DurableRunStore,
    manifests: RunManifestService,
    reference: RunWorkflowReference,
    facts: TrustedExecutionFacts,
    *,
    value: int = 1,
) -> None:
    assert await runs.begin_execution(reference) is True
    assert await manifests.record_execution(reference, facts) is True
    assert await manifests.record_execution(reference, facts) is False
    await runs.complete(reference, {"count": value})


async def _mark_demo_project_pending(database: Database) -> None:
    requested_at = datetime.now(UTC)
    async with database.session() as session:
        project = await session.get(Project, DEMO_PROJECT_ID)
        assert project is not None
        project.lifecycle_state = ProjectLifecycle.DELETION_PENDING
        project.deletion_requested_at = requested_at
        project.deletion_retention_until = requested_at + timedelta(days=30)
        project.deletion_corpus_sentences = 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_project_blocks_parent_fact_recording(
    tmp_path: Path,
) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        _, reference = await _submitted(jobs, key="pending-parent")
        assert await runs.begin_execution(reference) is True
        await _mark_demo_project_pending(database)
        with pytest.raises(ReproducibilityError, match="run_not_found"):
            await manifests.record_execution(reference, _facts())
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_project_blocks_manifest_finalization(tmp_path: Path) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        _, reference = await _submitted(jobs, key="pending-finalize")
        await _finish(runs, manifests, reference, _facts())
        await _mark_demo_project_pending(database)
        with pytest.raises(ReproducibilityError, match="run_not_found"):
            await manifests.finalize(reference)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_project_hides_replay_submission_and_projection(tmp_path: Path) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        source_id, reference = await _submitted(jobs, key="pending-replay-source")
        await _finish(runs, manifests, reference, _facts())
        await manifests.finalize(reference)
        replay = await manifests.submit_replay(
            _actor(),
            project_id=DEMO_PROJECT_ID,
            source_run_id=source_id,
            idempotency_key="pending-replay",
        )
        await _mark_demo_project_pending(database)

        with pytest.raises(ResourceNotFoundError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=source_id,
                idempotency_key="pending-replay-second",
            )
        with pytest.raises(ResourceNotFoundError):
            await manifests.get_replay(_actor(), replay.replay.replay_run_id)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authoritative_manifest_is_canonical_idempotent_and_audited(tmp_path: Path) -> None:
    database, jobs, runs, manifests, store = await _stack(tmp_path)
    try:
        run_id, reference = await _submitted(jobs, key="manifest-source")
        await _finish(runs, manifests, reference, _facts())

        first = await manifests.finalize(reference)
        second = await manifests.finalize(reference)

        assert first.created is True
        assert second.created is False
        assert first.artifact_id == second.artifact_id
        assert first.manifest.sha256 == second.manifest.sha256
        assert tuple(item.name for item in first.manifest.input_digests) == (
            "run-spec",
            "worker-policy",
        )
        assert first.manifest.output_digests[0].name == "result-summary"
        keys = await store.list_keys("artifacts/v1", limit=10)
        assert len(keys) == 1
        async with database.session() as session:
            recorded = await session.get(RunExecutionFact, run_id)
            assert recorded is not None
            assert recorded.manifest_artifact_id == first.artifact_id
            assert recorded.manifest_sha256 == first.manifest.sha256
            actions = tuple(await session.scalars(select(AuditEvent.action)))
        assert actions.count(AuditAction.RUN_MANIFEST_CREATED) == 1
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_replay_redelivery_converges_and_compares_atomically(tmp_path: Path) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        source_id, source_reference = await _submitted(jobs, key="replay-source")
        await _finish(runs, manifests, source_reference, _facts())
        source_manifest = await manifests.finalize(source_reference)

        first = await manifests.submit_replay(
            _actor(),
            project_id=DEMO_PROJECT_ID,
            source_run_id=source_id,
            idempotency_key="replay-exact-1",
        )
        duplicate = await manifests.submit_replay(
            _actor(),
            project_id=DEMO_PROJECT_ID,
            source_run_id=source_id,
            idempotency_key="replay-exact-1",
        )
        assert first.created is True
        assert duplicate.created is False
        assert duplicate.replay.replay_run_id == first.replay.replay_run_id
        replay_reference = RunWorkflowReference(
            str(DEMO_PRINCIPAL.organization_id),
            str(first.replay.replay_run_id),
            source_reference.spec_sha256,
        )
        await _finish(runs, manifests, replay_reference, _facts())

        observed = await manifests.finalize(replay_reference)
        redelivery = await manifests.finalize(replay_reference)
        status = await manifests.get_replay(_actor(), first.replay.replay_run_id)

        assert observed.created is True
        assert redelivery.created is False
        assert status.lifecycle is ReplayLifecycle.COMPARED
        assert status.classification is DeterminismClass.EXACT
        assert status.comparison is not None
        assert status.comparison.verdict is ReplayVerdict.EXACT_MATCH
        assert status.comparison.outputs_match is True
        assert status.source_manifest_artifact_id == source_manifest.artifact_id
        async with database.session() as session:
            replays = tuple(await session.scalars(select(RunReplay)))
            artifacts = tuple(await session.scalars(select(Artifact)))
            actions = tuple(await session.scalars(select(AuditEvent.action)))
        assert len(replays) == 1
        assert sum(item.kind == "run-manifest" for item in artifacts) == 2
        assert actions.count(AuditAction.RUN_REPLAY_SUBMITTED) == 1
        assert actions.count(AuditAction.RUN_REPLAY_COMPARED) == 1
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classification", "replay_value", "verdict"),
    [
        (DeterminismClass.BEST_EFFORT, 2, ReplayVerdict.BEST_EFFORT_DIVERGENCE),
        (DeterminismClass.NONREPRODUCIBLE, 1, ReplayVerdict.NONREPRODUCIBLE),
    ],
)
async def test_replay_discloses_best_effort_and_nonreproducible_results(
    tmp_path: Path,
    classification: DeterminismClass,
    replay_value: int,
    verdict: ReplayVerdict,
) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        source_id, source_reference = await _submitted(jobs, key=f"source-{classification}")
        await _finish(runs, manifests, source_reference, _facts(classification))
        await manifests.finalize(source_reference)
        created = await manifests.submit_replay(
            _actor(),
            project_id=DEMO_PROJECT_ID,
            source_run_id=source_id,
            idempotency_key=f"replay-{classification}",
        )
        replay_reference = RunWorkflowReference(
            str(DEMO_PRINCIPAL.organization_id),
            str(created.replay.replay_run_id),
            source_reference.spec_sha256,
        )
        await _finish(
            runs,
            manifests,
            replay_reference,
            _facts(classification),
            value=replay_value,
        )
        await manifests.finalize(replay_reference)
        status = await manifests.get_replay(_actor(), created.replay.replay_run_id)
        assert status.comparison is not None
        assert status.comparison.verdict is verdict
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_facts_conflicts_versions_and_tampering_fail_closed(tmp_path: Path) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        run_id, reference = await _submitted(jobs, key="facts-conflict")
        assert await runs.begin_execution(reference) is True
        with pytest.raises(ReproducibilityError, match="runtime_version_mismatch"):
            await manifests.record_execution(
                reference,
                _facts().model_copy(update={"corpusgen_version": "forged"}),
            )
        assert await manifests.record_execution(reference, _facts()) is True
        with pytest.raises(ReproducibilityError, match="execution_facts_conflict"):
            await manifests.record_execution(
                reference,
                _facts().model_copy(update={"worker_image_digest": f"sha256:{'c' * 64}"}),
            )
        await runs.complete(reference, {"count": 1})
        async with database.session() as session:
            recorded = await session.get(RunExecutionFact, run_id)
            assert recorded is not None
            recorded.facts_sha256 = "0" * 64
        with pytest.raises(ReproducibilityError, match="execution_facts_integrity_violation"):
            await manifests.finalize(reference)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_replay_ids_are_non_enumerating(tmp_path: Path) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        run_id, reference = await _submitted(jobs, key="tenant-source")
        await _finish(runs, manifests, reference, _facts())
        await manifests.finalize(reference)
        attacker = replace(_actor(), organization_id=uuid4(), subject="attacker")
        with pytest.raises(ResourceNotFoundError):
            await manifests.submit_replay(
                attacker,
                project_id=DEMO_PROJECT_ID,
                source_run_id=run_id,
                idempotency_key="forged-replay",
            )
        with pytest.raises(ResourceNotFoundError):
            await manifests.get_replay(attacker, uuid4())
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manifest_object_corruption_blocks_replay_submission(tmp_path: Path) -> None:
    database, jobs, runs, manifests, store = await _stack(tmp_path)
    try:
        run_id, reference = await _submitted(jobs, key="corrupt-source")
        await _finish(runs, manifests, reference, _facts())
        created = await manifests.finalize(reference)
        async with database.session() as session:
            key = await session.scalar(
                select(Artifact.storage_key).where(Artifact.id == created.artifact_id)
            )
        assert key is not None
        store.corrupt(key, b'{"forged":true}')
        with pytest.raises(DependencyUnavailableError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=run_id,
                idempotency_key="corrupt-replay",
            )
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manifest_binds_verified_corpus_input_and_output_artifacts(tmp_path: Path) -> None:
    database, jobs, runs, manifests, store = await _stack(tmp_path)
    try:
        corpus_id = uuid4()
        version_id = uuid4()
        normalized_sentences = ["A valid replayable sentence."]
        corpus_bytes = json.dumps(
            {"language": "en-us", "sentences": normalized_sentences},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        async with database.session() as session:
            user_id = await session.scalar(
                select(User.id).where(User.oidc_subject == DEMO_PRINCIPAL.subject)
            )
            assert user_id is not None
            session.add(
                Corpus(
                    id=corpus_id,
                    organization_id=DEMO_PRINCIPAL.organization_id,
                    project_id=DEMO_PROJECT_ID,
                    created_by=user_id,
                    name="Replay corpus",
                )
            )
            session.add(
                CorpusVersion(
                    id=version_id,
                    organization_id=DEMO_PRINCIPAL.organization_id,
                    corpus_id=corpus_id,
                    created_by=user_id,
                    version_number=1,
                    language="en-us",
                    sentence_count=1,
                    content_sha256=hashlib.sha256(corpus_bytes).hexdigest(),
                    corpusgen_version=GEN_VERSION,
                )
            )
            session.add_all(
                [
                    Sentence(
                        organization_id=DEMO_PRINCIPAL.organization_id,
                        corpus_version_id=version_id,
                        ordinal=index,
                        original_text=text,
                        normalized_text=text,
                    )
                    for index, text in enumerate(normalized_sentences)
                ]
            )
        input_id = await _persist_artifact(
            database,
            store,
            kind=ArtifactKind.CORPUS_TEXT,
            content=b'{"input":true}',
            run_id=None,
        )
        run_id, reference = await _submitted(
            jobs,
            key="manifest-full-inputs",
            corpus_version_id=version_id,
        )
        facts = _facts().model_copy(update={"input_artifact_ids": (input_id,)})
        assert await runs.begin_execution(reference)
        assert await manifests.record_execution(reference, facts)
        await runs.complete(reference, {"stop_reason": "target-reached", "count": 2})
        output_id = await _persist_artifact(
            database,
            store,
            kind=ArtifactKind.RUN_RESULT,
            content=b'{"count":2}',
            run_id=run_id,
        )

        created = await manifests.finalize(reference)

        assert {item.name for item in created.manifest.input_digests} == {
            "run-spec",
            "corpus-version",
            "worker-policy",
            "input-corpus-text-001",
        }
        assert created.manifest.output_digests[0].name == "output-run-result-001"
        assert created.manifest.stop_reason.value == "target-reached"
        assert output_id != created.artifact_id
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manifest_state_missing_facts_output_and_storage_failures_are_stable(
    tmp_path: Path,
) -> None:
    database, jobs, runs, manifests, store = await _stack(tmp_path)
    try:
        queued_id, queued = await _submitted(jobs, key="manifest-queued")
        with pytest.raises(ReproducibilityError, match="execution_facts_invalid_state"):
            await manifests.record_execution(queued, _facts())
        with pytest.raises(ReproducibilityError, match="manifest_invalid_state"):
            await manifests.finalize(queued)

        missing_id, missing = await _submitted(jobs, key="manifest-no-facts")
        assert await runs.begin_execution(missing)
        await runs.complete(missing, {"count": 1})
        with pytest.raises(ReproducibilityError, match="execution_facts_missing"):
            await manifests.finalize(missing)

        no_output_id, no_output = await _submitted(jobs, key="manifest-no-output")
        assert await runs.begin_execution(no_output)
        assert await manifests.record_execution(no_output, _facts())
        await runs.complete(no_output, {"count": 1})
        async with database.session() as session:
            run = await session.get(Run, no_output_id)
            assert run is not None
            run.result_summary = None
        with pytest.raises(ReproducibilityError, match="manifest_outputs_missing"):
            await manifests.finalize(no_output)

        storage_id, storage = await _submitted(jobs, key="manifest-store-failure")
        await _finish(runs, manifests, storage, _facts())
        store.fail_put = True
        with pytest.raises(DependencyUnavailableError):
            await manifests.finalize(storage)
        store.fail_put = False
        assert (await manifests.finalize(storage)).created

        assert {queued_id, missing_id, no_output_id, storage_id}
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manifest_rejects_model_corpus_timestamp_and_schema_integrity_failures(
    tmp_path: Path,
) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        model_id, model_reference = await _submitted(jobs, key="manifest-model-facts")
        assert await runs.begin_execution(model_reference)
        async with database.session() as session:
            run = await session.get(Run, model_id)
            assert run is not None
            run.kind = RunKind.PERPLEXITY
        with pytest.raises(ReproducibilityError, match="model_provenance_missing"):
            await manifests.record_execution(model_reference, _facts())

        timestamp_id, timestamp_reference = await _submitted(jobs, key="manifest-time")
        await _finish(runs, manifests, timestamp_reference, _facts())
        async with database.session() as session:
            await session.execute(
                delete(RunEvent).where(
                    RunEvent.run_id == timestamp_id,
                    RunEvent.event_type == "run.started",
                )
            )
        with pytest.raises(ReproducibilityError, match="execution_timestamps_missing"):
            await manifests.finalize(timestamp_reference)

        schema_id, schema_reference = await _submitted(jobs, key="manifest-schema")
        assert await runs.begin_execution(schema_reference)
        async with database.session() as session:
            run = await session.get(Run, schema_id)
            assert run is not None
            spec = dict(run.spec)
            spec["target_source"] = "phoible"
            from corpuskit.domain.jobs import normalize_run_spec

            normalized, digest = normalize_run_spec(spec)
            run.spec = normalized
            run.spec_sha256 = digest
            schema_reference = RunWorkflowReference(
                schema_reference.organization_id,
                schema_reference.run_id,
                digest,
            )
        assert await manifests.record_execution(schema_reference, _facts())
        await runs.complete(schema_reference, {"count": 1})
        with pytest.raises(ReproducibilityError, match="manifest_spec_invalid"):
            await manifests.finalize(schema_reference)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manifest_and_replay_authorization_and_conflicts_are_non_enumerating(
    tmp_path: Path,
) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        source_id, source_reference = await _submitted(jobs, key="source-conflicts")
        with pytest.raises(ResourceNotFoundError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=source_id,
                idempotency_key="source-not-finished",
            )
        await _finish(runs, manifests, source_reference, _facts())
        with pytest.raises(ResourceNotFoundError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=source_id,
                idempotency_key="source-no-manifest",
            )
        await manifests.finalize(source_reference)
        with pytest.raises(InvalidRequestError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=source_id,
                idempotency_key="bad key",
            )
        with pytest.raises(ResourceConflictError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=source_id,
                idempotency_key="source-conflicts",
            )
        with pytest.raises(ResourceNotFoundError):
            await manifests.get_replay(_actor(), uuid4())

        async with database.session() as session:
            membership = await session.scalar(
                select(Membership)
                .join(User, User.id == Membership.user_id)
                .where(User.oidc_subject == DEMO_PRINCIPAL.subject)
            )
            assert membership is not None
            membership.role = Role.VIEWER
        with pytest.raises(ResourceNotFoundError):
            await manifests.submit_replay(
                _actor(),
                project_id=DEMO_PROJECT_ID,
                source_run_id=source_id,
                idempotency_key="viewer-replay",
            )
        assert source_id
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_fact_and_input_integrity_fail_closed_before_computation(
    tmp_path: Path,
) -> None:
    database, jobs, runs, manifests, _ = await _stack(tmp_path)
    try:
        missing_input_id, missing_input = await _submitted(jobs, key="missing-input")
        assert await runs.begin_execution(missing_input)
        with pytest.raises(ReproducibilityError, match="input_artifact_not_found"):
            await manifests.record_execution(
                missing_input,
                _facts().model_copy(update={"input_artifact_ids": (uuid4(),)}),
            )

        malformed_id, malformed = await _submitted(jobs, key="malformed-facts")
        await _finish(runs, manifests, malformed, _facts())
        async with database.session() as session:
            recorded = await session.get(RunExecutionFact, malformed_id)
            assert recorded is not None
            recorded.facts = {"forged": True}
        with pytest.raises(ReproducibilityError, match="execution_facts_integrity_violation"):
            await manifests.finalize(malformed)

        async with database.session() as session:
            run = await session.get(Run, missing_input_id)
            assert run is not None
            with pytest.raises(ReproducibilityError, match="execution_facts_integrity_violation"):
                await manifests._validate_input_artifacts(
                    session,
                    run,
                    [{"artifact_id": "not-a-uuid"}],
                )
            with pytest.raises(ReproducibilityError, match="input_artifact_integrity_violation"):
                await manifests._validate_input_artifacts(
                    session,
                    run,
                    [
                        {
                            "artifact_id": str(uuid4()),
                            "sha256": "a" * 64,
                            "size_bytes": 1,
                        }
                    ],
                )
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manifest_object_metadata_json_and_canonical_bytes_are_verified(
    tmp_path: Path,
) -> None:
    database, jobs, runs, manifests, store = await _stack(tmp_path)
    try:
        run_id, reference = await _submitted(jobs, key="manifest-object-integrity")
        await _finish(runs, manifests, reference, _facts())
        created = await manifests.finalize(reference)
        async with database.session() as session:
            row = await session.get(Artifact, created.artifact_id)
            assert row is not None
            record = _artifact_record(row)

        with pytest.raises(DependencyUnavailableError):
            await manifests._read_verified(replace(record, size_bytes=record.size_bytes + 1))
        with pytest.raises(ReproducibilityError, match="manifest_metadata_conflict"):
            await manifests._load_manifest(
                replace(record, kind=ArtifactKind.EXPORT.value),
                run_id=run_id,
            )

        invalid = b"not-json"
        invalid_sha = hashlib.sha256(invalid).hexdigest()
        invalid_key = f"{record.storage_key}-invalid"
        await store.put(
            key=invalid_key,
            content=invalid,
            sha256=invalid_sha,
            media_type="application/json",
        )
        invalid_record = replace(
            record,
            storage_key=invalid_key,
            sha256=invalid_sha,
            size_bytes=len(invalid),
        )
        with pytest.raises(ReproducibilityError, match="manifest_integrity_violation"):
            await manifests._load_manifest(invalid_record, run_id=run_id)

        pretty = json.dumps(created.manifest.model_dump(mode="json"), indent=2).encode()
        pretty_sha = hashlib.sha256(pretty).hexdigest()
        pretty_key = f"{record.storage_key}-pretty"
        await store.put(
            key=pretty_key,
            content=pretty,
            sha256=pretty_sha,
            media_type="application/json",
        )
        with pytest.raises(ReproducibilityError, match="manifest_integrity_violation"):
            await manifests._load_manifest(
                replace(
                    record,
                    storage_key=pretty_key,
                    sha256=pretty_sha,
                    size_bytes=len(pretty),
                ),
                run_id=run_id,
            )

        store.corrupt(invalid_key, invalid + b"-oversized")
        with pytest.raises(DependencyUnavailableError):
            await manifests._read_verified(invalid_record)

        with pytest.raises(ReproducibilityError, match="run_not_found"):
            await manifests.finalize(
                RunWorkflowReference(
                    str(DEMO_PRINCIPAL.organization_id),
                    str(uuid4()),
                    "a" * 64,
                )
            )
    finally:
        await database.dispose()
