"""Authenticated artifact HTTP acceptance tests using the real service stack."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from corpuskit.api.app import create_app
from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.services.artifacts import ArtifactService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane


@pytest.mark.integration
@pytest.mark.asyncio
async def test_artifact_routes_cover_upload_metadata_download_range_and_tombstone(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        artifact_root=tmp_path / "objects",
        artifact_max_bytes=64,
        max_upload_bytes=1_024,
        _env_file=None,
    )
    app = create_app(settings)
    service = app.state.artifact_service
    jobs = app.state.job_service
    assert isinstance(service, ArtifactService)
    assert isinstance(jobs, JobControlPlane)
    assert service.database is jobs.database
    await service.database.create_schema()
    await jobs.bootstrap_demo(
        JobActor(subject=DEMO_PRINCIPAL.subject, organization_id=DEMO_PRINCIPAL.organization_id),
        environment="test",
    )
    content = b"source sentence\n"
    digest = hashlib.sha256(content).hexdigest()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            uploaded = await client.post(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts",
                data={"kind": "corpus-text", "expected_sha256": digest},
                files={"file": ("source.txt", content, "text/plain")},
            )
            assert uploaded.status_code == 201
            artifact = uploaded.json()["artifact"]
            artifact_id = artifact["id"]
            assert uploaded.json()["created"] is True
            assert artifact["sha256"] == digest
            assert "storage_key" not in artifact

            duplicate = await client.post(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts",
                data={"kind": "corpus-text", "expected_sha256": digest},
                files={"file": ("source.txt", content, "text/plain")},
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["created"] is False

            metadata = await client.get(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts/{artifact_id}"
            )
            listed = await client.get(f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts")
            downloaded = await client.get(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts/{artifact_id}/download"
            )
            rejected_range = await client.get(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts/{artifact_id}/download",
                headers={"Range": "bytes=0-2"},
            )

            assert metadata.status_code == 200
            assert listed.json() == [metadata.json()]
            assert downloaded.content == content
            assert downloaded.headers["x-content-sha256"] == digest
            assert downloaded.headers["accept-ranges"] == "none"
            assert rejected_range.status_code == 416
            assert rejected_range.headers["accept-ranges"] == "none"
            assert rejected_range.json()["request_id"] != "unavailable"

            signed = await client.post(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts/{artifact_id}/download-url"
            )
            assert signed.status_code == 409

            deleted = await client.delete(
                f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts/{artifact_id}"
            )
            assert deleted.status_code == 204
            assert (
                await client.get(f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts/{artifact_id}")
            ).status_code == 404
    finally:
        await service.database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_upload_cannot_fabricate_worker_artifact_kinds(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'trusted.db').as_posix()}",
        artifact_root=tmp_path / "objects",
        _env_file=None,
    )
    app = create_app(settings)
    service = app.state.artifact_service
    jobs = app.state.job_service
    assert isinstance(service, ArtifactService)
    assert isinstance(jobs, JobControlPlane)
    await service.database.create_schema()
    await jobs.bootstrap_demo(
        JobActor(subject=DEMO_PRINCIPAL.subject, organization_id=DEMO_PRINCIPAL.organization_id),
        environment="test",
    )
    content = b"untrusted"
    digest = hashlib.sha256(content).hexdigest()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            assert (
                "/api/v1/projects/{project_id}/runs/{run_id}/manifest" not in app.openapi()["paths"]
            )
            for kind in (
                "run-manifest",
                "checkpoint",
                "model-adapter",
                "evaluation-report",
                "export",
                "run-result",
            ):
                response = await client.post(
                    f"/api/v1/projects/{DEMO_PROJECT_ID}/artifacts",
                    data={"kind": kind, "expected_sha256": digest},
                    files={"file": ("payload.bin", content, "application/octet-stream")},
                )
                assert response.status_code == 422
                assert response.json()["code"] == "invalid_request"
    finally:
        await service.database.dispose()
