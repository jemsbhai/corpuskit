"""Versioned contracts for PostgreSQL backup and restore-drill evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
BUNDLE_ID_PATTERN = r"^ckpg_[0-9]{8}T[0-9]{12}Z_[0-9a-f]{12}_[0-9a-f]{12}$"
POSTGRES_VERSION_PATTERN = r"^[0-9]{1,2}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$"
ALEMBIC_REVISION_PATTERN = r"^[A-Za-z0-9_]{1,64}$"
ARCHIVE_FILENAME = "database.dump"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_DIGEST_FILENAME = "manifest.sha256"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("continuity timestamps must include an offset")
    return value.astimezone(UTC)


class PostgresBackupManifest(BaseModel):
    """Credential-free, canonical metadata published with one custom-format archive."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_id: Literal["corpuskit.postgres-backup-manifest.v1"] = (
        "corpuskit.postgres-backup-manifest.v1"
    )
    bundle_id: str = Field(pattern=BUNDLE_ID_PATTERN)
    created_at: datetime
    archive_filename: Literal["database.dump"] = "database.dump"
    archive_format: Literal["postgresql-custom"] = "postgresql-custom"
    archive_sha256: str = Field(pattern=SHA256_PATTERN)
    archive_size_bytes: int = Field(gt=0, le=2**63 - 1)
    toc_entry_count: int = Field(gt=0, le=50_000_000)
    pg_dump_version: str = Field(pattern=POSTGRES_VERSION_PATTERN)
    pg_restore_version: str = Field(pattern=POSTGRES_VERSION_PATTERN)
    owner_commands_included: Literal[False] = False
    privilege_commands_included: Literal[False] = False

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_bundle_digest(self) -> Self:
        digest_prefix = self.bundle_id.split("_")[2]
        if digest_prefix != self.archive_sha256[:12]:
            raise ValueError("bundle identifier must bind the archive digest")
        if self.pg_dump_version.split(".", 1)[0] != self.pg_restore_version.split(".", 1)[0]:
            raise ValueError("backup and restore tools must have the same PostgreSQL major")
        return self

    def canonical_file_bytes(self) -> bytes:
        """Return the exact UTF-8 bytes protected by ``manifest.sha256``."""

        payload = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return payload.encode("utf-8") + b"\n"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_file_bytes()).hexdigest()


class BackupCreationReport(BaseModel):
    """Safe operator output for a newly published backup bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_id: Literal["corpuskit.postgres-backup-created.v1"] = (
        "corpuskit.postgres-backup-created.v1"
    )
    bundle_id: str = Field(pattern=BUNDLE_ID_PATTERN)
    archive_sha256: str = Field(pattern=SHA256_PATTERN)
    archive_size_bytes: int = Field(gt=0, le=2**63 - 1)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class BackupVerificationReport(BaseModel):
    """Offline archive, manifest, and PostgreSQL TOC verification evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_id: Literal["corpuskit.postgres-backup-verification.v1"] = (
        "corpuskit.postgres-backup-verification.v1"
    )
    bundle_id: str = Field(pattern=BUNDLE_ID_PATTERN)
    archive_sha256: str = Field(pattern=SHA256_PATTERN)
    archive_size_bytes: int = Field(gt=0, le=2**63 - 1)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    toc_entry_count: int = Field(gt=0, le=50_000_000)
    pg_restore_version: str = Field(pattern=POSTGRES_VERSION_PATTERN)
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class RestoreDrillReport(BaseModel):
    """Credential- and target-free evidence from an isolated restore drill."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_id: Literal["corpuskit.postgres-restore-drill.v1"] = (
        "corpuskit.postgres-restore-drill.v1"
    )
    bundle_id: str = Field(pattern=BUNDLE_ID_PATTERN)
    archive_sha256: str = Field(pattern=SHA256_PATTERN)
    started_at: datetime
    completed_at: datetime
    restored_relation_count: int = Field(gt=0, le=50_000_000)
    alembic_revision: str = Field(pattern=ALEMBIC_REVISION_PATTERN)
    pg_restore_version: str = Field(pattern=POSTGRES_VERSION_PATTERN)

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("restore drill completion cannot precede its start")
        return self


def parse_manifest_file(payload: bytes) -> PostgresBackupManifest:
    """Parse only canonical, size-bounded manifest bytes supplied by the operations layer."""

    manifest = PostgresBackupManifest.model_validate_json(payload)
    if payload != manifest.canonical_file_bytes():
        raise ValueError("backup manifest is not in canonical form")
    return manifest


def manifest_digest_line(manifest: PostgresBackupManifest) -> bytes:
    """Return a deterministic detached digest in sha256sum-compatible form."""

    return f"{manifest.sha256}  {MANIFEST_FILENAME}\n".encode("ascii")


def is_alembic_revision(value: str) -> bool:
    return re.fullmatch(ALEMBIC_REVISION_PATTERN, value, flags=re.ASCII) is not None


__all__ = [
    "ARCHIVE_FILENAME",
    "BUNDLE_ID_PATTERN",
    "MANIFEST_DIGEST_FILENAME",
    "MANIFEST_FILENAME",
    "BackupCreationReport",
    "BackupVerificationReport",
    "PostgresBackupManifest",
    "RestoreDrillReport",
    "is_alembic_revision",
    "manifest_digest_line",
    "parse_manifest_file",
]
