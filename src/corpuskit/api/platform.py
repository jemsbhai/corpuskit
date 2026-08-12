"""Authenticated tenant quota and immutable audit read contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from corpuskit.auth import Principal, require_principal
from corpuskit.domain.platform import (
    AuditAction,
    AuditActorKind,
    AuditResourceType,
    QuotaPolicyValues,
)
from corpuskit.services.platform import (
    AuditPage,
    PlatformActor,
    QuotaSnapshot,
)


class PlatformApiService(Protocol):
    async def quota(self, actor: PlatformActor) -> QuotaSnapshot: ...

    async def audit_events(
        self,
        actor: PlatformActor,
        *,
        cursor: str | None,
        limit: int,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        action: AuditAction | None,
        resource_type: AuditResourceType | None,
    ) -> AuditPage: ...


class QuotaUsageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    active_cpu_jobs: int
    active_expensive_jobs: int
    artifact_bytes: int
    artifact_count: int
    corpus_sentences: int


class QuotaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    policy: QuotaPolicyValues
    usage: QuotaUsageResponse


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    actor_kind: AuditActorKind
    actor_id: str
    action: AuditAction
    resource_type: AuditResourceType
    resource_id: UUID
    request_id: str | None
    occurred_at: datetime
    metadata: dict[str, object]
    previous_hash: str
    event_hash: str


class AuditPageResponse(BaseModel):
    events: tuple[AuditEventResponse, ...]
    next_cursor: str | None


ReaderPrincipal = Annotated[Principal, Depends(require_principal)]


def platform_router(service: PlatformApiService) -> APIRouter:
    router = APIRouter()

    @router.get("/platform/quota", response_model=QuotaResponse)
    async def get_quota(
        request: Request,
        principal: ReaderPrincipal,
    ) -> QuotaSnapshot:
        return await service.quota(_actor(principal, request))

    @router.get("/platform/audit-events", response_model=AuditPageResponse)
    async def list_audit_events(
        request: Request,
        principal: ReaderPrincipal,
        cursor: Annotated[str | None, Query(max_length=19)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        occurred_from: Annotated[datetime | None, Query()] = None,
        occurred_to: Annotated[datetime | None, Query()] = None,
        action: Annotated[AuditAction | None, Query()] = None,
        resource_type: Annotated[AuditResourceType | None, Query()] = None,
    ) -> AuditPage:
        return await service.audit_events(
            _actor(principal, request),
            cursor=cursor,
            limit=limit,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            action=action,
            resource_type=resource_type,
        )

    return router


def _actor(principal: Principal, request: Request) -> PlatformActor:
    return PlatformActor(
        subject=principal.subject,
        organization_id=principal.organization_id,
        request_id=getattr(request.state, "request_id", None),
    )


__all__ = [
    "AuditEventResponse",
    "AuditPageResponse",
    "PlatformApiService",
    "QuotaResponse",
    "QuotaUsageResponse",
    "platform_router",
]
