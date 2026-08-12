"""Centralized, low-cardinality rate limiting for authenticated API traffic."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.auth.models import Principal
from corpuskit.domain.errors import DependencyUnavailableError, TrafficRateLimitExceededError
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import ApiRateLimitWindow
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext

_SAFE_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_SAFE_ROUTE = re.compile(r"^/[\x20-\x7e]{0,255}$", flags=re.ASCII)


class AuthenticatedRateLimiter(Protocol):
    async def enforce(self, principal: Principal, *, method: str, route: str) -> None: ...


class DisabledRateLimiter:
    """Explicit development/test no-op; staging and production reject it in Settings."""

    async def enforce(self, principal: Principal, *, method: str, route: str) -> None:
        del principal, method, route


class DatabaseRateLimiter:
    """Atomically count one fixed window across every API replica."""

    def __init__(
        self,
        database: Database,
        *,
        window_seconds: int,
        read_requests: int,
        write_requests: int,
        retention_windows: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not 10 <= window_seconds <= 3_600:
            raise ValueError("rate-limit window is outside the supported range")
        if not 1 <= read_requests <= 100_000 or not 1 <= write_requests <= 10_000:
            raise ValueError("rate-limit request ceilings are outside the supported range")
        if not 2 <= retention_windows <= 100:
            raise ValueError("rate-limit retention is outside the supported range")
        self._database = database
        self._window_seconds = window_seconds
        self._read_requests = read_requests
        self._write_requests = write_requests
        self._retention_windows = retention_windows
        self._clock = clock

    async def enforce(self, principal: Principal, *, method: str, route: str) -> None:
        method = method.upper()
        if method not in _SAFE_METHODS or _SAFE_ROUTE.fullmatch(route) is None:
            raise TrafficRateLimitExceededError(
                "http.rate_limit.contract",
                retry_after_seconds=self._window_seconds,
            )
        subject_sha256 = _digest(principal.subject)
        route_sha256 = _digest(route)
        limit = (
            self._read_requests if method in {"GET", "HEAD", "OPTIONS"} else self._write_requests
        )
        context = TenantContext.user(principal.organization_id, principal.subject)
        try:
            async with self._database.session(context) as session:
                window_epoch, retry_after_seconds = await self._window_state(session)
                values = {
                    "id": uuid4(),
                    "organization_id": principal.organization_id,
                    "subject_sha256": subject_sha256,
                    "route_sha256": route_sha256,
                    "method": method,
                    "window_epoch": window_epoch,
                    "request_count": 1,
                }
                dialect = session.get_bind().dialect.name
                if dialect == "postgresql":
                    statement = (
                        postgres_insert(ApiRateLimitWindow)
                        .values(**values)
                        .on_conflict_do_update(
                            constraint="uq_api_rate_limit_windows_scope",
                            set_={
                                "request_count": ApiRateLimitWindow.request_count + 1,
                                "updated_at": func.now(),
                            },
                            where=ApiRateLimitWindow.request_count < limit,
                        )
                        .returning(ApiRateLimitWindow.request_count)
                    )
                elif dialect == "sqlite":
                    statement = (
                        sqlite_insert(ApiRateLimitWindow)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=(
                                ApiRateLimitWindow.organization_id,
                                ApiRateLimitWindow.subject_sha256,
                                ApiRateLimitWindow.route_sha256,
                                ApiRateLimitWindow.method,
                                ApiRateLimitWindow.window_epoch,
                            ),
                            set_={
                                "request_count": ApiRateLimitWindow.request_count + 1,
                                "updated_at": func.now(),
                            },
                            where=ApiRateLimitWindow.request_count < limit,
                        )
                        .returning(ApiRateLimitWindow.request_count)
                    )
                else:  # pragma: no cover - supported URLs are PostgreSQL/SQLite only.
                    raise RuntimeError("rate limiting requires PostgreSQL or SQLite")
                counted = (await session.execute(statement)).scalar_one_or_none()
                count = int(counted) if counted is not None else None
        except SQLAlchemyError as error:
            raise DependencyUnavailableError("http.rate_limit") from error
        if count is None or count > limit:
            raise TrafficRateLimitExceededError(
                "http.rate_limit",
                retry_after_seconds=retry_after_seconds,
            )

    async def purge_expired(self, *, limit: int = 1_000) -> int:
        """Delete a bounded oldest page under the maintenance-only database identity."""

        if not 1 <= limit <= 1_000:
            raise ValueError("rate-limit cleanup must be between 1 and 1000")
        async with self._database.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            current, _ = await self._window_state(session)
            ids = tuple(
                (
                    await session.scalars(
                        select(ApiRateLimitWindow.id)
                        .where(ApiRateLimitWindow.window_epoch < current - self._retention_windows)
                        .order_by(ApiRateLimitWindow.window_epoch, ApiRateLimitWindow.id)
                        .limit(limit)
                    )
                ).all()
            )
            if ids:
                await session.execute(
                    delete(ApiRateLimitWindow).where(ApiRateLimitWindow.id.in_(ids))
                )
            return len(ids)

    async def _window_state(self, session: AsyncSession) -> tuple[int, int]:
        if self._clock is not None:
            value = self._clock()
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("rate-limit clock must return a nonnegative timestamp")
            timestamp = value
        else:
            # Database time avoids replica clock skew in production and keeps one window
            # authority for both the bucket and its Retry-After boundary.
            dialect = session.get_bind().dialect.name
            expression = (
                func.extract("epoch", func.clock_timestamp())
                if dialect == "postgresql"
                else func.strftime("%s", "now")
            )
            database_timestamp = await session.scalar(select(expression))
            if database_timestamp is None:  # pragma: no cover - database scalar contract defense.
                raise RuntimeError("rate-limit database clock returned no value")
            timestamp = float(database_timestamp)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise RuntimeError("rate-limit database clock returned no value")
        whole_seconds = int(timestamp)
        return (
            whole_seconds // self._window_seconds,
            self._window_seconds - (whole_seconds % self._window_seconds),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


__all__ = [
    "AuthenticatedRateLimiter",
    "DatabaseRateLimiter",
    "DisabledRateLimiter",
]
