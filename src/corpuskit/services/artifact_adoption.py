"""Authoritative parent-side adoption of unowned child-process result bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ArtifactKind,
    StagedArtifactResult,
    artifact_storage_key,
    staged_artifact_storage_key,
)
from corpuskit.domain.datg import (
    DatgGuidedGenerationResult,
    DatgIndexBuildRequest,
    DatgIndexBuildResult,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import ApplicationError, EngineUnavailableError
from corpuskit.domain.generation import RepositoryGenerationResult
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.model_runtime import (
    HostedGenerationResult,
    LanguageModelAnalysisResult,
    LocalGenerationResult,
)
from corpuskit.domain.phon_rl import PhonRlTrainingResult
from corpuskit.domain.selection import (
    MAX_SELECTION_RESULT_ARTIFACT_BYTES,
    SELECTION_ARTIFACT_SCHEMA_ID,
    CorpusSelectionArtifactV1,
)
from corpuskit.persistence.artifact_store import (
    ObjectDescriptor,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)
from corpuskit.services.datg import DatgIndexPublisher, DatgRuntimePolicy
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.store import (
    AdoptedArtifact,
    AdoptedDatgIndex,
    ArtifactCommit,
    DurableRunStore,
    RunStoreError,
)

_STAGING_PREFIX = "staging/v1/sha256/"
_RESULT_SCHEMAS: dict[RunKind, tuple[str, type[BaseModel]]] = {
    RunKind.SELECT: (
        SELECTION_ARTIFACT_SCHEMA_ID,
        CorpusSelectionArtifactV1,
    ),
    RunKind.GENERATE_REPOSITORY: (
        "corpuskit.repository-generation-result.v1",
        RepositoryGenerationResult,
    ),
    RunKind.GENERATE_LLM: (
        "corpuskit.hosted-generation-result.v1",
        HostedGenerationResult,
    ),
    RunKind.GENERATE_LOCAL: (
        "corpuskit.local-generation-result.v1",
        LocalGenerationResult,
    ),
    RunKind.PERPLEXITY: (
        "corpuskit.language-model-analysis-result.v1",
        LanguageModelAnalysisResult,
    ),
    RunKind.BUILD_DATG_INDEX: (
        "corpuskit.datg-index-build-result.v1",
        DatgIndexBuildResult,
    ),
    RunKind.GENERATE_DATG: (
        "corpuskit.datg-guided-generation-result.v1",
        DatgGuidedGenerationResult,
    ),
    RunKind.TRAIN_PHON_RL: (
        "corpuskit.phon-rl-training-result.v1",
        PhonRlTrainingResult,
    ),
}


class ArtifactAdoptionError(RuntimeError):
    """A sanitized staged-result failure suitable for durable retry classification."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StagingCleanupReport:
    scanned: int
    deleted: int
    deferred: int
    failed: int
    next_cursor: str | None


