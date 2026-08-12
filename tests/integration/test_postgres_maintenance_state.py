"""Real PostgreSQL acceptance for private, forced-RLS maintenance progress."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from corpuskit.persistence.database import Database
from corpuskit.persistence.models import MaintenanceCursor
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.maintenance import DatabaseMaintenanceState, MaintenanceOperation

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


@pytest.mark.asyncio
async def test_maintenance_cursor_is_forced_rls_and_maintenance_only() -> None:
    assert OWNER_URL is not None
    assert APP_URL is not None
    assert MAINTENANCE_URL is not None
    fingerprint = uuid4().hex + uuid4().hex
    cursor = "staging/v1/sha256/ab/" + "a" * 64
    owner = Database(OWNER_URL)
    maintenance = Database(MAINTENANCE_URL)
    app = Database(APP_URL)
    state = DatabaseMaintenanceState(maintenance, fingerprint)
    try:
        await state.advance(
            MaintenanceOperation.STAGING_CLEANUP,
            expected=None,
            next_cursor=cursor,
        )
        assert await state.load(MaintenanceOperation.STAGING_CLEANUP) == cursor

        async with owner.session(TenantContext.service(ServiceIdentity.MAINTENANCE)) as session:
            flags = (
                await session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'maintenance_cursors'"
                    )
                )
            ).one()
            assert flags._tuple() == (True, True)
            privileges = (
                await session.execute(
                    text(
                        "SELECT "
                        "has_table_privilege('corpuskit_maintenance', "
                        "'maintenance_cursors', 'SELECT'), "
                        "has_table_privilege('corpuskit_maintenance', "
                        "'maintenance_cursors', 'INSERT'), "
                        "has_table_privilege('corpuskit_maintenance', "
                        "'maintenance_cursors', 'UPDATE'), "
                        "has_table_privilege('corpuskit_maintenance', "
                        "'maintenance_cursors', 'DELETE'), "
                        "has_table_privilege('corpuskit_api', "
                        "'maintenance_cursors', 'SELECT'), "
                        "has_table_privilege('corpuskit_worker', "
                        "'maintenance_cursors', 'SELECT')"
                    )
                )
            ).one()
            assert privileges._tuple() == (True, True, True, True, False, False)

        with pytest.raises(DBAPIError):
            async with app.session(
                TenantContext.user(uuid4(), f"oidc|maintenance-denial-{uuid4()}")
            ) as session:
                await session.scalar(select(MaintenanceCursor.cursor))

        await state.advance(
            MaintenanceOperation.STAGING_CLEANUP,
            expected=cursor,
            next_cursor=None,
        )
    finally:
        await owner.dispose()
        await maintenance.dispose()
        await app.dispose()
