"""Authenticated HTTP contracts for the durable job control plane."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from corpuskit.auth import AuthRole, Principal, require_principal, require_roles
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.persistence.models import OutboxState
from corpuskit.services.jobs import (
    EventSnapshot,
    JobActor,
    RunSnapshot,
    RunSubmission,
    SubmissionResult,
)


class SubmittableRunKind(StrEnum):
    """Run kinds accepted by the public durable-submission endpoint."""

    PHONEMIZE = RunKind.PHONEMIZE.value
    EVALUATE = RunKind.EVALUATE.value
    DISTRIBUTION = RunKind.DISTRIBUTION.value
    TRAJECTORY = RunKind.TRAJECTORY.value
    ERROR_RATES = RunKind.ERROR_RATES.value
    PERPLEXITY = RunKind.PERPLEXITY.value
    SELECT = RunKind.SELECT.value
    GENERATE_REPOSITORY = RunKind.GENERATE_REPOSITORY.value
    GENERATE_LLM = RunKind.GENERATE_LLM.value
    GENERATE_LOCAL = RunKind.GENERATE_LOCAL.value
    BUILD_DATG_INDEX = RunKind.BUILD_DATG_INDEX.value
    GENERATE_DATG = RunKind.GENERATE_DATG.value
    TRAIN_PHON_RL = RunKind.TRAIN_PHON_RL.value


if {RunKind(kind.value) for kind in SubmittableRunKind} != set(RunKind) - {RunKind.EXPORT}:
    raise RuntimeError("Public submission kinds must match handled RunKind values")


class JobApiService(Protocol):
    async def submit(
        self, actor: JobActor, submission: RunSubmission, *, idempotency_key: str
    ) -> SubmissionResult: ...

    async def get(self, actor: JobActor, run_id: UUID) -> RunSnapshot: ...

    async def list(
        self,
        actor: JobActor,
        *,
        state: RunState | None,
        kind: RunKind | None,
        offset: int,
        limit: int,
    ) -> tuple[RunSnapshot, ...]: ...

    async def events(
        self, actor: JobActor, run_id: UUID, *, after: int, limit: int
    ) -> tuple[EventSnapshot, ...]: ...

    async def request_cancellation(self, actor: JobActor, run_id: UUID) -> RunSnapshot: ...

    async def retry(
        self, actor: JobActor, run_id: UUID, *, idempotency_key: str
    ) -> SubmissionResult: ...


class JobSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    corpus_version_id: UUID | None = None
    kind: SubmittableRunKind
    spec: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    project_id: UUID
    corpus_version_id: UUID | None
    parent_run_id: UUID | None
    kind: RunKind
    state: RunState
    attempt: int
    spec: dict[str, Any]
    spec_sha256: str
    outbox_state: OutboxState
    cancellation_requested_at: datetime | None
    created_at: datetime
    result_summary: dict[str, Any] | None
    failure_code: str | None


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime


_writer = require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)
WriterPrincipal = Annotated[Principal, Depends(_writer)]
ReaderPrincipal = Annotated[Principal, Depends(require_principal)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128),
]


def job_router(service: JobApiService) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/runs",
        response_model=RunResponse,
        status_code=status.HTTP_201_CREATED,
        responses={status.HTTP_200_OK: {"model": RunResponse}},
    )
    async def submit_run(
        payload: Annotated[JobSubmissionRequest, Body()],
        principal: WriterPrincipal,
        idempotency_key: IdempotencyKey,
        http_request: Request,
        response: Response,
    ) -> RunSnapshot:
        result = await service.submit(
            _actor(principal, http_request),
            RunSubmission(
                project_id=payload.project_id,
                corpus_version_id=payload.corpus_version_id,
                kind=RunKind(payload.kind.value),
                spec=payload.spec,
            ),
            idempotency_key=idempotency_key,
        )
        response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return result.run

    @router.get("/runs", response_model=tuple[RunResponse, ...])
    async def list_runs(
        principal: ReaderPrincipal,
        state: Annotated[RunState | None, Query()] = None,
        kind: Annotated[RunKind | None, Query()] = None,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> tuple[RunSnapshot, ...]:
        return await service.list(
            _actor(principal), state=state, kind=kind, offset=offset, limit=limit
        )

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: UUID, principal: ReaderPrincipal) -> RunSnapshot:
        return await service.get(_actor(principal), run_id)

    @router.get("/runs/{run_id}/events", response_model=tuple[EventResponse, ...])
    async def get_run_events(
        run_id: UUID,
        principal: ReaderPrincipal,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[EventSnapshot, ...]:
        return await service.events(_actor(principal), run_id, after=after, limit=limit)

    @router.post(
        "/runs/{run_id}/cancellation",
        response_model=RunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_run(
        run_id: UUID, principal: WriterPrincipal, http_request: Request
    ) -> RunSnapshot:
        return await service.request_cancellation(_actor(principal, http_request), run_id)

    @router.post(
        "/runs/{run_id}/retries",
        response_model=RunResponse,
        status_code=status.HTTP_201_CREATED,
        responses={status.HTTP_200_OK: {"model": RunResponse}},
    )
    async def retry_run(
        run_id: UUID,
        principal: WriterPrincipal,
        idempotency_key: IdempotencyKey,
        http_request: Request,
        response: Response,
    ) -> RunSnapshot:
        result = await service.retry(
            _actor(principal, http_request), run_id, idempotency_key=idempotency_key
        )
        response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return result.run

    return router


def _actor(principal: Principal, request: Request | None = None) -> JobActor:
    return JobActor(
        subject=principal.subject,
        organization_id=principal.organization_id,
        request_id=(getattr(request.state, "request_id", None) if request is not None else None),
    )


__all__ = [
    "EventResponse",
    "JobApiService",
    "JobSubmissionRequest",
    "RunResponse",
    "SubmittableRunKind",
    "job_router",
]
