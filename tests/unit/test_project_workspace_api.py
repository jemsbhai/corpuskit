"""HTTP contract tests for the standalone project workspace router factory."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from corpuskit.api.app import create_app
from corpuskit.auth import AuthRole, Principal
from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.errors import (
    ApplicationError,
    QuotaExceededError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.workspaces import (
    CorpusExportFormat,
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

PROJECT_ID = UUID("00000000-0000-4000-8000-000000000021")
CORPUS_ID = UUID("00000000-0000-4000-8000-000000000022")
VERSION_ID = UUID("00000000-0000-4000-8000-000000000023")
NOW = datetime(2026, 8, 11, tzinfo=UTC)


class FakeWorkspaceService:
    def __init__(self) -> None:
        self.upload: CorpusUpload | None = None
        self.actor: WorkspaceActor | None = None
        self.project = ProjectSnapshot(PROJECT_ID, "Demo", "Description", NOW)
        self.corpus = CorpusSnapshot(CORPUS_ID, PROJECT_ID, "Seed", NOW)
        self.version = VersionSnapshot(
            VERSION_ID,
            CORPUS_ID,
            None,
            1,
            "en-us",
            1,
            "a" * 64,
            "0.1.7",
            NOW,
        )
        self.deletion_request: ProjectDeletionInput | None = None
        self.version_request: ManualCorpusVersionInput | None = None
        self.version_upload: CorpusVersionUpload | None = None
        self.version_error: ApplicationError | None = None

    async def create_project(self, actor: WorkspaceActor, request: ProjectInput) -> ProjectSnapshot:
        self.actor = actor
        return ProjectSnapshot(PROJECT_ID, request.name, request.description, NOW)

    async def list_projects(self, actor: WorkspaceActor) -> tuple[ProjectSnapshot, ...]:
        self.actor = actor
        return (self.project,)

    async def request_project_deletion(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        request: ProjectDeletionInput,
    ) -> ProjectDeletionSnapshot:
        self.actor = actor
        assert project_id == PROJECT_ID
        self.deletion_request = request
        return ProjectDeletionSnapshot(
            project_id=project_id,
            state=ProjectLifecycle.DELETION_PENDING,
            requested_at=NOW,
            retention_until=NOW,
        )

    async def create_manual_corpus(
        self, actor: WorkspaceActor, project_id: UUID, request: ManualCorpusInput
    ) -> CorpusCreation:
        self.actor = actor
        assert project_id == PROJECT_ID
        assert request.sentences == ("Hello",)
        return CorpusCreation(self.corpus, self.version)

    async def import_corpus(
        self, actor: WorkspaceActor, project_id: UUID, upload: CorpusUpload
    ) -> CorpusCreation:
        self.actor = actor
        assert project_id == PROJECT_ID
        self.upload = upload
        return CorpusCreation(self.corpus, self.version)

    async def create_manual_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        request: ManualCorpusVersionInput,
    ) -> VersionSnapshot:
        self.actor = actor
        assert (project_id, corpus_id) == (PROJECT_ID, CORPUS_ID)
        if self.version_error is not None:
            raise self.version_error
        self.version_request = request
        return VersionSnapshot(
            UUID("00000000-0000-4000-8000-000000000024"),
            CORPUS_ID,
            VERSION_ID,
            2,
            request.language,
            len(request.sentences),
            "c" * 64,
            "0.1.7",
            NOW,
        )

    async def import_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        upload: CorpusVersionUpload,
    ) -> VersionSnapshot:
        self.actor = actor
        assert (project_id, corpus_id) == (PROJECT_ID, CORPUS_ID)
        self.version_upload = upload
        return VersionSnapshot(
            UUID("00000000-0000-4000-8000-000000000024"),
            CORPUS_ID,
            VERSION_ID,
            2,
            upload.language,
            1,
            "c" * 64,
            "0.1.7",
            NOW,
        )

    async def list_corpora(
        self, actor: WorkspaceActor, project_id: UUID
    ) -> tuple[CorpusSnapshot, ...]:
        self.actor = actor
        assert project_id == PROJECT_ID
        return (self.corpus,)

    async def list_versions(
        self, actor: WorkspaceActor, project_id: UUID, corpus_id: UUID
    ) -> tuple[VersionSnapshot, ...]:
        self.actor = actor
        assert (project_id, corpus_id) == (PROJECT_ID, CORPUS_ID)
        return (self.version,)

    async def list_sentences(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> tuple[SentenceSnapshot, ...]:
        self.actor = actor
        assert (project_id, corpus_id, version_id, offset, limit) == (
            PROJECT_ID,
            CORPUS_ID,
            VERSION_ID,
            2,
            10,
        )
        return (SentenceSnapshot(2, "  Héllo ", "Héllo"),)

    async def export_version(
        self,
        actor: WorkspaceActor,
        project_id: UUID,
        corpus_id: UUID,
        version_id: UUID,
        export_format: CorpusExportFormat,
    ) -> ExportedCorpus:
        self.actor = actor
        assert (project_id, corpus_id, version_id) == (PROJECT_ID, CORPUS_ID, VERSION_ID)
        assert export_format is CorpusExportFormat.TXT
        return ExportedCorpus(
            content="Héllo\n".encode(),
            media_type="text/plain; charset=utf-8",
            filename="seed-v1.txt",
            content_disposition=(
                "attachment; filename=\"seed-v1.txt\"; filename*=UTF-8''seed-v1.txt"
            ),
            sha256="b" * 64,
            content_digest="sha-256=:ZmFrZQ==:",
        )


class FixedAuthenticator:
    def __init__(self, role: AuthRole) -> None:
        self._role = role

    async def authenticate(self, token: str | None) -> Principal:
        del token
        return Principal(
            subject="api-user",
            organization_id=UUID("00000000-0000-4000-8000-000000000020"),
            role=self._role,
        )


def _client(
    ready_report: CapabilityReport,
    service: FakeWorkspaceService,
    *,
    role: AuthRole = AuthRole.OWNER,
    upload_limit: int = 10 * 1024 * 1024,
) -> httpx.AsyncClient:
    app = create_app(
        Settings(
            environment="test",
            api_docs_enabled=True,
            max_upload_bytes=upload_limit,
        ),
        reporter_factory=lambda _: type("Reporter", (), {"report": lambda self: ready_report})(),
        authenticator_factory=lambda _: FixedAuthenticator(role),
        workspace_service_factory=lambda _: service,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR])
async def test_project_and_manual_corpus_http_contracts(
    ready_report: CapabilityReport,
    role: AuthRole,
) -> None:
    service = FakeWorkspaceService()
    async with _client(ready_report, service, role=role) as client:
        created = await client.post(
            "/api/v1/projects",
            json={"name": "Demo", "description": "Description"},
        )
        listed = await client.get("/api/v1/projects")
        corpus = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/corpora",
            json={"name": "Seed", "language": "en-us", "sentences": ["Hello"]},
        )

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(PROJECT_ID)
    assert corpus.status_code == 201
    assert corpus.json()["version"]["version_number"] == 1
    assert service.actor is not None
    assert service.actor.subject == "api-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [AuthRole.OWNER, AuthRole.ADMIN])
async def test_project_deletion_requires_exact_body_and_returns_accepted_lifecycle(
    ready_report: CapabilityReport,
    role: AuthRole,
) -> None:
    service = FakeWorkspaceService()
    async with _client(ready_report, service, role=role) as client:
        response = await client.request(
            "DELETE",
            f"/api/v1/projects/{PROJECT_ID}",
            json={"confirmation": "DELETE Demo"},
            headers={"X-Request-ID": "delete-project"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "project_id": str(PROJECT_ID),
        "state": "deletion_pending",
        "requested_at": NOW.isoformat().replace("+00:00", "Z"),
        "retention_until": NOW.isoformat().replace("+00:00", "Z"),
    }
    assert service.deletion_request == ProjectDeletionInput(confirmation="DELETE Demo")
    assert service.actor is not None
    assert service.actor.request_id == "delete-project"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [AuthRole.EDITOR, AuthRole.VIEWER])
async def test_project_deletion_rejects_non_admin_roles_before_service(
    ready_report: CapabilityReport,
    role: AuthRole,
) -> None:
    service = FakeWorkspaceService()
    async with _client(ready_report, service, role=role) as client:
        response = await client.request(
            "DELETE",
            f"/api/v1/projects/{PROJECT_ID}",
            json={"confirmation": "DELETE Demo"},
        )

    assert response.status_code == 403
    assert service.deletion_request is None


@pytest.mark.asyncio
async def test_file_import_is_multipart_typed_and_bounded(ready_report: CapabilityReport) -> None:
    service = FakeWorkspaceService()
    async with _client(ready_report, service, upload_limit=1_024) as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/corpora/imports",
            data={
                "name": "CSV seed",
                "language": "en-us",
                "format": "csv",
                "text_column": "utterance",
            },
            files={"file": ("seed.csv", b"utterance\nhello world\n", "text/csv")},
        )

    assert response.status_code == 201
    assert service.upload is not None
    assert service.upload.filename == "seed.csv"
    assert service.upload.text_column == "utterance"
    assert service.upload.content == b"utterance\nhello world\n"

    bounded_service = FakeWorkspaceService()
    async with _client(ready_report, bounded_service, upload_limit=5) as client:
        rejected = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/corpora/imports",
            data={"name": "CSV seed", "language": "en-us", "format": "csv"},
            files={"file": ("seed.csv", b"utterance\nhello world\n", "text/csv")},
        )

    assert rejected.status_code == 413
    assert bounded_service.upload is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR])
async def test_manual_and_file_version_http_contracts(
    ready_report: CapabilityReport,
    role: AuthRole,
) -> None:
    service = FakeWorkspaceService()
    base = f"/api/v1/projects/{PROJECT_ID}/corpora/{CORPUS_ID}/versions"
    async with _client(ready_report, service, role=role) as client:
        manual = await client.post(
            base,
            json={"language": "en-gb", "sentences": ["Second"]},
            headers={"X-Request-ID": "manual-version"},
        )
        imported = await client.post(
            f"{base}/imports",
            data={"language": "fr-fr", "format": "txt"},
            files={"file": ("second.txt", b"Deuxieme\n", "text/plain")},
            headers={"X-Request-ID": "file-version"},
        )

    assert manual.status_code == 201
    assert manual.json()["version_number"] == 2
    assert manual.json()["parent_version_id"] == str(VERSION_ID)
    assert service.version_request == ManualCorpusVersionInput(
        language="en-gb", sentences=("Second",)
    )
    assert imported.status_code == 201
    assert imported.json()["language"] == "fr-fr"
    assert service.version_upload is not None
    assert service.version_upload.filename == "second.txt"
    assert service.version_upload.content == b"Deuxieme\n"
    assert service.actor is not None
    assert service.actor.request_id == "file-version"


@pytest.mark.asyncio
async def test_viewer_cannot_append_a_version(ready_report: CapabilityReport) -> None:
    service = FakeWorkspaceService()
    base = f"/api/v1/projects/{PROJECT_ID}/corpora/{CORPUS_ID}/versions"
    async with _client(ready_report, service, role=AuthRole.VIEWER) as client:
        manual = await client.post(
            base,
            json={"language": "en-us", "sentences": ["Denied"]},
        )
        imported = await client.post(
            f"{base}/imports",
            data={"language": "en-us", "format": "txt"},
            files={"file": ("denied.txt", b"Denied\n", "text/plain")},
        )

    assert manual.status_code == 403
    assert imported.status_code == 403
    assert service.version_request is None
    assert service.version_upload is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (ResourceNotFoundError("corpus.version.create"), 404, "resource_not_found"),
        (ResourceConflictError("corpus.version.create"), 409, "resource_conflict"),
        (QuotaExceededError("corpus.version.create"), 429, "quota_exceeded"),
    ],
)
async def test_version_errors_keep_stable_http_status_and_operation(
    ready_report: CapabilityReport,
    error: ApplicationError,
    expected_status: int,
    expected_code: str,
) -> None:
    service = FakeWorkspaceService()
    service.version_error = error
    base = f"/api/v1/projects/{PROJECT_ID}/corpora/{CORPUS_ID}/versions"
    async with _client(ready_report, service, role=AuthRole.EDITOR) as client:
        response = await client.post(base, json={"sentences": ["Second"]})

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert response.json()["operation"] == "corpus.version.create"


@pytest.mark.asyncio
async def test_multipart_metadata_validation_is_sanitized(ready_report: CapabilityReport) -> None:
    service = FakeWorkspaceService()
    async with _client(ready_report, service) as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/corpora/imports",
            data={"name": "Seed", "language": "en-us", "format": "txt"},
            files={"file": (f"{'x' * 256}.txt", b"Hello", "text/plain")},
            headers={"X-Request-ID": "invalid-upload"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": "invalid_request",
        "message": "The request is not valid for this operation.",
        "operation": "corpus.import",
        "request_id": "invalid-upload",
    }
    assert service.upload is None


@pytest.mark.asyncio
async def test_list_and_export_contracts_preserve_integrity_headers(
    ready_report: CapabilityReport,
) -> None:
    service = FakeWorkspaceService()
    base = f"/api/v1/projects/{PROJECT_ID}/corpora/{CORPUS_ID}"
    async with _client(ready_report, service) as client:
        corpora = await client.get(f"/api/v1/projects/{PROJECT_ID}/corpora")
        versions = await client.get(f"{base}/versions")
        sentences = await client.get(f"{base}/versions/{VERSION_ID}/sentences?offset=2&limit=10")
        exported = await client.get(f"{base}/versions/{VERSION_ID}/export?format=txt")

    assert corpora.json()[0]["name"] == "Seed"
    assert versions.json()[0]["content_sha256"] == "a" * 64
    assert sentences.json() == [
        {"ordinal": 2, "original_text": "  Héllo ", "normalized_text": "Héllo"}
    ]
    assert exported.content == "Héllo\n".encode()
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["content-disposition"].startswith("attachment;")
    assert exported.headers["content-digest"] == "sha-256=:ZmFrZQ==:"
    assert exported.headers["etag"] == f'"{"b" * 64}"'
    assert exported.headers["x-content-sha256"] == "b" * 64


@pytest.mark.asyncio
async def test_viewer_can_read_but_writer_route_returns_sanitized_forbidden(
    ready_report: CapabilityReport,
) -> None:
    service = FakeWorkspaceService()
    async with _client(ready_report, service, role=AuthRole.VIEWER) as client:
        listed = await client.get("/api/v1/projects", headers={"X-Request-ID": "viewer-read"})
        exported = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/corpora/{CORPUS_ID}"
            f"/versions/{VERSION_ID}/export?format=txt"
        )
        denied = await client.post(
            "/api/v1/projects",
            json={"name": "Denied"},
            headers={"X-Request-ID": "viewer-write"},
        )

    assert listed.status_code == 200
    assert exported.status_code == 200
    assert denied.status_code == 403
    assert denied.json() == {
        "code": "forbidden",
        "message": "The authenticated identity is not permitted to perform this operation.",
        "request_id": "viewer-write",
    }
    assert "token" not in denied.text.lower()
