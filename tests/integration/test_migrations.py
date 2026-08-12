"""Migration acceptance tests for clean round trips and populated safety."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic import context as alembic_context
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from corpuskit.persistence import migration_cli
from corpuskit.persistence.migration_cli import (
    MigrationConfigurationError,
    build_alembic_config,
    resolve_database_url,
    run,
)
from corpuskit.persistence.models import Base

_POSTGRES_OWNER_URL = os.getenv("CORPUSKIT_TEST_POSTGRES_OWNER_URL")
_DOWNGRADE_COLLISION_ERROR = (
    "Cannot downgrade 0003_artifact_integrity to 0002_durable_job_outbox: "
    "artifact rows cannot be represented by the legacy "
    "(organization_id, sha256, kind) uniqueness constraint. No schema changes were applied; "
    "keep revision 0003 or later and use an approved data-preserving remediation or restore."
)


def _sqlite_urls(path: Path) -> tuple[str, str]:
    posix_path = path.resolve().as_posix()
    return f"sqlite+aiosqlite:///{posix_path}", f"sqlite:///{posix_path}"


@pytest.mark.integration
def test_baseline_upgrades_empty_database_and_has_no_model_drift(tmp_path: Path) -> None:
    async_url, sync_url = _sqlite_urls(tmp_path / "migrations.db")
    config = build_alembic_config(async_url)

    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(sync_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert tables == set(Base.metadata.tables) | {"alembic_version"}


@pytest.mark.integration
def test_baseline_can_downgrade_and_reapply_in_clean_database(tmp_path: Path) -> None:
    async_url, sync_url = _sqlite_urls(tmp_path / "roundtrip.db")
    config = build_alembic_config(async_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(sync_url)
    try:
        assert set(inspect(engine).get_table_names()).isdisjoint(Base.metadata.tables)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.integration
def test_artifact_integrity_downgrade_rejects_unrepresentable_rows_atomically(
    tmp_path: Path,
) -> None:
    async_url, sync_url = _sqlite_urls(tmp_path / "artifact-collision.db")
    config = build_alembic_config(async_url)
    command.upgrade(config, "head")

    organization_id = "00000000-0000-4000-8000-000000000001"
    user_id = "00000000-0000-4000-8000-000000000002"
    first_project_id = "00000000-0000-4000-8000-000000000003"
    second_project_id = "00000000-0000-4000-8000-000000000004"
    digest = "a" * 64
    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) "
                    "VALUES (:id, 'migration-test', 'Migration test')"
                ),
                {"id": organization_id},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, oidc_subject, display_name) "
                    "VALUES (:id, 'migration-test-user', 'Migration test user')"
                ),
                {"id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, created_by, name, description) VALUES "
                    "(:first_id, :organization_id, :user_id, 'First', ''), "
                    "(:second_id, :organization_id, :user_id, 'Second', '')"
                ),
                {
                    "first_id": first_project_id,
                    "second_id": second_project_id,
                    "organization_id": organization_id,
                    "user_id": user_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO artifacts "
                    "(id, organization_id, project_id, run_id, kind, sha256, size_bytes, "
                    "storage_key, media_type, created_by, scope_key, filename, state, "
                    "retention_until) VALUES "
                    "(:first_id, :organization_id, :first_project_id, NULL, 'run-result', "
                    ":digest, 1, 'artifacts/first', 'application/octet-stream', :user_id, "
                    "'project', 'first.bin', 'ACTIVE', '2026-09-10 00:00:00+00:00'), "
                    "(:second_id, :organization_id, :second_project_id, NULL, 'run-result', "
                    ":digest, 1, 'artifacts/second', 'application/octet-stream', :user_id, "
                    "'project', 'second.bin', 'ACTIVE', '2026-09-10 00:00:00+00:00')"
                ),
                {
                    "first_id": "00000000-0000-4000-8000-000000000005",
                    "second_id": "00000000-0000-4000-8000-000000000006",
                    "organization_id": organization_id,
                    "first_project_id": first_project_id,
                    "second_project_id": second_project_id,
                    "digest": digest,
                    "user_id": user_id,
                },
            )

        command.downgrade(config, "0003_artifact_integrity")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0003_artifact_integrity")
            assert connection.execute(text("SELECT COUNT(*) FROM artifacts")).scalar_one() == 2
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO api_rate_limit_windows "
                    "(id, organization_id, subject_sha256, route_sha256, method, "
                    "window_epoch, request_count) VALUES "
                    "('00000000-0000-4000-8000-000000000007', :organization_id, "
                    ":subject_sha256, :route_sha256, 'GET', 1, 1)"
                ),
                {
                    "organization_id": organization_id,
                    "subject_sha256": "b" * 64,
                    "route_sha256": "c" * 64,
                },
            )

        before_inspector = inspect(engine)
        before_columns = [column["name"] for column in before_inspector.get_columns("artifacts")]
        before_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in before_inspector.get_unique_constraints("artifacts")
        }
        before_tables = set(before_inspector.get_table_names())
        with engine.connect() as connection:
            before_triggers = (
                connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
                )
                .scalars()
                .all()
            )

        with pytest.raises(RuntimeError) as raised:
            command.downgrade(config, "base")

        message = str(raised.value)
        assert message == _DOWNGRADE_COLLISION_ERROR
        assert organization_id not in message
        assert digest not in message

        after_inspector = inspect(engine)
        assert set(after_inspector.get_table_names()) == before_tables
        assert [column["name"] for column in after_inspector.get_columns("artifacts")] == (
            before_columns
        )
        assert {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in after_inspector.get_unique_constraints("artifacts")
        } == before_constraints
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0009_api_rate_limits")
            assert (
                connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
                )
                .scalars()
                .all()
                == before_triggers
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM api_rate_limit_windows "
                        "WHERE id = '00000000-0000-4000-8000-000000000007'"
                    )
                ).scalar_one()
                == 1
            )
            rows = connection.execute(
                text("SELECT id, project_id, storage_key FROM artifacts ORDER BY storage_key")
            )
            assert [tuple(row) for row in rows] == [
                (
                    "00000000-0000-4000-8000-000000000005",
                    first_project_id,
                    "artifacts/first",
                ),
                (
                    "00000000-0000-4000-8000-000000000006",
                    second_project_id,
                    "artifacts/second",
                ),
            ]
        command.check(config)
    finally:
        engine.dispose()


async def _postgres_migration_snapshot(
    connection: AsyncConnection,
    *,
    organization_id: UUID,
    marker_id: UUID,
) -> dict[str, Any]:
    return {
        "revision": await connection.scalar(text("SELECT version_num FROM alembic_version")),
        "tables": [
            tuple(row)
            for row in await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            )
        ],
        "policies": [
            tuple(row)
            for row in await connection.execute(
                text(
                    "SELECT tablename, policyname, permissive, roles, cmd, qual, with_check "
                    "FROM pg_policies WHERE schemaname = 'public' "
                    "ORDER BY tablename, policyname"
                )
            )
        ],
        "rls_tables": [
            tuple(row)
            for row in await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace "
                    "WHERE pg_namespace.nspname = 'public' AND pg_class.relkind = 'r' "
                    "ORDER BY relname"
                )
            )
        ],
        "artifact_columns": [
            tuple(row)
            for row in await connection.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'artifacts' "
                    "ORDER BY ordinal_position"
                )
            )
        ],
        "artifact_constraints": [
            tuple(row)
            for row in await connection.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE conrelid = 'artifacts'::regclass "
                    "ORDER BY conname"
                )
            )
        ],
        "collision_rows": [
            tuple(row)
            for row in await connection.execute(
                text(
                    "SELECT id, project_id, storage_key FROM artifacts "
                    "WHERE organization_id = :organization_id ORDER BY storage_key"
                ),
                {"organization_id": organization_id},
            )
        ],
        "head_marker": await connection.scalar(
            text("SELECT COUNT(*) FROM api_rate_limit_windows WHERE id = :marker_id"),
            {"marker_id": marker_id},
        ),
    }


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    _POSTGRES_OWNER_URL is None,
    reason="PostgreSQL migration-owner URL is not configured",
)
async def test_postgres_populated_artifact_downgrade_refusal_is_command_atomic() -> None:
    assert _POSTGRES_OWNER_URL is not None
    organization_id = uuid4()
    user_id = uuid4()
    project_ids = (uuid4(), uuid4())
    artifact_ids = (uuid4(), uuid4())
    marker_id = uuid4()
    digest = hashlib.sha256(f"migration-collision:{organization_id}".encode()).hexdigest()
    engine = create_async_engine(_POSTGRES_OWNER_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) "
                    "VALUES (:id, :slug, 'Migration rollback test')"
                ),
                {"id": organization_id, "slug": f"rollback-{organization_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, oidc_subject, display_name) "
                    "VALUES (:id, :subject, 'Migration rollback test')"
                ),
                {"id": user_id, "subject": f"migration-rollback|{user_id}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, created_by, name, description) VALUES "
                    "(:first_id, :organization_id, :user_id, 'First', ''), "
                    "(:second_id, :organization_id, :user_id, 'Second', '')"
                ),
                {
                    "first_id": project_ids[0],
                    "second_id": project_ids[1],
                    "organization_id": organization_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO artifacts "
                    "(id, organization_id, project_id, run_id, kind, sha256, size_bytes, "
                    "storage_key, media_type, created_by, scope_key, filename, state, "
                    "retention_until) VALUES "
                    "(:first_id, :organization_id, :first_project_id, NULL, 'run-result', "
                    ":digest, 1, :first_key, 'application/octet-stream', :user_id, "
                    "'project', 'first.bin', 'ACTIVE', now() + INTERVAL '30 days'), "
                    "(:second_id, :organization_id, :second_project_id, NULL, 'run-result', "
                    ":digest, 1, :second_key, 'application/octet-stream', :user_id, "
                    "'project', 'second.bin', 'ACTIVE', now() + INTERVAL '30 days')"
                ),
                {
                    "first_id": artifact_ids[0],
                    "second_id": artifact_ids[1],
                    "organization_id": organization_id,
                    "first_project_id": project_ids[0],
                    "second_project_id": project_ids[1],
                    "digest": digest,
                    "first_key": f"migration/{artifact_ids[0]}",
                    "second_key": f"migration/{artifact_ids[1]}",
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO api_rate_limit_windows "
                    "(id, organization_id, subject_sha256, route_sha256, method, "
                    "window_epoch, request_count) VALUES "
                    "(:id, :organization_id, :subject_sha256, :route_sha256, 'GET', 1, 1)"
                ),
                {
                    "id": marker_id,
                    "organization_id": organization_id,
                    "subject_sha256": "b" * 64,
                    "route_sha256": "c" * 64,
                },
            )

        async with engine.connect() as connection:
            before = await _postgres_migration_snapshot(
                connection,
                organization_id=organization_id,
                marker_id=marker_id,
            )
        assert before["revision"] == "0009_api_rate_limits"
        assert ("api_rate_limit_windows",) in before["tables"]
        assert before["policies"]
        assert len(before["collision_rows"]) == 2
        assert before["head_marker"] == 1

        config = build_alembic_config(_POSTGRES_OWNER_URL)
        with pytest.raises(RuntimeError) as raised:
            await asyncio.to_thread(command.downgrade, config, "base")

        message = str(raised.value)
        assert message == _DOWNGRADE_COLLISION_ERROR
        assert str(organization_id) not in message
        assert digest not in message

        async with engine.connect() as connection:
            assert (
                await _postgres_migration_snapshot(
                    connection,
                    organization_id=organization_id,
                    marker_id=marker_id,
                )
                == before
            )
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM organizations WHERE id = :organization_id"),
                    {"organization_id": organization_id},
                )
                await connection.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
        finally:
            await engine.dispose()


def test_migration_environment_groups_revisions_in_one_command_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_url, _ = _sqlite_urls(tmp_path / "configuration.db")
    configured_values: list[bool] = []
    original_configure = alembic_context.configure

    def capture_configuration(*args: Any, **kwargs: Any) -> None:
        configured_values.append(bool(kwargs["transaction_per_migration"]))
        original_configure(*args, **kwargs)

    monkeypatch.setattr(alembic_context, "configure", capture_configuration)
    command.current(build_alembic_config(async_url))

    assert configured_values == [False]


def test_migration_history_has_exactly_one_head() -> None:
    config = build_alembic_config("sqlite+aiosqlite:///:memory:")
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0009_api_rate_limits"]


def test_unique_constraint_names_do_not_collide_in_postgres_schema() -> None:
    names = [
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert None not in names
    assert len(names) == len(set(names))


def test_database_url_is_explicit_and_async(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORPUSKIT_DATABASE_URL", raising=False)
    with pytest.raises(MigrationConfigurationError, match="must be set explicitly"):
        resolve_database_url()
    with pytest.raises(MigrationConfigurationError, match="must use"):
        resolve_database_url("postgresql://user:password@example.invalid/corpuskit")
    with pytest.raises(MigrationConfigurationError, match="invalid"):
        resolve_database_url("not a database url")
    with pytest.raises(MigrationConfigurationError, match="name a database"):
        resolve_database_url("sqlite+aiosqlite://")


@pytest.mark.parametrize("operation", ["upgrade", "current", "check"])
def test_cli_dispatches_only_allowlisted_operations(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("CORPUSKIT_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        migration_cli.command,
        "upgrade",
        lambda config, revision: calls.append(f"upgrade:{revision}"),
    )
    monkeypatch.setattr(
        migration_cli.command,
        "current",
        lambda config, *, verbose: calls.append(f"current:{verbose}"),
    )
    monkeypatch.setattr(
        migration_cli.command,
        "check",
        lambda config: calls.append("check"),
    )

    assert run([operation]) == 0
    assert (
        calls
        == {
            "upgrade": ["upgrade:head"],
            "current": ["current:False"],
            "check": ["check"],
        }[operation]
    )


def test_cli_reports_invalid_configuration_without_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CORPUSKIT_DATABASE_URL", raising=False)

    assert run(["check"]) == 2
    assert "database details were not displayed" in capsys.readouterr().err


def test_main_returns_cli_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration_cli, "run", lambda: 7)

    with pytest.raises(SystemExit) as raised:
        migration_cli.main()

    assert raised.value.code == 7


def test_cli_redacts_database_secret_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "migration-secret-must-not-leak"
    monkeypatch.setenv(
        "CORPUSKIT_DATABASE_URL",
        f"postgresql+asyncpg://corpuskit:{secret}@127.0.0.1:1/corpuskit",
    )

    assert run(["current"]) == 1
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err
    assert "sensitive details were redacted" in output.err
