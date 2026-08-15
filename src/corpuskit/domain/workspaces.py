"""Typed contracts for immutable project and corpus workspace workflows."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceModel(BaseModel):
    """Strict immutable base for workspace inputs crossing service boundaries."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CorpusFileFormat(StrEnum):
    """Explicit file formats accepted by the bounded corpus importer."""

    TXT = "txt"
    CSV = "csv"
    JSON = "json"


class CorpusExportFormat(StrEnum):
    """Deterministic export encodings available for immutable versions."""

    TXT = "txt"
    JSON = "json"
    CSV = "csv"


class ProjectLifecycle(StrEnum):
    """Server-owned lifecycle for an otherwise immutable project workspace."""

    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"


class ProjectInput(WorkspaceModel):
    """A new tenant-owned workspace."""

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4_000)


class ProjectDeletionInput(WorkspaceModel):
    """Exact human confirmation required before scheduling project erasure."""

    confirmation: str = Field(min_length=8, max_length=167)


class ManualCorpusInput(WorkspaceModel):
    """A corpus created directly from an ordered sentence list."""

    name: str = Field(min_length=1, max_length=160)
    language: str = Field(default="en-us", min_length=1, max_length=64)
    sentences: tuple[str, ...] = Field(min_length=1, max_length=10_000)


class ManualCorpusVersionInput(WorkspaceModel):
    """A new immutable version appended to an existing corpus."""

    language: str = Field(default="en-us", min_length=1, max_length=64)
    sentences: tuple[str, ...] = Field(min_length=1, max_length=10_000)


class CorpusUpload(WorkspaceModel):
    """A fully buffered, bounded upload ready for strict format validation."""

    name: str = Field(min_length=1, max_length=160)
    language: str = Field(default="en-us", min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=160)
    file_format: CorpusFileFormat
    content: bytes
    text_column: str | None = Field(default=None, min_length=1, max_length=160)


class CorpusVersionUpload(WorkspaceModel):
    """A bounded upload used to append one immutable corpus version."""

    language: str = Field(default="en-us", min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=160)
    file_format: CorpusFileFormat
    content: bytes
    text_column: str | None = Field(default=None, min_length=1, max_length=160)


__all__ = [
    "CorpusExportFormat",
    "CorpusFileFormat",
    "CorpusUpload",
    "CorpusVersionUpload",
    "ManualCorpusInput",
    "ManualCorpusVersionInput",
    "ProjectDeletionInput",
    "ProjectInput",
    "ProjectLifecycle",
]
