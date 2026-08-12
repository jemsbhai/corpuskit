"""Strict tenant quota and immutable audit contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpuskit.domain.datg import DatgGuidedGenerationRequest, DatgIndexBuildRequest
from corpuskit.domain.generation import RepositoryGenerationRequest
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
)
from corpuskit.domain.phon_rl import PhonRlTrainingRequest

AUDIT_GENESIS_HASH = "0" * 64
_SAFE_CORRELATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_SAFE_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:/+-]{0,254}$", re.ASCII)


class RunQuotaClass(StrEnum):
    CPU = "cpu"
    EXPENSIVE = "expensive"


class QuotaReservationState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class AuditActorKind(StrEnum):
    USER = "user"
    SERVICE = "service"


class AuditAction(StrEnum):
    PROJECT_CREATED = "project.created"
    PROJECT_DELETION_REQUESTED = "project.deletion_requested"
    PROJECT_PURGED = "project.purged"
    CORPUS_CREATED = "corpus.created"
    RUN_SUBMITTED = "run.submitted"
    RUN_CANCELLATION_REQUESTED = "run.cancellation_requested"
    RUN_RETRY_SUBMITTED = "run.retry_submitted"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_TOMBSTONED = "artifact.tombstoned"
    ARTIFACT_PURGED = "artifact.purged"
    ARTIFACT_ADOPTED = "artifact.adopted"
    RUN_MANIFEST_CREATED = "run.manifest_created"
    RUN_REPLAY_SUBMITTED = "run.replay_submitted"
    RUN_REPLAY_COMPARED = "run.replay_compared"
    QUOTA_POLICY_CHANGED = "quota.policy_changed"
    QUOTA_RESERVATION_EXPIRED = "quota.reservation_expired"


class AuditResourceType(StrEnum):
    PROJECT = "project"
    CORPUS = "corpus"
    RUN = "run"
    ARTIFACT = "artifact"
    QUOTA_POLICY = "quota-policy"
    QUOTA_RESERVATION = "quota-reservation"
    REPLAY = "replay"


class QuotaPolicyValues(BaseModel):
    """Server-owned limits; request DTOs never embed or override this policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_concurrent_cpu_jobs: int = Field(default=3, ge=1, le=1_000)
    max_concurrent_expensive_jobs: int = Field(default=1, ge=1, le=1_000)
    max_artifact_bytes: int = Field(default=10 * 1024 * 1024 * 1024, ge=1)
    max_artifact_count: int = Field(default=10_000, ge=1)
    max_corpus_sentences: int = Field(default=1_000_000, ge=1)
    max_generation_accepted_sentences: int = Field(default=100, ge=1)
    max_generation_iterations: int = Field(default=500, ge=1)
    max_activity_deadline_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    max_provider_input_tokens: int = Field(default=1_000_000, ge=1)
    max_provider_output_tokens: int = Field(default=100_000, ge=1)
    max_provider_cost_microusd: int = Field(default=10_000_000, ge=1)
    max_rl_steps: int = Field(default=10_000, ge=1)
    max_rl_tokens: int = Field(default=10_000_000, ge=1)
    max_checkpoint_bytes: int = Field(default=100 * 1024 * 1024, ge=1)

    @model_validator(mode="after")
    def finite_deadline(self) -> Self:
        if not math.isfinite(self.max_activity_deadline_seconds):
            raise ValueError("quota deadline must be finite")
        return self


RUN_QUOTA_CLASSES: dict[RunKind, RunQuotaClass] = {
    RunKind.PHONEMIZE: RunQuotaClass.CPU,
    RunKind.EVALUATE: RunQuotaClass.CPU,
    RunKind.DISTRIBUTION: RunQuotaClass.CPU,
    RunKind.TRAJECTORY: RunQuotaClass.CPU,
    RunKind.ERROR_RATES: RunQuotaClass.CPU,
    RunKind.PERPLEXITY: RunQuotaClass.EXPENSIVE,
    RunKind.SELECT: RunQuotaClass.CPU,
    RunKind.GENERATE_REPOSITORY: RunQuotaClass.CPU,
    RunKind.GENERATE_LLM: RunQuotaClass.EXPENSIVE,
    RunKind.GENERATE_LOCAL: RunQuotaClass.EXPENSIVE,
    RunKind.BUILD_DATG_INDEX: RunQuotaClass.CPU,
    RunKind.GENERATE_DATG: RunQuotaClass.EXPENSIVE,
    RunKind.TRAIN_PHON_RL: RunQuotaClass.EXPENSIVE,
    RunKind.EXPORT: RunQuotaClass.CPU,
}
if set(RUN_QUOTA_CLASSES) != set(RunKind):  # pragma: no cover - import-time fail-closed guard
    raise RuntimeError("every durable run kind requires an explicit quota class")


