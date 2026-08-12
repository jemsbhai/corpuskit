"""Production-path acceptance for durable selection artifact adoption."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.adapters.corpusgen.analysis import CorpusgenAnalysisAdapter
from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.analysis import CoverageTrajectoryRequest
from corpuskit.domain.artifacts import (
    ArtifactKind,
    StagedArtifactResult,
    artifact_storage_key,
    staged_artifact_storage_key,
)
from corpuskit.domain.corpus import CoverageUnit, EvaluationTarget, EvaluationTargetMode
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.selection import (
    MAX_SELECTION_RESULT_ARTIFACT_BYTES,
    CorpusSelection,
    CorpusSelectionArtifactV1,
    SelectionAlgorithm,
    SelectionOptions,
    SelectionRequest,
)
from corpuskit.persistence.artifact_store import (
    ConfiguredStagedArtifactWriter,
    InMemoryObjectStore,
    ObjectStore,
    PutResult,
    build_object_store,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact, Run, User
from corpuskit.services.artifact_adoption import ArtifactAdoptionError, ArtifactAdoptionService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.handlers import build_core_handler_registry
from corpuskit.workflows.process_runner import ProcessExecutionRunner
from corpuskit.workflows.store import (
    AdoptedArtifact,
    ArtifactCommit,
    DurableRunStore,
    RunStoreError,
)

pytestmark = pytest.mark.integration


def _settings(
    tmp_path: Path,
    *,
    artifact_max_bytes: int = MAX_SELECTION_RESULT_ARTIFACT_BYTES,
) -> Settings:
    return Settings(
        environment="test",
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / f'selection-{uuid4()}.db').as_posix()}"),
        artifact_root=tmp_path / f"artifacts-{uuid4()}",
        artifact_max_bytes=artifact_max_bytes,
        artifact_download_chunk_bytes=16 * 1024,
        artifact_orphan_grace_seconds=60,
        _env_file=None,
    )


def _spec() -> dict[str, object]:
    return {
        "candidates": ["Pea.", "Bee.", "Tea.", "Key.", "Pea bee tea key."],
        "language": "en-us",
        "unit": "phoneme",
        "target": {"mode": "explicit", "phonemes": ["p", "b", "t", "k"]},
        "options": {"algorithm": "greedy", "max_sentences": 2},
    }


def _selection_result() -> CorpusSelection:
    spec = _spec()
    return CorpusgenAdapter().select(
        SelectionRequest(
            candidates=tuple(spec["candidates"]),  # type: ignore[arg-type]
            language="en-us",
            unit=CoverageUnit.PHONEME,
            target=EvaluationTarget(
                mode=EvaluationTargetMode.EXPLICIT,
                phonemes=("p", "b", "t", "k"),
            ),
            options=SelectionOptions(
                algorithm=SelectionAlgorithm.GREEDY,
                max_sentences=2,
            ),
        )
    )


def _selection_payload() -> bytes:
    return CorpusSelectionArtifactV1.from_selection(_selection_result()).canonical_bytes()


async def _submitted_run(
    tmp_path: Path,
    *,
    runs_type: type[DurableRunStore] = DurableRunStore,
    objects: ObjectStore | None = None,
    artifact_max_bytes: int = MAX_SELECTION_RESULT_ARTIFACT_BYTES,
) -> tuple[
    Settings,
    Database,
    JobActor,
    RunWorkflowReference,
    DurableRunStore,
    ObjectStore,
    ArtifactAdoptionService,
]:
    settings = _settings(tmp_path, artifact_max_bytes=artifact_max_bytes)
    database = Database(settings.database_url)
    await database.create_schema()
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    jobs = JobControlPlane(database)
    await jobs.bootstrap_demo(actor, environment="test")
    submitted = await jobs.submit(
        actor,
        RunSubmission(
            project_id=DEMO_PROJECT_ID,
            kind=RunKind.SELECT,
            spec=_spec(),
        ),
        idempotency_key=f"selection-{uuid4()}",
    )
    reference = RunWorkflowReference(
        organization_id=str(actor.organization_id),
        run_id=str(submitted.run.id),
        spec_sha256=submitted.run.spec_sha256,
    )
    runs = runs_type(database)
    assert await runs.begin_execution(reference) is True
    resolved_objects = objects or InMemoryObjectStore()
    adopter = ArtifactAdoptionService(runs, resolved_objects, settings)
    return settings, database, actor, reference, runs, resolved_objects, adopter


async def _stage_selection(
    objects: ObjectStore,
    payload: bytes,
) -> tuple[dict[str, object], str]:
    digest = hashlib.sha256(payload).hexdigest()
    await objects.put(
        key=staged_artifact_storage_key(digest),
        content=payload,
        sha256=digest,
        media_type="application/json",
    )
    claim = StagedArtifactResult(
        staged_artifact_ref=f"staged-artifact://sha256/{digest}",
        schema_id="corpuskit.corpus-selection.v1",
        artifact_type="run-result",
        media_type="application/json",
        size_bytes=len(payload),
    )
    return claim.model_dump(mode="json"), digest


@pytest.mark.asyncio
async def test_process_claim_parent_adoption_and_rehydration_preserve_trajectory(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    jobs = JobControlPlane(database)
    await jobs.bootstrap_demo(actor, environment="test")
    submitted = await jobs.submit(
        actor,
        RunSubmission(project_id=DEMO_PROJECT_ID, kind=RunKind.SELECT, spec=_spec()),
        idempotency_key="selection-production-path",
    )
    reference = RunWorkflowReference(
        str(actor.organization_id),
        str(submitted.run.id),
        submitted.run.spec_sha256,
    )
    runs = DurableRunStore(database)
    assert await runs.begin_execution(reference) is True
    objects = build_object_store(settings)
    adopter = ArtifactAdoptionService(runs, objects, settings)
    runner = ProcessExecutionRunner(
        build_core_handler_registry(
            settings,
            stager=ConfiguredStagedArtifactWriter.from_settings(settings),
        ),
        hard_timeout_seconds=30,
    )

    async def tick() -> None:
        return None

    try:
        expected_selection = _selection_result()
        claim = await runner.execute(
            RunKind.SELECT,
            _spec(),
            tick=tick,
            tick_seconds=0.05,
        )
        assert set(claim) == {
            "artifact_type",
            "contract",
            "media_type",
            "schema_id",
            "size_bytes",
            "staged_artifact_ref",
        }
        candidates = _spec()["candidates"]
        assert isinstance(candidates, list)
        assert not any(sentence in repr(claim) for sentence in candidates)
        assert "staging/v1" not in repr(claim)
        assert "artifacts/v1" not in repr(claim)

        first = await adopter.adopt(reference, claim)
        duplicate = await adopter.adopt(reference, claim)
        assert first.state is RunState.SUCCEEDED
        assert first.created is True
        assert duplicate == ArtifactCommit(RunState.SUCCEEDED, first.artifact_id, False)

        async with database.session() as session:
            artifact = await session.scalar(select(Artifact))
            run = await session.get(Run, UUID(reference.run_id))
            user_id = await session.scalar(
                select(User.id).where(User.oidc_subject == actor.subject)
            )
            artifact_count = await session.scalar(select(func.count()).select_from(Artifact))
        assert artifact is not None
        assert run is not None
        assert user_id is not None
        assert artifact_count == 1
        assert (
            artifact.organization_id,
            artifact.project_id,
            artifact.run_id,
            artifact.created_by,
            artifact.kind,
        ) == (
            actor.organization_id,
            DEMO_PROJECT_ID,
            UUID(reference.run_id),
            user_id,
            ArtifactKind.RUN_RESULT.value,
        )
        assert run.result_summary == {
            "artifact_id": str(artifact.id),
            "artifact_type": "run-result",
            "media_type": "application/json",
            "schema_id": "corpuskit.corpus-selection.v1",
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
        expected_key = artifact_storage_key(
            organization_id=actor.organization_id,
            project_id=DEMO_PROJECT_ID,
            run_id=UUID(reference.run_id),
            kind=ArtifactKind.RUN_RESULT,
            sha256=artifact.sha256,
        )
        assert artifact.storage_key == expected_key
        opened = await objects.open(expected_key, chunk_bytes=17)
        payload = b"".join([chunk async for chunk in opened.chunks])
        assert hashlib.sha256(payload).hexdigest() == artifact.sha256
        restored = CorpusSelectionArtifactV1.model_validate_json(payload, strict=True)
        restored.validate_run_spec(_spec())
        selection = restored.to_selection()
        assert selection.model_dump(exclude={"elapsed_seconds"}) == expected_selection.model_dump(
            exclude={"elapsed_seconds"}
        )
        transcriptions = CorpusgenAdapter().phonemize_batch(
            selection.selected_sentences,
            language="en-us",
        )
        trajectory = CorpusgenAnalysisAdapter().trajectory(
            CoverageTrajectoryRequest(
                phoneme_sequences=tuple(item.phonemes for item in transcriptions),
                target_units=("p", "b", "t", "k"),
                unit=CoverageUnit.PHONEME,
            )
        )
        expected_transcriptions = CorpusgenAdapter().phonemize_batch(
            expected_selection.selected_sentences,
            language="en-us",
        )
        expected_trajectory = CorpusgenAnalysisAdapter().trajectory(
            CoverageTrajectoryRequest(
                phoneme_sequences=tuple(item.phonemes for item in expected_transcriptions),
                target_units=("p", "b", "t", "k"),
                unit=CoverageUnit.PHONEME,
            )
        )
        assert trajectory == expected_trajectory
        assert trajectory.coverages[-1] == selection.coverage

        foreign = RunWorkflowReference(str(uuid4()), reference.run_id, reference.spec_sha256)
        with pytest.raises(RunStoreError, match="run_not_found"):
            await adopter.adopt(foreign, claim)
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["text", "target"])
async def test_parent_rejects_digest_valid_selection_semantic_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    _, database, _, reference, _, objects, adopter = await _submitted_run(tmp_path)
    try:
        valid = CorpusSelectionArtifactV1.model_validate_json(
            _selection_payload(),
            strict=True,
        )
        tampered = (
            valid.model_copy(
                update={"selected_sentences": tuple("foreign text" for _ in valid.selected_indices)}
            )
            if tamper == "text"
            else valid.model_copy(
                update={
                    "covered_units": ("p",),
                    "missing_units": ("b", "t"),
                    "coverage": 1 / 3,
                }
            )
        )
        claim, _ = await _stage_selection(objects, tampered.canonical_bytes())

        with pytest.raises(ArtifactAdoptionError) as caught:
            await adopter.adopt(reference, claim)
        assert (caught.value.code, caught.value.retryable) == (
            "staged_result_spec_mismatch",
            False,
        )
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
        assert not await objects.list_keys("artifacts/v1/", limit=10)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_parent_enforces_selection_budget_before_reading_staging(
    tmp_path: Path,
) -> None:
    _, database, _, reference, _, objects, adopter = await _submitted_run(
        tmp_path,
        artifact_max_bytes=10 * 1024 * 1024,
    )
    try:
        digest = "0" * 64
        claim = StagedArtifactResult(
            staged_artifact_ref=f"staged-artifact://sha256/{digest}",
            schema_id="corpuskit.corpus-selection.v1",
            artifact_type="run-result",
            media_type="application/json",
            size_bytes=MAX_SELECTION_RESULT_ARTIFACT_BYTES + 1,
        ).model_dump(mode="json")

        with pytest.raises(ArtifactAdoptionError) as caught:
            await adopter.adopt(reference, claim)
        assert (caught.value.code, caught.value.retryable) == (
            "staged_result_size_mismatch",
            False,
        )
        assert not await objects.list_keys("staging/v1/", limit=10)
        assert not await objects.list_keys("artifacts/v1/", limit=10)
    finally:
        await database.dispose()


class FailOnceCommitStore(DurableRunStore):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.fail_next_commit = True

    async def commit_adopted_result(
        self,
        reference: RunWorkflowReference,
        adopted: AdoptedArtifact,
    ) -> ArtifactCommit:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise RunStoreError("persistence_unavailable")
        return await super().commit_adopted_result(reference, adopted)


class CancelOnFinalPutStore(InMemoryObjectStore):
    cancel: Callable[[], Awaitable[None]] | None = None

    async def put(self, **kwargs: object) -> PutResult:
        result = await super().put(**kwargs)  # type: ignore[arg-type]
        if str(kwargs["key"]).startswith("artifacts/v1/") and self.cancel is not None:
            callback, self.cancel = self.cancel, None
            await callback()
        return result


@pytest.mark.asyncio
async def test_selection_crash_redelivery_and_cancellation_never_late_commit(
    tmp_path: Path,
) -> None:
    _, database, actor, reference, _, objects, adopter = await _submitted_run(
        tmp_path,
        runs_type=FailOnceCommitStore,
    )
    try:
        claim, digest = await _stage_selection(objects, _selection_payload())
        with pytest.raises(RunStoreError, match="persistence_unavailable"):
            await adopter.adopt(reference, claim)
        final_key = artifact_storage_key(
            organization_id=actor.organization_id,
            project_id=DEMO_PROJECT_ID,
            run_id=UUID(reference.run_id),
            kind=ArtifactKind.RUN_RESULT,
            sha256=digest,
        )
        assert (await objects.stat(final_key)).sha256 == digest
        committed = await adopter.adopt(reference, claim)
        assert committed.state is RunState.SUCCEEDED
        assert committed.created is True
    finally:
        await database.dispose()

    cancelling_objects = CancelOnFinalPutStore()
    _, database, _, reference, runs, objects, adopter = await _submitted_run(
        tmp_path,
        objects=cancelling_objects,
    )

    async def cancel() -> None:
        assert await runs.request_cancellation(reference) is RunState.CANCELLING

    cancelling_objects.cancel = cancel
    try:
        claim, _ = await _stage_selection(objects, _selection_payload())
        result = await adopter.adopt(reference, claim)
        assert result == ArtifactCommit(RunState.CANCELLED, None, False)
        duplicate = await adopter.adopt(reference, claim)
        assert duplicate == ArtifactCommit(RunState.CANCELLED, None, False)
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
            run = await session.get(Run, UUID(reference.run_id))
        assert run is not None
        assert run.result_summary is None
    finally:
        await database.dispose()
