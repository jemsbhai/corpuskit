"""HTTP contracts for persisted job submission and polling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from fastapi import FastAPI

from corpuskit.api.app import CapabilityReporter, create_app
from corpuskit.api.jobs import JobApiService, SubmittableRunKind
from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.errors import QuotaExceededError
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.persistence.migration_cli import build_alembic_config
from corpuskit.persistence.models import OutboxState
from corpuskit.services.jobs import (
    DEMO_PROJECT_ID,
    EventSnapshot,
    JobActor,
    RunSnapshot,
    RunSubmission,
    SubmissionResult,
)


class ReadyReporter:
    def report(self, *, force: bool = False) -> CapabilityReport:
        del force
        return CapabilityReport(checked_at=datetime.now(UTC), ready=True)


class FakeJobs:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.project_id = UUID("00000000-0000-4000-8000-000000000099")
        self.replayed = False
        self.calls: list[tuple[str, object]] = []

    def snapshot(self, *, state: RunState = RunState.QUEUED) -> RunSnapshot:
        return RunSnapshot(
            id=self.run_id,
            organization_id=UUID("00000000-0000-4000-8000-000000000001"),
            project_id=self.project_id,
            corpus_version_id=None,
            parent_run_id=None,
            kind=RunKind.GENERATE_REPOSITORY,
            state=state,
            attempt=1,
            spec={"source_ref": "corpus:v1"},
            spec_sha256="a" * 64,
            outbox_state=OutboxState.PENDING,
            cancellation_requested_at=None,
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
            result_summary=None,
            failure_code=None,
        )

    async def submit(
        self, actor: JobActor, submission: RunSubmission, *, idempotency_key: str
    ) -> SubmissionResult:
        self.calls.append(("submit", (actor, submission, idempotency_key)))
        created = not self.replayed
        self.replayed = True
        return SubmissionResult(self.snapshot(), created=created)

    async def get(self, actor: JobActor, run_id: UUID) -> RunSnapshot:
        self.calls.append(("get", (actor, run_id)))
        return self.snapshot()

    async def list(
        self,
        actor: JobActor,
        *,
        state: RunState | None,
        kind: RunKind | None,
        offset: int,
        limit: int,
    ) -> tuple[RunSnapshot, ...]:
        self.calls.append(("list", (actor, state, kind, offset, limit)))
        return (self.snapshot(),)

    async def events(
        self, actor: JobActor, run_id: UUID, *, after: int, limit: int
    ) -> tuple[EventSnapshot, ...]:
        self.calls.append(("events", (actor, run_id, after, limit)))
        return (
            EventSnapshot(
                sequence=after + 1,
                event_type="run.progress",
                payload={
                    "schema_version": 1,
                    "activity_attempt": 1,
                    "sequence": 0,
                    "phase": "training",
                    "completed": 1,
                    "total": 2,
                    "coverage": None,
                    "accepted_count": None,
                },
                occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
            ),
        )

    async def request_cancellation(self, actor: JobActor, run_id: UUID) -> RunSnapshot:
        self.calls.append(("cancel", (actor, run_id)))
        return self.snapshot(state=RunState.CANCELLING)

    async def retry(
        self, actor: JobActor, run_id: UUID, *, idempotency_key: str
    ) -> SubmissionResult:
        self.calls.append(("retry", (actor, run_id, idempotency_key)))
        return SubmissionResult(self.snapshot(), created=True)


class QuotaJobs(FakeJobs):
    async def submit(
        self, actor: JobActor, submission: RunSubmission, *, idempotency_key: str
    ) -> SubmissionResult:
        del actor, submission, idempotency_key
        raise QuotaExceededError("run.submit", retry_after_seconds=30)


def _application(service: FakeJobs, **settings: Any) -> FastAPI:
    resolved = Settings(environment="test", _env_file=None, **settings)

    def reporter_factory(_: Settings) -> CapabilityReporter:
        return ReadyReporter()

    def job_service_factory(_: Settings) -> JobApiService:
        return service

    return create_app(
        resolved,
        reporter_factory=reporter_factory,
        job_service_factory=job_service_factory,
    )


def _client(service: FakeJobs, **settings: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_application(service, **settings)),
        base_url="http://testserver",
    )


def test_submission_openapi_excludes_reserved_export_kind() -> None:
    service = FakeJobs()
    document = _application(service).openapi()
    request_schema = document["components"]["schemas"]["SubmittableRunKind"]
    response_schema = document["components"]["schemas"]["RunKind"]

    assert set(request_schema["enum"]) == {kind.value for kind in SubmittableRunKind}
    assert set(request_schema["enum"]) == {kind.value for kind in RunKind} - {RunKind.EXPORT.value}
    assert RunKind.EXPORT.value in response_schema["enum"]


@pytest.mark.asyncio
async def test_submission_rejects_reserved_export_before_service_dispatch() -> None:
    service = FakeJobs()
    payload = {
        "project_id": str(service.project_id),
        "kind": RunKind.EXPORT.value,
        "spec": {},
    }
    async with _client(service) as client:
        response = await client.post(
            "/api/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "reserved-export"},
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_job_submission_replay_get_list_events_cancel_and_retry_contracts() -> None:
    service = FakeJobs()
    payload = {
        "project_id": str(service.project_id),
        "kind": "generate-repository",
        "spec": {"source_ref": "corpus:v1"},
    }
    async with _client(service) as client:
        missing_key = await client.post("/api/v1/runs", json=payload)
        created = await client.post(
            "/api/v1/runs", json=payload, headers={"Idempotency-Key": "job-1"}
        )
        replay = await client.post(
            "/api/v1/runs", json=payload, headers={"Idempotency-Key": "job-1"}
        )
        listed = await client.get("/api/v1/runs", params={"state": "queued", "limit": 10})
        fetched = await client.get(f"/api/v1/runs/{service.run_id}")
        events = await client.get(f"/api/v1/runs/{service.run_id}/events", params={"after": 4})
        cancelled = await client.post(f"/api/v1/runs/{service.run_id}/cancellation")
        retried = await client.post(
            f"/api/v1/runs/{service.run_id}/retries",
            headers={"Idempotency-Key": "retry-1"},
        )

    assert missing_key.status_code == 422
    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.json()["state"] == "queued"
    assert created.json()["outbox_state"] == "pending"
    assert listed.status_code == fetched.status_code == 200
    assert events.json()[0]["sequence"] == 5
    assert events.json()[0]["event_type"] == "run.progress"
    assert events.json()[0]["payload"] == {
        "accepted_count": None,
        "activity_attempt": 1,
        "completed": 1,
        "coverage": None,
        "phase": "training",
        "schema_version": 1,
        "sequence": 0,
        "total": 2,
    }
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelling"
    assert retried.status_code == 201
    assert [call[0] for call in service.calls] == [
        "submit",
        "submit",
        "list",
        "get",
        "events",
        "cancel",
        "retry",
    ]


@pytest.mark.asyncio
async def test_quota_error_is_stable_sanitized_and_has_bounded_retry_after() -> None:
    service = QuotaJobs()
    payload = {
        "project_id": str(service.project_id),
        "kind": "evaluate",
        "spec": {},
    }
    async with _client(service) as client:
        response = await client.post(
            "/api/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "quota", "X-Request-ID": "quota-request"},
        )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json() == {
        "code": "quota_exceeded",
        "message": "The organization resource quota is currently exhausted.",
        "operation": "run.submit",
        "request_id": "quota-request",
    }


@pytest.mark.asyncio
async def test_default_application_lifespan_bootstraps_demo_identity(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'demo.db').as_posix()}"
    await asyncio.to_thread(command.upgrade, build_alembic_config(url), "head")
    app = create_app(Settings(environment="test", database_url=url, _env_file=None))

    async with app.router.lifespan_context(app):
        assert app.state.job_service is not None
        jobs = app.state.job_service
        actor = JobActor(
            subject="demo-user",
            organization_id=UUID("00000000-0000-4000-8000-000000000001"),
        )
        created = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.EVALUATE,
                spec={"corpus_ref": "demo"},
            ),
            idempotency_key="demo-bootstrap-test",
        )
        assert created.created is True