def run_quota_class(kind: RunKind) -> RunQuotaClass:
    """Return the reviewed class without a permissive fallback."""

    try:
        return RUN_QUOTA_CLASSES[kind]
    except (KeyError, TypeError):
        raise ValueError("run kind has no quota classification") from None


def validate_run_resource_policy(
    kind: RunKind,
    spec: dict[str, Any],
    policy: QuotaPolicyValues,
) -> float | None:
    """Validate kind-specific resource ceilings against a server-owned policy."""

    accepted: int | None = None
    iterations: int | None = None
    deadline: float | None = None
    if kind is RunKind.GENERATE_REPOSITORY:
        repository = RepositoryGenerationRequest.model_validate(spec)
        accepted = repository.stopping.max_sentences
        iterations = repository.stopping.max_iterations
        deadline = repository.activity_timeout_seconds
    elif kind is RunKind.GENERATE_LLM:
        hosted = HostedGenerationRequest.model_validate(spec)
        accepted = hosted.stopping.max_sentences
        iterations = hosted.stopping.max_iterations
        deadline = hosted.activity_timeout_seconds
        if hosted.budget.max_input_tokens > policy.max_provider_input_tokens:
            raise ValueError("provider input token quota exceeded")
        if hosted.budget.max_output_tokens > policy.max_provider_output_tokens:
            raise ValueError("provider output token quota exceeded")
        if _microusd(hosted.budget.max_cost_usd) > policy.max_provider_cost_microusd:
            raise ValueError("provider cost quota exceeded")
    elif kind is RunKind.GENERATE_LOCAL:
        local = LocalGenerationRequest.model_validate(spec)
        accepted = local.stopping.max_sentences
        iterations = local.stopping.max_iterations
        deadline = local.activity_timeout_seconds
        if (
            local.max_new_tokens * local.candidates_per_iteration
            > policy.max_provider_output_tokens
        ):
            raise ValueError("local generation token quota exceeded")
    elif kind is RunKind.PERPLEXITY:
        analysis = LanguageModelAnalysisRequest.model_validate(spec)
        deadline = analysis.activity_timeout_seconds
        if len(analysis.texts) * analysis.max_length > policy.max_provider_input_tokens:
            raise ValueError("analysis token quota exceeded")
    elif kind is RunKind.BUILD_DATG_INDEX:
        datg_index = DatgIndexBuildRequest.model_validate(spec)
        deadline = datg_index.activity_timeout_seconds
    elif kind is RunKind.GENERATE_DATG:
        datg_generation = DatgGuidedGenerationRequest.model_validate(spec)
        deadline = datg_generation.activity_timeout_seconds
        accepted = datg_generation.candidates
        if (
            datg_generation.candidates * datg_generation.max_new_tokens
            > policy.max_provider_output_tokens
        ):
            raise ValueError("DATG token quota exceeded")
    elif kind is RunKind.TRAIN_PHON_RL:
        rl = PhonRlTrainingRequest.model_validate(spec)
        deadline = rl.parameters.activity_timeout_seconds
        if rl.parameters.num_steps > policy.max_rl_steps:
            raise ValueError("RL step quota exceeded")
        generated_tokens = (
            rl.parameters.num_steps * rl.parameters.batch_size * rl.parameters.max_new_tokens
        )
        if generated_tokens > policy.max_rl_tokens:
            raise ValueError("RL token quota exceeded")

    if accepted is not None and accepted > policy.max_generation_accepted_sentences:
        raise ValueError("generation accepted-sentence quota exceeded")
    if iterations is not None and iterations > policy.max_generation_iterations:
        raise ValueError("generation iteration quota exceeded")
    if deadline is not None and deadline > policy.max_activity_deadline_seconds:
        raise ValueError("activity deadline quota exceeded")
    return deadline


def safe_correlation_id(value: str | None) -> str | None:
    if value is None:
        return None
    if _SAFE_CORRELATION.fullmatch(value) is None:
        raise ValueError("correlation ID is not safe")
    return value


