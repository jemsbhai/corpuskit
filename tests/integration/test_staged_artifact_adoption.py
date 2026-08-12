"""Adversarial acceptance tests for authoritative staged-result adoption."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ArtifactKind,
    StagedArtifactResult,
    artifact_storage_key,
    staged_artifact_storage_key,
)
from corpuskit.domain.errors import InvalidRequestError
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
from corpuskit.persistence.artifact_store import (
    InMemoryObjectStore,
    ObjectDescriptor,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectStream,
    PutResult,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact, Run, User
from corpuskit.services.artifact_adoption import ArtifactAdoptionError, ArtifactAdoptionService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.workflows.activities import CoreRunActivities
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.handlers import HandlerRegistry
from corpuskit.workflows.store import (
    AdoptedArtifact,
    ArtifactCommit,
    DurableRunStore,
    RunStoreError,
)

DEMO_ORGANIZATION_ID = UUID("00000000-0000-4000-8000-000000000001")


def _settings(tmp_path: Path) -> Settings:
    pin = ImmutableModelPin(model="acme/tiny-model", revision="a" * 40)
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / f'adoption-{uuid4()}.db').as_posix()}",
        artifact_max_bytes=1024 * 1024,
        artifact_download_chunk_bytes=16 * 1024,
        artifact_orphan_grace_seconds=60,
        worker_local_model_policies=(
            LocalModelPolicy(
                pin=pin,
                artifact_sha256="b" * 64,
                allowed_devices=(ModelDevice.CPU,),
                allowed_quantizations=(ModelQuantization.NONE,),
            ),
        ),
        _env_file=None,
    )


def _payload() -> bytes:
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
    return result.model_dump_json().encode()


def _run_spec() -> dict[str, object]:
    return LocalGenerationRequest(
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
    ).model_dump(mode="json")


async def _stack(
    tmp_path: Path,
    *,
    objects: InMemoryObjectStore | None = None,
    runs_type: type[DurableRunStore] = DurableRunStore,
    spec: dict[str, object] | None = None,
    kind: RunKind = RunKind.GENERATE_LOCAL,
) -> tuple[
    Database,
    JobActor,
    RunWorkflowReference,
    DurableRunStore,
    InMemoryObjectStore,
    ArtifactAdoptionService,
]:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    actor = JobActor(
        subject=DEMO_PRINCIPAL.subject,
        organization_id=DEMO_PRINCIPAL.organization_id,
    )
    jobs = JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings))
    await jobs.bootstrap_demo(actor, environment="test")
    submitted = await jobs.submit(
        actor,
        RunSubmission(
            project_id=DEMO_PROJECT_ID,
            kind=kind,
            spec=spec if spec is not None else _run_spec(),
        ),
        idempotency_key=f"adoption-{uuid4()}",
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
    return database, actor, reference, runs, resolved_objects, adopter


async def _stage(
    objects: InMemoryObjectStore,
    payload: bytes | None = None,
) -> tuple[dict[str, object], str]:
    content = payload or _payload()
    digest = hashlib.sha256(content).hexdigest()
    await objects.put(
        key=staged_artifact_storage_key(digest),
        content=content,
        sha256=digest,
        media_type="application/json",
    )
    claim = StagedArtifactResult(
        staged_artifact_ref=f"staged-artifact://sha256/{digest}",
        schema_id="corpuskit.local-generation-result.v1",
        artifact_type="run-result",
        media_type="application/json",
        size_bytes=len(content),
    )
    return claim.model_dump(mode="json"), digest


@dataclass(frozen=True, slots=True)
class StagedClaimHandler:
    claim: dict[str, object]
    kind: RunKind = RunKind.GENERATE_LOCAL

    def execute(self, spec: Mapping[str, object]) -> dict[str, object]:
        assert spec == _run_spec()
        return self.claim


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adoption_uses_authoritative_scope_and_redelivery_is_exactly_once(
    tmp_path: Path,
) -> None:
    database, actor, reference, _, objects, adopter = await _stack(tmp_path)
    try:
        claim, digest = await _stage(objects)
        first = await adopter.adopt(reference, claim)
        duplicate = await adopter.adopt(reference, claim)

        assert first.state is RunState.SUCCEEDED
        assert first.created is True
        assert duplicate == type(first)(RunState.SUCCEEDED, first.artifact_id, False)
        async with database.session() as session:
            artifact = await session.scalar(select(Artifact))
            run = await session.get(Run, UUID(reference.run_id))
            user_id = await session.scalar(
                select(User.id).where(User.oidc_subject == actor.subject)
            )
            count = await session.scalar(select(func.count()).select_from(Artifact))
        assert artifact is not None
        assert run is not None
        assert user_id is not None
        assert count == 1
        assert artifact.organization_id == actor.organization_id
        assert artifact.project_id == DEMO_PROJECT_ID
        assert artifact.run_id == UUID(reference.run_id)
        assert artifact.created_by == user_id
        assert artifact.kind == ArtifactKind.RUN_RESULT.value
        assert artifact.sha256 == digest
        assert run.result_summary == {
            "artifact_id": str(artifact.id),
            "artifact_type": "run-result",
            "media_type": "application/json",
            "schema_id": "corpuskit.local-generation-result.v1",
            "sha256": digest,
            "size_bytes": len(_payload()),
        }

        async with database.session() as session:
            await session.execute(update(Artifact).values(size_bytes=1))
        with pytest.raises(RunStoreError, match="artifact_integrity_violation"):
            await adopter.adopt(reference, claim)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_activity_adopts_before_terminal_success(tmp_path: Path) -> None:
    database, actor, reference, runs, objects, adopter = await _stack(
        tmp_path,
        spec=_run_spec(),
    )
    try:
        claim, _ = await _stage(objects)
        activities = CoreRunActivities(
            runs,
            HandlerRegistry((StagedClaimHandler(claim),)),
            heartbeat_seconds=0.01,
            artifact_adopter=adopter,
        )

        await ActivityEnvironment().run(activities.execute_run, reference)

        run = await JobControlPlane(database).get(actor, UUID(reference.run_id))
        assert run.state is RunState.SUCCEEDED
        assert run.result_summary is not None
        assert run.result_summary["artifact_type"] == "run-result"
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_activity_fails_closed_without_adopter_or_valid_staged_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, reference, runs, objects, _ = await _stack(tmp_path, spec=_run_spec())
    try:
        claim, _ = await _stage(objects)
        activities = CoreRunActivities(
            runs,
            HandlerRegistry((StagedClaimHandler(claim),)),
            heartbeat_seconds=0.01,
        )

        async def computed(*_args: object, **_kwargs: object) -> dict[str, object]:
            return claim

        monkeypatch.setattr(activities, "_compute", computed)
        with pytest.raises(ApplicationError) as unavailable:
            await ActivityEnvironment().run(activities.execute_run, reference)
        assert (unavailable.value.type, unavailable.value.non_retryable) == (
            "staged_result_adoption_unavailable",
            False,
        )
    finally:
        await database.dispose()

    database, _, reference, runs, objects, adopter = await _stack(
        tmp_path,
        spec=_run_spec(),
    )
    try:
        claim, digest = await _stage(objects)
        await objects.delete(staged_artifact_storage_key(digest))
        activities = CoreRunActivities(
            runs,
            HandlerRegistry((StagedClaimHandler(claim),)),
            heartbeat_seconds=0.01,
            artifact_adopter=adopter,
        )

        async def missing(*_args: object, **_kwargs: object) -> dict[str, object]:
            return claim

        monkeypatch.setattr(activities, "_compute", missing)
        with pytest.raises(ApplicationError) as unavailable:
            await ActivityEnvironment().run(activities.execute_run, reference)
        assert (unavailable.value.type, unavailable.value.non_retryable) == (
            "staged_result_missing",
            False,
        )
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_cancellation_after_compute_wins_before_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, reference, runs, objects, adopter = await _stack(
        tmp_path,
        spec=_run_spec(),
    )
    try:
        claim, _ = await _stage(objects)
        activities = CoreRunActivities(
            runs,
            HandlerRegistry((StagedClaimHandler(claim),)),
            heartbeat_seconds=0.01,
            artifact_adopter=adopter,
        )

        async def cancel_after_compute(
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            assert await runs.request_cancellation(reference) is RunState.CANCELLING
            return claim

        monkeypatch.setattr(activities, "_compute", cancel_after_compute)
        await ActivityEnvironment().run(activities.execute_run, reference)
        assert await runs.state(reference) is RunState.CANCELLED
        assert not await objects.list_keys("artifacts/v1/", limit=10)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submission_rejects_invalid_deadline_spec_before_child_start(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    actor = JobActor(
        subject=DEMO_PRINCIPAL.subject,
        organization_id=DEMO_PRINCIPAL.organization_id,
    )
    try:
        jobs = JobControlPlane(database)
        await jobs.bootstrap_demo(actor, environment="test")
        with pytest.raises(InvalidRequestError):
            await jobs.submit(
                actor,
                RunSubmission(
                    project_id=DEMO_PROJECT_ID,
                    kind=RunKind.GENERATE_LOCAL,
                    spec={"request_ref": "not-a-local-generation-contract"},
                ),
                idempotency_key="invalid-deadline-contract",
            )
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forged_authority_malicious_reference_and_schema_claim_fail_closed(
    tmp_path: Path,
) -> None:
    database, _, reference, _, objects, adopter = await _stack(tmp_path)
    try:
        claim, _ = await _stage(objects)
        forged = dict(claim, organization_id=str(uuid4()))
        malicious = dict(claim, staged_artifact_ref="staged-artifact://sha256/../../secret")
        wrong_schema = dict(claim, schema_id="corpuskit.hosted-generation-result.v1")

        for value, code in (
            (forged, "staged_result_contract"),
            (malicious, "staged_result_contract"),
            (wrong_schema, "staged_result_schema_mismatch"),
        ):
            with pytest.raises(ArtifactAdoptionError) as caught:
                await adopter.adopt(reference, value)
            assert caught.value.code == code
            assert str(caught.value) == code

        foreign = RunWorkflowReference(str(uuid4()), reference.run_id, reference.spec_sha256)
        with pytest.raises(RunStoreError, match="run_not_found"):
            await adopter.adopt(foreign, claim)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_corrupt_size_and_payload_schema_fail_with_stable_codes(
    tmp_path: Path,
) -> None:
    database, _, reference, _, objects, adopter = await _stack(tmp_path)
    try:
        claim, digest = await _stage(objects)
        key = staged_artifact_storage_key(digest)
        await objects.delete(key)
        with pytest.raises(ArtifactAdoptionError) as missing:
            await adopter.adopt(reference, claim)
        assert (missing.value.code, missing.value.retryable) == ("staged_result_missing", True)

        await _stage(objects)
        objects.corrupt(key, b"x" * len(_payload()))
        with pytest.raises(ArtifactAdoptionError) as corrupt:
            await adopter.adopt(reference, claim)
        assert corrupt.value.code == "staged_result_digest_mismatch"

        bad_size = dict(claim, size_bytes=len(_payload()) - 1)
        with pytest.raises(ArtifactAdoptionError) as size:
            await adopter.adopt(reference, bad_size)
        assert size.value.code == "staged_result_corrupt"

        invalid_payload = b'{"schema_id":"corpuskit.local-generation-result.v1"}'
        invalid_claim, _ = await _stage(objects, invalid_payload)
        with pytest.raises(ArtifactAdoptionError) as schema:
            await adopter.adopt(reference, invalid_claim)
        assert schema.value.code == "staged_result_schema_mismatch"

        objects.corrupt(key, b"x" * (len(_payload()) + 1))
        with pytest.raises(ArtifactAdoptionError) as oversized_stream:
            await adopter.adopt(reference, claim)
        assert oversized_stream.value.code == "staged_result_size_mismatch"
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsupported_kind_and_oversized_claim_fail_before_object_read(
    tmp_path: Path,
) -> None:
    database, _, reference, _, objects, adopter = await _stack(
        tmp_path,
        kind=RunKind.EVALUATE,
        spec={"language": "en-us"},
    )
    try:
        claim, _ = await _stage(objects)
        with pytest.raises(ArtifactAdoptionError) as unsupported:
            await adopter.adopt(reference, claim)
        assert (unsupported.value.code, unsupported.value.retryable) == (
            "staged_result_unsupported",
            False,
        )
    finally:
        await database.dispose()

    database, _, reference, _, objects, adopter = await _stack(tmp_path)
    try:
        claim, _ = await _stage(objects)
        claim["size_bytes"] = 1024 * 1024 + 1
        with pytest.raises(ArtifactAdoptionError) as oversized:
            await adopter.adopt(reference, claim)
        assert (oversized.value.code, oversized.value.retryable) == (
            "staged_result_size_mismatch",
            False,
        )
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


class UnexpectedCommitFailureStore(DurableRunStore):
    async def commit_adopted_result(
        self,
        reference: RunWorkflowReference,
        adopted: AdoptedArtifact,
    ) -> ArtifactCommit:
        del reference, adopted
        raise OSError("private database location")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_crash_after_final_put_then_redelivery_converges(tmp_path: Path) -> None:
    database, actor, reference, _, objects, adopter = await _stack(
        tmp_path,
        runs_type=FailOnceCommitStore,
    )
    try:
        claim, digest = await _stage(objects)
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unexpected_database_failure_is_sanitized_and_leaves_retriable_object(
    tmp_path: Path,
) -> None:
    database, actor, reference, _, objects, adopter = await _stack(
        tmp_path,
        runs_type=UnexpectedCommitFailureStore,
    )
    try:
        claim, digest = await _stage(objects)
        with pytest.raises(ArtifactAdoptionError) as unavailable:
            await adopter.adopt(reference, claim)
        assert (unavailable.value.code, unavailable.value.retryable) == (
            "persistence_unavailable",
            True,
        )
        assert str(unavailable.value) == "persistence_unavailable"
        final_key = artifact_storage_key(
            organization_id=actor.organization_id,
            project_id=DEMO_PROJECT_ID,
            run_id=UUID(reference.run_id),
            kind=ArtifactKind.RUN_RESULT,
            sha256=digest,
        )
        assert (await objects.stat(final_key)).sha256 == digest
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_object_store_failure_is_retryable_and_does_not_commit(tmp_path: Path) -> None:
    database, _, reference, _, objects, adopter = await _stack(tmp_path)
    try:
        claim, _ = await _stage(objects)
        objects.fail_put = True
        with pytest.raises(ArtifactAdoptionError) as failed:
            await adopter.adopt(reference, claim)
        assert (failed.value.code, failed.value.retryable) == (
            "artifact_store_unavailable",
            True,
        )
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
    finally:
        await database.dispose()


class CancelOnFinalPutStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.cancel: Callable[[], Awaitable[None]] | None = None
        self.cancelled = False

    async def put(self, **kwargs: object) -> PutResult:
        result = await super().put(**kwargs)  # type: ignore[arg-type]
        if (
            str(kwargs["key"]).startswith("artifacts/v1/")
            and self.cancel is not None
            and not self.cancelled
        ):
            self.cancelled = True
            await self.cancel()
        return result


class CancelOnStagingReadStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.cancel: Callable[[], Awaitable[None]] | None = None
        self.cancelled = False

    async def open(self, key: str, *, chunk_bytes: int) -> ObjectStream:
        opened = await super().open(key, chunk_bytes=chunk_bytes)
        if key.startswith("staging/v1/") and self.cancel is not None and not self.cancelled:
            self.cancelled = True
            await self.cancel()
        return opened


class CorruptFinalStore(InMemoryObjectStore):
    async def put(self, **kwargs: object) -> PutResult:
        result = await super().put(**kwargs)  # type: ignore[arg-type]
        if str(kwargs["key"]).startswith("artifacts/v1/"):
            self.corrupt(str(kwargs["key"]), b"x" * int(result.descriptor.size_bytes))
        return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancellation_between_final_object_and_database_commit_wins(tmp_path: Path) -> None:
    objects = CancelOnFinalPutStore()
    database, actor, reference, runs, _, adopter = await _stack(tmp_path, objects=objects)

    async def cancel() -> None:
        assert await runs.request_cancellation(reference) is RunState.CANCELLING

    objects.cancel = cancel
    try:
        claim, digest = await _stage(objects)
        result = await adopter.adopt(reference, claim)
        assert result.state is RunState.CANCELLED
        assert result.artifact_id is None
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
            run = await session.get(Run, UUID(reference.run_id))
        assert run is not None
        assert run.result_summary is None
        final_key = artifact_storage_key(
            organization_id=actor.organization_id,
            project_id=DEMO_PROJECT_ID,
            run_id=UUID(reference.run_id),
            kind=ArtifactKind.RUN_RESULT,
            sha256=digest,
        )
        assert (await objects.stat(final_key)).sha256 == digest
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancellation_before_and_after_staging_read_prevents_publication(
    tmp_path: Path,
) -> None:
    database, _, reference, runs, objects, adopter = await _stack(tmp_path)
    try:
        claim, _ = await _stage(objects)
        assert await runs.request_cancellation(reference) is RunState.CANCELLING
        result = await adopter.adopt(reference, claim)
        assert result == type(result)(RunState.CANCELLED, None, False)
        assert not await objects.list_keys("artifacts/v1/", limit=10)
    finally:
        await database.dispose()

    objects = CancelOnStagingReadStore()
    database, _, reference, runs, _, adopter = await _stack(tmp_path, objects=objects)

    async def cancel() -> None:
        assert await runs.request_cancellation(reference) is RunState.CANCELLING

    objects.cancel = cancel
    try:
        claim, _ = await _stage(objects)
        result = await adopter.adopt(reference, claim)
        assert result == type(result)(RunState.CANCELLED, None, False)
        assert not await objects.list_keys("artifacts/v1/", limit=10)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corrupt_final_object_fails_closed_without_metadata(tmp_path: Path) -> None:
    objects = CorruptFinalStore()
    database, _, reference, _, _, adopter = await _stack(tmp_path, objects=objects)
    try:
        claim, _ = await _stage(objects)
        with pytest.raises(ArtifactAdoptionError) as corrupt:
            await adopter.adopt(reference, claim)
        assert (corrupt.value.code, corrupt.value.retryable) == (
            "artifact_store_integrity",
            False,
        )
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
    finally:
        await database.dispose()


class ListingFailureStore(InMemoryObjectStore):
    async def list_keys(
        self,
        prefix: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> tuple[str, ...]:
        del prefix, limit, after
        raise ObjectStoreError("private storage detail")


class VanishingCleanupStore(InMemoryObjectStore):
    vanish = False

    async def stat(self, key: str) -> ObjectDescriptor:
        if self.vanish and key.startswith("staging/v1/"):
            await self.delete(key)
            raise ObjectNotFoundError("object not found")
        return await super().stat(key)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_staging_cleanup_is_graceful_cursor_bounded_and_retry_safe(tmp_path: Path) -> None:
    database, _, _, _, objects, adopter = await _stack(tmp_path)
    try:
        for value in (b"one", b"two", b"three"):
            digest = hashlib.sha256(value).hexdigest()
            await objects.put(
                key=staged_artifact_storage_key(digest),
                content=value,
                sha256=digest,
                media_type="application/json",
            )
        deferred = await adopter.cleanup_staging(limit=2, now=datetime.now(UTC))
        assert (deferred.scanned, deferred.deferred, deferred.deleted) == (2, 2, 0)

        first = await adopter.cleanup_staging(
            limit=2,
            now=datetime.now(UTC) + timedelta(hours=2),
        )
        assert (first.scanned, first.deleted) == (2, 2)
        assert first.next_cursor is not None
        second = await adopter.cleanup_staging(
            cursor=first.next_cursor,
            limit=2,
            now=datetime.now(UTC) + timedelta(hours=2),
        )
        assert (second.scanned, second.deleted, second.next_cursor) == (1, 1, None)

        value = b"retry"
        digest = hashlib.sha256(value).hexdigest()
        await objects.put(
            key=staged_artifact_storage_key(digest),
            content=value,
            sha256=digest,
            media_type="application/json",
        )
        objects.fail_delete = True
        failed = await adopter.cleanup_staging(now=datetime.now(UTC) + timedelta(hours=2))
        assert (failed.scanned, failed.failed, failed.deleted) == (1, 1, 0)
        objects.fail_delete = False
        recovered = await adopter.cleanup_staging(now=datetime.now(UTC) + timedelta(hours=2))
        assert (recovered.scanned, recovered.deleted) == (1, 1)
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_staging_cleanup_rejects_bad_batches_and_sanitizes_listing_failure(
    tmp_path: Path,
) -> None:
    database, _, _, _, _, adopter = await _stack(tmp_path)
    try:
        with pytest.raises(ValueError, match="cleanup limit"):
            await adopter.cleanup_staging(limit=0)
    finally:
        await database.dispose()

    failing = ListingFailureStore()
    database, _, _, _, _, adopter = await _stack(tmp_path, objects=failing)
    try:
        with pytest.raises(ArtifactAdoptionError) as unavailable:
            await adopter.cleanup_staging()
        assert (unavailable.value.code, unavailable.value.retryable) == (
            "staging_store_unavailable",
            True,
        )
        assert str(unavailable.value) == "staging_store_unavailable"
    finally:
        await database.dispose()

    vanishing = VanishingCleanupStore()
    database, _, _, _, _, adopter = await _stack(tmp_path, objects=vanishing)
    try:
        await _stage(vanishing)
        vanishing.vanish = True
        report = await adopter.cleanup_staging(now=datetime.now(UTC) + timedelta(hours=2))
        assert (report.scanned, report.deleted, report.failed) == (1, 0, 0)
    finally:
        await database.dispose()
