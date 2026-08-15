"""Authenticated HTTP contracts for project and immutable corpus workspaces."""

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
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from corpuskit.auth import AuthRole, Principal, require_principal, require_roles
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.domain.workspaces import (
    CorpusExportFormat,
    CorpusFileFormat,
    CorpusUpload,
    CorpusVersionUpload,
    ManualCorpusInput,
    ManualCorpusVersionInput,
    ProjectDeletionInput,
    ProjectInput,
    ProjectLifecycle,
)
from corpuskit.services.project_workspaces import (
    CorpusCreation,
    CorpusSnapshot,
    ExportedCorpus,
    ProjectDeletionSnapshot,
    ProjectSnapshot,
    SentenceSnapshot,
    VersionSnapshot,
    WorkspaceActor,
)


class ProjectWorkspaceApi(Protocol):
    """Application-service boundary consumed by the workspace router factory."""

    async def create_project(
        self, actor: WorkspaceActor, request: ProjectInput
    ) -> ProjectSnapshot: ...

    async def list_projects(self, actor: WorkspaceActor) -> tuple[ProjectSnapshot, ...]: ...

    async def request_project_deletion(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        request: ProjectDeletionInput,
    ) -> ProjectDeletionSnapshot: ...

    async def create_manual_corpus(
        self, actor: WorkspaceActor, project_id: UUID, request: ManualCorpusInput
    ) -> CorpusCreation: ...

    async def import_corpus(
        self, actor: WorkspaceActor, project_id: UUID, upload: CorpusUpload
    ) -> CorpusCreation: ...

    async def create_manual_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        request: ManualCorpusVersionInput,
    ) -> VersionSnapshot:
        raise NotImplementedError

    async def import_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        upload: CorpusVersionUpload,
    ) -> VersionSnapshot:
        raise NotImplementedError

    async def list_corpora(
        self, actor: WorkspaceActor, project_id: UUID
    ) -> tuple[CorpusSnapshot, ...]: ...

    async def list_versions(
        self, actor: WorkspaceActor, project_id: UUID, corpus_id: UUID
    ) -> tuple[VersionSnapshot, ...]: ...

    async def list_sentences(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> tuple[SentenceSnapshot, ...]: ...

    async def export_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        export_format: CorpusExportFormat,
    ) -> ExportedCorpus: ...


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    created_at: datetime


class ProjectDeletionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: UUID
    state: ProjectLifecycle
    requested_at: datetime
    retention_until: datetime


class CorpusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    created_at: datetime


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    corpus_id: UUID
    parent_version_id: UUID | None
    version_number: int
    language: str
    sentence_count: int
    content_sha256: str
    corpusgen_version: str
    created_at: datetime


class SentenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    original_text: str
    normalized_text: str


class CorpusCreationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    corpus: CorpusResponse
    version: VersionResponse


_writer = require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)
_project_deleter = require_roles(AuthRole.OWNER, AuthRole.ADMIN)
WriterPrincipal = Annotated[Principal, Depends(_writer)]
ProjectDeleterPrincipal = Annotated[Principal, Depends(_project_deleter)]
ReaderPrincipal = Annotated[Principal, Depends(require_principal)]


