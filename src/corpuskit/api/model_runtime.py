"""Validation-only HTTP routes for worker model operations."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body

from corpuskit.domain.model_runtime import (
    HostedCostEstimate,
    HostedGenerationRequest,
    LanguageModelAnalysisEstimate,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    RuntimeValidationResult,
)


class ModelRuntimeHttpPolicy(Protocol):
    """Pure policy surface. It must not hold provider clients or model loaders."""

    def validate_hosted(self, request: HostedGenerationRequest) -> RuntimeValidationResult: ...

    def estimate_hosted(self, request: HostedGenerationRequest) -> HostedCostEstimate: ...

    def validate_local(self, request: LocalGenerationRequest) -> RuntimeValidationResult: ...

    def validate_analysis(
        self,
        request: LanguageModelAnalysisRequest,
    ) -> RuntimeValidationResult: ...

    def estimate_analysis(
        self,
        request: LanguageModelAnalysisRequest,
    ) -> LanguageModelAnalysisEstimate: ...


def model_runtime_router(policy: ModelRuntimeHttpPolicy) -> APIRouter:
    """Build safe control-plane routes; model execution is intentionally absent."""

    router = APIRouter()

    @router.post(
        "/model-runtime/hosted/validate",
        response_model=RuntimeValidationResult,
    )
    async def validate_hosted(
        request: Annotated[HostedGenerationRequest, Body()],
    ) -> RuntimeValidationResult:
        return policy.validate_hosted(request)

    @router.post(
        "/model-runtime/hosted/estimate",
        response_model=HostedCostEstimate,
    )
    async def estimate_hosted(
        request: Annotated[HostedGenerationRequest, Body()],
    ) -> HostedCostEstimate:
        return policy.estimate_hosted(request)

    @router.post(
        "/model-runtime/local/validate",
        response_model=RuntimeValidationResult,
    )
    async def validate_local(
        request: Annotated[LocalGenerationRequest, Body()],
    ) -> RuntimeValidationResult:
        return policy.validate_local(request)

    @router.post(
        "/model-runtime/analysis/validate",
        response_model=RuntimeValidationResult,
    )
    async def validate_analysis(
        request: Annotated[LanguageModelAnalysisRequest, Body()],
    ) -> RuntimeValidationResult:
        return policy.validate_analysis(request)

    @router.post(
        "/model-runtime/analysis/estimate",
        response_model=LanguageModelAnalysisEstimate,
    )
    async def estimate_analysis(
        request: Annotated[LanguageModelAnalysisRequest, Body()],
    ) -> LanguageModelAnalysisEstimate:
        return policy.estimate_analysis(request)

    return router


__all__ = ["ModelRuntimeHttpPolicy", "model_runtime_router"]
