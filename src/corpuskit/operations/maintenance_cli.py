"""One-shot maintenance command for a singleton scheduler or CronJob."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text

from corpuskit.config import RuntimeRole, Settings, get_settings
from corpuskit.persistence.artifact_store import build_object_store
from corpuskit.persistence.database import Database
from corpuskit.services.artifact_adoption import ArtifactAdoptionService
from corpuskit.services.artifacts import ArtifactService
from corpuskit.services.maintenance import DatabaseMaintenanceState, MaintenanceRunner
from corpuskit.services.platform import QuotaManager
from corpuskit.services.project_deletion import ProjectDeletionMaintenance
from corpuskit.services.rate_limits import DatabaseRateLimiter
from corpuskit.workflows.store import DurableRunStore

_ADVISORY_LOCK_KEY: Final = 0x434F525055534B54


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpuskit-maintenance",
        description="Run one bounded, idempotent CorpusKit maintenance batch.",
    )
    parser.add_argument("operation", choices=("run-once",))
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum rows or objects per operation (1-1000).",
    )
    parser.add_argument(
        "--max-reconciliation-pages",
        type=int,
        default=10,
        help="Maximum final-artifact reconciliation pages to scan (1-20).",
    )
    parser.add_argument(
        "--max-staging-pages",
        type=int,
        default=10,
        help="Maximum staging pages to scan in this invocation (1-20).",
    )
    return parser


async def execute(
    *,
    limit: int,
    max_reconciliation_pages: int,
    max_staging_pages: int,
) -> tuple[bool, dict[str, object]]:
    """Build production services, acquire the singleton lock, and execute one batch."""

    settings = get_settings()
    if settings.runtime_role is not RuntimeRole.MAINTENANCE:
        raise RuntimeError("corpuskit-maintenance requires CORPUSKIT_RUNTIME_ROLE=maintenance")
    database = Database(settings.database_url)
    try:
        store = build_object_store(settings)
        rate_limits = DatabaseRateLimiter(
            database,
            window_seconds=settings.api_rate_limit_window_seconds,
            read_requests=settings.api_rate_limit_read_requests,
            write_requests=settings.api_rate_limit_write_requests,
            retention_windows=settings.api_rate_limit_retention_windows,
        )
        runner = MaintenanceRunner(
            database,
            QuotaManager(),
            ArtifactService(database, store, settings),
            ArtifactAdoptionService(DurableRunStore(database), store, settings),
            ProjectDeletionMaintenance(database, store),
            DatabaseMaintenanceState(database, _backend_fingerprint(settings)),
            rate_limits=rate_limits,
        )
        async with maintenance_lock(database) as acquired:
            if not acquired:
                return False, {
                    "schema_id": "corpuskit.maintenance-report.v1",
                    "status": "already_running",
                }
            report = await runner.run_once(
                limit=limit,
                max_reconciliation_pages=max_reconciliation_pages,
                max_staging_pages=max_staging_pages,
            )
            return True, report.model_dump(mode="json")
    finally:
        await database.dispose()


@asynccontextmanager
async def maintenance_lock(database: Database) -> AsyncIterator[bool]:
    """Hold a PostgreSQL advisory lock; local SQLite batches remain single-process."""

    if database.engine.dialect.name != "postgresql":
        yield True
        return
    async with database.engine.connect() as connection:
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": _ADVISORY_LOCK_KEY},
            )
        )
        # The session-level advisory lock survives commit; avoid holding an idle
        # transaction open for the duration of object-store maintenance.
        await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _ADVISORY_LOCK_KEY},
                )
                await connection.commit()


def _backend_fingerprint(settings: Settings) -> str:
    """Scope private scan progress to one non-secret object-store namespace."""

    if settings.artifact_backend == "s3":
        namespace = (
            f"s3\0{settings.artifact_s3_endpoint or ''}\0"
            f"{settings.artifact_s3_region}\0{settings.artifact_s3_bucket}"
        )
    else:
        namespace = f"filesystem\0{settings.artifact_root.resolve()}"
    return hashlib.sha256(namespace.encode("utf-8", errors="strict")).hexdigest()


def run(argv: Sequence[str] | None = None) -> int:
    """Parse a bounded invocation and return a stable process exit code."""

    args = _parser().parse_args(argv)
    try:
        _, payload = asyncio.run(
            execute(
                limit=args.limit,
                max_reconciliation_pages=args.max_reconciliation_pages,
                max_staging_pages=args.max_staging_pages,
            )
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        sys.stderr.write("Maintenance failed; sensitive details were redacted.\n")
        return 1
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 2 if _degraded(payload) else 0


def _degraded(payload: dict[str, object]) -> bool:
    """Flag object integrity or cleanup failures while preserving the JSON evidence."""

    paths = (
        ("artifact_purge", "failed"),
        ("artifact_reconciliation", "delete_failures"),
        ("artifact_reconciliation", "missing"),
        ("artifact_reconciliation", "corrupt"),
        ("staging_cleanup", "failed"),
        ("project_purge", "failed"),
    )
    for section, field in paths:
        candidate = payload.get(section)
        value = candidate.get(field) if isinstance(candidate, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    return False


def main() -> None:
    raise SystemExit(run())


__all__ = ["execute", "main", "maintenance_lock", "run"]
