"""FastAPI authentication and role-protection contracts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import jwt
import pytest

from corpuskit.api.app import CapabilityReporter, create_app
from corpuskit.auth import (
    AuthenticationError,
    AuthenticationUnavailableError,
    Authenticator,
    AuthRole,
    Principal,
)
from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityReport


class ReadyReporter:
    def report(self, *, force: bool = False) -> CapabilityReport:
        del force
        return CapabilityReport(checked_at=datetime.now(UTC), checks=(), ready=True)


class FixedAuthenticator:
    def __init__(self, role: AuthRole) -> None:
        self.role = role

    async def authenticate(self, token: str | None) -> Principal:
        if token != "accepted-token":
            raise AuthenticationError
        return Principal(
            subject="user-1",
            organization_id="e44f2343-1c8c-42b5-93b8-42ca62e88e05",
            role=self.role,
        )


class UnavailableAuthenticator:
    async def authenticate(self, token: str | None) -> Principal:
        del token
        raise AuthenticationUnavailableError


def _client(role: AuthRole) -> httpx.AsyncClient:
    settings = Settings(
        environment="test",
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="corpuskit",
        _env_file=None,
    )

    def reporter_factory(_: Settings) -> CapabilityReporter:
        return ReadyReporter()

    def authenticator_factory(_: Settings) -> Authenticator:
        return FixedAuthenticator(role)

    app = create_app(
        settings,
        reporter_factory=reporter_factory,
        authenticator_factory=authenticator_factory,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _unavailable_client() -> httpx.AsyncClient:
    settings = Settings(
        environment="test",
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test/private/provider-path",
        oidc_audience="corpuskit",
        _env_file=None,
    )
    app = create_app(
        settings,
        reporter_factory=lambda _: ReadyReporter(),
        authenticator_factory=lambda _: UnavailableAuthenticator(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_identity_route_returns_verified_context_and_requires_bearer() -> None:
    async with _client(AuthRole.EDITOR) as client:
        missing = await client.get("/api/v1/auth/me")
        accepted = await client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer accepted-token"}
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["code"] == "invalid_authentication"
    assert accepted.status_code == 200
    assert accepted.json()["role"] == "editor"


@pytest.mark.asyncio
async def test_workflow_routes_reject_viewer_role_before_dispatch() -> None:
    async with _client(AuthRole.VIEWER) as client:
        response = await client.post(
            "/api/v1/g2p",
            json={"text": "hello", "language": "en-us"},
            headers={"Authorization": "Bearer accepted-token"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.asyncio
async def test_viewer_can_use_non_executing_cli_preview_lab() -> None:
    async with _client(AuthRole.VIEWER) as client:
        response = await client.post(
            "/api/v1/labs/cli/preview",
            json={"workflow": "inventory", "language": "en-us"},
            headers={"Authorization": "Bearer accepted-token"},
        )

    assert response.status_code == 200
    assert response.json()["argv"][:2] == ["corpusgen", "inventory"]


@pytest.mark.asyncio
async def test_advanced_reads_allow_viewers_but_policy_validation_requires_editor() -> None:
    headers = {"Authorization": "Bearer accepted-token"}
    async with _client(AuthRole.VIEWER) as viewer:
        accepted_catalog = await viewer.get("/api/v1/advanced/capabilities", headers=headers)
        accepted_lab = await viewer.post(
            "/api/v1/phon-rl/ppo/kl-penalty",
            json={
                "policy_log_probs": {"values": [[0.0]]},
                "reference_log_probs": {"values": [[0.0]]},
            },
            headers=headers,
        )
        denied_validation = await viewer.post(
            "/api/v1/model-runtime/hosted/validate",
            json={},
            headers=headers,
        )
    async with _client(AuthRole.EDITOR) as editor:
        parsed_validation = await editor.post(
            "/api/v1/model-runtime/hosted/validate",
            json={},
            headers=headers,
        )

    assert accepted_catalog.status_code == 200
    assert accepted_catalog.json()["advanced_operation_routes_validation_only"] is True
    assert accepted_catalog.json()["durable_run_submission_route"] == "/api/v1/runs"
    assert accepted_lab.status_code == 200
    assert denied_validation.status_code == 403
    assert parsed_validation.status_code == 422


@pytest.mark.asyncio
async def test_invalid_token_error_does_not_reflect_header_or_url_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    header_secret = "header-secret-must-not-leak"
    path_secret = "/private/path-secret-must-not-leak"
    malicious = jwt.encode(
        {"sub": "attacker"},
        key="",
        algorithm="none",
        headers={"kid": header_secret},
    )
    malicious_header = malicious.decode() if isinstance(malicious, bytes) else malicious
    caplog.set_level(logging.DEBUG)

    async with _client(AuthRole.EDITOR) as client:
        response = await client.get(
            "/api/v1/auth/me",
            params={"return_to": path_secret},
            headers={"Authorization": f"Bearer {malicious_header}"},
        )

    assert response.status_code == 401
    assert response.json()["message"] == "Authentication credentials are missing or invalid."
    combined = response.text + caplog.text
    assert header_secret not in combined
    assert path_secret not in combined


@pytest.mark.asyncio
async def test_provider_failure_returns_safe_retryable_error() -> None:
    async with _unavailable_client() as client:
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer provider-secret-token"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "authentication_unavailable"
    assert "provider-secret-token" not in response.text
    assert "private/provider-path" not in response.text
