"""HTTP contracts for durable replay without a public manifest-minting path."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from corpuskit.api.reproducibility import reproducibility_router
from corpuskit.auth import DemoAuthenticator
from corpuskit.domain.artifacts import DeterminismClass
from corpuskit.domain.errors import ApplicationError
from corpuskit.domain.reproducibility import ReplayLifecycle, ReplayStatus
from corpuskit.services.rate_limits import DisabledRateLimiter
from corpuskit.services.reproducibility import (
    ReplayCreation,
    ReproducibilityActor,
    ReproducibilityError,
)

ORG_ID = UUID("00000000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("00000000-0000-4000-8000-000000000003")
SOURCE_ID = UUID("10000000-0000-4000-8000-000000000001")
REPLAY_ID = UUID("20000000-0000-4000-8000-000000000002")
ARTIFACT_ID = UUID("30000000-0000-4000-8000-000000000003")


class FakeReproducibility:
    def __init__(self) -> None:
        self.created_replay = False
        self.calls: list[tuple[str, object]] = []

    async def submit_replay(
        self,
        actor: ReproducibilityActor,
        *,
        project_id: UUID,
        source_run_id: UUID,
        idempotency_key: str,
    ) -> ReplayCreation:
        self.calls.append(("submit", (actor, project_id, source_run_id, idempotency_key)))
        created = not self.created_replay
        self.created_replay = True
        return ReplayCreation(self.status(), created)

    async def get_replay(
        self,
        actor: ReproducibilityActor,
        replay_run_id: UUID,
    ) -> ReplayStatus:
        self.calls.append(("get", (actor, replay_run_id)))
        return self.status()

    @staticmethod
    def status() -> ReplayStatus:
        return ReplayStatus(
            replay_run_id=REPLAY_ID,
            source_run_id=SOURCE_ID,
            source_manifest_artifact_id=ARTIFACT_ID,
            expected_manifest_sha256="d" * 64,
            classification=DeterminismClass.EXACT,
            lifecycle=ReplayLifecycle.QUEUED,
        )


def _client(service: FakeReproducibility) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.authenticator = DemoAuthenticator()
    app.state.rate_limiter = DisabledRateLimiter()

    @app.exception_handler(ApplicationError)
    async def application_error(_request: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"code": error.code.value})

    app.include_router(reproducibility_router(service), prefix="/api/v1")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_replay_route_is_no_body_idempotent_and_manifest_minting_is_absent() -> None:
    service = FakeReproducibility()
    manifest_path = f"/api/v1/projects/{PROJECT_ID}/runs/{SOURCE_ID}/manifest"
    replay_path = f"/api/v1/projects/{PROJECT_ID}/runs/{SOURCE_ID}/replays"
    async with _client(service) as client:
        forged_manifest = await client.post(
            manifest_path,
            json={"worker_image_digest": "forged"},
        )
        missing_key = await client.post(replay_path)
        first_replay = await client.post(
            replay_path,
            headers={"Idempotency-Key": "replay-key"},
        )
        second_replay = await client.post(
            replay_path,
            headers={"Idempotency-Key": "replay-key"},
        )
        fetched = await client.get(f"/api/v1/replays/{REPLAY_ID}")

    assert forged_manifest.status_code == 404
    assert missing_key.status_code == 422
    assert first_replay.status_code == 201
    assert second_replay.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["lifecycle"] == "queued"
    assert [name for name, _ in service.calls] == ["submit", "submit", "get"]


def test_openapi_has_no_manifest_minting_path_or_replay_request_body() -> None:
    service = FakeReproducibility()
    app = FastAPI()
    app.state.authenticator = DemoAuthenticator()
    app.state.rate_limiter = DisabledRateLimiter()
    app.include_router(reproducibility_router(service), prefix="/api/v1")
    schema = app.openapi()

    replay = schema["paths"]["/api/v1/projects/{project_id}/runs/{source_run_id}/replays"]["post"]
    assert "/api/v1/projects/{project_id}/runs/{run_id}/manifest" not in schema["paths"]
    assert "requestBody" not in replay


class FailingReproducibility(FakeReproducibility):
    async def submit_replay(
        self,
        actor: ReproducibilityActor,
        *,
        project_id: UUID,
        source_run_id: UUID,
        idempotency_key: str,
    ) -> ReplayCreation:
        del actor, project_id, source_run_id, idempotency_key
        raise ReproducibilityError("internal_replay_detail")

    async def get_replay(
        self,
        actor: ReproducibilityActor,
        replay_run_id: UUID,
    ) -> ReplayStatus:
        del actor, replay_run_id
        raise ReproducibilityError("internal_projection_detail")


@pytest.mark.asyncio
async def test_internal_reproducibility_errors_are_mapped_without_detail_leakage() -> None:
    replay_path = f"/api/v1/projects/{PROJECT_ID}/runs/{SOURCE_ID}/replays"
    async with _client(FailingReproducibility()) as client:
        replay = await client.post(replay_path, headers={"Idempotency-Key": "replay-key"})
        projection = await client.get(f"/api/v1/replays/{REPLAY_ID}")

    for response in (replay, projection):
        assert response.status_code == 422
        assert "internal_" not in response.text
