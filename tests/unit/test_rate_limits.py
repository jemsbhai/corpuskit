from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.api.app import create_app
from corpuskit.auth.models import AuthRole, Principal
from corpuskit.config import Settings
from corpuskit.domain.errors import TrafficRateLimitExceededError
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import ApiRateLimitWindow, Membership, Organization, Role, User
from corpuskit.persistence.tenant_context import TenantContext
from corpuskit.services.rate_limits import DatabaseRateLimiter, DisabledRateLimiter

ORGANIZATION_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
PRINCIPAL = Principal(
    subject="rate-limit-user",
    organization_id=ORGANIZATION_ID,
    role=AuthRole.EDITOR,
)


class FixedAuthenticator:
    async def authenticate(self, token: str | None) -> Principal:
        del token
        return PRINCIPAL


async def _database(tmp_path: Path) -> Database:
    path = tmp_path / "rate-limit.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    async with database.session(TenantContext.user(ORGANIZATION_ID, PRINCIPAL.subject)) as session:
        session.add(Organization(id=ORGANIZATION_ID, slug="rate-limit", name="Rate Limit"))
        session.add(User(id=USER_ID, oidc_subject=PRINCIPAL.subject, display_name="Rate User"))
        session.add(
            Membership(
                organization_id=ORGANIZATION_ID,
                user_id=USER_ID,
                role=Role.EDITOR,
            )
        )
    return database


