"""Authenticated contracts for server-built manifests and durable replay lineage."""

from __future__ import annotations

from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict

from corpuskit.auth import AuthRole, Principal, require_principal, require_roles
from corpuskit.domain.errors import InvalidRequestError, ResourceConflictError
from corpuskit.domain.reproducibility import ReplayStatus
from corpuskit.services.reproducibility import (
    ReplayCreation,
    ReproducibilityActor,
    ReproducibilityError,
)


class ReproducibilityApiService(Protocol):
    async def submit_replay(
        self,
        actor: ReproducibilityActor,
        *,
        project_id: UUID,
        source_run_id: UUID,
        idempotency_key: str,
    ) -> ReplayCreation: ...

    async def get_replay(
        self,
        actor: ReproducibilityActor,
        replay_run_id: UUID,
    ) -> ReplayStatus: ...


class ReplayCreationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replay: ReplayStatus
    created: bool


_writer = require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)
WriterPrincipal = Annotated[Principal, Depends(_writer)]
ReaderPrincipal = Annotated[Principal, Depends(require_principal)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128),
]


def reproducibility_router(service: ReproducibilityApiService) -> APIRouter:
    """Expose triggers and projections without accepting any provenance fields."""

    router = APIRouter()

    @router.post(
        "/projects/{project_id}/runs/{source_run_id}/replays",
        response_model=ReplayCreationResponse,
        status_code=status.HTTP_201_CREATED,
        responses={status.HTTP_200_OK: {"model": ReplayCreationResponse}},
    )
    async def submit_replay(
        project_id: UUID,
        source_run_id: UUID,
        principal: WriterPrincipal,
        idempotency_key: IdempotencyKey,
        request: Request,
        response: Response,
    ) -> ReplayCreationResponse:
        await _require_empty_body(request, "replay.submit")
        try:
            created = await service.submit_replay(
                _actor(principal, request),
                project_id=project_id,
                source_run_id=source_run_id,
                idempotency_key=idempotency_key,
            )
        except ReproducibilityError as exc:
            raise ResourceConflictError("replay.submit") from exc
        response.status_code = status.HTTP_201_CREATED if created.created else status.HTTP_200_OK
        return ReplayCreationResponse(replay=created.replay, created=created.created)

    @router.get("/replays/{replay_run_id}", response_model=ReplayStatus)
    async def get_replay(
        replay_run_id: UUID,
        principal: ReaderPrincipal,
    ) -> ReplayStatus:
        try:
            return await service.get_replay(_actor(principal), replay_run_id)
        except ReproducibilityError as exc:
            raise ResourceConflictError("replay.get") from exc

    return router


def _actor(principal: Principal, request: Request | None = None) -> ReproducibilityActor:
    return ReproducibilityActor(
        subject=principal.subject,
        organization_id=principal.organization_id,
        request_id=(getattr(request.state, "request_id", None) if request is not None else None),
    )


async def _require_empty_body(request: Request, operation: str) -> None:
    if await request.body():
        raise InvalidRequestError(operation)


__all__ = [
    "ReplayCreationResponse",
    "ReproducibilityApiService",
    "reproducibility_router",
]