class ArtifactAdoptionService:
    """Validate staged bytes and publish them using facts reloaded from the run row."""

    def __init__(
        self,
        runs: DurableRunStore,
        objects: ObjectStore,
        settings: Settings,
        *,
        adoption_runs: DurableRunStore | None = None,
        datg_index_publisher: DatgIndexPublisher | None = None,
        datg_runtime_versions: tuple[str, str] | None = None,
    ) -> None:
        if (datg_index_publisher is None) != (datg_runtime_versions is None):
            raise ValueError("DATG publication requires trusted parent runtime versions")
        self._runs = runs
        self._adoption_runs = adoption_runs or runs
        self._objects = objects
        self._max_bytes = settings.artifact_max_bytes
        self._chunk_bytes = settings.artifact_download_chunk_bytes
        self._retention = timedelta(days=settings.artifact_retention_days)
        self._staging_grace = timedelta(seconds=settings.artifact_orphan_grace_seconds)
        self._datg_runtime_policies = settings.worker_datg_runtime_policies
        self._datg_index_publisher = datg_index_publisher
        self._datg_runtime_versions = datg_runtime_versions

    @staticmethod
    def requires_adoption(kind: RunKind) -> bool:
        return kind in _RESULT_SCHEMAS

    async def adopt(
        self,
        reference: RunWorkflowReference,
        summary: dict[str, Any],
    ) -> ArtifactCommit:
        claim = self._claim(summary)
        record = await self._runs.execution_record(reference)
        expected = _RESULT_SCHEMAS.get(record.kind)
        if expected is None:
            raise ArtifactAdoptionError("staged_result_unsupported", retryable=False)
        expected_schema, model = expected
        if claim.schema_id != expected_schema:
            raise ArtifactAdoptionError("staged_result_schema_mismatch", retryable=False)
        if claim.size_bytes > self._max_bytes:
            raise ArtifactAdoptionError("staged_result_size_mismatch", retryable=False)
        if record.kind is RunKind.SELECT and claim.size_bytes > MAX_SELECTION_RESULT_ARTIFACT_BYTES:
            raise ArtifactAdoptionError("staged_result_size_mismatch", retryable=False)
        if record.state in {
            RunState.CANCELLING,
            RunState.CANCELLED,
        } or await self._runs.cancellation_requested(reference):
            state = await self._runs.acknowledge_cancellation(reference)
            return ArtifactCommit(state, None, created=False)

        staging_key = staged_artifact_storage_key(claim.sha256)
        payload = await self._read_and_verify(staging_key, claim)
        parsed = self._validate_payload(payload, claim.schema_id, model)
        self._validate_selection(record.kind, record.spec, parsed)
        datg_build = self._validated_datg_build(record.kind, record.spec, parsed)

        if await self._runs.cancellation_requested(reference):
            state = await self._runs.acknowledge_cancellation(reference)
            return ArtifactCommit(state, None, created=False)

        final_key = artifact_storage_key(
            organization_id=record.organization_id,
            project_id=record.project_id,
            run_id=record.run_id,
            kind=ArtifactKind.RUN_RESULT,
            sha256=claim.sha256,
        )
        try:
            stored = await self._objects.put(
                key=final_key,
                content=payload,
                sha256=claim.sha256,
                media_type=claim.media_type,
            )
            self._verify_descriptor(stored.descriptor, final_key, claim)
            final_payload = await self._read_and_verify(final_key, claim)
            if final_payload != payload:
                raise ObjectIntegrityError("final artifact content mismatch")
        except ArtifactAdoptionError as exc:
            raise ArtifactAdoptionError(
                "artifact_store_unavailable" if exc.retryable else "artifact_store_integrity",
                retryable=exc.retryable,
            ) from exc
        except ObjectIntegrityError as exc:
            raise ArtifactAdoptionError("artifact_store_integrity", retryable=False) from exc
        except (ObjectStoreError, ValueError) as exc:
            raise ArtifactAdoptionError("artifact_store_unavailable", retryable=True) from exc

        adopted_datg_index: AdoptedDatgIndex | None = None
        if datg_build is not None:
            if self._datg_index_publisher is None:
                raise ArtifactAdoptionError(
                    "datg_index_publication_unavailable",
                    retryable=False,
                )
            request, result = datg_build
            try:
                published_size = self._datg_index_publisher.publish(result.artifact)
            except EngineUnavailableError as exc:
                integrity_failure = exc.operation in {
                    "datg.index.publication_boundary",
                    "datg.index.publication_conflict",
                    "datg.index.publication_identity",
                    "datg.index.publication_integrity",
                    "datg.index.publication_size",
                }
                raise ArtifactAdoptionError(
                    "datg_index_publication_integrity"
                    if integrity_failure
                    else "datg_index_publication_unavailable",
                    retryable=not integrity_failure,
                ) from exc
            adopted_datg_index = AdoptedDatgIndex(
                cache_key_sha256=result.artifact.identity.cache_key_sha256,
                content_sha256=result.artifact.content_sha256,
                runtime_id=request.runtime_id,
                language=result.artifact.identity.language,
                unit=result.artifact.identity.unit.value,
                vocabulary_size=result.artifact.vocabulary_size,
                indexed_token_count=result.artifact.indexed_token_count,
                size_bytes=published_size,
            )

        try:
            return await self._adoption_runs.commit_adopted_result(
                reference,
                AdoptedArtifact(
                    sha256=claim.sha256,
                    size_bytes=claim.size_bytes,
                    storage_key=final_key,
                    media_type=claim.media_type,
                    filename=f"{record.kind.value}-result.json",
                    schema_id=claim.schema_id,
                    retention_until=datetime.now(UTC) + self._retention,
                    datg_index=adopted_datg_index,
                ),
            )
        except RunStoreError:
            raise
        except Exception as exc:
            raise ArtifactAdoptionError("persistence_unavailable", retryable=True) from exc

    async def cleanup_staging(
        self,
        *,
        cursor: str | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> StagingCleanupReport:
        if not 1 <= limit <= 1_000:
            raise ValueError("staging cleanup limit is invalid")
        cutoff = (now or datetime.now(UTC)).astimezone(UTC) - self._staging_grace
        try:
            keys = await self._objects.list_keys(
                _STAGING_PREFIX,
                limit=limit,
                after=cursor,
            )
        except (ObjectStoreError, ValueError) as exc:
            raise ArtifactAdoptionError("staging_store_unavailable", retryable=True) from exc
        deleted = deferred = failed = 0
        for key in keys:
            try:
                descriptor = await self._objects.stat(key)
                if descriptor.modified_at > cutoff:
                    deferred += 1
                    continue
                await self._objects.delete(key)
                deleted += 1
            except ObjectNotFoundError:
                continue
            except ObjectStoreError:
                failed += 1
        return StagingCleanupReport(
            scanned=len(keys),
            deleted=deleted,
            deferred=deferred,
            failed=failed,
            next_cursor=keys[-1] if len(keys) == limit else None,
        )

    @staticmethod
    def _claim(summary: dict[str, Any]) -> StagedArtifactResult:
        try:
            return StagedArtifactResult.model_validate(summary, strict=True)
        except ValidationError:
            raise ArtifactAdoptionError("staged_result_contract", retryable=False) from None

    async def _read_and_verify(
        self,
        key: str,
        claim: StagedArtifactResult,
    ) -> bytes:
        try:
            opened = await self._objects.open(key, chunk_bytes=self._chunk_bytes)
            self._verify_descriptor(opened.descriptor, key, claim)
            digest = hashlib.sha256()
            size = 0
            chunks: list[bytes] = []
            async for chunk in opened.chunks:
                size += len(chunk)
                if size > claim.size_bytes or size > self._max_bytes:
                    raise ArtifactAdoptionError("staged_result_size_mismatch", retryable=False)
                digest.update(chunk)
                chunks.append(chunk)
        except ArtifactAdoptionError:
            raise
        except ObjectNotFoundError as exc:
            raise ArtifactAdoptionError("staged_result_missing", retryable=True) from exc
        except ObjectIntegrityError as exc:
            raise ArtifactAdoptionError("staged_result_corrupt", retryable=False) from exc
        except (ObjectStoreError, ValueError) as exc:
            raise ArtifactAdoptionError("staging_store_unavailable", retryable=True) from exc
        if size != claim.size_bytes:
            raise ArtifactAdoptionError("staged_result_size_mismatch", retryable=False)
        if digest.hexdigest() != claim.sha256:
            raise ArtifactAdoptionError("staged_result_digest_mismatch", retryable=False)
        return b"".join(chunks)

    @staticmethod
    def _validate_payload(payload: bytes, schema_id: str, model: type[BaseModel]) -> BaseModel:
        try:
            parsed = model.model_validate_json(payload, strict=True)
        except ValidationError:
            raise ArtifactAdoptionError("staged_result_schema_mismatch", retryable=False) from None
        if getattr(parsed, "schema_id", None) != schema_id:
            raise ArtifactAdoptionError("staged_result_schema_mismatch", retryable=False)
        return parsed

    def _validated_datg_build(
        self,
        kind: RunKind,
        spec: dict[str, Any],
        parsed: BaseModel,
    ) -> tuple[DatgIndexBuildRequest, DatgIndexBuildResult] | None:
        if kind is not RunKind.BUILD_DATG_INDEX:
            return None
        if not isinstance(parsed, DatgIndexBuildResult):
            raise ArtifactAdoptionError("staged_result_schema_mismatch", retryable=False)
        try:
            request = DatgIndexBuildRequest.model_validate(spec)
            policy = DatgRuntimePolicy(
                self._datg_runtime_policies,
                worker_profile=DatgWorkerProfile.LOCAL_CPU,
            ).authorize(request.runtime_id)
        except (ApplicationError, TypeError, ValueError):
            raise ArtifactAdoptionError("datg_index_publication_policy", retryable=False) from None
        artifact = parsed.artifact
        identity = artifact.identity
        runtime_versions = self._datg_runtime_versions
        if (
            runtime_versions is None
            or identity.tokenizer_id != policy.tokenizer.repository_id
            or identity.tokenizer_revision != policy.tokenizer.revision
            or identity.tokenizer_snapshot_sha256 != policy.tokenizer.snapshot_sha256
            or identity.corpusgen_version != runtime_versions[0]
            or identity.espeak_version != runtime_versions[1]
            or identity.language != request.language
            or identity.unit is not request.unit
            or artifact.vocabulary_size > request.max_vocabulary_size
            or parsed.elapsed_seconds > request.activity_timeout_seconds + 1.0
        ):
            raise ArtifactAdoptionError("datg_index_publication_policy", retryable=False)
        return request, parsed

    @staticmethod
    def _validate_selection(
        kind: RunKind,
        spec: dict[str, Any],
        parsed: BaseModel,
    ) -> None:
        if kind is not RunKind.SELECT:
            return
        if not isinstance(parsed, CorpusSelectionArtifactV1):
            raise ArtifactAdoptionError("staged_result_schema_mismatch", retryable=False)
        try:
            parsed.validate_run_spec(spec)
        except (TypeError, ValueError):
            raise ArtifactAdoptionError("staged_result_spec_mismatch", retryable=False) from None

    @staticmethod
    def _verify_descriptor(
        descriptor: ObjectDescriptor,
        key: str,
        claim: StagedArtifactResult,
    ) -> None:
        if (
            descriptor.key != key
            or descriptor.sha256 != claim.sha256
            or descriptor.size_bytes != claim.size_bytes
            or descriptor.media_type != claim.media_type
        ):
            raise ObjectIntegrityError("artifact descriptor mismatch")


__all__ = [
    "ArtifactAdoptionError",
    "ArtifactAdoptionService",
    "StagingCleanupReport",
]
