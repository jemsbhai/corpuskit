"""Project lifecycle serialization stays deterministic and least-privileged."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.services.project_lifecycle import lock_project_lifecycle


@pytest.mark.asyncio
async def test_postgres_project_lock_uses_transaction_advisory_lock() -> None:
    session = Mock(spec=AsyncSession)
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    session.scalar = AsyncMock(return_value=None)
    project_id = UUID("00000000-0000-4000-8000-000000000123")

    await lock_project_lifecycle(session, project_id)
    await lock_project_lifecycle(session, project_id)

    first = session.scalar.await_args_list[0].args[0]
    second = session.scalar.await_args_list[1].args[0]
    compiled = first.compile(dialect=postgresql.dialect())
    assert "pg_advisory_xact_lock" in str(compiled)
    assert tuple(compiled.params.values()) == tuple(
        second.compile(dialect=postgresql.dialect()).params.values()
    )


@pytest.mark.asyncio
async def test_sqlite_project_lock_is_an_explicit_noop() -> None:
    session = Mock(spec=AsyncSession)
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    session.scalar = AsyncMock(return_value=None)

    await lock_project_lifecycle(
        session,
        UUID("00000000-0000-4000-8000-000000000123"),
    )

    session.scalar.assert_not_awaited()
