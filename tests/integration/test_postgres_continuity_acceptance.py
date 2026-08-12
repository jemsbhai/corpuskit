"""Real, opt-in PostgreSQL backup and isolated restore-drill acceptance."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from corpuskit.operations.postgres_continuity import (
    PostgresContinuity,
    PostgresToolchain,
    restore_confirmation,
)

_RUN_ENV = "CORPUSKIT_RUN_POSTGRES_CONTINUITY_ACCEPTANCE"
_URL_ENV = "CORPUSKIT_TEST_POSTGRES_OWNER_URL"
_SAFE_SOURCE = re.compile(r"corpuskit_(?:migrations|continuity_ci)", flags=re.ASCII)
_SAFE_TARGET = re.compile(r"corpuskit_restore_drill_[a-f0-9]{24}", flags=re.ASCII)


def _acceptance_connection() -> tuple[str, int, str, str | None, str]:
    if os.getenv(_RUN_ENV) != "1":
        pytest.skip(f"set {_RUN_ENV}=1 for the destructive-isolated PostgreSQL drill")
    raw_url = os.getenv(_URL_ENV)
    if not raw_url:
        pytest.fail(f"{_URL_ENV} is required when {_RUN_ENV}=1")
    url = make_url(raw_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.port is None
        or url.username is None
        or url.database is None
        or _SAFE_SOURCE.fullmatch(url.database) is None
    ):
        pytest.fail("continuity acceptance requires the allowlisted loopback CI database")
    return (
        url.host,
        url.port,
        unquote(url.username),
        unquote(url.password) if url.password is not None else None,
        url.database,
    )


def _toolchain() -> PostgresToolchain:
    missing = [name for name in ("pg_dump", "pg_restore", "psql") if shutil.which(name) is None]
    if missing:
        pytest.fail("matching PostgreSQL client tools are required for continuity acceptance")
    return PostgresToolchain.discover()


def _libpq_environment(
    *, host: str, port: int, user: str, password: str | None, database: str
) -> dict[str, str]:
    environment = {
        "PGDATABASE": database,
        "PGHOST": host,
        "PGPORT": str(port),
        "PGSSLMODE": "disable",
        "PGUSER": user,
    }
    if password is not None:
        environment["PGPASSWORD"] = password
    return environment


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_backup_offline_verify_and_isolated_restore_roundtrip(tmp_path: Path) -> None:
    """Restore is permitted only into the empty database created in this test."""

    host, port, user, password, source_database = _acceptance_connection()
    tools = _toolchain()
    marker = uuid4().hex
    marker_table = f"continuity_acceptance_marker_{uuid4().hex[:12]}"
    assert re.fullmatch(r"continuity_acceptance_marker_[a-f0-9]{12}", marker_table) is not None
    target_database = f"corpuskit_restore_drill_{uuid4().hex[:24]}"
    assert _SAFE_TARGET.fullmatch(target_database) is not None
    root = (tmp_path / "continuity").resolve()
    root.mkdir(mode=0o700)
    root.chmod(0o700)

    source = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=source_database,
        timeout=10,
    )
    admin = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database="postgres",
        timeout=10,
    )
    target_created = False
    marker_table_created = False
    try:
        if await source.fetchval("SELECT to_regclass('public.alembic_version')") is None:
            pytest.fail("continuity acceptance source must already be migrated")
        await source.execute(f'CREATE TABLE public."{marker_table}" (marker text PRIMARY KEY)')
        marker_table_created = True
        await source.execute(
            f'INSERT INTO public."{marker_table}"(marker) VALUES($1)',
            marker,
        )
        # The identifier is generated locally and constrained above before interpolation.
        await admin.execute(f'CREATE DATABASE "{target_database}" TEMPLATE template0')
        target_created = True

        backup = PostgresContinuity(
            root,
            tools,
            process_environment=_libpq_environment(
                host=host,
                port=port,
                user=user,
                password=password,
                database=source_database,
            ),
        ).create_backup(timeout_seconds=300)

        offline = PostgresContinuity(root, tools, process_environment={}).verify_backup(
            backup.bundle_id,
            timeout_seconds=120,
        )
        assert offline.archive_sha256 == backup.archive_sha256

        target_environment = _libpq_environment(
            host=host,
            port=port,
            user=user,
            password=password,
            database=target_database,
        )
        restored = PostgresContinuity(
            root,
            tools,
            process_environment=target_environment,
        ).restore_drill(
            backup.bundle_id,
            confirmation=restore_confirmation(backup.bundle_id, target_database),
            timeout_seconds=300,
        )
        assert restored.archive_sha256 == backup.archive_sha256
        assert restored.restored_relation_count > 0

        target = await asyncpg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=target_database,
            timeout=10,
        )
        try:
            assert (
                await target.fetchval(
                    f'SELECT marker FROM public."{marker_table}" WHERE marker = $1',  # noqa: S608
                    marker,
                )
                == marker
            )
            assert (
                await target.fetchval("SELECT version_num FROM public.alembic_version LIMIT 1")
                == restored.alembic_revision
            )
        finally:
            await target.close()
    finally:
        try:
            if marker_table_created:
                assert (
                    re.fullmatch(r"continuity_acceptance_marker_[a-f0-9]{12}", marker_table)
                    is not None
                )
                await source.execute(f'DROP TABLE public."{marker_table}"')
        finally:
            try:
                if target_created:
                    assert _SAFE_TARGET.fullmatch(target_database) is not None
                    await admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = $1 AND pid <> pg_backend_pid()",
                        target_database,
                    )
                    # The target was freshly created above and remains pattern-guarded.
                    await admin.execute(f'DROP DATABASE "{target_database}"')
            finally:
                await source.close()
                await admin.close()
