"""HTTP routes for inventory exploration and deterministic analyses."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Body, Query
from fastapi.params import Depends as DependsParameter
from starlette.concurrency import run_in_threadpool

from corpuskit.domain import (
    CoverageTrajectory,
    CoverageTrajectoryRequest,
    DistributionAnalysisRequest,
    DistributionMetrics,
    ErrorRatesAnalysis,
    ErrorRatesAnalysisRequest,
    EspeakMappingPage,
    FeatureCatalog,
    Inventory,
    InventoryPage,
    InventorySources,
    LanguagePage,
    PhoibleStatus,
    SegmentPage,
    TextQualityAnalysisRequest,
    TextQualityMetrics,
)


class InventoryHttpService(Protocol):
    def status(self) -> PhoibleStatus: ...

    def load(self) -> PhoibleStatus: ...

    def features(self) -> FeatureCatalog: ...

    def languages(self, *, query: str | None, offset: int, limit: int) -> LanguagePage: ...

    def mappings(self, *, query: str | None, offset: int, limit: int) -> EspeakMappingPage: ...

    def sources(self, identifier: str) -> InventorySources: ...

    def inventory(self, identifier: str, *, source: str | None, union: bool) -> Inventory: ...

    def all_inventories(self, identifier: str, *, offset: int, limit: int) -> InventoryPage: ...

    def segments(
        self,
        identifier: str,
        *,
        source: str | None,
        union: bool,
        segment_class: str | None,
        marginal: bool | None,
        feature_name: str | None,
        feature_value: str | None,
        offset: int,
        limit: int,
    ) -> SegmentPage: ...


class AnalysisHttpService(Protocol):
    def distribution(self, request: DistributionAnalysisRequest) -> DistributionMetrics: ...

    def text_quality(self, request: TextQualityAnalysisRequest) -> TextQualityMetrics: ...

    def error_rates(self, request: ErrorRatesAnalysisRequest) -> ErrorRatesAnalysis: ...

    def trajectory(self, request: CoverageTrajectoryRequest) -> CoverageTrajectory: ...


def exploration_analysis_router(
    inventories: InventoryHttpService,
    analyses: AnalysisHttpService,
    *,
    load_dependencies: tuple[DependsParameter, ...] = (),
) -> APIRouter:
    router = APIRouter()

    @router.get("/phonology/status", response_model=PhoibleStatus)
    async def phonology_status() -> PhoibleStatus:
        return await run_in_threadpool(inventories.status)

    @router.post(
        "/phonology/load",
        response_model=PhoibleStatus,
        dependencies=load_dependencies,
    )
    async def load_phonology() -> PhoibleStatus:
        return await run_in_threadpool(inventories.load)

    @router.get("/phonology/features", response_model=FeatureCatalog)
    async def feature_catalog() -> FeatureCatalog:
        return await run_in_threadpool(inventories.features)

    @router.get("/phonology/languages", response_model=LanguagePage)
    async def languages(
        query: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> LanguagePage:
        return await run_in_threadpool(
            inventories.languages, query=query, offset=offset, limit=limit
        )

    @router.get("/phonology/espeak-mappings", response_model=EspeakMappingPage)
    async def mappings(
        query: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> EspeakMappingPage:
        return await run_in_threadpool(
            inventories.mappings, query=query, offset=offset, limit=limit
        )

    @router.get("/phonology/inventories/{identifier}/sources", response_model=InventorySources)
    async def inventory_sources(identifier: str) -> InventorySources:
        return await run_in_threadpool(inventories.sources, identifier)

    @router.get("/phonology/inventories/{identifier}/all", response_model=InventoryPage)
    async def all_inventories(
        identifier: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> InventoryPage:
        return await run_in_threadpool(
            inventories.all_inventories, identifier, offset=offset, limit=limit
        )

    @router.get("/phonology/inventories/{identifier}/segments", response_model=SegmentPage)
    async def segments(
        identifier: str,
        source: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        union: bool = False,
        segment_class: Literal["consonant", "vowel", "tone"] | None = None,
        marginal: bool | None = None,
        feature_name: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        feature_value: Annotated[
            str | None,
            Query(min_length=1, max_length=15, pattern=r"^[+\-0](?:,[+\-0])*$"),
        ] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> SegmentPage:
        return await run_in_threadpool(
            inventories.segments,
            identifier,
            source=source,
            union=union,
            segment_class=segment_class,
            marginal=marginal,
            feature_name=feature_name,
            feature_value=feature_value,
            offset=offset,
            limit=limit,
        )

    @router.get("/phonology/inventories/{identifier}", response_model=Inventory)
    async def inventory(
        identifier: str,
        source: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        union: bool = False,
    ) -> Inventory:
        return await run_in_threadpool(
            inventories.inventory, identifier, source=source, union=union
        )

    @router.post("/analyses/distribution", response_model=DistributionMetrics)
    async def distribution(
        request: Annotated[DistributionAnalysisRequest, Body()],
    ) -> DistributionMetrics:
        return await run_in_threadpool(analyses.distribution, request)

    @router.post("/analyses/text-quality", response_model=TextQualityMetrics)
    async def text_quality(
        request: Annotated[TextQualityAnalysisRequest, Body()],
    ) -> TextQualityMetrics:
        return await run_in_threadpool(analyses.text_quality, request)

    @router.post("/analyses/error-rates", response_model=ErrorRatesAnalysis)
    async def error_rates(
        request: Annotated[ErrorRatesAnalysisRequest, Body()],
    ) -> ErrorRatesAnalysis:
        return await run_in_threadpool(analyses.error_rates, request)

    @router.post("/analyses/coverage-trajectory", response_model=CoverageTrajectory)
    async def coverage_trajectory(
        request: Annotated[CoverageTrajectoryRequest, Body()],
    ) -> CoverageTrajectory:
        return await run_in_threadpool(analyses.trajectory, request)

    return router


__all__ = ["AnalysisHttpService", "InventoryHttpService", "exploration_analysis_router"]