@pytest.mark.asyncio
async def test_disabled_limiter_is_an_explicit_noop() -> None:
    await DisabledRateLimiter().enforce(PRINCIPAL, method="TRACE", route="not-a-route")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"window_seconds": 9}, "window"),
        ({"window_seconds": 3_601}, "window"),
        ({"read_requests": 0}, "ceilings"),
        ({"read_requests": 100_001}, "ceilings"),
        ({"write_requests": 0}, "ceilings"),
        ({"write_requests": 10_001}, "ceilings"),
        ({"retention_windows": 1}, "retention"),
        ({"retention_windows": 101}, "retention"),
    ],
)
async def test_constructor_rejects_out_of_contract_bounds(
    overrides: dict[str, int],
    message: str,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    values = {
        "window_seconds": 10,
        "read_requests": 10,
        "write_requests": 1,
        "retention_windows": 2,
        **overrides,
    }
    try:
        with pytest.raises(ValueError, match=message):
            DatabaseRateLimiter(database, **values)
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [-1.0, float("nan"), float("inf"), True, "invalid"])
async def test_clock_rejects_nonfinite_negative_boolean_and_nonnumeric_values(
    tmp_path: Path,
    invalid: object,
) -> None:
    database = await _database(tmp_path)

    def invalid_clock() -> float:
        return cast(float, invalid)

    limiter = DatabaseRateLimiter(
        database,
        window_seconds=10,
        read_requests=10,
        write_requests=1,
        retention_windows=2,
        clock=invalid_clock,
    )
    try:
        with pytest.raises(ValueError, match="nonnegative timestamp"):
            await limiter.enforce(PRINCIPAL, method="POST", route="/api/v1/runs")
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("database_value", [None, float("nan"), -1.0])
async def test_database_clock_rejects_absent_nonfinite_and_negative_values(
    database_value: float | None,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    limiter = DatabaseRateLimiter(
        database,
        window_seconds=10,
        read_requests=10,
        write_requests=1,
        retention_windows=2,
    )
    session = cast(
        AsyncSession,
        SimpleNamespace(
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
            scalar=AsyncMock(return_value=database_value),
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="database clock returned no value"):
            await limiter._window_state(session)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_sqlite_database_clock_controls_bucket_and_retry_boundary(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    limiter = DatabaseRateLimiter(
        database,
        window_seconds=10,
        read_requests=10,
        write_requests=1,
        retention_windows=2,
    )
    try:
        await limiter.enforce(PRINCIPAL, method="POST", route="/api/v1/database-clock")
        with pytest.raises(TrafficRateLimitExceededError) as raised:
            await limiter.enforce(PRINCIPAL, method="POST", route="/api/v1/database-clock")
        assert 1 <= raised.value.retry_after_seconds <= 10
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_atomic_fixed_window_enforces_concurrent_write_limit(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    limiter = DatabaseRateLimiter(
        database,
        window_seconds=60,
        read_requests=10,
        write_requests=2,
        retention_windows=3,
        clock=lambda: 125.0,
    )
    try:
        outcomes = await asyncio.gather(
            *(limiter.enforce(PRINCIPAL, method="POST", route="/api/v1/runs") for _ in range(3)),
            return_exceptions=True,
        )
        assert sum(outcome is None for outcome in outcomes) == 2
        failures = [
            outcome for outcome in outcomes if isinstance(outcome, TrafficRateLimitExceededError)
        ]
        assert len(failures) == 1
        assert failures[0].retry_after_seconds == 55
        saturated = await asyncio.gather(
            *(limiter.enforce(PRINCIPAL, method="POST", route="/api/v1/runs") for _ in range(8)),
            return_exceptions=True,
        )
        assert all(isinstance(outcome, TrafficRateLimitExceededError) for outcome in saturated)
        async with database.session(
            TenantContext.user(ORGANIZATION_ID, PRINCIPAL.subject)
        ) as session:
            row = await session.scalar(select(ApiRateLimitWindow))
            assert row is not None
            assert row.request_count == 2
            assert row.subject_sha256 != PRINCIPAL.subject
            assert row.route_sha256 != "/api/v1/runs"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_http_rate_limit_persistence_failure_is_stable_sanitized_503(tmp_path: Path) -> None:
    missing_schema = Database(f"sqlite+aiosqlite:///{tmp_path / 'missing-rate-limit-schema.db'}")
    limiter = DatabaseRateLimiter(
        missing_schema,
        window_seconds=10,
        read_requests=10,
        write_requests=1,
        retention_windows=2,
        clock=lambda: 5.0,
    )
    application = create_app(
        Settings(environment="test", _env_file=None),
        authenticator_factory=lambda _: FixedAuthenticator(),
        rate_limiter_factory=lambda _: limiter,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post("/api/v1/phonology/load")

        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "retry-after" not in response.headers
        assert response.json() == {
            "code": "dependency_unavailable",
            "message": "A required language-processing dependency is not available.",
            "operation": "http.rate_limit",
            "request_id": response.headers["x-request-id"],
        }
        assert PRINCIPAL.subject not in response.text
        assert "api_rate_limit_windows" not in response.text
        assert "sqlite" not in response.text.lower()
    finally:
        await missing_schema.dispose()


@pytest.mark.asyncio
async def test_read_write_route_subject_and_window_scopes_are_independent(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    moment = [120.0]
    limiter = DatabaseRateLimiter(
        database,
        window_seconds=60,
        read_requests=2,
        write_requests=1,
        retention_windows=3,
        clock=lambda: moment[0],
    )
    other = PRINCIPAL.model_copy(update={"subject": "second-user"})
    try:
        await limiter.enforce(PRINCIPAL, method="GET", route="/api/v1/runs")
        await limiter.enforce(PRINCIPAL, method="GET", route="/api/v1/runs")
        await limiter.enforce(PRINCIPAL, method="POST", route="/api/v1/runs")
        await limiter.enforce(PRINCIPAL, method="GET", route="/api/v1/projects")
        await limiter.enforce(other, method="GET", route="/api/v1/runs")
        with pytest.raises(TrafficRateLimitExceededError):
            await limiter.enforce(PRINCIPAL, method="GET", route="/api/v1/runs")
        moment[0] = 180.0
        await limiter.enforce(PRINCIPAL, method="GET", route="/api/v1/runs")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cleanup_is_bounded_and_contract_values_fail_closed(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    moment = [0.0]
    limiter = DatabaseRateLimiter(
        database,
        window_seconds=10,
        read_requests=10,
        write_requests=1,
        retention_windows=2,
        clock=lambda: moment[0],
    )
    try:
        for window in range(4):
            moment[0] = float(window * 10)
            await limiter.enforce(PRINCIPAL, method="GET", route=f"/api/v1/routes/{window}")
        moment[0] = 100.0
        assert await limiter.purge_expired(limit=2) == 2
        assert await limiter.purge_expired(limit=2) == 2
        assert await limiter.purge_expired(limit=2) == 0
        async with database.session(
            TenantContext.user(ORGANIZATION_ID, PRINCIPAL.subject)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(ApiRateLimitWindow)) == 0
        with pytest.raises(TrafficRateLimitExceededError, match="temporarily exhausted"):
            await limiter.enforce(PRINCIPAL, method="TRACE", route="/private")
        with pytest.raises(TrafficRateLimitExceededError):
            await limiter.enforce(PRINCIPAL, method="GET", route="\nsecret")
        with pytest.raises(ValueError, match="between 1 and 1000"):
            await limiter.purge_expired(limit=0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_http_auth_dependency_returns_stable_429_with_retry_after(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    limiter = DatabaseRateLimiter(
        database,
        window_seconds=10,
        read_requests=10,
        write_requests=1,
        retention_windows=2,
        clock=lambda: 5.0,
    )
    application = create_app(
        Settings(environment="test", _env_file=None),
        authenticator_factory=lambda _: FixedAuthenticator(),
        rate_limiter_factory=lambda _: limiter,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            first = await client.post("/api/v1/phonology/load")
            second = await client.post("/api/v1/phonology/load")

        assert first.status_code != 429
        assert second.status_code == 429
        assert second.headers["retry-after"] == "5"
        assert second.headers["cache-control"] == "no-store"
        assert second.json() == {
            "code": "rate_limited",
            "message": "The request rate limit is temporarily exhausted.",
            "operation": "http.rate_limit",
            "request_id": second.headers["x-request-id"],
        }
        assert PRINCIPAL.subject not in second.text
    finally:
        await database.dispose()
