"""Relational metadata for multi-tenant, immutable corpus workflows."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from corpuskit.domain.artifacts import ArtifactState, DeterminismClass, ReplayVerdict
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.platform import (
    AuditAction,
    AuditActorKind,
    AuditResourceType,
    QuotaReservationState,
    RunQuotaClass,
)
from corpuskit.domain.workspaces import ProjectLifecycle

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class OutboxState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PUBLISHED = "published"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ApiRateLimitWindow(Base):
    """Opaque distributed fixed-window state for authenticated HTTP traffic."""

    __tablename__ = "api_rate_limit_windows"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "subject_sha256",
            "route_sha256",
            "method",
            "window_epoch",
            name="uq_api_rate_limit_windows_scope",
        ),
        CheckConstraint("length(subject_sha256) = 64", name="subject_sha256_length"),
        CheckConstraint("length(route_sha256) = 64", name="route_sha256_length"),
        CheckConstraint("request_count > 0", name="positive_request_count"),
        CheckConstraint("window_epoch >= 0", name="nonnegative_window_epoch"),
        Index("ix_api_rate_limit_windows_expiry", "window_epoch", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    route_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(7), nullable=False)
    window_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    oidc_subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)


class Membership(Base, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), nullable=False)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        CheckConstraint(
            "(lifecycle_state = 'ACTIVE' AND deletion_requested_at IS NULL "
            "AND deletion_retention_until IS NULL AND deletion_corpus_sentences IS NULL) "
            "OR (lifecycle_state = 'DELETION_PENDING' AND deletion_requested_at IS NOT NULL "
            "AND deletion_retention_until IS NOT NULL "
            "AND deletion_retention_until >= deletion_requested_at "
            "AND deletion_corpus_sentences IS NOT NULL)",
            name="lifecycle_consistent",
        ),
        CheckConstraint(
            "deletion_corpus_sentences IS NULL OR deletion_corpus_sentences >= 0",
            name="nonnegative_deletion_corpus_sentences",
        ),
        Index("ix_projects_lifecycle_retention", "lifecycle_state", "deletion_retention_until"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lifecycle_state: Mapped[ProjectLifecycle] = mapped_column(
        Enum(ProjectLifecycle, native_enum=False),
        nullable=False,
        default=ProjectLifecycle.ACTIVE,
        server_default=ProjectLifecycle.ACTIVE.name,
    )
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_corpus_sentences: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Corpus(Base, TimestampMixin):
    __tablename__ = "corpora"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    versions: Mapped[list[CorpusVersion]] = relationship(
        back_populates="corpus", cascade="all, delete-orphan"
    )


class CorpusVersion(Base, TimestampMixin):
    __tablename__ = "corpus_versions"
    __table_args__ = (
        UniqueConstraint(
            "corpus_id", "version_number", name="uq_corpus_versions_corpus_version_number"
        ),
        UniqueConstraint(
            "corpus_id", "content_sha256", name="uq_corpus_versions_corpus_content_sha256"
        ),
        CheckConstraint("version_number > 0", name="positive_version"),
        CheckConstraint("sentence_count > 0", name="positive_sentence_count"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corpus_id: Mapped[UUID] = mapped_column(
        ForeignKey("corpora.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("corpus_versions.id"), nullable=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(64), nullable=False)
    sentence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    corpusgen_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.7")
    corpus: Mapped[Corpus] = relationship(back_populates="versions")
    sentences: Mapped[list[Sentence]] = relationship(
        back_populates="corpus_version",
        cascade="all, delete-orphan",
        order_by="Sentence.ordinal",
    )


class Sentence(Base):
    __tablename__ = "sentences"
    __table_args__ = (
        UniqueConstraint("corpus_version_id", "ordinal"),
        CheckConstraint("ordinal >= 0", name="nonnegative_ordinal"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corpus_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("corpus_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    phonemes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    corpus_version: Mapped[CorpusVersion] = relationship(back_populates="sentences")


class Run(Base, TimestampMixin):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key"),
        CheckConstraint("attempt > 0", name="positive_attempt"),
        CheckConstraint("event_sequence > 0", name="positive_event_sequence"),
        Index("ix_runs_org_state_created", "organization_id", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    corpus_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("corpus_versions.id"), nullable=True
    )
    parent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id"), nullable=True, index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[RunKind] = mapped_column(Enum(RunKind, native_enum=False), nullable=False)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, native_enum=False), nullable=False, default=RunState.DRAFT
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxMessage(Base, TimestampMixin):
    """At-least-once dispatch intent committed atomically with a run change."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_claim", "state", "available_at", "created_at"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[OutboxState] = mapped_column(
        Enum(OutboxState, native_enum=False), nullable=False, default=OutboxState.PENDING
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "project_id",
            "scope_key",
            "kind",
            "sha256",
            name="uq_artifacts_scope_digest",
        ),
        CheckConstraint("size_bytes > 0", name="positive_size"),
        Index("ix_artifacts_retention", "state", "retention_until"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(String(160), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[ArtifactState] = mapped_column(
        Enum(ArtifactState, native_enum=False), nullable=False, default=ArtifactState.ACTIVE
    )
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DatgIndexPublicationRecord(Base, TimestampMixin):
    """Immutable tenant authorization for one verified shared DATG cache entry."""

    __tablename__ = "datg_index_publications"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "project_id",
            "cache_key_sha256",
            name="uq_datg_index_publications_project_cache_key",
        ),
        CheckConstraint("length(cache_key_sha256) = 64", name="cache_key_sha256_length"),
        CheckConstraint("length(content_sha256) = 64", name="content_sha256_length"),
        CheckConstraint("vocabulary_size > 0", name="positive_vocabulary_size"),
        CheckConstraint("indexed_token_count >= 0", name="nonnegative_indexed_token_count"),
        CheckConstraint(
            "indexed_token_count <= vocabulary_size",
            name="indexed_token_count_bounded",
        ),
        CheckConstraint("size_bytes > 0", name="positive_size_bytes"),
        Index(
            "ix_datg_index_publications_org_project_created",
            "organization_id",
            "project_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    cache_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    vocabulary_size: Mapped[int] = mapped_column(Integer, nullable=False)
    indexed_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class RunExecutionFact(Base):
    """Immutable server-authored execution environment bound to one durable run."""

    __tablename__ = "run_execution_facts"
    __table_args__ = (
        CheckConstraint("length(facts_sha256) = 64", name="facts_sha256_length"),
        CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="manifest_sha256_length",
        ),
        CheckConstraint(
            "(manifest_artifact_id IS NULL AND manifest_sha256 IS NULL AND finalized_at IS NULL) "
            "OR (manifest_artifact_id IS NOT NULL AND manifest_sha256 IS NOT NULL "
            "AND finalized_at IS NOT NULL)",
            name="manifest_completion_consistent",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    facts_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digests: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    manifest_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True, unique=True
    )
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunReplay(Base, TimestampMixin):
    """Durable lineage and comparison projection for one replay run."""

    __tablename__ = "run_replays"
    __table_args__ = (
        CheckConstraint("length(expected_manifest_sha256) = 64", name="expected_sha256_length"),
        CheckConstraint(
            "(observed_manifest_artifact_id IS NULL AND verdict IS NULL "
            "AND comparison IS NULL AND completed_at IS NULL) "
            "OR (observed_manifest_artifact_id IS NOT NULL AND verdict IS NOT NULL "
            "AND comparison IS NOT NULL AND completed_at IS NOT NULL)",
            name="comparison_completion_consistent",
        ),
        Index("ix_run_replays_org_source", "organization_id", "source_run_id", "created_at"),
    )

    replay_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    source_manifest_artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey("artifacts.id"), nullable=False, index=True
    )
    expected_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_manifest_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True, unique=True
    )
    classification: Mapped[DeterminismClass] = mapped_column(
        Enum(DeterminismClass, native_enum=False), nullable=False
    )
    verdict: Mapped[ReplayVerdict | None] = mapped_column(
        Enum(ReplayVerdict, native_enum=False), nullable=True
    )
    comparison: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuotaPolicy(Base):
    """Server-owned per-organization resource ceilings."""

    __tablename__ = "quota_policies"
    __table_args__ = (
        CheckConstraint("max_concurrent_cpu_jobs > 0", name="positive_cpu_limit"),
        CheckConstraint("max_concurrent_expensive_jobs > 0", name="positive_expensive_limit"),
        CheckConstraint("max_artifact_bytes > 0", name="positive_artifact_bytes_limit"),
        CheckConstraint("max_artifact_count > 0", name="positive_artifact_count_limit"),
        CheckConstraint("max_corpus_sentences > 0", name="positive_corpus_sentence_limit"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    max_concurrent_cpu_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_concurrent_expensive_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_artifact_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=10 * 1024 * 1024 * 1024
    )
    max_artifact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10_000)
    max_corpus_sentences: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1_000_000)
    max_generation_accepted_sentences: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100
    )
    max_generation_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    max_activity_deadline_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=300.0
    )
    max_provider_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1_000_000
    )
    max_provider_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=100_000
    )
    max_provider_cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=10_000_000
    )
    max_rl_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=10_000)
    max_rl_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=10_000_000)
    max_checkpoint_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=100 * 1024 * 1024
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class QuotaUsage(Base):
    """Transactionally maintained aggregate usage for one organization."""

    __tablename__ = "quota_usages"
    __table_args__ = (
        CheckConstraint("active_cpu_jobs >= 0", name="nonnegative_active_cpu_jobs"),
        CheckConstraint("active_expensive_jobs >= 0", name="nonnegative_active_expensive_jobs"),
        CheckConstraint("artifact_bytes >= 0", name="nonnegative_artifact_bytes"),
        CheckConstraint("artifact_count >= 0", name="nonnegative_artifact_count"),
        CheckConstraint("corpus_sentences >= 0", name="nonnegative_corpus_sentences"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    active_cpu_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_expensive_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    corpus_sentences: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class QuotaReservation(Base, TimestampMixin):
    """Idempotent active-job capacity reservation tied to one durable run."""

    __tablename__ = "quota_reservations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "run_id", name="uq_quota_reservations_organization_run"
        ),
        CheckConstraint("amount = 1", name="unit_amount"),
        Index("ix_quota_reservations_expiry", "state", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quota_class: Mapped[RunQuotaClass] = mapped_column(
        Enum(RunQuotaClass, native_enum=False), nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[QuotaReservationState] = mapped_column(
        Enum(QuotaReservationState, native_enum=False),
        nullable=False,
        default=QuotaReservationState.ACTIVE,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditHead(Base):
    """Locked per-organization head for the append-only audit hash chain."""

    __tablename__ = "audit_heads"
    __table_args__ = (
        CheckConstraint("last_sequence >= 0", name="nonnegative_last_sequence"),
        CheckConstraint("length(last_hash) = 64", name="last_hash_length"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuditEvent(Base):
    """Immutable, allowlisted, tamper-evident organization audit evidence."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "sequence", name="uq_audit_events_organization_sequence"
        ),
        CheckConstraint("sequence > 0", name="positive_sequence"),
        CheckConstraint("length(previous_hash) = 64", name="previous_hash_length"),
        CheckConstraint("length(event_hash) = 64", name="event_hash_length"),
        Index("ix_audit_events_org_time", "organization_id", "occurred_at", "sequence"),
        Index("ix_audit_events_org_action", "organization_id", "action", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_kind: Mapped[AuditActorKind] = mapped_column(
        Enum(AuditActorKind, native_enum=False), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, native_enum=False), nullable=False
    )
    resource_type: Mapped[AuditResourceType] = mapped_column(
        Enum(AuditResourceType, native_enum=False), nullable=False
    )
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class MaintenanceCursor(Base):
    """Opaque global scan progress scoped to one operation and storage backend."""

    __tablename__ = "maintenance_cursors"
    __table_args__ = (
        CheckConstraint("length(backend_fingerprint) = 64", name="backend_fingerprint_length"),
    )

    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    backend_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