def project_workspace_router(
    service: ProjectWorkspaceApi,
    *,
    max_upload_bytes: int = 10 * 1024 * 1024,
) -> APIRouter:
    """Create isolated routes bound to an explicit service and upload budget."""

    router = APIRouter()

    @router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
    async def create_project(
        payload: Annotated[ProjectInput, Body()],
        principal: WriterPrincipal,
        http_request: Request,
    ) -> ProjectSnapshot:
        return await service.create_project(_actor(principal, http_request), payload)

    @router.get("/projects", response_model=tuple[ProjectResponse, ...])
    async def list_projects(principal: ReaderPrincipal) -> tuple[ProjectSnapshot, ...]:
        return await service.list_projects(_actor(principal))

    @router.delete(
        "/projects/{project_id}",
        response_model=ProjectDeletionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def request_project_deletion(
        project_id: UUID,
        payload: Annotated[ProjectDeletionInput, Body()],
        principal: ProjectDeleterPrincipal,
        http_request: Request,
    ) -> ProjectDeletionSnapshot:
        return await service.request_project_deletion(
            _actor(principal, http_request),
            project_id,
            payload,
        )

    @router.post(
        "/projects/{project_id}/corpora",
        response_model=CorpusCreationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_manual_corpus(
        project_id: UUID,
        payload: Annotated[ManualCorpusInput, Body()],
        principal: WriterPrincipal,
        http_request: Request,
    ) -> CorpusCreation:
        return await service.create_manual_corpus(
            _actor(principal, http_request), project_id, payload
        )

    @router.post(
        "/projects/{project_id}/corpora/imports",
        response_model=CorpusCreationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_corpus(
        project_id: UUID,
        principal: WriterPrincipal,
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form(min_length=1, max_length=160)],
        language: Annotated[str, Form(min_length=1, max_length=64)],
        file_format: Annotated[CorpusFileFormat, Form(alias="format")],
        http_request: Request,
        text_column: Annotated[str | None, Form(min_length=1, max_length=160)] = None,
    ) -> CorpusCreation:
        try:
            content = await file.read(max_upload_bytes + 1)
        finally:
            await file.close()
        try:
            upload = CorpusUpload(
                name=name,
                language=language,
                filename=file.filename or "",
                content_type=file.content_type or "",
                file_format=file_format,
                content=content,
                text_column=text_column,
            )
        except ValidationError as exc:
            raise InvalidRequestError("corpus.import") from exc
        return await service.import_corpus(_actor(principal, http_request), project_id, upload)

    @router.post(
        "/projects/{project_id}/corpora/{corpus_id}/versions",
        response_model=VersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_manual_version(
        project_id: UUID,
        corpus_id: UUID,
        payload: Annotated[ManualCorpusVersionInput, Body()],
        principal: WriterPrincipal,
        http_request: Request,
    ) -> VersionSnapshot:
        return await service.create_manual_version(
            _actor(principal, http_request),
            project_id,
            corpus_id,
            payload,
        )

    @router.post(
        "/projects/{project_id}/corpora/{corpus_id}/versions/imports",
        response_model=VersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_version(
        project_id: UUID,
        corpus_id: UUID,
        principal: WriterPrincipal,
        file: Annotated[UploadFile, File()],
        language: Annotated[str, Form(min_length=1, max_length=64)],
        file_format: Annotated[CorpusFileFormat, Form(alias="format")],
        http_request: Request,
        text_column: Annotated[str | None, Form(min_length=1, max_length=160)] = None,
    ) -> VersionSnapshot:
        try:
            content = await file.read(max_upload_bytes + 1)
        finally:
            await file.close()
        try:
            upload = CorpusVersionUpload(
                language=language,
                filename=file.filename or "",
                content_type=file.content_type or "",
                file_format=file_format,
                content=content,
                text_column=text_column,
            )
        except ValidationError as exc:
            raise InvalidRequestError("corpus.version.import") from exc
        return await service.import_version(
            _actor(principal, http_request),
            project_id,
            corpus_id,
            upload,
        )

    @router.get(
        "/projects/{project_id}/corpora",
        response_model=tuple[CorpusResponse, ...],
    )
    async def list_corpora(
        project_id: UUID,
        principal: ReaderPrincipal,
    ) -> tuple[CorpusSnapshot, ...]:
        return await service.list_corpora(_actor(principal), project_id)

    @router.get(
        "/projects/{project_id}/corpora/{corpus_id}/versions",
        response_model=tuple[VersionResponse, ...],
    )
    async def list_versions(
        project_id: UUID,
        corpus_id: UUID,
        principal: ReaderPrincipal,
    ) -> tuple[VersionSnapshot, ...]:
        return await service.list_versions(_actor(principal), project_id, corpus_id)

    @router.get(
        "/projects/{project_id}/corpora/{corpus_id}/versions/{version_id}/sentences",
        response_model=tuple[SentenceResponse, ...],
    )
    async def list_sentences(
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        principal: ReaderPrincipal,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[SentenceSnapshot, ...]:
        return await service.list_sentences(
            _actor(principal),
            project_id,
            corpus_id,
            version_id,
            offset=offset,
            limit=limit,
        )

    @router.get(
        "/projects/{project_id}/corpora/{corpus_id}/versions/{version_id}/export",
        response_class=Response,
    )
    async def export_version(
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        principal: ReaderPrincipal,
        export_format: Annotated[CorpusExportFormat, Query(alias="format")],
    ) -> Response:
        exported = await service.export_version(
            _actor(principal),
            project_id,
            corpus_id,
            version_id,
            export_format,
        )
        return _download_response(exported)

    return router


def _actor(principal: Principal, request: Request | None = None) -> WorkspaceActor:
    return WorkspaceActor(
        subject=principal.subject,
        organization_id=principal.organization_id,
        request_id=(getattr(request.state, "request_id", None) if request is not None else None),
    )


def _download_response(exported: ExportedCorpus) -> Response:
    return Response(
        content=exported.content,
        media_type=exported.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": exported.content_disposition,
            "Content-Digest": exported.content_digest,
            "ETag": f'"{exported.sha256}"',
            "X-Content-SHA256": exported.sha256,
        },
    )


__all__ = [
    "CorpusCreationResponse",
    "CorpusResponse",
    "ProjectDeletionResponse",
    "ProjectResponse",
    "ProjectWorkspaceApi",
    "SentenceResponse",
    "VersionResponse",
    "project_workspace_router",
]
