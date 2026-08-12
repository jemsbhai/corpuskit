"""Authenticated artifact and reproducibility-manifest HTTP contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from corpuskit.auth import AuthRole, Principal, require_principal, require_roles
from corpuskit.domain.artifacts import ArtifactKind, ArtifactState, ReplayComparison, RunManifest
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.services.artifacts import (
    ArtifactActor,
    ArtifactCreation,
    ArtifactDownload,
    ArtifactSnapshot,
    SignedArtifactDownload,
)


class ArtifactApiService(Protocol):
    async def create(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        run_id: UUID | None,
        kind: ArtifactKind,
        content: bytes,
        expected_sha256: str,
        media_type: str,
        filename: str,
    ) -> ArtifactCreation: ...

    async def get(
        self, actor: ArtifactActor, *, project_id: UUID, artifact_id: UUID
    ) -> ArtifactSnapshot: ...

    async def list(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        offset: int,
        limit: int,
    ) -> tuple[ArtifactSnapshot, ...]: ...

    async def download(
        self, actor: ArtifactActor, *, project_id: UUID, artifact_id: UUID
    ) -> ArtifactDownload: ...

    async def sign_download(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
        expires_seconds: int,
    ) -> SignedArtifactDownload: ...

    async def tombstone(
        self, actor: ArtifactActor, *, project_id: UUID, artifact_id: UUID
    ) -> None: ...

    async def compare_manifest(
        self,
        actor: ArtifactActor,
        *,
        project_id: UUID,
        artifact_id: UUID,
        observed: RunManifest,
    ) -> ReplayComparison: ...


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    run_id: UUID | None
    kind: ArtifactKind
    sha256: str
    size_bytes: int
    media_type: str
    filename: str
    state: ArtifactState
    retention_until: datetime
    created_at: datetime


class ArtifactCreationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact: ArtifactResponse
    created: bool


class SignedDownloadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str
    expires_at: datetime


_writer = require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)
_PUBLIC_UPLOAD_KINDS = frozenset({ArtifactKind.CORPUS_TEXT, ArtifactKind.PROMPT_SET})
WriterPrincipal = Annotated[Principal, Depends(_writer)]
ReaderPrincipal = Annotated[Principal, Depends(require_principal)]


def artifact_router(
    service: ArtifactApiService,
    *,
    max_upload_bytes: int,
    default_presign_seconds: int,
) -> APIRouter:
    """Bind artifact routes to an explicit service and server-controlled limits."""

    router = APIRouter()

    @router.post(
        "/projects/{project_id}/artifacts",
        response_model=ArtifactCreationResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": ArtifactCreationResponse,
                "description": "Existing immutable artifact returned idempotently.",
            }
        },
    )
    async def upload_artifact(
        project_id: UUID,
        request: Request,
        response: Response,
        principal: WriterPrincipal,
        file: Annotated[UploadFile, File()],
        kind: Annotated[ArtifactKind, Form()],
        expected_sha256: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
        run_id: Annotated[UUID | None, Form()] = None,
    ) -> ArtifactCreation:
        if kind not in _PUBLIC_UPLOAD_KINDS:
            raise InvalidRequestError("artifact.create")
        try:
            content = await file.read(max_upload_bytes + 1)
        finally:
            await file.close()
        result = await service.create(
            _actor(principal, request),
            project_id=project_id,
            run_id=run_id,
            kind=kind,
            content=content,
            expected_sha256=expected_sha256,
            media_type=file.content_type or "",
            filename=file.filename or "",
        )
        response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return result

    @router.get(
        "/projects/{project_id}/artifacts",
        response_model=tuple[ArtifactResponse, ...],
    )
    async def list_artifacts(
        project_id: UUID,
        principal: ReaderPrincipal,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[ArtifactSnapshot, ...]:
        return await service.list(
            _actor(principal),
            project_id=project_id,
            offset=offset,
            limit=limit,
        )

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}",
        response_model=ArtifactResponse,
    )
    async def get_artifact(
        project_id: UUID,
        artifact_id: UUID,
        principal: ReaderPrincipal,
    ) -> ArtifactSnapshot:
        return await service.get(
            _actor(principal),
            project_id=project_id,
            artifact_id=artifact_id,
        )

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}/download",
        response_model=None,
    )
    async def download_artifact(
        request: Request,
        project_id: UUID,
        artifact_id: UUID,
        principal: ReaderPrincipal,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse | JSONResponse:
        if range_header is not None:
            return JSONResponse(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                content={
                    "code": "range_not_supported",
                    "message": "Artifact downloads require full-object integrity verification.",
                    "operation": "artifact.download",
                    "request_id": getattr(request.state, "request_id", "unavailable"),
                },
                headers={"Accept-Ranges": "none"},
            )
        download = await service.download(
            _actor(principal),
            project_id=project_id,
            artifact_id=artifact_id,
        )
        return StreamingResponse(
            download.chunks,
            media_type=download.media_type,
            headers={
                "Accept-Ranges": "none",
                "Content-Digest": download.content_digest,
                "Content-Disposition": download.content_disposition,
                "Content-Length": str(download.size_bytes),
                "ETag": f'"{download.sha256}"',
                "X-Content-SHA256": download.sha256,
            },
        )

    @router.post(
        "/projects/{project_id}/artifacts/{artifact_id}/download-url",
        response_model=SignedDownloadResponse,
    )
    async def sign_artifact_download(
        project_id: UUID,
        artifact_id: UUID,
        principal: ReaderPrincipal,
        expires_seconds: Annotated[int, Query(ge=30, le=900)] = default_presign_seconds,
    ) -> SignedArtifactDownload:
        return await service.sign_download(
            _actor(principal),
            project_id=project_id,
            artifact_id=artifact_id,
            expires_seconds=expires_seconds,
        )

    @router.post(
        "/projects/{project_id}/artifacts/{artifact_id}/replay-comparison",
        response_model=ReplayComparison,
    )
    async def compare_manifest(
        project_id: UUID,
        artifact_id: UUID,
        observed: Annotated[RunManifest, Body()],
        principal: ReaderPrincipal,
    ) -> ReplayComparison:
        if observed.project_id != project_id:
            raise InvalidRequestError("manifest.compare")
        return await service.compare_manifest(
            _actor(principal),
            project_id=project_id,
            artifact_id=artifact_id,
            observed=observed,
        )

    @router.delete(
        "/projects/{project_id}/artifacts/{artifact_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_artifact(
        project_id: UUID,
        artifact_id: UUID,
        principal: WriterPrincipal,
        request: Request,
    ) -> None:
        await service.tombstone(
            _actor(principal, request),
            project_id=project_id,
            artifact_id=artifact_id,
        )

    return router


def _actor(principal: Principal, request: Request | None = None) -> ArtifactActor:
    return ArtifactActor(
        subject=principal.subject,
        organization_id=principal.organization_id,
        request_id=(getattr(request.state, "request_id", None) if request is not None else None),
    )


__all__ = [
    "ArtifactApiService",
    "ArtifactCreationResponse",
    "ArtifactResponse",
    "SignedDownloadResponse",
    "artifact_router",
]
