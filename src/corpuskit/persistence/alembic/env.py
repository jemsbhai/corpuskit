"""Alembic environment for async PostgreSQL and test SQLite databases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from alembic import context
from alembic.script.revision import RangeNotAncestorError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from corpuskit.persistence.migration_cli import resolve_database_url
from corpuskit.persistence.models import Base

target_metadata = Base.metadata
POSTGRES_MIGRATION_LOCK_ID = 4_915_396_595_806_804_339
_ARTIFACT_INTEGRITY_REVISION = "0003_artifact_integrity"
_LEGACY_IDENTITY_COLLISION_ERROR = (
    "Cannot downgrade 0003_artifact_integrity to 0002_durable_job_outbox: "
    "artifact rows cannot be represented by the legacy "
    "(organization_id, sha256, kind) uniqueness constraint. No schema changes were applied; "
    "keep revision 0003 or later and use an approved data-preserving remediation or restore."
)


def _preflight_downgrade(connection: Connection) -> None:
    migration_context = context.get_context()
    current_heads = migration_context.get_current_heads()
    if len(current_heads) != 1:
        return
    try:
        destination = context.get_revision_argument()
    except KeyError:
        return
    try:
        downgrade_revisions = {
            migration_revision.revision
            for migration_revision in context.script.iterate_revisions(
                current_heads[0], destination
            )
        }
    except RangeNotAncestorError:
        return
    if _ARTIFACT_INTEGRITY_REVISION not in downgrade_revisions:
        return

    collision_exists = connection.execute(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM artifacts "
            "GROUP BY organization_id, sha256, kind "
            "HAVING COUNT(*) > 1"
            ")"
        )
    ).scalar_one()
    if collision_exists:
        raise RuntimeError(_LEGACY_IDENTITY_COLLISION_ERROR)


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        has_lock = False
        if connection.dialect.name == "postgresql":
            has_lock = bool(
                connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": POSTGRES_MIGRATION_LOCK_ID},
                )
            )
            if not has_lock:
                raise RuntimeError("Another CorpusKit migration process holds the database lock.")
        try:
            _preflight_downgrade(connection)
            context.run_migrations()
        finally:
            if has_lock:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": POSTGRES_MIGRATION_LOCK_ID},
                )


async def _run_online(database_url: str) -> None:
    engine = create_async_engine(
        database_url,
        echo=False,
        hide_parameters=True,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online(runner: Callable[[Any], Any] = asyncio.run) -> None:
    """Run migrations against an explicit URL without rendering it in logs."""

    configured_url = context.config.attributes.get("database_url")
    database_url = resolve_database_url(configured_url if isinstance(configured_url, str) else None)
    runner(_run_online(database_url))


if context.is_offline_mode():
    raise RuntimeError(
        "Offline SQL generation is disabled; migrations require a verified database."
    )

run_migrations_online()
