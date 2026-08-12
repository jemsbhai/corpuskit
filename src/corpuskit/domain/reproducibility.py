"""Trusted execution provenance and durable replay contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpuskit.domain.artifacts import (
    ContentDigest,
    DatasetProvenance,
    DeterminismClass,
    ModelProvenance,
    PhoibleProvenance,
    ReplayComparison,
)


class TrustedExecutionFacts(BaseModel):
    """Server-authored worker facts; deliberately contains no tenant or run authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["corpuskit.trusted-execution-facts.v1"] = (
        "corpuskit.trusted-execution-facts.v1"
    )
    corpuskit_version: str = Field(min_length=1, max_length=64)
    corpusgen_version: str = Field(min_length=1, max_length=64)
    worker_profile: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    worker_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    worker_policy: ContentDigest
    espeak_version: str | None = Field(default=None, min_length=1, max_length=160)
    phoible: PhoibleProvenance | None = None
    model: ModelProvenance | None = None
    dataset: DatasetProvenance | None = None
    input_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=32)
    input_attestations: tuple[ContentDigest, ...] = Field(default=(), max_length=32)
    determinism: DeterminismClass

    @model_validator(mode="after")
    def validate_names(self) -> Self:
        if self.worker_policy.name != "worker-policy":
            raise ValueError("worker policy digest must use the worker-policy semantic name")
        if len(self.input_artifact_ids) != len(set(self.input_artifact_ids)):
            raise ValueError("execution input artifact IDs must be unique")
        names = [item.name for item in self.input_attestations]
        if len(names) != len(set(names)) or "worker-policy" in names:
            raise ValueError("execution attestation names must be unique")
        reserved = {"run-spec", "corpus-version"}
        if reserved.intersection(names):
            raise ValueError("execution attestation uses a reserved input name")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ReplayLifecycle(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPARED = "compared"
    UNAVAILABLE = "unavailable"


class ReplayStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    replay_run_id: UUID
    source_run_id: UUID
    source_manifest_artifact_id: UUID
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_manifest_artifact_id: UUID | None = None
    classification: DeterminismClass
    lifecycle: ReplayLifecycle
    comparison: ReplayComparison | None = None


__all__ = ["ReplayLifecycle", "ReplayStatus", "TrustedExecutionFacts"]
