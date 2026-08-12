"""Async SQLAlchemy engine and transaction lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from corpuskit.persistence.models import Base
from corpuskit.persistence.tenant_context import (
    TenantContext,
    TenantContextError,
    apply_postgresql_context,
)


class Database:
    """Own an async engine and create short-lived transactional sessions."""

    def __init__(self, url: str, *, echo: bool = False, engine: AsyncEngine | None = None) -> None:
        self.engine = engine or create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        """Create tables for tests and local demo mode; production uses Alembic."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_schema(self) -> None:
        """Drop test/demo tables. Never called by production application code."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(
        self,
        context: TenantContext | None = None,
    ) -> AsyncIterator[AsyncSession]:
        """Yield a session that commits once or rolls back atomically."""

        async with self.sessions() as session:
            try:
                if context is not None:
                    context.validate()
                    session.info["tenant_context"] = context
                if session.get_bind().dialect.name == "postgresql":
                    if context is None:
                        raise TenantContextError(
                            "PostgreSQL application sessions require an explicit tenant context"
                        )
                    await apply_postgresql_context(session, context)
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            finally:
                session.info.clear()

    async def dispose(self) -> None:
        """Release database connections during shutdown."""

        await self.engine.dispose()
