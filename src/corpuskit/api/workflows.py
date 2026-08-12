"""Interactive corpus workflow HTTP contracts."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from corpuskit.domain import (
    CorpusEvaluation,
    CorpusSelection,
    CoverageUnit,
    EvaluationTarget,
    G2PTranscription,
    SelectionOptions,
)
from corpuskit.services import (
    MAX_SYNC_EVALUATION_SENTENCES,
    MAX_SYNC_G2P_ITEMS,
    MAX_SYNC_SELECTION_CANDIDATES,
)


class WorkflowService(Protocol):
    """Application-service surface used by workflow endpoints."""

    def phonemize(self, text: str, *, language: str) -> G2PTranscription: ...

    def phonemize_batch(
        self, texts: tuple[str, ...], *, language: str
    ) -> tuple[G2PTranscription, ...]: ...

    def evaluate(
        self,
        sentences: tuple[str, ...],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
    ) -> CorpusEvaluation: ...

    def select(
        self,
        candidates: tuple[str, ...],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
        options: SelectionOptions,
    ) -> CorpusSelection: ...


class WorkflowRequest(BaseModel):
    """Strict base transport contract."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class G2PRequest(WorkflowRequest):
    text: str
    language: str = Field(default="en-us", min_length=2, max_length=32)


class G2PBatchRequest(WorkflowRequest):
    texts: tuple[str, ...] = Field(min_length=1, max_length=MAX_SYNC_G2P_ITEMS)
    language: str = Field(default="en-us", min_length=2, max_length=32)


class EvaluationRequest(WorkflowRequest):
    sentences: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_SYNC_EVALUATION_SENTENCES,
    )
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: CoverageUnit = CoverageUnit.PHONEME
    target: EvaluationTarget = EvaluationTarget()


class SelectionHttpRequest(WorkflowRequest):
    candidates: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_SYNC_SELECTION_CANDIDATES,
    )
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: CoverageUnit = CoverageUnit.PHONEME
    target: EvaluationTarget = EvaluationTarget()
    options: SelectionOptions = SelectionOptions()


def workflow_router(service: WorkflowService) -> APIRouter:
    """Create routes bound to an explicitly supplied application service."""

    router = APIRouter()

    @router.post("/g2p", response_model=G2PTranscription)
    async def phonemize(request: Annotated[G2PRequest, Body()]) -> G2PTranscription:
        return await run_in_threadpool(
            service.phonemize,
            request.text,
            language=request.language,
        )

    @router.post("/g2p/batch", response_model=tuple[G2PTranscription, ...])
    async def phonemize_batch(
        request: Annotated[G2PBatchRequest, Body()],
    ) -> tuple[G2PTranscription, ...]:
        return await run_in_threadpool(
            service.phonemize_batch,
            request.texts,
            language=request.language,
        )

    @router.post("/evaluations", response_model=CorpusEvaluation)
    async def evaluate(
        request: Annotated[EvaluationRequest, Body()],
    ) -> CorpusEvaluation:
        return await run_in_threadpool(
            service.evaluate,
            request.sentences,
            language=request.language,
            unit=request.unit,
            target=request.target,
        )

    @router.post("/selections", response_model=CorpusSelection)
    async def select(
        request: Annotated[SelectionHttpRequest, Body()],
    ) -> CorpusSelection:
        return await run_in_threadpool(
            service.select,
            request.candidates,
            language=request.language,
            unit=request.unit,
            target=request.target,
            options=request.options,
        )

    return router


__all__ = [
    "EvaluationRequest",
    "G2PBatchRequest",
    "G2PRequest",
    "SelectionHttpRequest",
    "WorkflowService",
    "workflow_router",
]
