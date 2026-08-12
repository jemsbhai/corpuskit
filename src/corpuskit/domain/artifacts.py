"""Immutable artifact and reproducibility-manifest contracts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from corpuskit.domain.jobs import RunKind, normalize_run_spec

SHA256_PATTERN = r"^[0-9a-f]{64}$"
IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
MAX_FILENAME_BYTES = 255
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ArtifactKind(StrEnum):
    """Supported immutable artifact classes."""

    RUN_MANIFEST = "run-manifest"
    CORPUS_TEXT = "corpus-text"
    EVALUATION_REPORT = "evaluation-report"
    EXPORT = "export"
    CHECKPOINT = "checkpoint"
    MODEL_ADAPTER = "model-adapter"
    PROMPT_SET = "prompt-set"
    RUN_RESULT = "run-result"


class ArtifactState(StrEnum):
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"
    DELETED = "deleted"


class DeterminismClass(StrEnum):
    EXACT = "exact"
    BEST_EFFORT = "best-effort"
    NONREPRODUCIBLE = "nonreproducible"


class StopReason(StrEnum):
    COMPLETED = "completed"
    TARGET_REACHED = "target-reached"
    BUDGET_EXHAUSTED = "budget-exhausted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ContentDigest(BaseModel):
    """Named content digest included in a run manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0, le=100 * 1024 * 1024)


class PhoibleProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    revision: str = Field(min_length=7, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    sha256: str = Field(pattern=SHA256_PATTERN)


class DatasetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=160)
    config: str = Field(min_length=1, max_length=160)
    split: str = Field(min_length=1, max_length=160)
    revision: str = Field(min_length=1, max_length=160)
    selector_sha256: str = Field(pattern=SHA256_PATTERN)
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ModelProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    backend: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    identifier: str = Field(min_length=1, max_length=255)
    revision: str = Field(min_length=1, max_length=255)
    artifact_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class RunManifest(BaseModel):
    """Stable, versioned replay recipe with canonical JSON hashing."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["corpuskit.run-manifest.v1"] = "corpuskit.run-manifest.v1"
    project_id: UUID
    run_id: UUID
    operation: RunKind
    corpuskit_version: str = Field(min_length=1, max_length=64)
    corpusgen_version: str = Field(min_length=1, max_length=64)
    espeak_version: str | None = Field(default=None, min_length=1, max_length=160)
    phoible: PhoibleProvenance | None = None
    model: ModelProvenance | None = None
    dataset: DatasetProvenance | None = None
    worker_image_digest: str = Field(pattern=IMAGE_DIGEST_PATTERN)
    runtime_profile: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    language: str = Field(min_length=1, max_length=64)
    target_source: str = Field(min_length=1, max_length=160)
    unit: str = Field(min_length=1, max_length=32)
    parameters: dict[str, Any]
    seed: int | None = None
    input_digests: tuple[ContentDigest, ...] = Field(min_length=1, max_length=128)
    output_digests: tuple[ContentDigest, ...] = Field(min_length=1, max_length=128)
    started_at: datetime
    finished_at: datetime
    stop_reason: StopReason
    determinism: DeterminismClass

    @field_validator("started_at", "finished_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manifest timestamps must include an offset")
        return value.astimezone(UTC)

    @field_validator("parameters")
    @classmethod
    def normalize_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        normalized, _ = normalize_run_spec(value)
        return normalized

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("manifest finish must not precede its start")
        if len({item.name for item in self.input_digests}) != len(self.input_digests):
            raise ValueError("manifest input digest names must be unique")
        if len({item.name for item in self.output_digests}) != len(self.output_digests):
            raise ValueError("manifest output digest names must be unique")
        if self.target_source.casefold() == "phoible" and self.phoible is None:
            raise ValueError("PHOIBLE provenance is required when PHOIBLE supplies the target")
        if (
            self.operation
            in {
                RunKind.GENERATE_LLM,
                RunKind.GENERATE_LOCAL,
                RunKind.GENERATE_DATG,
                RunKind.PERPLEXITY,
                RunKind.TRAIN_PHON_RL,
            }
            and self.model is None
        ):
            raise ValueError("model provenance is required for model-backed workflows")
        source = self.parameters.get("source")
        if (
            self.operation is RunKind.GENERATE_REPOSITORY
            and isinstance(source, dict)
            and source.get("kind") == "hugging_face"
        ):
            selector = source.get("spec")
            if not isinstance(selector, dict) or self.dataset is None:
                raise ValueError("Hugging Face workflows require dataset provenance")
            encoded = json.dumps(
                selector,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if (
                self.dataset.name != selector.get("dataset")
                or self.dataset.config != selector.get("config")
                or self.dataset.split != selector.get("split")
                or self.dataset.revision != selector.get("revision")
                or self.dataset.selector_sha256 != hashlib.sha256(encoded).hexdigest()
            ):
                raise ValueError("Hugging Face dataset provenance does not match the run selector")
        return self

    def canonical_bytes(self) -> bytes:
        """Return RFC-8259-compatible stable JSON bytes (no NaN or whitespace variance)."""

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


STAGED_ARTIFACT_REFERENCE_PATTERN = r"^staged-artifact://sha256/[0-9a-f]{64}$"


class StagedArtifactResult(BaseModel):
    """The complete authority-free result envelope permitted from a child process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract: Literal["corpuskit.staged-artifact-result.v1"] = "corpuskit.staged-artifact-result.v1"
    staged_artifact_ref: str = Field(pattern=STAGED_ARTIFACT_REFERENCE_PATTERN)
    schema_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    artifact_type: Literal["run-result"]
    media_type: Literal["application/json"]
    size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)

    @property
    def sha256(self) -> str:
        return self.staged_artifact_ref.removeprefix("staged-artifact://sha256/")