def safe_audit_actor(value: str) -> str:
    if _SAFE_ACTOR.fullmatch(value) is None:
        raise ValueError("audit actor identifier is not safe")
    return value


_AUDIT_METADATA_KEYS: dict[AuditAction, frozenset[str]] = {
    AuditAction.PROJECT_CREATED: frozenset(),
    AuditAction.PROJECT_DELETION_REQUESTED: frozenset(
        {"artifact_count", "corpus_sentences", "retention_until"}
    ),
    AuditAction.PROJECT_PURGED: frozenset({"artifact_count", "corpus_sentences"}),
    AuditAction.CORPUS_CREATED: frozenset({"content_sha256", "language", "sentence_count"}),
    AuditAction.RUN_SUBMITTED: frozenset({"attempt", "kind", "quota_class"}),
    AuditAction.RUN_CANCELLATION_REQUESTED: frozenset({"prior_state"}),
    AuditAction.RUN_RETRY_SUBMITTED: frozenset({"attempt", "kind", "quota_class", "source_run_id"}),
    AuditAction.RUN_SUCCEEDED: frozenset({"kind"}),
    AuditAction.RUN_FAILED: frozenset({"failure_code", "kind"}),
    AuditAction.RUN_CANCELLED: frozenset({"kind"}),
    AuditAction.ARTIFACT_CREATED: frozenset({"kind", "sha256", "size_bytes"}),
    AuditAction.ARTIFACT_TOMBSTONED: frozenset({"kind", "size_bytes"}),
    AuditAction.ARTIFACT_PURGED: frozenset({"kind", "size_bytes"}),
    AuditAction.ARTIFACT_ADOPTED: frozenset({"kind", "schema_id", "sha256", "size_bytes"}),
    AuditAction.RUN_MANIFEST_CREATED: frozenset({"sha256", "size_bytes"}),
    AuditAction.RUN_REPLAY_SUBMITTED: frozenset(
        {"classification", "kind", "source_manifest_id", "source_run_id"}
    ),
    AuditAction.RUN_REPLAY_COMPARED: frozenset({"classification", "outputs_match", "verdict"}),
    AuditAction.QUOTA_POLICY_CHANGED: frozenset({"changed_fields"}),
    AuditAction.QUOTA_RESERVATION_EXPIRED: frozenset({"kind", "quota_class"}),
}


def normalize_audit_metadata(action: AuditAction, metadata: dict[str, Any]) -> dict[str, Any]:
    if set(metadata) - _AUDIT_METADATA_KEYS[action]:
        raise ValueError("audit metadata contains fields outside the action allowlist")
    encoded = json.dumps(
        metadata,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > 2_048:
        raise ValueError("audit metadata exceeds its size limit")
    normalized = json.loads(encoded)
    if not isinstance(normalized, dict):
        raise ValueError("audit metadata must be an object")
    return normalized


def audit_event_hash(
    *,
    organization_id: UUID,
    sequence: int,
    actor_kind: AuditActorKind,
    actor_id: str,
    action: AuditAction,
    resource_type: AuditResourceType,
    resource_id: UUID,
    request_id: str | None,
    occurred_at: datetime,
    metadata: dict[str, Any],
    previous_hash: str,
) -> str:
    payload = {
        "action": action.value,
        "actor_id": safe_audit_actor(actor_id),
        "actor_kind": actor_kind.value,
        "metadata": normalize_audit_metadata(action, metadata),
        "occurred_at": _canonical_timestamp(occurred_at),
        "organization_id": str(organization_id),
        "previous_hash": previous_hash,
        "request_id": safe_correlation_id(request_id),
        "resource_id": str(resource_id),
        "resource_type": resource_type.value,
        "sequence": sequence,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _microusd(value: Decimal) -> int:
    try:
        scaled = value * Decimal(1_000_000)
        if scaled != scaled.to_integral_value():
            raise ValueError("provider cost requires exact micro-USD precision")
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError("provider cost is not finite and bounded") from None


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


__all__ = [
    "AUDIT_GENESIS_HASH",
    "RUN_QUOTA_CLASSES",
    "AuditAction",
    "AuditActorKind",
    "AuditResourceType",
    "QuotaPolicyValues",
    "QuotaReservationState",
    "RunQuotaClass",
    "audit_event_hash",
    "normalize_audit_metadata",
    "run_quota_class",
    "safe_audit_actor",
    "safe_correlation_id",
    "validate_run_resource_policy",
]
