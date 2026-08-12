"""Bounded maintenance orchestration and operator CLI contracts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from corpuskit.config import RuntimeRole, Settings
from corpuskit.operations import maintenance_cli
from corpuskit.persistence.database import Database
from corpuskit.services.artifact_adoption import StagingCleanupReport
from corpuskit.services.artifacts import PurgeReport, ReconciliationReport
from corpuskit.services.maintenance import (
    DatabaseMaintenanceState,
    MaintenanceOperation,
    MaintenanceReport,
    MaintenanceRunner,
    MaintenanceStateConflictError,
)
from corpuskit.services.project_deletion import ProjectPurgeReport

_RECONCILIATION_CURSOR = (
    "artifacts/v1/" + "1" * 32 + "/" + "2" * 32 + "/project/run-result/bb/" + "b" * 64
)
_STAGING_CURSOR_A = "staging/v1/sha256/aa/" + "a" * 64
_STAGING_CURSOR_B = "staging/v1/sha256/bb/" + "b" * 64


class FakeQuota:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime | None, int]] = []

    async def expire_stale(
        self,
        database: Database,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> int:
        del database
        self.calls.append((now, limit))
        return 2


class FakeArtifacts:
    def __init__(self) -> None:
        self.purge_calls: list[tuple[datetime | None, int]] = []
        self.reconcile_calls: list[tuple[str | None, datetime | None, int]] = []

    async def purge_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> PurgeReport:
        self.purge_calls.append((now, limit))
        return PurgeReport(eligible=4, deleted=3, failed=1)

    async def reconcile_orphans(
        self,
        *,
        cursor: str | None = None,
        limit: int = 1_000,
        now: datetime | None = None,
    ) -> ReconciliationReport:
        self.reconcile_calls.append((cursor, now, limit))
        return ReconciliationReport(
            scanned=8,
            orphaned=2,
            deleted=1,
            delete_failures=1,
            missing=1,
            corrupt=1,
            next_cursor=_RECONCILIATION_CURSOR if len(self.reconcile_calls) == 1 else None,
        )


class FakeStaging:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, int, datetime | None]] = []

    async def cleanup_staging(
        self,
        *,
        cursor: str | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> StagingCleanupReport:
        self.calls.append((cursor, limit, now))
        return StagingCleanupReport(
            scanned=5,
            deleted=3,
            deferred=1,
            failed=1,
            next_cursor=_STAGING_CURSOR_B if len(self.calls) == 1 else None,
        )


class FakeProjects:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime | None, int]] = []

    async def purge_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> ProjectPurgeReport:
        self.calls.append((now, limit))
        return ProjectPurgeReport(eligible=3, deleted=1, deferred=1, failed=1)


class FakeRateLimits:
    def __init__(self) -> None:
        self.limits: list[int] = []

    async def purge_expired(self, *, limit: int = 1_000) -> int:
        self.limits.append(limit)
        return 7


class FakeState:
    def __init__(
        self,
        cursors: dict[MaintenanceOperation, str | None] | None = None,
    ) -> None:
        self.cursors = dict(cursors or {})
        self.advances: list[tuple[MaintenanceOperation, str | None, str | None]] = []

    async def load(self, operation: MaintenanceOperation) -> str | None:
        return self.cursors.get(operation)

    async def advance(
        self,
        operation: MaintenanceOperation,
        *,
        expected: str | None,
        next_cursor: str | None,
    ) -> None:
        if self.cursors.get(operation) != expected:
            raise MaintenanceStateConflictError("maintenance cursor changed concurrently")
        self.advances.append((operation, expected, next_cursor))
        self.cursors[operation] = next_cursor


@pytest.mark.asyncio
async def test_runner_uses_one_cutoff_and_returns_counts_only() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    quota = FakeQuota()
    artifacts = FakeArtifacts()
    staging = FakeStaging()
    projects = FakeProjects()
    started = datetime(2026, 8, 11, 20, 0, tzinfo=UTC)
    completed = datetime(2026, 8, 11, 20, 0, 1, tzinfo=UTC)
    times = iter((started, completed))
    state = FakeState({MaintenanceOperation.STAGING_CLEANUP: _STAGING_CURSOR_A})
    rate_limits = FakeRateLimits()
    runner = MaintenanceRunner(
        database,
        quota,
        artifacts,
        staging,
        projects,
        state,
        rate_limits=rate_limits,
        clock=lambda: next(times),
    )

    try:
        report = await runner.run_once(
            limit=25,
            max_reconciliation_pages=1,
            max_staging_pages=1,
        )
    finally:
        await database.dispose()

    assert report.schema_id == "corpuskit.maintenance-report.v1"
    assert report.started_at == started
    assert report.completed_at == completed
    assert report.quota_reservations_expired == 2
    assert report.rate_limit_windows_deleted == 7
    assert report.artifact_purge.model_dump() == {"eligible": 4, "deleted": 3, "failed": 1}
    assert report.artifact_reconciliation.missing == 1
    assert report.artifact_reconciliation.pages == 1
    assert report.artifact_reconciliation.more_available is True
    assert report.staging_cleanup.more_available is True
    assert report.staging_cleanup.pages == 1
    assert report.project_purge.model_dump() == {
        "eligible": 3,
        "deleted": 1,
        "deferred": 1,
        "failed": 1,
    }
    assert quota.calls == [(started, 25)]
    assert artifacts.purge_calls == [(started, 25)]
    assert artifacts.reconcile_calls == [(None, started, 25)]
    assert staging.calls == [(_STAGING_CURSOR_A, 25, started)]
    assert projects.calls == [(started, 25)]
    assert rate_limits.limits == [25]
    assert state.cursors == {
        MaintenanceOperation.ARTIFACT_RECONCILIATION: _RECONCILIATION_CURSOR,
        MaintenanceOperation.STAGING_CLEANUP: _STAGING_CURSOR_B,
    }
    serialized = report.model_dump_json()
    assert "organization" not in serialized
    assert "storage_key" not in serialized


@pytest.mark.asyncio
async def test_runner_aggregates_reconciliation_and_staging_pages() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    staging = FakeStaging()
    state = FakeState()
    started = datetime(2026, 8, 11, 20, 0, tzinfo=UTC)
    runner = MaintenanceRunner(
        database,
        FakeQuota(),
        FakeArtifacts(),
        staging,
        FakeProjects(),
        state,
        clock=lambda: started,
    )

    try:
        report = await runner.run_once(limit=5, max_staging_pages=10)
    finally:
        await database.dispose()

    assert report.staging_cleanup.pages == 2
    assert report.staging_cleanup.scanned == 10
    assert report.staging_cleanup.deleted == 6
    assert report.staging_cleanup.deferred == 2
    assert report.staging_cleanup.failed == 2
    assert report.staging_cleanup.more_available is False
    assert staging.calls[1][0] == _STAGING_CURSOR_B
    assert report.artifact_reconciliation.pages == 2
    assert report.artifact_reconciliation.scanned == 16
    assert report.artifact_reconciliation.deleted == 2
    assert report.artifact_reconciliation.more_available is False
    assert state.cursors == {
        MaintenanceOperation.ARTIFACT_RECONCILIATION: None,
        MaintenanceOperation.STAGING_CLEANUP: None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1_001])
async def test_runner_rejects_invalid_limit_before_work(limit: int) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    quota = FakeQuota()
    runner = MaintenanceRunner(
        database, quota, FakeArtifacts(), FakeStaging(), FakeProjects(), FakeState()
    )

    try:
        with pytest.raises(ValueError, match="between 1 and 1000"):
            await runner.run_once(limit=limit)
    finally:
        await database.dispose()
    assert quota.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pages", [0, 21])
async def test_runner_rejects_invalid_staging_page_budget_before_work(pages: int) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    quota = FakeQuota()
    runner = MaintenanceRunner(
        database, quota, FakeArtifacts(), FakeStaging(), FakeProjects(), FakeState()
    )

    try:
        with pytest.raises(ValueError, match="page limit"):
            await runner.run_once(max_staging_pages=pages)
    finally:
        await database.dispose()
    assert quota.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pages", [0, 21])
async def test_runner_rejects_invalid_reconciliation_page_budget_before_work(
    pages: int,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    quota = FakeQuota()
    runner = MaintenanceRunner(
        database, quota, FakeArtifacts(), FakeStaging(), FakeProjects(), FakeState()
    )

    try:
        with pytest.raises(ValueError, match="reconciliation page limit"):
            await runner.run_once(max_reconciliation_pages=pages)
    finally:
        await database.dispose()
    assert quota.calls == []


@pytest.mark.asyncio
async def test_runner_rejects_untrusted_cursor_and_naive_clock() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    quota = FakeQuota()
    try:
        invalid_cursor = MaintenanceRunner(
            database,
            quota,
            FakeArtifacts(),
            FakeStaging(),
            FakeProjects(),
            FakeState({MaintenanceOperation.STAGING_CLEANUP: "../tenant/private"}),
        )
        with pytest.raises(ValueError, match="cursor"):
            await invalid_cursor.run_once()

        naive_clock = MaintenanceRunner(
            database,
            quota,
            FakeArtifacts(),
            FakeStaging(),
            FakeProjects(),
            FakeState(),
            clock=lambda: datetime(2026, 8, 11, tzinfo=UTC).replace(tzinfo=None),
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            await naive_clock.run_once()
    finally:
        await database.dispose()
    assert quota.calls == []


@pytest.mark.asyncio
async def test_runner_rejects_nonadvancing_service_cursor() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    state = FakeState({MaintenanceOperation.ARTIFACT_RECONCILIATION: _RECONCILIATION_CURSOR})
    runner = MaintenanceRunner(
        database,
        FakeQuota(),
        FakeArtifacts(),
        FakeStaging(),
        FakeProjects(),
        state,
    )
    try:
        with pytest.raises(ValueError, match="did not advance"):
            await runner.run_once(max_reconciliation_pages=1)
    finally:
        await database.dispose()
    assert state.advances == []


@pytest.mark.asyncio
async def test_database_state_cas_persists_and_resets_private_cursor() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    state = DatabaseMaintenanceState(database, "a" * 64)
    try:
        assert await state.load(MaintenanceOperation.STAGING_CLEANUP) is None
        await state.advance(
            MaintenanceOperation.STAGING_CLEANUP,
            expected=None,
            next_cursor=_STAGING_CURSOR_A,
        )
        assert await state.load(MaintenanceOperation.STAGING_CLEANUP) == _STAGING_CURSOR_A
        with pytest.raises(MaintenanceStateConflictError, match="concurrently"):
            await state.advance(
                MaintenanceOperation.STAGING_CLEANUP,
                expected=None,
                next_cursor=_STAGING_CURSOR_B,
            )
        await state.advance(
            MaintenanceOperation.STAGING_CLEANUP,
            expected=_STAGING_CURSOR_A,
            next_cursor=None,
        )
        assert await state.load(MaintenanceOperation.STAGING_CLEANUP) is None
    finally:
        await database.dispose()


def test_database_state_rejects_invalid_backend_fingerprint() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        DatabaseMaintenanceState(cast(Database, object()), "not-a-fingerprint")


def test_backend_fingerprint_is_stable_scoped_and_nonrevealing(tmp_path: Path) -> None:
    filesystem = cast(
        Settings,
        SimpleNamespace(
            artifact_backend="filesystem",
            artifact_root=tmp_path,
            artifact_s3_endpoint=None,
            artifact_s3_region="us-east-1",
            artifact_s3_bucket="unused",
        ),
    )
    first = maintenance_cli._backend_fingerprint(filesystem)
    second = maintenance_cli._backend_fingerprint(filesystem)
    s3 = maintenance_cli._backend_fingerprint(
        cast(
            Settings,
            SimpleNamespace(
                artifact_backend="s3",
                artifact_root=tmp_path,
                artifact_s3_endpoint="https://objects.example.test",
                artifact_s3_region="us-east-1",
                artifact_s3_bucket="corpuskit-a",
            ),
        )
    )
    assert first == second
    assert first != s3
    assert len(first) == 64
    assert str(tmp_path) not in first


@pytest.mark.asyncio
async def test_sqlite_lock_is_immediate() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    try:
        async with maintenance_cli.maintenance_lock(database) as acquired:
            assert acquired is True
    finally:
        await database.dispose()


class FakeConnection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.scalar_calls = 0
        self.execute_calls = 0
        self.commit_calls = 0

    async def scalar(self, statement: object, parameters: object) -> bool:
        del statement, parameters
        self.scalar_calls += 1
        return self.acquired

    async def execute(self, statement: object, parameters: object) -> None:
        del statement, parameters
        self.execute_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self._connection = connection

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeConnection]:
        yield self._connection


@pytest.mark.asyncio
@pytest.mark.parametrize("acquired", [True, False])
async def test_postgres_lock_releases_only_when_acquired(acquired: bool) -> None:
    connection = FakeConnection(acquired)
    database = SimpleNamespace(engine=FakeEngine(connection))

    async with maintenance_cli.maintenance_lock(cast(Database, database)) as result:
        assert result is acquired

    assert connection.scalar_calls == 1
    assert connection.execute_calls == int(acquired)
    assert connection.commit_calls == 1 + int(acquired)


@pytest.mark.asyncio
async def test_execute_builds_services_and_disposes_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed: list[bool] = []

    class FakeDatabase:
        def __init__(self, url: str) -> None:
            assert url == "sqlite+aiosqlite:///maintenance.db"
            self.engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def dispose(self) -> None:
            disposed.append(True)

    report = MaintenanceReport.model_validate(
        {
            "started_at": "2026-08-11T20:00:00Z",
            "completed_at": "2026-08-11T20:00:01Z",
            "quota_reservations_expired": 0,
            "artifact_purge": {"eligible": 0, "deleted": 0, "failed": 0},
            "artifact_reconciliation": {
                "scanned": 0,
                "pages": 1,
                "orphaned": 0,
                "deleted": 0,
                "delete_failures": 0,
                "missing": 0,
                "corrupt": 0,
                "more_available": False,
            },
            "staging_cleanup": {
                "scanned": 0,
                "pages": 1,
                "deleted": 0,
                "deferred": 0,
                "failed": 0,
                "more_available": False,
            },
            "project_purge": {
                "eligible": 0,
                "deleted": 0,
                "deferred": 0,
                "failed": 0,
            },
        }
    )

    class FakeRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            assert len(args) == 6
            assert set(kwargs) == {"rate_limits"}

        async def run_once(
            self,
            *,
            limit: int,
            max_reconciliation_pages: int,
            max_staging_pages: int,
        ) -> MaintenanceReport:
            assert (
                limit,
                max_reconciliation_pages,
                max_staging_pages,
            ) == (7, 4, 3)
            return report

    monkeypatch.setattr(
        maintenance_cli,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="sqlite+aiosqlite:///maintenance.db",
            runtime_role=RuntimeRole.MAINTENANCE,
            api_rate_limit_window_seconds=60,
            api_rate_limit_read_requests=600,
            api_rate_limit_write_requests=120,
            api_rate_limit_retention_windows=3,
        ),
    )
    monkeypatch.setattr(maintenance_cli, "Database", FakeDatabase)
    monkeypatch.setattr(maintenance_cli, "build_object_store", lambda _: object())
    monkeypatch.setattr(maintenance_cli, "QuotaManager", lambda: object())
    monkeypatch.setattr(maintenance_cli, "ArtifactService", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "ArtifactAdoptionService", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "ProjectDeletionMaintenance", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "DurableRunStore", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "DatabaseMaintenanceState", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "DatabaseRateLimiter", lambda *args, **kwargs: object())
    monkeypatch.setattr(maintenance_cli, "_backend_fingerprint", lambda _: "a" * 64)
    monkeypatch.setattr(maintenance_cli, "MaintenanceRunner", FakeRunner)

    acquired, payload = await maintenance_cli.execute(
        limit=7,
        max_reconciliation_pages=4,
        max_staging_pages=3,
    )

    assert acquired is True
    assert payload["schema_id"] == "corpuskit.maintenance-report.v1"
    assert disposed == [True]


@pytest.mark.asyncio
async def test_execute_skips_safely_when_another_batch_holds_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed: list[bool] = []

    class FakeDatabase:
        def __init__(self, url: str) -> None:
            del url
            self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def dispose(self) -> None:
            disposed.append(True)

    @asynccontextmanager
    async def denied_lock(database: object) -> AsyncIterator[bool]:
        del database
        yield False

    monkeypatch.setattr(
        maintenance_cli,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://redacted",
            runtime_role=RuntimeRole.MAINTENANCE,
            api_rate_limit_window_seconds=60,
            api_rate_limit_read_requests=600,
            api_rate_limit_write_requests=120,
            api_rate_limit_retention_windows=3,
        ),
    )
    monkeypatch.setattr(maintenance_cli, "Database", FakeDatabase)
    monkeypatch.setattr(maintenance_cli, "build_object_store", lambda _: object())
    monkeypatch.setattr(maintenance_cli, "QuotaManager", lambda: object())
    monkeypatch.setattr(maintenance_cli, "ArtifactService", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "ArtifactAdoptionService", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "ProjectDeletionMaintenance", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "DurableRunStore", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "DatabaseMaintenanceState", lambda *args: object())
    monkeypatch.setattr(maintenance_cli, "DatabaseRateLimiter", lambda *args, **kwargs: object())
    monkeypatch.setattr(maintenance_cli, "_backend_fingerprint", lambda _: "a" * 64)
    monkeypatch.setattr(
        maintenance_cli,
        "MaintenanceRunner",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(maintenance_cli, "maintenance_lock", denied_lock)

    acquired, payload = await maintenance_cli.execute(
        limit=7,
        max_reconciliation_pages=4,
        max_staging_pages=3,
    )

    assert acquired is False
    assert payload == {
        "schema_id": "corpuskit.maintenance-report.v1",
        "status": "already_running",
    }
    assert disposed == [True]


def test_cli_emits_compact_json_for_success_and_already_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_execute(
        *,
        limit: int,
        max_reconciliation_pages: int,
        max_staging_pages: int,
    ) -> tuple[bool, dict[str, object]]:
        assert (
            limit,
            max_reconciliation_pages,
            max_staging_pages,
        ) == (9, 10, 10)
        return False, {
            "schema_id": "corpuskit.maintenance-report.v1",
            "status": "already_running",
        }

    monkeypatch.setattr(maintenance_cli, "execute", fake_execute)

    assert maintenance_cli.run(["run-once", "--limit", "9"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "schema_id": "corpuskit.maintenance-report.v1",
        "status": "already_running",
    }
    assert output.err == ""


def test_cli_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(
        *,
        limit: int,
        max_reconciliation_pages: int,
        max_staging_pages: int,
    ) -> tuple[bool, dict[str, object]]:
        del limit, max_reconciliation_pages, max_staging_pages
        raise RuntimeError("database-password-canary")

    monkeypatch.setattr(maintenance_cli, "execute", fail)

    assert maintenance_cli.run(["run-once"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "redacted" in output.err
    assert "database-password-canary" not in output.err


def test_cli_returns_degraded_status_for_integrity_or_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def degraded(
        *,
        limit: int,
        max_reconciliation_pages: int,
        max_staging_pages: int,
    ) -> tuple[bool, dict[str, object]]:
        del limit, max_reconciliation_pages, max_staging_pages
        return True, {
            "artifact_purge": {"failed": 0},
            "artifact_reconciliation": {
                "delete_failures": 0,
                "missing": 1,
                "corrupt": 0,
            },
            "staging_cleanup": {"failed": 0},
            "project_purge": {"failed": 0},
        }

    monkeypatch.setattr(maintenance_cli, "execute", degraded)

    assert maintenance_cli.run(["run-once"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["artifact_reconciliation"]["missing"] == 1
    assert output.err == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"artifact_purge": {"failed": True}},
        {"artifact_reconciliation": "invalid"},
        {"staging_cleanup": {"failed": 0}},
    ],
)
def test_degraded_classifier_ignores_nonpositive_or_malformed_counts(
    payload: dict[str, object],
) -> None:
    assert maintenance_cli._degraded(payload) is False


def test_cli_does_not_swallow_operator_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(
        *,
        limit: int,
        max_reconciliation_pages: int,
        max_staging_pages: int,
    ) -> tuple[bool, dict[str, object]]:
        del limit, max_reconciliation_pages, max_staging_pages
        raise KeyboardInterrupt

    monkeypatch.setattr(maintenance_cli, "execute", interrupted)

    with pytest.raises(KeyboardInterrupt):
        maintenance_cli.run(["run-once"])


def test_main_exits_with_run_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance_cli, "run", lambda: 7)

    with pytest.raises(SystemExit) as raised:
        maintenance_cli.main()

    assert raised.value.code == 7


@pytest.mark.asyncio
async def test_execute_rejects_wrong_runtime_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        maintenance_cli,
        "get_settings",
        lambda: Settings(environment="test", runtime_role="worker", _env_file=None),
    )

    with pytest.raises(RuntimeError, match="RUNTIME_ROLE=maintenance"):
        await maintenance_cli.execute(
            limit=1,
            max_reconciliation_pages=1,
            max_staging_pages=1,
        )