class ReplayVerdict(StrEnum):
    EXACT_MATCH = "exact-match"
    BEST_EFFORT_MATCH = "best-effort-match"
    BEST_EFFORT_DIVERGENCE = "best-effort-divergence"
    MISMATCH = "mismatch"
    NONREPRODUCIBLE = "nonreproducible"


class ReplayComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: DeterminismClass
    verdict: ReplayVerdict
    replay_inputs_match: bool
    outputs_match: bool
    differences: tuple[str, ...]


def compare_replay(expected: RunManifest, observed: RunManifest) -> ReplayComparison:
    """Compare replay-critical inputs separately from result bytes."""

    expected_value = expected.model_dump(mode="json")
    observed_value = observed.model_dump(mode="json")
    ignored = {"run_id", "started_at", "finished_at", "stop_reason", "output_digests"}
    differences = tuple(
        key
        for key in sorted(expected_value)
        if key not in ignored and expected_value[key] != observed_value.get(key)
    )
    outputs_match = expected.output_digests == observed.output_digests
    replay_inputs_match = not differences
    if expected.determinism is DeterminismClass.NONREPRODUCIBLE:
        verdict = ReplayVerdict.NONREPRODUCIBLE
    elif expected.determinism is DeterminismClass.EXACT:
        verdict = (
            ReplayVerdict.EXACT_MATCH
            if replay_inputs_match
            and outputs_match
            and observed.determinism is DeterminismClass.EXACT
            else ReplayVerdict.MISMATCH
        )
    elif not replay_inputs_match or observed.determinism is DeterminismClass.NONREPRODUCIBLE:
        verdict = ReplayVerdict.MISMATCH
    elif outputs_match:
        verdict = ReplayVerdict.BEST_EFFORT_MATCH
    else:
        verdict = ReplayVerdict.BEST_EFFORT_DIVERGENCE
    return ReplayComparison(
        classification=expected.determinism,
        verdict=verdict,
        replay_inputs_match=replay_inputs_match,
        outputs_match=outputs_match,
        differences=differences,
    )


_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/jsonl",
        "application/octet-stream",
        "application/parquet",
        "application/zip",
        "text/csv",
        "text/plain",
    }
)


def normalize_media_type(value: str) -> str:
    media_type = value.partition(";")[0].strip().lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise ValueError("unsupported artifact media type")
    return media_type


def safe_download_filename(value: str) -> str:
    """Return a header-safe, spreadsheet-formula-safe ASCII download name."""

    if (
        not value
        or len(value.encode("utf-8")) > MAX_FILENAME_BYTES
        or _CONTROL.search(value)
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    ):
        raise ValueError("unsafe artifact filename")
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = _UNSAFE_FILENAME.sub("-", normalized).strip(".")
    if not normalized:
        normalized = "artifact.bin"
    if normalized.startswith(_FORMULA_PREFIXES):
        normalized = f"_{normalized.lstrip('=+-@')}"
    return normalized[:MAX_FILENAME_BYTES]


def content_disposition(filename: str) -> str:
    safe = safe_download_filename(filename)
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(safe, safe='')}"


def artifact_storage_key(
    *,
    organization_id: UUID,
    project_id: UUID,
    run_id: UUID | None,
    kind: ArtifactKind,
    sha256: str,
) -> str:
    """Build a tenant-isolated content address without including user-controlled names."""

    if re.fullmatch(SHA256_PATTERN, sha256) is None:
        raise ValueError("invalid artifact digest")
    scope = run_id.hex if run_id is not None else "project"
    return (
        f"artifacts/v1/{organization_id.hex}/{project_id.hex}/{scope}/"
        f"{kind.value}/{sha256[:2]}/{sha256}"
    )


def staged_artifact_reference(sha256: str) -> str:
    """Build the only child-to-parent artifact reference format."""

    if re.fullmatch(SHA256_PATTERN, sha256) is None:
        raise ValueError("invalid staged artifact digest")
    return f"staged-artifact://sha256/{sha256}"


def staged_artifact_storage_key(sha256: str) -> str:
    """Map a digest to the authority-free internal staging namespace."""

    if re.fullmatch(SHA256_PATTERN, sha256) is None:
        raise ValueError("invalid staged artifact digest")
    return f"staging/v1/sha256/{sha256[:2]}/{sha256}"


__all__ = [
    "ArtifactKind",
    "ArtifactState",
    "ContentDigest",
    "DatasetProvenance",
    "DeterminismClass",
    "ModelProvenance",
    "PhoibleProvenance",
    "ReplayComparison",
    "ReplayVerdict",
    "RunManifest",
    "StagedArtifactResult",
    "StopReason",
    "artifact_storage_key",
    "compare_replay",
    "content_disposition",
    "normalize_media_type",
    "safe_download_filename",
    "staged_artifact_reference",
    "staged_artifact_storage_key",
]
