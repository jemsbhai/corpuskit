"""Public health, readiness, and version contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from corpuskit.api.app import _validated_request_id, create_app
from corpuskit.config import Settings
from corpuskit.domain.capabilities import (
    CapabilityCheck,
    CapabilityId,
    CapabilityReport,
    CapabilityState,
)


def test_application_factory_registers_labs_and_project_workspaces() -> None:
    application = create_app()
    paths = application.openapi()["paths"]

    assert {
        "/api/v1/labs/runtime",
        "/api/v1/labs/g2p/languages",
        "/api/v1/labs/g2p/variants",
        "/api/v1/labs/coverage/estimate",
        "/api/v1/labs/coverage/track",
        "/api/v1/labs/reports/render",
        "/api/v1/labs/reports/export",
        "/api/v1/labs/weights/compute",
        "/api/v1/labs/weights/validate",
        "/api/v1/labs/cli/preview",
        "/api/v1/labs/demos/multilingual",
        "/api/v1/generation/preview",
        "/api/v1/scoring/composite",
        "/api/v1/scoring/ngram/scorers",
        "/api/v1/scoring/ngram/constraints",
        "/api/v1/scoring/phonotactics",
        "/api/v1/scoring/readability",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/corpora",
        "/api/v1/projects/{project_id}/corpora/imports",
        "/api/v1/projects/{project_id}/corpora/{corpus_id}/versions",
        ("/api/v1/projects/{project_id}/corpora/{corpus_id}/versions/{version_id}/sentences"),
        ("/api/v1/projects/{project_id}/corpora/{corpus_id}/versions/{version_id}/export"),
        "/api/v1/projects/{project_id}/artifacts",
        "/api/v1/projects/{project_id}/artifacts/{artifact_id}",
        "/api/v1/projects/{project_id}/artifacts/{artifact_id}/download",
        "/api/v1/projects/{project_id}/artifacts/{artifact_id}/download-url",
        "/api/v1/projects/{project_id}/artifacts/{artifact_id}/replay-comparison",
        "/api/v1/projects/{project_id}/runs/{source_run_id}/replays",
        "/api/v1/replays/{replay_run_id}",
    } <= paths.keys()
    assert application.state.job_service.database is application.state.workspace_service.database
    assert application.state.job_service.database is application.state.artifact_service.database
    assert (
        application.state.job_service.database is application.state.reproducibility_service.database
    )
    assert (
        application.state.artifact_service._store
        is application.state.reproducibility_service._store
    )


@pytest.mark.asyncio
async def test_application_factory_executes_bounded_repository_generation(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    async with client_factory(ready_report) as client:
        response = await client.post(
            "/api/v1/generation/preview",
            json={
                "source": {
                    "kind": "prephonemized",
                    "entries": [
                        {"source_id": "p", "text": "Pat.", "phonemes": ["p"]},
                    ],
                },
                "target": {"phonemes": ["p"], "unit": "phoneme"},
                "stopping": {
                    "max_sentences": 1,
                    "max_iterations": 2,
                    "timeout_seconds": 1,
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "target_coverage"


@pytest.mark.asyncio
async def test_liveness_is_dependency_free_and_hardened(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    async with client_factory(ready_report) as client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in response.headers
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    )
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_cors_preflight_keeps_request_id_and_security_headers(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    async with client_factory(ready_report) as client:
        response = await client.options(
            "/api/v1/evaluations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-request-id",
                "X-Request-ID": "preflight-123",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["x-request-id"] == "preflight-123"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )


@pytest.mark.asyncio
async def test_oversized_query_is_rejected_before_routing_without_reflection(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    oversized_value = "sensitive" + ("x" * 4_096)
    async with client_factory(ready_report) as client:
        response = await client.get(
            "/api/v1/capabilities",
            params={"query": oversized_value},
            headers={"X-Request-ID": "target-limit-123"},
        )

    assert response.status_code == 414
    assert response.json() == {
        "code": "request_uri_too_long",
        "message": "The request target exceeds the configured limit.",
        "operation": "http.request_target",
        "request_id": "target-limit-123",
    }
    assert oversized_value not in response.text
    assert response.headers["x-request-id"] == "target-limit-123"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )


@pytest.mark.asyncio
async def test_production_responses_enable_hsts_and_disable_interactive_docs() -> None:
    settings = Settings(
        environment="production",
        auth_mode="oidc",
        job_backend="temporal",
        temporal_tls=True,
        api_docs_enabled=False,
        oidc_issuer="https://identity.example",
        oidc_audience="corpuskit",
        database_url="postgresql+asyncpg://corpuskit:secret@db.example/corpuskit",
        allowed_origins=["https://app.example"],
        artifact_backend="s3",
        artifact_s3_endpoint="https://objects.example",
        artifact_s3_sse="AES256",
        metrics_bearer_token="m" * 32,
        api_rate_limit_enabled=True,
        _env_file=None,
    )
    application = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://corpuskit.example",
    ) as client:
        live = await client.get("/api/v1/health/live")
        docs = await client.get("/docs")

    assert live.status_code == 200
    assert live.headers["strict-transport-security"] == ("max-age=31536000; includeSubDomains")
    assert docs.status_code == 404


@pytest.mark.asyncio
async def test_request_id_is_propagated(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    async with client_factory(ready_report) as client:
        response = await client.get("/api/v1/version", headers={"X-Request-ID": "trace-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "trace-123"
    assert response.json()["corpusgen_contract"] == "0.1.7"


@pytest.mark.parametrize(
    "unsafe_request_id",
    [
        "line-feed\nsecret",
        "carriage-return\rsecret",
        "control\x00secret",
        "x" * 129,
        "résumé-secret",
        " leading-space",
    ],
)
def test_unsafe_request_ids_are_replaced_without_reflection(unsafe_request_id: str) -> None:
    replacement = _validated_request_id(unsafe_request_id)

    assert replacement != unsafe_request_id
    assert "secret" not in replacement
    assert len(replacement) == 36


@pytest.mark.asyncio
async def test_oversized_request_id_is_not_reflected_over_http(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    supplied = "s" * 129
    async with client_factory(ready_report) as client:
        response = await client.get("/api/v1/version", headers={"X-Request-ID": supplied})

    assert response.status_code == 200
    assert response.headers["x-request-id"] != supplied
    assert supplied not in response.text


@pytest.mark.asyncio
async def test_development_docs_have_scoped_csp_without_weakening_api_routes(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    async with client_factory(ready_report) as client:
        docs = await client.get("/docs")
        api = await client.get("/api/v1/version")

    assert docs.status_code == 200
    assert "https://cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    assert "connect-src 'self'" in docs.headers["content-security-policy"]
    assert api.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


@pytest.mark.asyncio
async def test_readiness_returns_report_when_required_capabilities_exist(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
    ready_report: CapabilityReport,
) -> None:
    async with client_factory(ready_report) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True


@pytest.mark.asyncio
async def test_readiness_returns_structured_503_for_missing_requirement(
    client_factory: Callable[[CapabilityReport], httpx.AsyncClient],
) -> None:
    report = CapabilityReport(
        checked_at=datetime(2026, 8, 11, tzinfo=UTC),
        checks=(
            CapabilityCheck(
                id=CapabilityId.ESPEAK_G2P,
                state=CapabilityState.UNAVAILABLE,
                label="eSpeak NG G2P",
                detail="Unavailable.",
                remediation="Install eSpeak NG.",
                required=True,
            ),
        ),
        ready=False,
        missing_required=(CapabilityId.ESPEAK_G2P,),
    )

    async with client_factory(report) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["report"]["missing_required"] == ["espeak-g2p"]
