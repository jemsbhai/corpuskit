"""Validation and inspection-only HTTP lab for Phon-DATG."""

from __future__ import annotations

from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from fastapi.params import Depends as DependsParameter

from corpuskit.auth.dependencies import require_principal
from corpuskit.auth.models import Principal
from corpuskit.domain.datg import (
    DatgCoveredInspectionRequest,
    DatgFrequencyInspectionRequest,
    DatgGuidedGenerationRequest,
    DatgIndexBuildRequest,
    DatgIndexPublication,
    DatgInspectionResult,
    DatgLogitDeltaPreviewRequest,
    DatgLogitDeltaPreviewResult,
    DatgRuntimeValidationResult,
    DatgTargetInspectionRequest,
)
from corpuskit.services.datg_catalog import DatgCatalogActor


class DatgInspectionHttpService(Protocol):
    async def list(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        offset: int,
        limit: int,
    ) -> tuple[DatgIndexPublication, ...]: ...

    async def target(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgTargetInspectionRequest,
    ) -> DatgInspectionResult: ...

    async def covered(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgCoveredInspectionRequest,
    ) -> DatgInspectionResult: ...

    async def frequency(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgFrequencyInspectionRequest,
    ) -> DatgInspectionResult: ...

    async def preview_logits(
        self,
        actor: DatgCatalogActor,
        *,
        project_id: UUID,
        request: DatgLogitDeltaPreviewRequest,
    ) -> DatgLogitDeltaPreviewResult: ...


class DatgValidationHttpPolicy(Protocol):
    def validate_build(self, request: DatgIndexBuildRequest) -> DatgRuntimeValidationResult: ...

    def validate_generation(
        self,
        request: DatgGuidedGenerationRequest,
    ) -> DatgRuntimeValidationResult: ...


def datg_lab_router(
    inspection: DatgInspectionHttpService,
    policy: DatgValidationHttpPolicy,
    *,
    inspection_dependencies: tuple[DependsParameter, ...] = (),
    validation_dependencies: tuple[DependsParameter, ...] = (),
) -> APIRouter:
    """Expose bounded reads and validation; no build or generation execution route exists."""

    router = APIRouter()

    @router.get(
        "/projects/{project_id}/datg/indexes",
        response_model=tuple[DatgIndexPublication, ...],
        dependencies=inspection_dependencies,
    )
    async def list_indexes(
        project_id: UUID,
        principal: Annotated[Principal, Depends(require_principal)],
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> tuple[DatgIndexPublication, ...]:
        return await inspection.list(
            _actor(principal),
            project_id=project_id,
            offset=offset,
            limit=limit,
        )

    @router.post(
        "/projects/{project_id}/datg/index/inspect/targets",
        response_model=DatgInspectionResult,
        dependencies=inspection_dependencies,
    )
    async def inspect_targets(
        project_id: UUID,
        request: Annotated[DatgTargetInspectionRequest, Body()],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> DatgInspectionResult:
        return await inspection.target(
            _actor(principal),
            project_id=project_id,
            request=request,
        )

    @router.post(
        "/projects/{project_id}/datg/index/inspect/anti/covered",
        response_model=DatgInspectionResult,
        dependencies=inspection_dependencies,
    )
    async def inspect_covered(
        project_id: UUID,
        request: Annotated[DatgCoveredInspectionRequest, Body()],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> DatgInspectionResult:
        return await inspection.covered(
            _actor(principal),
            project_id=project_id,
            request=request,
        )

    @router.post(
        "/projects/{project_id}/datg/index/inspect/anti/frequency",
        response_model=DatgInspectionResult,
        dependencies=inspection_dependencies,
    )
    async def inspect_frequency(
        project_id: UUID,
        request: Annotated[DatgFrequencyInspectionRequest, Body()],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> DatgInspectionResult:
        return await inspection.frequency(
            _actor(principal),
            project_id=project_id,
            request=request,
        )

    @router.post(
        "/projects/{project_id}/datg/index/preview/logits",
        response_model=DatgLogitDeltaPreviewResult,
        dependencies=inspection_dependencies,
    )
    async def preview_logits(
        project_id: UUID,
        request: Annotated[DatgLogitDeltaPreviewRequest, Body()],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> DatgLogitDeltaPreviewResult:
        """Preview additive steering only; no model loading or generation."""

        return await inspection.preview_logits(
            _actor(principal),
            project_id=project_id,
            request=request,
        )

    @router.post(
        "/datg/index/validate",
        response_model=DatgRuntimeValidationResult,
        dependencies=validation_dependencies,
    )
    async def validate_build(
        request: Annotated[DatgIndexBuildRequest, Body()],
    ) -> DatgRuntimeValidationResult:
        return policy.validate_build(request)

    @router.post(
        "/datg/generation/validate",
        response_model=DatgRuntimeValidationResult,
        dependencies=validation_dependencies,
    )
    async def validate_generation(
        request: Annotated[DatgGuidedGenerationRequest, Body()],
    ) -> DatgRuntimeValidationResult:
        return policy.validate_generation(request)

    return router


def _actor(principal: Principal) -> DatgCatalogActor:
    return DatgCatalogActor(
        subject=principal.subject,
        organization_id=principal.organization_id,
    )


__all__ = [
    "DatgInspectionHttpService",
    "DatgValidationHttpPolicy",
    "datg_lab_router",
]
