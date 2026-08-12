"""Policy layer for bounded generation activities and synchronous scoring."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

from corpuskit.domain.errors import (
    ApplicationError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    MAX_SOURCE_ITEMS,
    MAX_SYNC_SOURCE_ITEMS,
    AcceptedCandidate,
    CompositeScoringRequest,
    CompositeScoringResult,
    GenerationExecutionMode,
    GenerationPhase,
    GenerationProgress,
    HuggingFaceRepository,
    HuggingFaceRepositorySpec,
    NgramConstraintTrainingRequest,
    NgramScorerTrainingRequest,
    PhonotacticArtifact,
    PhonotacticScoreRequest,
    PhonotacticScoreResult,
    ReadabilityBatchResult,
    ReadabilityRequest,
    RepositoryGenerationRequest,
    RepositoryGenerationResult,
    RepositoryGenerationValidation,
)

MAX_REPOSITORY_TEXT_BYTES = 1_048_576
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$", re.ASCII)


class GenerationEngine(Protocol):
    """Adapter contract consumed by both preview and worker handlers."""

    def run_repository(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        on_accepted: Callable[[AcceptedCandidate, float], None] | None = None,
    ) -> RepositoryGenerationResult: ...


class ScoringEngine(Protocol):
    def composite(self, request: CompositeScoringRequest) -> CompositeScoringResult: ...

    def train_ngram_scorer(
        self,
        request: NgramScorerTrainingRequest,
    ) -> PhonotacticArtifact: ...

    def train_ngram_constraint(
        self,
        request: NgramConstraintTrainingRequest,
    ) -> PhonotacticArtifact: ...

    def score_phonotactics(self, request: PhonotacticScoreRequest) -> PhonotacticScoreResult: ...

    def readability(self, request: ReadabilityRequest) -> ReadabilityBatchResult: ...


ProgressSink = Callable[[GenerationProgress], None]


class GenerationCoordinator:
    """Pure state-machine handler suitable for a Temporal activity function."""

    def __init__(
        self,
        engine: GenerationEngine,
        *,
        allowed_huggingface_revisions: frozenset[tuple[str, str, str]] = frozenset(),
        allowed_huggingface_sources: tuple[HuggingFaceRepositorySpec, ...] = (),
    ) -> None:
        self._engine = engine
        self._allowed_huggingface_revisions = allowed_huggingface_revisions
        self._allowed_huggingface_sources = allowed_huggingface_sources

    def execute(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        emit: ProgressSink | None = None,
    ) -> RepositoryGenerationResult:
        """Validate, run, and emit sanitized deterministic progress events."""

        sequence = 0

        def publish(
            phase: GenerationPhase,
            *,
            iteration: int = 0,
            coverage: float = 0.0,
            accepted_count: int = 0,
            accepted: AcceptedCandidate | None = None,
            result: RepositoryGenerationResult | None = None,
        ) -> None:
            nonlocal sequence
            if emit is not None:
                emit(
                    GenerationProgress(
                        sequence=sequence,
                        phase=phase,
                        iteration=iteration,
                        coverage=coverage,
                        accepted_count=accepted_count,
                        accepted_source_id=(accepted.source_id if accepted else None),
                        coverage_gain=(accepted.coverage_gain if accepted else None),
                        stop_reason=(result.stop_reason if result else None),
                    )
                )
            sequence += 1

        publish(GenerationPhase.VALIDATING)
        try:
            self.validate(request, execution_mode=execution_mode)
            publish(GenerationPhase.PREPARING_REPOSITORY)
            publish(GenerationPhase.GENERATING)
            accepted_count = 0

            def accepted(candidate: AcceptedCandidate, coverage: float) -> None:
                nonlocal accepted_count
                accepted_count += 1
                publish(
                    GenerationPhase.CANDIDATE_ACCEPTED,
                    iteration=candidate.iteration,
                    coverage=coverage,
                    accepted_count=accepted_count,
                    accepted=candidate,
                )

            result = self._engine.run_repository(
                request,
                execution_mode=execution_mode,
                on_accepted=accepted,
            )
            if len(result.accepted) != accepted_count:
                raise EngineUnavailableError("generation.progress")
            publish(
                GenerationPhase.FINISHED,
                iteration=result.iterations,
                coverage=result.coverage,
                accepted_count=len(result.accepted),
                result=result,
            )
            return result
        except ApplicationError:
            publish(GenerationPhase.FAILED)
            raise
        except Exception:
            publish(GenerationPhase.FAILED)
            raise EngineUnavailableError("generation.execute") from None

    def validate(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
    ) -> None:
        """Apply allowlists and mode-specific limits without invoking the engine."""
        validate_repository_request(
            request,
            execution_mode=execution_mode,
            allowed_huggingface_revisions=self._allowed_huggingface_revisions,
            allowed_huggingface_sources=self._allowed_huggingface_sources,
        )


class GenerationPreviewService:
    """Optional bounded synchronous preview; never imports remote datasets."""

    def __init__(self, coordinator: GenerationCoordinator) -> None:
        self._coordinator = coordinator

    def preview(self, request: RepositoryGenerationRequest) -> RepositoryGenerationResult:
        return self._coordinator.execute(
            request,
            execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
        )

    def validate_worker(
        self,
        request: RepositoryGenerationRequest,
    ) -> RepositoryGenerationValidation:
        """Authorize a durable request without opening the repository or executing CorpusGen."""

        self._coordinator.validate(
            request,
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
        source = request.source
        source_item_limit = (
            source.spec.max_samples
            if isinstance(source, HuggingFaceRepository)
            else len(source.entries)
        )
        return RepositoryGenerationValidation(
            source_kind=source.kind,
            source_item_limit=source_item_limit,
            activity_timeout_seconds=request.activity_timeout_seconds,
        )


def validate_repository_request(
    request: RepositoryGenerationRequest,
    *,
    execution_mode: GenerationExecutionMode,
    allowed_huggingface_revisions: frozenset[tuple[str, str, str]] = frozenset(),
    allowed_huggingface_sources: tuple[HuggingFaceRepositorySpec, ...] = (),
) -> None:
    """Apply the exact no-I/O repository policy shared by API admission and workers."""

    source = request.source
    if isinstance(source, HuggingFaceRepository):
        if execution_mode is GenerationExecutionMode.SYNCHRONOUS_PREVIEW:
            raise InvalidRequestError("generation.preview.remote_source")
        if _LANGUAGE_PATTERN.fullmatch(source.spec.language) is None:
            raise InvalidRequestError("generation.repository.language")
        exact_match = any(
            source.spec.dataset == policy.dataset
            and source.spec.config == policy.config
            and source.spec.split == policy.split
            and source.spec.text_column == policy.text_column
            and source.spec.revision == policy.revision
            and source.spec.language == policy.language
            and source.spec.max_samples <= policy.max_samples
            for policy in allowed_huggingface_sources
        )
        legacy_pin = (source.spec.dataset, source.spec.config, source.spec.revision)
        if not exact_match and legacy_pin not in allowed_huggingface_revisions:
            raise InvalidRequestError("generation.huggingface.allowlist")
        return

    entries = source.entries
    limit = (
        MAX_SYNC_SOURCE_ITEMS
        if execution_mode is GenerationExecutionMode.SYNCHRONOUS_PREVIEW
        else MAX_SOURCE_ITEMS
    )
    if len(entries) > limit:
        raise InvalidRequestError("generation.repository.size")
    language = getattr(source, "language", "en-us")
    if _LANGUAGE_PATTERN.fullmatch(language) is None:
        raise InvalidRequestError("generation.repository.language")
    total_bytes = sum(len(item.text.encode("utf-8")) for item in entries)
    if total_bytes > MAX_REPOSITORY_TEXT_BYTES:
        raise InvalidRequestError("generation.repository.payload")


class ScoringService:
    """Thin synchronous policy boundary around deterministic scoring operations."""

    def __init__(self, engine: ScoringEngine) -> None:
        self._engine = engine

    def composite(self, request: CompositeScoringRequest) -> CompositeScoringResult:
        return self._engine.composite(request)

    def train_ngram_scorer(
        self,
        request: NgramScorerTrainingRequest,
    ) -> PhonotacticArtifact:
        return self._engine.train_ngram_scorer(request)

    def train_ngram_constraint(
        self,
        request: NgramConstraintTrainingRequest,
    ) -> PhonotacticArtifact:
        return self._engine.train_ngram_constraint(request)

    def score_phonotactics(self, request: PhonotacticScoreRequest) -> PhonotacticScoreResult:
        return self._engine.score_phonotactics(request)

    def readability(self, request: ReadabilityRequest) -> ReadabilityBatchResult:
        return self._engine.readability(request)


__all__ = [
    "GenerationCoordinator",
    "GenerationEngine",
    "GenerationPreviewService",
    "ProgressSink",
    "ScoringEngine",
    "ScoringService",
    "validate_repository_request",
]
