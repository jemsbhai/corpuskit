"""Standalone FastAPI router for the Coverage and Weighting Lab."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body, Query
from starlette.concurrency import run_in_threadpool

from corpuskit.domain.lab import (
    CoverageLabRequest,
    CoverageLabResult,
    ExportedReport,
    ExportReportRequest,
    G2PLanguages,
    G2PVariants,
    G2PVariantsRequest,
    RenderedReport,
    RenderReportRequest,
    RuntimeOverview,
    TargetSpaceEstimate,
    TargetSpaceRequest,
    WeightComputeRequest,
    WeightSet,
    WeightValidationRequest,
    WeightValidationResult,
)


class LabHttpService(Protocol):
    def runtime(self, *, force: bool = False) -> RuntimeOverview: ...

    def g2p_languages(self) -> G2PLanguages: ...

    def g2p_variants(self, request: G2PVariantsRequest) -> G2PVariants: ...

    def estimate(self, request: TargetSpaceRequest) -> TargetSpaceEstimate: ...

    def coverage(self, request: CoverageLabRequest) -> CoverageLabResult: ...

    def render_report(self, request: RenderReportRequest) -> RenderedReport: ...

    def export_report(self, request: ExportReportRequest) -> ExportedReport: ...

    def compute_weights(self, request: WeightComputeRequest) -> WeightSet: ...

    def validate_weights(self, request: WeightValidationRequest) -> WeightValidationResult: ...


def coverage_weighting_lab_router(service: LabHttpService) -> APIRouter:
    """Build lab routes without mutating the shared application factory."""

    router = APIRouter()

    @router.get("/labs/runtime", response_model=RuntimeOverview)
    async def runtime(force: Annotated[bool, Query()] = False) -> RuntimeOverview:
        return await run_in_threadpool(service.runtime, force=force)

    @router.get("/labs/g2p/languages", response_model=G2PLanguages)
    async def g2p_languages() -> G2PLanguages:
        return await run_in_threadpool(service.g2p_languages)

    @router.post("/labs/g2p/variants", response_model=G2PVariants)
    async def g2p_variants(
        request: Annotated[G2PVariantsRequest, Body()],
    ) -> G2PVariants:
        return await run_in_threadpool(service.g2p_variants, request)

    @router.post("/labs/coverage/estimate", response_model=TargetSpaceEstimate)
    async def estimate(
        request: Annotated[TargetSpaceRequest, Body()],
    ) -> TargetSpaceEstimate:
        return await run_in_threadpool(service.estimate, request)

    @router.post("/labs/coverage/track", response_model=CoverageLabResult)
    async def coverage(
        request: Annotated[CoverageLabRequest, Body()],
    ) -> CoverageLabResult:
        return await run_in_threadpool(service.coverage, request)

    @router.post("/labs/reports/render", response_model=RenderedReport)
    async def render_report(
        request: Annotated[RenderReportRequest, Body()],
    ) -> RenderedReport:
        return await run_in_threadpool(service.render_report, request)

    @router.post("/labs/reports/export", response_model=ExportedReport)
    async def export_report(
        request: Annotated[ExportReportRequest, Body()],
    ) -> ExportedReport:
        return await run_in_threadpool(service.export_report, request)

    @router.post("/labs/weights/compute", response_model=WeightSet)
    async def compute_weights(
        request: Annotated[WeightComputeRequest, Body()],
    ) -> WeightSet:
        return await run_in_threadpool(service.compute_weights, request)

    @router.post("/labs/weights/validate", response_model=WeightValidationResult)
    async def validate_weights(
        request: Annotated[WeightValidationRequest, Body()],
    ) -> WeightValidationResult:
        return await run_in_threadpool(service.validate_weights, request)

    return router


__all__ = ["LabHttpService", "coverage_weighting_lab_router"]
