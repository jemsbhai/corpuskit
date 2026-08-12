"""Real PostgreSQL acceptance for centralized rate-limit concurrency and role isolation."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from corpuskit.auth.models import AuthRole, Principal
from corpuskit.domain.errors import DependencyUnavailableError, TrafficRateLimitExceededError
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import ApiRateLimitWindow, Membership, Organization, Role, User
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.rate_limits import DatabaseRateLimiter

OWNER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_OWNER_URL")
APP_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_APP_URL")
MAINTENANCE_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_MAINTENANCE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all((OWNER_URL, APP_URL, MAINTENANCE_URL)),
        reason="separate PostgreSQL owner, API, and maintenance roles are not configured",
    ),
]


async def _principal(label: str) -> Principal:
    assert OWNER_URL is not None
    organization_id = uuid4()
    user_id = uuid4()
    subject = f"oidc|rate-limit-{label}-{uuid4()}"
    owner = Database(OWNER_URL)
    try:
        async with owner.session(
            TenantContext.service(ServiceIdentity.PLATFORM, organization_id)
        ) as session:
            session.add_all(
                (
                    Organization(
                        id=organization_id,
                        slug=f"rate-{label}-{organization_id.hex[:10]}",
                        name=f"Rate {label}",
                    ),
                    User(id=user_id, oidc_subject=subject, display_name=label),
                )
            )
            await session.flush()
            session.add(
                Membership(
                    organization_id=organization_id,
                    user_id=user_id,
                    role=Role.EDITOR,
                )
            )
    finally:
        await owner.dispose()
    return Principal(
        subject=subject,
        organization_id=organization_id,
        role=AuthRole.EDITOR,
    )


@pytest.mark.asyncio
async def test_postgres_rate_limits_are_atomic_capped_and_role_isolated() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    assert MAINTENANCE_URL is not None
    first, second = await asyncio.gather(_principal("first"), _principal("second"))
    moment = [125.0]
    api = Database(APP_URL)
    maintenance = Database(MAINTENANCE_URL)
    owner = Database(OWNER_URL)
    limiter = DatabaseRateLimiter(
        api,
        window_seconds=60,
        read_requests=10,
        write_requests=3,
        retention_windows=2,
        clock=lambda: moment[0],
    )
    cleanup = DatabaseRateLimiter(
        maintenance,
        window_seconds=60,
        read_requests=10,
        write_requests=3,
        retention_windows=2,
        clock=lambda: moment[0],
    )
    try:
        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            await session.execute(delete(ApiRateLimitWindow))

        outcomes = await asyncio.gather(
            *(limiter.enforce(first, method="POST", route="/api/v1/runs") for _ in range(12)),
            return_exceptions=True,
        )
        assert sum(outcome is None for outcome in outcomes) == 3
        rejected = [
            outcome for outcome in outcomes if isinstance(outcome, TrafficRateLimitExceededError)
        ]
        assert len(rejected) == 9
        assert {error.retry_after_seconds for error in rejected} == {55}

        await limiter.enforce(first, method="GET", route="/api/v1/runs")
        await limiter.enforce(first, method="POST", route="/api/v1/projects")
        await limiter.enforce(second, method="POST", route="/api/v1/runs")

        unregistered = first.model_copy(update={"subject": "oidc|not-a-member"})
        with pytest.raises(DependencyUnavailableError):
            await limiter.enforce(unregistered, method="POST", route="/api/v1/runs")

        async with api.session(TenantContext.user(first.organization_id, first.subject)) as session:
            rows = (await session.scalars(select(ApiRateLimitWindow))).all()
            assert len(rows) == 3
            saturated = next(row for row in rows if row.method == "POST" and row.request_count > 1)
            assert saturated.request_count == 3
            assert all(len(row.subject_sha256) == 64 for row in rows)
            assert all(len(row.route_sha256) == 64 for row in rows)
            assert first.subject not in repr(rows)
            privileges = (
                await session.execute(
                    text(
                        "SELECT "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'SELECT'), "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'INSERT'), "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'UPDATE'), "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'DELETE')"
                    )
                )
            ).one()
            assert privileges._tuple() == (True, True, True, False)

        async with api.session(
            TenantContext.user(second.organization_id, second.subject)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(ApiRateLimitWindow)) == 1

        async def delete_as_api() -> None:
            async with api.session(
                TenantContext.user(first.organization_id, first.subject)
            ) as session:
                await session.execute(delete(ApiRateLimitWindow))

        with pytest.raises(DBAPIError):
            await delete_as_api()

        async with api.session(TenantContext.service(ServiceIdentity.MAINTENANCE)) as session:
            assert await session.scalar(select(func.count()).select_from(ApiRateLimitWindow)) == 0

        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(ApiRateLimitWindow)) == 4
            privileges = (
                await session.execute(
                    text(
                        "SELECT "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'SELECT'), "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'INSERT'), "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'UPDATE'), "
                        "has_table_privilege(current_user, 'api_rate_limit_windows', 'DELETE')"
                    )
                )
            ).one()
            assert privileges._tuple() == (True, False, False, True)

        async def update_as_maintenance() -> None:
            async with maintenance.session(
                TenantContext.service(ServiceIdentity.MAINTENANCE)
            ) as session:
                await session.execute(update(ApiRateLimitWindow).values(request_count=10))

        with pytest.raises(DBAPIError):
            await update_as_maintenance()

        async with owner.session(
            TenantContext.service(ServiceIdentity.PLATFORM, first.organization_id)
        ) as session:
            for role in (
                "corpuskit_dispatcher",
                "corpuskit_worker",
                "corpuskit_adoption",
            ):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert (
                        await session.scalar(
                            text(
                                "SELECT has_table_privilege("
                                ":role, 'api_rate_limit_windows', :privilege)"
                            ),
                            {"role": role, "privilege": privilege},
                        )
                        is False
                    )
            assert (
                await session.scalar(
                    text(
                        "SELECT has_table_privilege("
                        "'corpuskit_platform', 'api_rate_limit_windows', 'SELECT')"
                    )
                )
                is True
            )
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert (
                    await session.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "'corpuskit_platform', 'api_rate_limit_windows', :privilege)"
                        ),
                        {"privilege": privilege},
                    )
                    is False
                )

        moment[0] = 400.0
        assert await cleanup.purge_expired(limit=2) == 2
        assert await cleanup.purge_expired(limit=2) == 2
        assert await cleanup.purge_expired(limit=2) == 0
        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            assert await session.scalar(select(func.count()).select_from(ApiRateLimitWindow)) == 0

        database_clock_limiter = DatabaseRateLimiter(
            api,
            window_seconds=60,
            read_requests=10,
            write_requests=1,
            retention_windows=2,
        )
        database_clock_error: TrafficRateLimitExceededError | None = None
        for _ in range(3):
            try:
                await database_clock_limiter.enforce(
                    first,
                    method="POST",
                    route="/api/v1/database-clock",
                )
            except TrafficRateLimitExceededError as error:
                database_clock_error = error
                break
        assert database_clock_error is not None
        assert 1 <= database_clock_error.retry_after_seconds <= 60
        async with api.session(TenantContext.user(first.organization_id, first.subject)) as session:
            database_epoch = await session.scalar(
                select(func.floor(func.extract("epoch", func.clock_timestamp()) / 60))
            )
            epochs = set(await session.scalars(select(ApiRateLimitWindow.window_epoch)))
            assert database_epoch is not None
            assert epochs <= {int(database_epoch), int(database_epoch) - 1}
        async with maintenance.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            await session.execute(delete(ApiRateLimitWindow))
    finally:
        await api.dispose()
        await maintenance.dispose()
        await owner.dispose()
