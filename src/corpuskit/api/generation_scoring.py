"""Standalone FastAPI router for bounded generation preview and scoring."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from corpuskit.domain.generation import (
    CompositeScoringRequest,
    CompositeScoringResult,
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


class GenerationPreviewHttpService(Protocol):
    def preview(self, request: RepositoryGenerationRequest) -> RepositoryGenerationResult: ...

    def validate_worker(
        self,
        request: RepositoryGenerationRequest,
    ) -> RepositoryGenerationValidation: ...


class ScoringHttpService(Protocol):
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


def generation_scoring_router(
    generation: GenerationPreviewHttpService,
    scoring: ScoringHttpService,
) -> APIRouter:
    """Build isolated routes without mutating the shared application factory."""

    router = APIRouter()

    @router.post("/generation/preview", response_model=RepositoryGenerationResult)
    async def generation_preview(
        request: Annotated[RepositoryGenerationRequest, Body()],
    ) -> RepositoryGenerationResult:
        return await run_in_threadpool(generation.preview, request)

    @router.post(
        "/generation/repository/validate",
        response_model=RepositoryGenerationValidation,
    )
    async def validate_repository_generation(
        request: Annotated[RepositoryGenerationRequest, Body()],
    ) -> RepositoryGenerationValidation:
        return await run_in_threadpool(generation.validate_worker, request)

    @router.post("/scoring/composite", response_model=CompositeScoringResult)
    async def composite(
        request: Annotated[CompositeScoringRequest, Body()],
    ) -> CompositeScoringResult:
        return await run_in_threadpool(scoring.composite, request)

    @router.post("/scoring/ngram/scorers", response_model=PhonotacticArtifact)
    async def train_ngram_scorer(
        request: Annotated[NgramScorerTrainingRequest, Body()],
    ) -> PhonotacticArtifact:
        return await run_in_threadpool(scoring.train_ngram_scorer, request)

    @router.post("/scoring/ngram/constraints", response_model=PhonotacticArtifact)
    async def train_ngram_constraint(
        request: Annotated[NgramConstraintTrainingRequest, Body()],
    ) -> PhonotacticArtifact:
        return await run_in_threadpool(scoring.train_ngram_constraint, request)

    @router.post("/scoring/phonotactics", response_model=PhonotacticScoreResult)
    async def score_phonotactics(
        request: Annotated[PhonotacticScoreRequest, Body()],
    ) -> PhonotacticScoreResult:
        return await run_in_threadpool(scoring.score_phonotactics, request)

    @router.post("/scoring/readability", response_model=ReadabilityBatchResult)
    async def readability(
        request: Annotated[ReadabilityRequest, Body()],
    ) -> ReadabilityBatchResult:
        return await run_in_threadpool(scoring.readability, request)

    return router


__all__ = [
    "GenerationPreviewHttpService",
    "ScoringHttpService",
    "generation_scoring_router",
]
