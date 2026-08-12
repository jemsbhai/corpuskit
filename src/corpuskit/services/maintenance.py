"""Bounded orchestration for idempotent maintenance primitives."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from corpuskit.persistence.database import Database
from corpuskit.persistence.models import MaintenanceCursor
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.artifact_adoption import StagingCleanupReport
from corpuskit.services.artifacts import PurgeReport, ReconciliationReport
from corpuskit.services.project_deletion import ProjectPurgeReport

_ARTIFACT_CURSOR = re.compile(
    r"artifacts/v1/[0-9a-f]{32}/[0-9a-f]{32}/(?:project|[0-9a-f]{32})/"
    r"(?:run-manifest|corpus-text|evaluation-report|export|checkpoint|model-adapter|run-result)/"
    r"[0-9a-f]{2}/[0-9a-f]{64}",
    flags=re.ASCII,
)
_STAGING_CURSOR = re.compile(
    r"staging/v1/sha256/[0-9a-f]{2}/[0-9a-f]{64}",
    flags=re.ASCII,
)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)


class MaintenanceOperation(StrEnum):
    ARTIFACT_RECONCILIATION = "artifact-reconciliation"
    STAGING_CLEANUP = "staging-cleanup"


class MaintenanceStateConflictError(RuntimeError):
    """A second scheduler changed maintenance progress unexpectedly."""


class MaintenanceState(Protocol):
    async def load(self, operation: MaintenanceOperation) -> str | None: ...

    async def advance(
        self,
        operation: MaintenanceOperation,
        *,
        expected: str | None,
        next_cursor: str | None,
    ) -> None: ...


class DatabaseMaintenanceState:
    """Persist private scan progress under the maintenance-only database identity."""

    def __init__(self, database: Database, backend_fingerprint: str) -> None:
        if _FINGERPRINT.fullmatch(backend_fingerprint) is None:
            raise ValueError("maintenance backend fingerprint is invalid")
        self._database = database
        self._backend_fingerprint = backend_fingerprint

    async def load(self, operation: MaintenanceOperation) -> str | None:
        async with self._database.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            row = await session.scalar(
                select(MaintenanceCursor).where(
                    MaintenanceCursor.operation == operation.value,
                    MaintenanceCursor.backend_fingerprint == self._backend_fingerprint,
                )
            )
            return row.cursor if row is not None else None

    async def advance(
        self,
        operation: MaintenanceOperation,
        *,
        expected: str | None,
        next_cursor: str | None,
    ) -> None:
        async with self._database.session(
            TenantContext.service(ServiceIdentity.MAINTENANCE)
        ) as session:
            row = await session.scalar(
                select(MaintenanceCursor)
                .where(
                    MaintenanceCursor.operation == operation.value,
                    MaintenanceCursor.backend_fingerprint == self._backend_fingerprint,
                )
                .with_for_update()
            )
            current = row.cursor if row is not None else None
            if current != expected:
                raise MaintenanceStateConflictError("maintenance cursor changed concurrently")
            if row is None:
                session.add(
                    MaintenanceCursor(
                        operation=operation.value,
                        backend_fingerprint=self._backend_fingerprint,
                        cursor=next_cursor,
                    )
                )
            else:
                row.cursor = next_cursor


class QuotaExpiry(Protocol):
    """Narrow static-service contract used by the maintenance runner."""

    async def expire_stale(
        self,
        database: Database,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> int: ...


class ArtifactMaintenance(Protocol):
    async def purge_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> PurgeReport: ...

    async def reconcile_orphans(
        self,
        *,
        cursor: str | None = None,
        limit: int = 1_000,
        now: datetime | None = None,
    ) -> ReconciliationReport: ...


class StagingMaintenance(Protocol):
    async def cleanup_staging(
        self,
        *,
        cursor: str | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> StagingCleanupReport: ...


class ProjectMaintenance(Protocol):
    async def purge_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> ProjectPurgeReport: ...


class RateLimitMaintenance(Protocol):
    async def purge_expired(self, *, limit: int = 1_000) -> int: ...


class ArtifactPurgeSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: int = Field(ge=0)
    deleted: int = Field(ge=0)
    failed: int = Field(ge=0)


class ArtifactReconciliationSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    scanned: int = Field(ge=0)
    pages: int = Field(ge=1, le=20)
    orphaned: int = Field(ge=0)
    deleted: int = Field(ge=0)
    delete_failures: int = Field(ge=0)
    missing: int = Field(ge=0)
    corrupt: int = Field(ge=0)
    more_available: bool


class StagingCleanupSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    scanned: int = Field(ge=0)
    pages: int = Field(ge=1, le=20)
    deleted: int = Field(ge=0)
    deferred: int = Field(ge=0)
    failed: int = Field(ge=0)
    more_available: bool


class ProjectPurgeSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: int = Field(ge=0)
    deleted: int = Field(ge=0)
    deferred: int = Field(ge=0)
    failed: int = Field(ge=0)


class MaintenanceReport(BaseModel):
    """Public operator report containing counts only, never object or tenant identifiers."""

    model_config = ConfigDict(frozen=True)

    schema_id: str = "corpuskit.maintenance-report.v1"
    started_at: datetime
    completed_at: datetime
    quota_reservations_expired: int = Field(ge=0)
    rate_limit_windows_deleted: int = Field(default=0, ge=0)
    artifact_purge: ArtifactPurgeSummary
    artifact_reconciliation: ArtifactReconciliationSummary
    staging_cleanup: StagingCleanupSummary
    project_purge: ProjectPurgeSummary


class MaintenanceRunner:
    """Run independent idempotent cleanup operations under one bounded cutoff."""

    def __init__(
        self,
        database: Database,
        quota: QuotaExpiry,
        artifacts: ArtifactMaintenance,
        staging: StagingMaintenance,
        projects: ProjectMaintenance,
        state: MaintenanceState,
        *,
        rate_limits: RateLimitMaintenance | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._quota = quota
        self._artifacts = artifacts
        self._staging = staging
        self._projects = projects
        self._state = state
        self._rate_limits = rate_limits
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(
        self,
        *,
        limit: int = 500,
        max_reconciliation_pages: int = 10,
        max_staging_pages: int = 10,
    ) -> MaintenanceReport:
        if not 1 <= limit <= 1_000:
            raise ValueError("maintenance limit must be between 1 and 1000")
        if not 1 <= max_staging_pages <= 20:
            raise ValueError("staging page limit must be between 1 and 20")
        if not 1 <= max_reconciliation_pages <= 20:
            raise ValueError("reconciliation page limit must be between 1 and 20")
        reconciliation_cursor = await self._state.load(MaintenanceOperation.ARTIFACT_RECONCILIATION)
        staging_cursor = await self._state.load(MaintenanceOperation.STAGING_CLEANUP)
        _validate_cursor(reconciliation_cursor, _ARTIFACT_CURSOR, "reconciliation")
        _validate_cursor(staging_cursor, _STAGING_CURSOR, "staging")
        started_at = _utc(self._clock())
        expired = await self._quota.expire_stale(
            self._database,
            now=started_at,
            limit=limit,
        )
        purge = await self._artifacts.purge_due(now=started_at, limit=limit)
        reconciliation_scanned = 0
        reconciliation_orphaned = 0
        reconciliation_deleted = 0
        reconciliation_delete_failures = 0
        reconciliation_missing = 0
        reconciliation_corrupt = 0
        reconciliation_pages = 0
        for _ in range(max_reconciliation_pages):
            reconciliation = await self._artifacts.reconcile_orphans(
                cursor=reconciliation_cursor,
                limit=limit,
                now=started_at,
            )
            reconciliation_pages += 1
            reconciliation_scanned += reconciliation.scanned
            reconciliation_orphaned += reconciliation.orphaned
            reconciliation_deleted += reconciliation.deleted
            reconciliation_delete_failures += reconciliation.delete_failures
            reconciliation_missing += reconciliation.missing
            reconciliation_corrupt += reconciliation.corrupt
            next_cursor = reconciliation.next_cursor
            _validate_progress(
                reconciliation_cursor,
                next_cursor,
                _ARTIFACT_CURSOR,
                "reconciliation",
            )
            await self._state.advance(
                MaintenanceOperation.ARTIFACT_RECONCILIATION,
                expected=reconciliation_cursor,
                next_cursor=next_cursor,
            )
            reconciliation_cursor = next_cursor
            if reconciliation_cursor is None:
                break
        cursor = staging_cursor
        staging_scanned = staging_deleted = staging_deferred = staging_failed = 0
        pages = 0
        for _ in range(max_staging_pages):
            staging = await self._staging.cleanup_staging(
                cursor=cursor,
                limit=limit,
                now=started_at,
            )
            pages += 1
            staging_scanned += staging.scanned
            staging_deleted += staging.deleted
            staging_deferred += staging.deferred
            staging_failed += staging.failed
            next_cursor = staging.next_cursor
            _validate_progress(cursor, next_cursor, _STAGING_CURSOR, "staging")
            await self._state.advance(
                MaintenanceOperation.STAGING_CLEANUP,
                expected=cursor,
                next_cursor=next_cursor,
            )
            cursor = next_cursor
            if cursor is None:
                break
        project_purge = await self._projects.purge_due(now=started_at, limit=limit)
        rate_limit_windows_deleted = (
            await self._rate_limits.purge_expired(limit=limit)
            if self._rate_limits is not None
            else 0
        )
        return MaintenanceReport(
            started_at=started_at,
            completed_at=_utc(self._clock()),
            quota_reservations_expired=expired,
            rate_limit_windows_deleted=rate_limit_windows_deleted,
            artifact_purge=ArtifactPurgeSummary(
                eligible=purge.eligible,
                deleted=purge.deleted,
                failed=purge.failed,
            ),
            artifact_reconciliation=ArtifactReconciliationSummary(
                scanned=reconciliation_scanned,
                pages=reconciliation_pages,
                orphaned=reconciliation_orphaned,
                deleted=reconciliation_deleted,
                delete_failures=reconciliation_delete_failures,
                missing=reconciliation_missing,
                corrupt=reconciliation_corrupt,
                more_available=reconciliation_cursor is not None,
            ),
            staging_cleanup=StagingCleanupSummary(
                scanned=staging_scanned,
                pages=pages,
                deleted=staging_deleted,
                deferred=staging_deferred,
                failed=staging_failed,
                more_available=cursor is not None,
            ),
            project_purge=ProjectPurgeSummary(
                eligible=project_purge.eligible,
                deleted=project_purge.deleted,
                deferred=project_purge.deferred,
                failed=project_purge.failed,
            ),
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("maintenance clock must be timezone-aware")
    return value.astimezone(UTC)


def _validate_cursor(value: str | None, pattern: re.Pattern[str], label: str) -> None:
    if value is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{label} cursor is invalid")


def _validate_progress(
    current: str | None,
    next_cursor: str | None,
    pattern: re.Pattern[str],
    label: str,
) -> None:
    _validate_cursor(next_cursor, pattern, label)
    if next_cursor is not None and current is not None and next_cursor <= current:
        raise ValueError(f"{label} cursor did not advance")


__all__ = [
    "DatabaseMaintenanceState",
    "MaintenanceReport",
    "MaintenanceRunner",
]
