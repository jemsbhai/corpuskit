"""Bounded application contracts for Phon-DATG index and guidance work."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from corpuskit.domain.corpus import FrozenDomainModel

MAX_DATG_VOCABULARY = 100_000
MAX_DATG_BATCH_SIZE = 1_024
MAX_DATG_ACTIVITY_SECONDS = 900.0
MAX_DATG_TARGET_PHONEMES = 32
MAX_DATG_TARGET_UNITS = 4_096
MAX_DATG_COVERAGE_SEQUENCES = 500
MAX_DATG_SEQUENCE_PHONEMES = 1_000
MAX_DATG_CANDIDATES = 8
MAX_DATG_NEW_TOKENS = 512
MAX_DATG_INDEX_BYTES = 32 * 1024 * 1024
MAX_DATG_INSPECTION_RESULTS = 500
MAX_DATG_LOGIT_BATCH = 8
MAX_DATG_LOGIT_VOCABULARY = 2_048

_SAFE_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
    re.ASCII,
)
_SAFE_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$", re.ASCII)
_SAFE_RUNTIME_ID = re.compile(r"^[a-z][a-z0-9._-]{1,63}$", re.ASCII)
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+:-]{0,127}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_REVISION = re.compile(r"^[0-9a-f]{40}$", re.ASCII)


class DatgModel(FrozenDomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class DatgUnit(StrEnum):
    PHONEME = "phoneme"
    DIPHONE = "diphone"
    TRIPHONE = "triphone"


class DatgAntiMode(StrEnum):
    COVERED = "covered"
    FREQUENCY = "frequency"


class DatgQuantization(StrEnum):
    NONE = "none"
    FOUR_BIT = "4bit"
    EIGHT_BIT = "8bit"


class DatgWorkerProfile(StrEnum):
    LOCAL_CPU = "local_cpu"
    LOCAL_GPU = "local_gpu"


class DatgSnapshotPin(DatgModel):
    repository_id: str = Field(min_length=3, max_length=192)
    revision: str = Field(min_length=40, max_length=40)
    snapshot_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_pin(self) -> Self:
        if _SAFE_REPOSITORY.fullmatch(self.repository_id) is None:
            raise ValueError(
                "DATG repositories require a namespaced identifier, not a path or URL."
            )
        if _REVISION.fullmatch(self.revision) is None:
            raise ValueError("DATG revisions require a lowercase immutable 40-character commit.")
        if _SHA256.fullmatch(self.snapshot_sha256) is None:
            raise ValueError("DATG snapshot digests require lowercase SHA-256.")
        return self


class DatgRuntimePolicyEntry(DatgModel):
    runtime_id: str = Field(min_length=2, max_length=64)
    model: DatgSnapshotPin
    tokenizer: DatgSnapshotPin
    allowed_quantizations: tuple[DatgQuantization, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if _SAFE_RUNTIME_ID.fullmatch(self.runtime_id) is None:
            raise ValueError("DATG runtime IDs must use the safe identifier grammar.")
        if self.model != self.tokenizer:
            raise ValueError(
                "CorpusGen LocalBackend currently requires model and tokenizer from one snapshot."
            )
        if len(set(self.allowed_quantizations)) != len(self.allowed_quantizations):
            raise ValueError("DATG quantization allowlists must be unique.")
        return self


class DatgIndexBuildRequest(DatgModel):
    runtime_id: str = Field(min_length=2, max_length=64)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: DatgUnit = DatgUnit.PHONEME
    batch_size: int = Field(default=256, ge=1, le=MAX_DATG_BATCH_SIZE)
    max_vocabulary_size: int = Field(default=50_000, ge=1, le=MAX_DATG_VOCABULARY)
    activity_timeout_seconds: float = Field(
        default=300.0,
        gt=0.0,
        le=MAX_DATG_ACTIVITY_SECONDS,
    )

    @model_validator(mode="after")
    def validate_identifiers(self) -> Self:
        _validate_runtime_language(self.runtime_id, self.language)
        return self


class DatgCacheIdentity(DatgModel):
    schema_id: Literal["corpuskit.datg-index-cache-key.v1"] = "corpuskit.datg-index-cache-key.v1"
    tokenizer_id: str
    tokenizer_revision: str
    tokenizer_snapshot_sha256: str
    language: str
    unit: DatgUnit
    corpusgen_version: str = Field(min_length=1, max_length=128)
    espeak_version: str = Field(min_length=1, max_length=128)
    cache_key_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        tokenizer: DatgSnapshotPin,
        language: str,
        unit: DatgUnit,
        corpusgen_version: str,
        espeak_version: str,
    ) -> DatgCacheIdentity:
        payload = _identity_payload(
            tokenizer=tokenizer,
            language=language,
            unit=unit,
            corpusgen_version=corpusgen_version,
            espeak_version=espeak_version,
        )
        return cls(
            **payload,
            cache_key_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        tokenizer = DatgSnapshotPin(
            repository_id=self.tokenizer_id,
            revision=self.tokenizer_revision,
            snapshot_sha256=self.tokenizer_snapshot_sha256,
        )
        _validate_runtime_language("runtime-placeholder", self.language)
        if (
            _SAFE_VERSION.fullmatch(self.corpusgen_version) is None
            or _SAFE_VERSION.fullmatch(self.espeak_version) is None
        ):
            raise ValueError("DATG runtime versions must use the safe version grammar.")
        payload = _identity_payload(
            tokenizer=tokenizer,
            language=self.language,
            unit=self.unit,
            corpusgen_version=self.corpusgen_version,
            espeak_version=self.espeak_version,
        )
        if hashlib.sha256(_canonical(payload)).hexdigest() != self.cache_key_sha256:
            raise ValueError("DATG cache identity integrity check failed.")
        return self


class DatgUnitTokenSet(DatgModel):
    unit: str = Field(min_length=1, max_length=192)
    token_ids: tuple[int, ...] = Field(min_length=1, max_length=MAX_DATG_VOCABULARY)

    @model_validator(mode="after")
    def validate_tokens(self) -> Self:
        if not self.unit.strip() or tuple(sorted(set(self.token_ids))) != self.token_ids:
            raise ValueError("DATG unit token IDs must be sorted, unique, and non-empty.")
        if any(token_id < 0 or token_id > 10_000_000 for token_id in self.token_ids):
            raise ValueError("DATG token IDs must be nonnegative and bounded.")
        return self


class DatgIndexedToken(DatgModel):
    token_id: int = Field(ge=0, le=10_000_000)
    decoded_text: str = Field(max_length=512)
    units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_SEQUENCE_PHONEMES * 3)

    @model_validator(mode="after")
    def validate_units(self) -> Self:
        if tuple(sorted(set(self.units))) != self.units or any(
            not unit.strip() or len(unit) > 192 for unit in self.units
        ):
            raise ValueError("DATG token units must be sorted, unique, non-empty, and bounded.")
        return self


class DatgIndexArtifact(DatgModel):
    schema_id: Literal["corpuskit.datg-index.v1"] = "corpuskit.datg-index.v1"
    identity: DatgCacheIdentity
    vocabulary_size: int = Field(ge=1, le=MAX_DATG_VOCABULARY)
    indexed_token_count: int = Field(ge=0, le=MAX_DATG_VOCABULARY)
    unit_to_tokens: tuple[DatgUnitTokenSet, ...] = Field(max_length=MAX_DATG_TARGET_UNITS * 16)
    token_units: tuple[DatgIndexedToken, ...] = Field(max_length=MAX_DATG_VOCABULARY)
    content_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        identity: DatgCacheIdentity,
        vocabulary_size: int,
        unit_to_tokens: tuple[DatgUnitTokenSet, ...],
        token_units: tuple[DatgIndexedToken, ...],
    ) -> DatgIndexArtifact:
        payload = {
            "schema_id": "corpuskit.datg-index.v1",
            "identity": identity.model_dump(mode="json"),
            "vocabulary_size": vocabulary_size,
            "indexed_token_count": len(token_units),
            "unit_to_tokens": [item.model_dump(mode="json") for item in unit_to_tokens],
            "token_units": [item.model_dump(mode="json") for item in token_units],
        }
        return cls(**payload, content_sha256=hashlib.sha256(_canonical(payload)).hexdigest())

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.indexed_token_count != len(self.token_units):
            raise ValueError("DATG indexed token count is inconsistent.")
        if self.indexed_token_count > self.vocabulary_size:
            raise ValueError("DATG indexed token count exceeds the vocabulary.")
        unit_names = tuple(item.unit for item in self.unit_to_tokens)
        token_ids = tuple(item.token_id for item in self.token_units)
        if tuple(sorted(set(unit_names))) != unit_names:
            raise ValueError("DATG indexed units must be sorted and unique.")
        if tuple(sorted(set(token_ids))) != token_ids:
            raise ValueError("DATG indexed token records must be sorted and unique.")
        _validate_unit_level(self.identity.unit, unit_names)
        _validate_unit_level(
            self.identity.unit,
            tuple(unit for token in self.token_units for unit in token.units),
        )
        token_unit_names = tuple(
            sorted({unit for token in self.token_units for unit in token.units})
        )
        if token_unit_names != unit_names:
            raise ValueError("DATG bidirectional index units are inconsistent.")
        expected = {
            unit: tuple(token.token_id for token in self.token_units if unit in token.units)
            for unit in unit_names
        }
        if expected != {item.unit: item.token_ids for item in self.unit_to_tokens}:
            raise ValueError("DATG bidirectional index mappings are inconsistent.")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        encoded = _canonical(payload)
        if len(encoded) > MAX_DATG_INDEX_BYTES:
            raise ValueError("DATG index artifacts exceed the application size limit.")
        if hashlib.sha256(encoded).hexdigest() != self.content_sha256:
            raise ValueError("DATG index artifact integrity check failed.")
        return self


class DatgIndexBuildResult(DatgModel):
    schema_id: Literal["corpuskit.datg-index-build-result.v1"] = (
        "corpuskit.datg-index-build-result.v1"
    )
    artifact: DatgIndexArtifact
    elapsed_seconds: float = Field(ge=0.0, le=MAX_DATG_ACTIVITY_SECONDS + 1.0)


class DatgIndexPublication(DatgModel):
    """Tenant-scoped catalog entry for one parent-verified reusable index."""

    schema_id: Literal["corpuskit.datg-index-publication.v1"] = (
        "corpuskit.datg-index-publication.v1"
    )
    build_run_id: UUID
    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_id: str = Field(min_length=2, max_length=64)
    language: str = Field(min_length=2, max_length=32)
    unit: DatgUnit
    vocabulary_size: int = Field(ge=1, le=MAX_DATG_VOCABULARY)
    indexed_token_count: int = Field(ge=0, le=MAX_DATG_VOCABULARY)
    size_bytes: int = Field(ge=1, le=MAX_DATG_INDEX_BYTES)
    created_at: datetime

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        _validate_runtime_language(self.runtime_id, self.language)
        if self.indexed_token_count > self.vocabulary_size:
            raise ValueError("DATG publication counts are inconsistent.")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("DATG publication timestamps require an offset.")
        return self


class DatgTokenMatch(DatgModel):
    token_id: int = Field(ge=0, le=10_000_000)
    decoded_text: str = Field(max_length=512)
    units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_SEQUENCE_PHONEMES * 3)

    @model_validator(mode="after")
    def validate_units(self) -> Self:
        if tuple(sorted(set(self.units))) != self.units or any(
            not unit.strip() or len(unit) > 192 for unit in self.units
        ):
            raise ValueError(
                "DATG inspection token units must be sorted, unique, non-empty, and bounded."
            )
        return self


class DatgInspectionResult(DatgModel):
    schema_id: Literal["corpuskit.datg-inspection.v1"] = "corpuskit.datg-inspection.v1"
    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_INSPECTION_RESULTS)
    matches: tuple[DatgTokenMatch, ...] = Field(max_length=MAX_DATG_INSPECTION_RESULTS)
    total_matches: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if tuple(sorted(set(self.token_ids))) != self.token_ids:
            raise ValueError("DATG inspection token IDs must be sorted and unique.")
        if tuple(item.token_id for item in self.matches) != self.token_ids:
            raise ValueError("DATG inspection matches must align with token IDs.")
        if self.total_matches < len(self.token_ids):
            raise ValueError("DATG inspection total cannot be smaller than returned matches.")
        if self.truncated != (self.total_matches > len(self.token_ids)):
            raise ValueError("DATG inspection truncation state is inconsistent.")
        return self


class DatgTargetInspectionRequest(DatgModel):
    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_UNITS)
    max_results: int = Field(default=100, ge=1, le=MAX_DATG_INSPECTION_RESULTS)


class DatgCoveredInspectionRequest(DatgModel):
    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_UNITS)
    max_results: int = Field(default=100, ge=1, le=MAX_DATG_INSPECTION_RESULTS)


class DatgUnitFrequency(DatgModel):
    unit: str = Field(min_length=1, max_length=192)
    count: int = Field(ge=0, le=1_000_000_000)


class DatgFrequencyInspectionRequest(DatgModel):
    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_counts: tuple[DatgUnitFrequency, ...] = Field(
        min_length=1,
        max_length=MAX_DATG_TARGET_UNITS,
    )
    threshold: int = Field(ge=0, le=1_000_000_000)
    max_results: int = Field(default=100, ge=1, le=MAX_DATG_INSPECTION_RESULTS)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        units = tuple(item.unit for item in self.unit_counts)
        if len(units) != len(set(units)):
            raise ValueError("DATG frequency units must be unique.")
        return self


class DatgGuidanceOptions(DatgModel):
    boost_strength: float = Field(default=5.0, ge=0.0, le=100.0)
    penalty_strength: float = Field(default=-5.0, ge=-100.0, le=0.0)
    anti_attribute_mode: DatgAntiMode = DatgAntiMode.COVERED
    frequency_threshold: int = Field(default=10, ge=0, le=1_000_000_000)


class DatgPhonemeSequence(DatgModel):
    phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_SEQUENCE_PHONEMES)

    @model_validator(mode="after")
    def validate_phonemes(self) -> Self:
        if any(not item.strip() or len(item) > 64 for item in self.phonemes):
            raise ValueError("DATG phonemes must be non-empty and bounded.")
        return self


class DatgGuidedGenerationRequest(DatgModel):
    runtime_id: str = Field(min_length=2, max_length=64)
    index_cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: DatgUnit = DatgUnit.PHONEME
    target_phonemes: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_DATG_TARGET_PHONEMES,
    )
    target_units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_UNITS)
    coverage_sequences: tuple[DatgPhonemeSequence, ...] = Field(
        default=(),
        max_length=MAX_DATG_COVERAGE_SEQUENCES,
    )
    guidance: DatgGuidanceOptions = DatgGuidanceOptions()
    quantization: DatgQuantization = DatgQuantization.NONE
    candidates: int = Field(default=3, ge=1, le=MAX_DATG_CANDIDATES)
    max_new_tokens: int = Field(default=128, ge=1, le=MAX_DATG_NEW_TOKENS)
    temperature: float = Field(default=0.8, gt=0.0, le=2.0)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    do_sample: bool = False
    seed: int = Field(default=0, ge=0, le=4_294_967_295)
    activity_timeout_seconds: float = Field(
        default=300.0,
        gt=0.0,
        le=MAX_DATG_ACTIVITY_SECONDS,
    )

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_runtime_language(self.runtime_id, self.language)
        if len(set(self.target_phonemes)) != len(self.target_phonemes):
            raise ValueError("DATG target phonemes must be unique.")
        if any(not item.strip() or len(item) > 64 for item in self.target_phonemes):
            raise ValueError("DATG target phonemes must be non-empty and bounded.")
        if len(set(self.target_units)) != len(self.target_units):
            raise ValueError("DATG target units must be unique.")
        _validate_unit_level(self.unit, self.target_units)
        return self


class DatgGuidanceManifest(DatgModel):
    schema_id: Literal["corpuskit.datg-guidance-manifest.v1"] = (
        "corpuskit.datg-guidance-manifest.v1"
    )
    runtime_id: str = Field(min_length=2, max_length=64)
    model_id: str = Field(min_length=3, max_length=192)
    model_revision: str = Field(min_length=40, max_length=40)
    model_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokenizer_id: str = Field(min_length=3, max_length=192)
    tokenizer_revision: str = Field(min_length=40, max_length=40)
    tokenizer_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str = Field(min_length=2, max_length=32)
    unit: DatgUnit
    guidance: DatgGuidanceOptions
    quantization: DatgQuantization
    seed: int
    sampling_enabled: bool
    local_files_only: Literal[True] = True
    trust_remote_code: Literal[False] = False
    safetensors_only: Literal[True] = True
    worker_profile: Literal["gpu-inference"] = "gpu-inference"
    reproducibility: Literal["best_effort"] = "best_effort"
    corpusgen_version: str
    espeak_version: str

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        _validate_runtime_language(self.runtime_id, self.language)
        DatgSnapshotPin(
            repository_id=self.model_id,
            revision=self.model_revision,
            snapshot_sha256=self.model_snapshot_sha256,
        )
        DatgSnapshotPin(
            repository_id=self.tokenizer_id,
            revision=self.tokenizer_revision,
            snapshot_sha256=self.tokenizer_snapshot_sha256,
        )
        if (
            _SAFE_VERSION.fullmatch(self.corpusgen_version) is None
            or _SAFE_VERSION.fullmatch(self.espeak_version) is None
        ):
            raise ValueError("DATG manifest versions must use the safe version grammar.")
        return self


class DatgGeneratedCandidate(DatgModel):
    source_id: str = Field(pattern=r"^datg:[0-9a-f]{48}$")
    text: str = Field(min_length=1, max_length=4_000)
    phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_SEQUENCE_PHONEMES)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        if not self.text.strip() or any(
            not phoneme.strip() or len(phoneme) > 64 for phoneme in self.phonemes
        ):
            raise ValueError("DATG generated candidate content must be non-empty and bounded.")
        return self


class DatgGuidedGenerationResult(DatgModel):
    schema_id: Literal["corpuskit.datg-guided-generation-result.v1"] = (
        "corpuskit.datg-guided-generation-result.v1"
    )
    manifest: DatgGuidanceManifest
    candidates: tuple[DatgGeneratedCandidate, ...] = Field(
        min_length=1,
        max_length=MAX_DATG_CANDIDATES,
    )
    attribute_token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_VOCABULARY)
    anti_attribute_token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_VOCABULARY)
    elapsed_seconds: float = Field(ge=0.0, le=MAX_DATG_ACTIVITY_SECONDS + 1.0)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        source_ids = tuple(candidate.source_id for candidate in self.candidates)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("DATG generated candidate source IDs must be unique.")
        for token_ids in (self.attribute_token_ids, self.anti_attribute_token_ids):
            if tuple(sorted(set(token_ids))) != token_ids or any(
                token_id < 0 or token_id > 10_000_000 for token_id in token_ids
            ):
                raise ValueError("DATG guided token IDs must be sorted, unique, and bounded.")
        return self


class DatgRuntimeValidationResult(DatgModel):
    schema_id: Literal["corpuskit.datg-runtime-validation.v1"] = (
        "corpuskit.datg-runtime-validation.v1"
    )
    valid: Literal[True] = True
    operation: Literal["build_index", "guided_generation"]
    runtime_id: str
    worker_only: Literal[True] = True
    network_during_validation: Literal[False] = False
    required_deployment_profile: Literal["batch-cpu", "gpu-inference"]
    activity_timeout_seconds: float


class DatgLogitPreviewRequest(DatgModel):
    artifact: DatgIndexArtifact
    target_phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_PHONEMES)
    target_units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_UNITS)
    coverage_sequences: tuple[DatgPhonemeSequence, ...] = Field(
        default=(), max_length=MAX_DATG_COVERAGE_SEQUENCES
    )
    guidance: DatgGuidanceOptions = DatgGuidanceOptions()
    logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_DATG_LOGIT_BATCH,
    )

    @model_validator(mode="after")
    def validate_logits(self) -> Self:
        _validate_logit_matrix(self.logits)
        _validate_preview_targets(self.target_phonemes, self.target_units)
        _validate_unit_level(self.artifact.identity.unit, self.target_units)
        return self


class DatgLogitPreviewResult(DatgModel):
    schema_id: Literal["corpuskit.datg-logit-preview.v1"] = "corpuskit.datg-logit-preview.v1"
    original_logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1, max_length=MAX_DATG_LOGIT_BATCH
    )
    modified_logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1, max_length=MAX_DATG_LOGIT_BATCH
    )
    attribute_token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_LOGIT_VOCABULARY)
    anti_attribute_token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_LOGIT_VOCABULARY)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        shape = _validate_logit_matrix(self.original_logits)
        if _validate_logit_matrix(self.modified_logits) != shape:
            raise ValueError("DATG original and modified logits require the same shape.")
        _validate_preview_token_ids(
            self.attribute_token_ids,
            self.anti_attribute_token_ids,
            vocabulary_width=shape[1],
        )
        return self


class DatgLogitDeltaPreviewRequest(DatgModel):
    """Public cache-key request; callers never provide an index artifact."""

    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_PHONEMES)
    target_units: tuple[str, ...] = Field(min_length=1, max_length=MAX_DATG_TARGET_UNITS)
    coverage_sequences: tuple[DatgPhonemeSequence, ...] = Field(
        default=(), max_length=MAX_DATG_COVERAGE_SEQUENCES
    )
    guidance: DatgGuidanceOptions = DatgGuidanceOptions()
    logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_DATG_LOGIT_BATCH,
    )

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_logit_matrix(self.logits)
        _validate_preview_targets(self.target_phonemes, self.target_units)
        return self


class DatgLogitDeltaPreviewResult(DatgModel):
    schema_id: Literal["corpuskit.datg-logit-delta-preview.v1"] = (
        "corpuskit.datg-logit-delta-preview.v1"
    )
    cache_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1, max_length=MAX_DATG_LOGIT_BATCH
    )
    delta_logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1, max_length=MAX_DATG_LOGIT_BATCH
    )
    modified_logits: tuple[tuple[float, ...], ...] = Field(
        min_length=1, max_length=MAX_DATG_LOGIT_BATCH
    )
    attribute_token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_LOGIT_VOCABULARY)
    anti_attribute_token_ids: tuple[int, ...] = Field(max_length=MAX_DATG_LOGIT_VOCABULARY)
    generation_executed: Literal[False] = False
    model_loaded: Literal[False] = False
    network_used: Literal[False] = False

    @classmethod
    def from_preview(
        cls,
        *,
        cache_key_sha256: str,
        preview: DatgLogitPreviewResult,
    ) -> DatgLogitDeltaPreviewResult:
        deltas = tuple(
            tuple(modified - original for original, modified in zip(before, after, strict=True))
            for before, after in zip(
                preview.original_logits,
                preview.modified_logits,
                strict=True,
            )
        )
        return cls(
            cache_key_sha256=cache_key_sha256,
            original_logits=preview.original_logits,
            delta_logits=deltas,
            modified_logits=preview.modified_logits,
            attribute_token_ids=preview.attribute_token_ids,
            anti_attribute_token_ids=preview.anti_attribute_token_ids,
        )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        shape = _validate_logit_matrix(self.original_logits)
        if (
            _validate_logit_matrix(self.delta_logits) != shape
            or _validate_logit_matrix(self.modified_logits) != shape
        ):
            raise ValueError("DATG before, delta, and after logits require the same shape.")
        for before, deltas, after in zip(
            self.original_logits,
            self.delta_logits,
            self.modified_logits,
            strict=True,
        ):
            for original, delta, modified in zip(before, deltas, after, strict=True):
                if modified - original != delta:
                    raise ValueError("DATG logit deltas must exactly match after minus before.")
        _validate_preview_token_ids(
            self.attribute_token_ids,
            self.anti_attribute_token_ids,
            vocabulary_width=shape[1],
        )
        return self


def _identity_payload(
    *,
    tokenizer: DatgSnapshotPin,
    language: str,
    unit: DatgUnit,
    corpusgen_version: str,
    espeak_version: str,
) -> dict[str, object]:
    return {
        "schema_id": "corpuskit.datg-index-cache-key.v1",
        "tokenizer_id": tokenizer.repository_id,
        "tokenizer_revision": tokenizer.revision,
        "tokenizer_snapshot_sha256": tokenizer.snapshot_sha256,
        "language": language,
        "unit": unit.value,
        "corpusgen_version": corpusgen_version,
        "espeak_version": espeak_version,
    }


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _validate_runtime_language(runtime_id: str, language: str) -> None:
    if _SAFE_RUNTIME_ID.fullmatch(runtime_id) is None:
        raise ValueError("DATG runtime IDs must use the safe identifier grammar.")
    if _SAFE_LANGUAGE.fullmatch(language) is None:
        raise ValueError("DATG language tags must use the supported grammar.")


def _validate_unit_level(unit: DatgUnit, values: tuple[str, ...]) -> None:
    expected_hyphens = {
        DatgUnit.PHONEME: 0,
        DatgUnit.DIPHONE: 1,
        DatgUnit.TRIPHONE: 2,
    }[unit]
    if any(
        not value.strip() or len(value) > 192 or value.count("-") != expected_hyphens
        for value in values
    ):
        raise ValueError("DATG phonetic units must match the declared unit level.")


def _validate_logit_matrix(
    logits: tuple[tuple[float, ...], ...],
) -> tuple[int, int]:
    widths = {len(row) for row in logits}
    if len(widths) != 1 or not widths or next(iter(widths)) < 1:
        raise ValueError("DATG logit rows require one non-empty shared width.")
    width = next(iter(widths))
    if width > MAX_DATG_LOGIT_VOCABULARY:
        raise ValueError("DATG logit previews exceed the vocabulary bound.")
    return (len(logits), width)


def _validate_preview_targets(
    target_phonemes: tuple[str, ...],
    target_units: tuple[str, ...],
) -> None:
    if len(set(target_phonemes)) != len(target_phonemes) or any(
        not item.strip() or len(item) > 64 for item in target_phonemes
    ):
        raise ValueError("DATG preview target phonemes must be unique, non-empty, and bounded.")
    if len(set(target_units)) != len(target_units) or any(
        not item.strip() or len(item) > 192 for item in target_units
    ):
        raise ValueError("DATG preview target units must be unique, non-empty, and bounded.")


def _validate_preview_token_ids(
    attribute: tuple[int, ...],
    anti_attribute: tuple[int, ...],
    *,
    vocabulary_width: int,
) -> None:
    for token_ids in (attribute, anti_attribute):
        if tuple(sorted(set(token_ids))) != token_ids or any(
            token_id < 0 or token_id >= vocabulary_width for token_id in token_ids
        ):
            raise ValueError(
                "DATG preview token IDs must be sorted, unique, and inside the logit width."
            )


__all__ = [
    "MAX_DATG_ACTIVITY_SECONDS",
    "MAX_DATG_INDEX_BYTES",
    "MAX_DATG_VOCABULARY",
    "DatgAntiMode",
    "DatgCacheIdentity",
    "DatgCoveredInspectionRequest",
    "DatgFrequencyInspectionRequest",
    "DatgGeneratedCandidate",
    "DatgGuidanceManifest",
    "DatgGuidanceOptions",
    "DatgGuidedGenerationRequest",
    "DatgGuidedGenerationResult",
    "DatgIndexArtifact",
    "DatgIndexBuildRequest",
    "DatgIndexBuildResult",
    "DatgIndexPublication",
    "DatgIndexedToken",
    "DatgInspectionResult",
    "DatgLogitDeltaPreviewRequest",
    "DatgLogitDeltaPreviewResult",
    "DatgLogitPreviewRequest",
    "DatgLogitPreviewResult",
    "DatgModel",
    "DatgPhonemeSequence",
    "DatgQuantization",
    "DatgRuntimePolicyEntry",
    "DatgRuntimeValidationResult",
    "DatgSnapshotPin",
    "DatgTargetInspectionRequest",
    "DatgTokenMatch",
    "DatgUnit",
    "DatgUnitFrequency",
    "DatgUnitTokenSet",
    "DatgWorkerProfile",
]
