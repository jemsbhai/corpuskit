"""Stable, bounded contracts for repository generation and candidate scoring."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from corpuskit.domain.corpus import CoverageUnit, FrozenDomainModel

MAX_SOURCE_ITEMS = 1_000
MAX_SYNC_SOURCE_ITEMS = 250
MAX_CANDIDATES_PER_ITERATION = 32
MAX_GENERATION_ITERATIONS = 100
MAX_GENERATION_SECONDS = 30.0
MAX_ACTIVITY_SECONDS = 300.0
MAX_SENTENCE_CHARACTERS = 4_000
MAX_PHONEMES_PER_SENTENCE = 1_000
MAX_TARGET_PHONEMES = 64
MAX_TARGET_UNITS = 4_096
MAX_ARTIFACT_BYTES = 1_048_576

_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,191}$", re.ASCII)
_SAFE_HF_DATASET = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
    re.ASCII,
)
_SAFE_HF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
_IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$", re.ASCII)


class GenerationDomainModel(FrozenDomainModel):
    """Forbid non-finite floats in every JSON-facing generation DTO."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class GenerationSourceKind(StrEnum):
    """Supported repository construction modes."""

    PREPHONEMIZED = "prephonemized"
    RAW_TEXT = "raw_text"
    HUGGING_FACE = "hugging_face"


class GenerationExecutionMode(StrEnum):
    """Truthful execution context for one bounded run."""

    SYNCHRONOUS_PREVIEW = "synchronous_preview"
    WORKER_ACTIVITY = "worker_activity"


class GenerationPhase(StrEnum):
    """Stable phases emitted by the pure job handler."""

    VALIDATING = "validating"
    PREPARING_REPOSITORY = "preparing_repository"
    GENERATING = "generating"
    CANDIDATE_ACCEPTED = "candidate_accepted"
    FINISHED = "finished"
    FAILED = "failed"


class GenerationStopReason(StrEnum):
    """Normalized finite reasons returned by CorpusGen's loop."""

    TARGET_COVERAGE = "target_coverage"
    MAX_SENTENCES = "max_sentences"
    MAX_ITERATIONS = "max_iterations"
    TIMEOUT = "timeout"
    BACKEND_EXHAUSTED = "backend_exhausted"


class ReadabilityStatus(StrEnum):
    """Whether Flesch readability has a meaningful value."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class NgramScorerMode(StrEnum):
    """CorpusGen n-gram scorer construction modes."""

    INVENTORY_DERIVED = "inventory_derived"
    CORPUS_TRAINED = "corpus_trained"


class PhonotacticArtifactType(StrEnum):
    """Discriminator for the two distinct CorpusGen n-gram APIs."""

    NGRAM_SCORER = "corpusgen.ngram-phonotactic-scorer"
    NGRAM_CONSTRAINT = "corpusgen.ngram-phonotactic-constraint"


class PhonemeSequence(GenerationDomainModel):
    """One bounded, non-empty phoneme sequence."""

    phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_PHONEMES_PER_SENTENCE)

    @model_validator(mode="after")
    def validate_symbols(self) -> Self:
        if any(not value.strip() or len(value) > 64 for value in self.phonemes):
            raise ValueError("Phoneme symbols must be non-empty and at most 64 characters.")
        return self


class RepositoryCandidate(PhonemeSequence):
    """Pre-phonemized repository row with stable external provenance."""

    source_id: str = Field(min_length=1, max_length=192)
    text: str = Field(min_length=1, max_length=MAX_SENTENCE_CHARACTERS)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        if _SAFE_SOURCE_ID.fullmatch(self.source_id) is None or not self.text.strip():
            raise ValueError("Repository source identifiers and text must be safe and non-empty.")
        return self


class RawTextCandidate(GenerationDomainModel):
    """Raw repository row that will be phonemized locally by eSpeak."""

    source_id: str = Field(min_length=1, max_length=192)
    text: str = Field(min_length=1, max_length=MAX_SENTENCE_CHARACTERS)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        if _SAFE_SOURCE_ID.fullmatch(self.source_id) is None or not self.text.strip():
            raise ValueError("Repository source identifiers and text must be safe and non-empty.")
        return self


class HuggingFaceRepositorySpec(GenerationDomainModel):
    """Immutable and allowlist-ready Hugging Face dataset import manifest."""

    dataset: str = Field(min_length=3, max_length=192)
    config: str = Field(min_length=1, max_length=128)
    split: str = Field(min_length=1, max_length=128)
    text_column: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=40, max_length=40)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    max_samples: int = Field(default=1_000, ge=1, le=MAX_SOURCE_ITEMS)
    trust_remote_code: Literal[False] = False

    @model_validator(mode="after")
    def validate_hub_identifiers(self) -> Self:
        if _SAFE_HF_DATASET.fullmatch(self.dataset) is None:
            raise ValueError("Dataset must be a namespaced Hugging Face identifier.")
        for value in (self.config, self.split, self.text_column):
            if _SAFE_HF_NAME.fullmatch(value) is None:
                raise ValueError("Dataset config, split, and text column must be safe identifiers.")
        if _IMMUTABLE_REVISION.fullmatch(self.revision) is None:
            raise ValueError("Dataset revision must be a lowercase 40-character commit SHA.")
        return self


class PrephonemizedRepository(GenerationDomainModel):
    kind: Literal[GenerationSourceKind.PREPHONEMIZED] = GenerationSourceKind.PREPHONEMIZED
    entries: tuple[RepositoryCandidate, ...] = Field(min_length=1, max_length=MAX_SOURCE_ITEMS)

    @model_validator(mode="after")
    def validate_source_ids(self) -> Self:
        _require_unique_source_ids(tuple(item.source_id for item in self.entries))
        return self


class RawTextRepository(GenerationDomainModel):
    kind: Literal[GenerationSourceKind.RAW_TEXT] = GenerationSourceKind.RAW_TEXT
    entries: tuple[RawTextCandidate, ...] = Field(min_length=1, max_length=MAX_SOURCE_ITEMS)
    language: str = Field(default="en-us", min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_source_ids(self) -> Self:
        _require_unique_source_ids(tuple(item.source_id for item in self.entries))
        return self


class HuggingFaceRepository(GenerationDomainModel):
    kind: Literal[GenerationSourceKind.HUGGING_FACE] = GenerationSourceKind.HUGGING_FACE
    spec: HuggingFaceRepositorySpec


RepositorySource = Annotated[
    PrephonemizedRepository | RawTextRepository | HuggingFaceRepository,
    Field(discriminator="kind"),
]


class GenerationTarget(GenerationDomainModel):
    """Explicit bounded target inventory for repository generation."""

    phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_TARGET_PHONEMES)
    unit: CoverageUnit = CoverageUnit.PHONEME

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if len(set(self.phonemes)) != len(self.phonemes):
            raise ValueError("Target phonemes must be unique.")
        if any(not value.strip() or len(value) > 64 for value in self.phonemes):
            raise ValueError("Target phonemes must be non-empty and at most 64 characters.")
        target_size = (
            len(self.phonemes)
            ** {
                CoverageUnit.PHONEME: 1,
                CoverageUnit.DIPHONE: 2,
                CoverageUnit.TRIPHONE: 3,
            }[self.unit]
        )
        if target_size > MAX_TARGET_UNITS:
            raise ValueError("The expanded target space exceeds the synchronous safety limit.")
        return self


class GenerationStoppingCriteria(GenerationDomainModel):
    """Stop when any criterion is met, with at least one finite safety cap."""

    target_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    max_sentences: int | None = Field(default=50, ge=1, le=250)
    max_iterations: int | None = Field(default=25, ge=1, le=MAX_GENERATION_ITERATIONS)
    timeout_seconds: float | None = Field(default=5.0, gt=0.0, le=MAX_GENERATION_SECONDS)

    @model_validator(mode="after")
    def require_safety_stop(self) -> Self:
        if all(
            value is None
            for value in (self.max_sentences, self.max_iterations, self.timeout_seconds)
        ):
            raise ValueError("At least one finite sentence, iteration, or timeout cap is required.")
        return self


class ScoreWeights(GenerationDomainModel):
    """Composite score weights; model-backed fluency is authorized by its caller."""

    coverage: float = Field(default=1.0, ge=0.0, le=1_000.0)
    phonotactic: float = Field(default=0.0, ge=0.0, le=1_000.0)
    readability: float = Field(default=0.0, ge=0.0, le=1_000.0)
    fluency: float = Field(default=0.0, ge=0.0, le=1_000.0)

    @model_validator(mode="after")
    def require_component(self) -> Self:
        if self.coverage == self.phonotactic == self.readability == self.fluency == 0:
            raise ValueError("At least one score component must be enabled.")
        return self


class ReadabilityRange(GenerationDomainModel):
    minimum: float = Field(ge=0.0, le=100.0)
    maximum: float = Field(ge=0.0, le=100.0)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("Readability minimum must not exceed maximum.")
        return self


class PhonotacticArtifact(GenerationDomainModel):
    """Versioned JSON artifact with an integrity digest."""

    artifact_type: PhonotacticArtifactType
    schema_version: Literal[1] = 1
    payload: dict[str, JsonValue]
    content_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        artifact_type: PhonotacticArtifactType,
        payload: dict[str, JsonValue],
    ) -> PhonotacticArtifact:
        canonical = _canonical_payload(payload)
        return cls(
            artifact_type=artifact_type,
            payload=payload,
            content_sha256=hashlib.sha256(canonical).hexdigest(),
        )

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        canonical = _canonical_payload(self.payload)
        if len(canonical) > MAX_ARTIFACT_BYTES:
            raise ValueError("Phonotactic artifacts may not exceed one MiB.")
        if not hashlib.sha256(canonical).hexdigest() == self.content_sha256:
            raise ValueError("Phonotactic artifact integrity check failed.")
        return self


class GenerationScoringOptions(GenerationDomainModel):
    weights: ScoreWeights = ScoreWeights()
    phonotactic_artifact: PhonotacticArtifact | None = None
    readability_target: ReadabilityRange | None = None
    readability_filter: ReadabilityRange | None = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if self.weights.phonotactic > 0 and self.phonotactic_artifact is None:
            raise ValueError("A phonotactic artifact is required when its weight is non-zero.")
        if self.weights.readability == 0 and (
            self.readability_target is not None or self.readability_filter is not None
        ):
            raise ValueError("Readability options require a non-zero readability weight.")
        return self


class RepositoryGenerationRequest(GenerationDomainModel):
    """One fully bounded repository generation run specification."""

    source: RepositorySource
    target: GenerationTarget
    stopping: GenerationStoppingCriteria = GenerationStoppingCriteria()
    scoring: GenerationScoringOptions = GenerationScoringOptions()
    candidates_per_iteration: int = Field(default=5, ge=1, le=MAX_CANDIDATES_PER_ITERATION)
    activity_timeout_seconds: float = Field(default=30.0, gt=0.0, le=MAX_ACTIVITY_SECONDS)

    @model_validator(mode="after")
    def reject_unbound_fluency(self) -> Self:
        if self.scoring.weights.fluency > 0:
            raise ValueError(
                "Repository generation cannot use fluency without a durable local-model policy."
            )
        return self


class RepositoryGenerationValidation(GenerationDomainModel):
    """Sanitized control-plane acknowledgement; it never executes or downloads a dataset."""

    schema_id: Literal["corpuskit.repository-generation-validation.v1"] = (
        "corpuskit.repository-generation-validation.v1"
    )
    operation: Literal["repository_generation"] = "repository_generation"
    valid: Literal[True] = True
    worker_only: Literal[True] = True
    network_during_validation: Literal[False] = False
    source_kind: GenerationSourceKind
    source_item_limit: int = Field(ge=1, le=MAX_SOURCE_ITEMS)
    activity_timeout_seconds: float = Field(gt=0.0, le=MAX_ACTIVITY_SECONDS)


class AcceptedCandidate(RepositoryCandidate):
    iteration: int = Field(ge=1)
    coverage_gain: int = Field(ge=1)


class GenerationProgress(GenerationDomainModel):
    """Versioned state-machine event suitable for durable activity heartbeats."""

    schema_version: Literal[1] = 1
    sequence: int = Field(ge=0)
    phase: GenerationPhase
    iteration: int = Field(default=0, ge=0)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    accepted_count: int = Field(default=0, ge=0)
    accepted_source_id: str | None = None
    coverage_gain: int | None = Field(default=None, ge=1)
    stop_reason: GenerationStopReason | None = None


class RepositoryGenerationResult(GenerationDomainModel):
    """Stable result manifest; it does not imply external durable-job completion."""

    schema_id: Literal["corpuskit.repository-generation-result.v1"] = (
        "corpuskit.repository-generation-result.v1"
    )
    execution_mode: GenerationExecutionMode
    source_kind: GenerationSourceKind
    unit: CoverageUnit
    backend: Literal["repository"] = "repository"
    accepted: tuple[AcceptedCandidate, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    iterations: int = Field(ge=0, le=MAX_GENERATION_ITERATIONS)
    elapsed_seconds: float = Field(ge=0.0, le=MAX_GENERATION_SECONDS + 1.0)
    stop_reason: GenerationStopReason


class ScoringState(GenerationDomainModel):
    """Immutable application state passed between atomic scoring requests."""

    covered_sequences: tuple[PhonemeSequence, ...] = Field(default=(), max_length=MAX_SOURCE_ITEMS)
    accepted_source_ids: tuple[str, ...] = Field(default=(), max_length=MAX_SOURCE_ITEMS)

    @model_validator(mode="after")
    def validate_source_ids(self) -> Self:
        _require_unique_source_ids(self.accepted_source_ids)
        return self


class CompositeScoringRequest(GenerationDomainModel):
    target: GenerationTarget
    candidates: tuple[RepositoryCandidate, ...] = Field(min_length=1, max_length=250)
    state: ScoringState = Field(default_factory=ScoringState)
    options: GenerationScoringOptions = GenerationScoringOptions()
    top_k: int | None = Field(default=None, ge=1, le=250)
    commit_source_id: str | None = Field(default=None, max_length=192)

    @model_validator(mode="after")
    def validate_commit(self) -> Self:
        source_ids = tuple(item.source_id for item in self.candidates)
        _require_unique_source_ids(source_ids)
        if self.top_k is not None and self.top_k > len(self.candidates):
            raise ValueError("top_k may not exceed the candidate count.")
        if self.commit_source_id is not None:
            if self.commit_source_id not in source_ids:
                raise ValueError("The committed source must be present in candidates.")
            if self.commit_source_id in self.state.accepted_source_ids:
                raise ValueError("A source may not be accepted more than once.")
        return self


class CandidateScore(GenerationDomainModel):
    source_id: str
    text: str
    phonemes: tuple[str, ...]
    coverage_gain: int = Field(ge=0)
    weighted_coverage_gain: float = Field(ge=0.0)
    phonotactic_score: float = Field(ge=0.0, le=1.0)
    fluency_score: float = Field(default=0.0, ge=0.0, le=1.0)
    readability_status: ReadabilityStatus
    readability_score: float | None = Field(default=None, ge=0.0, le=1.0)
    composite_score: float = Field(ge=0.0)
    new_units: tuple[str, ...]


class CompositeScoringResult(GenerationDomainModel):
    schema_id: Literal["corpuskit.composite-scoring-result.v1"] = (
        "corpuskit.composite-scoring-result.v1"
    )
    ranked: tuple[CandidateScore, ...]
    committed: CandidateScore | None
    state_before: ScoringState
    state_after: ScoringState
    covered_units_before: tuple[str, ...]
    covered_units_after: tuple[str, ...]


class NgramScorerTrainingRequest(GenerationDomainModel):
    mode: NgramScorerMode
    n: int = Field(default=2, ge=2, le=5)
    phonemes: tuple[str, ...] = Field(default=(), max_length=MAX_TARGET_PHONEMES)
    sequences: tuple[PhonemeSequence, ...] = Field(default=(), max_length=MAX_SOURCE_ITEMS)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode is NgramScorerMode.INVENTORY_DERIVED:
            if len(set(self.phonemes)) < 2 or self.sequences:
                raise ValueError("Inventory mode requires at least two unique phonemes only.")
            if len(set(self.phonemes)) != len(self.phonemes) or any(
                not value.strip() or len(value) > 64 for value in self.phonemes
            ):
                raise ValueError("Inventory phonemes must be unique, non-empty, and bounded.")
        elif not self.sequences or self.phonemes:
            raise ValueError("Corpus mode requires phoneme sequences only.")
        return self


class NgramConstraintTrainingRequest(GenerationDomainModel):
    order: int = Field(default=2, ge=1, le=5)
    smoothing: float = Field(default=0.01, gt=0.0, le=1.0)
    sequences: tuple[PhonemeSequence, ...] = Field(default=(), max_length=MAX_SOURCE_ITEMS)
    texts: tuple[str, ...] = Field(default=(), max_length=MAX_SOURCE_ITEMS)
    language: str = Field(default="en-us", min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_training_source(self) -> Self:
        if bool(self.sequences) == bool(self.texts):
            raise ValueError("Provide exactly one of phoneme sequences or raw texts.")
        if any(not text.strip() or len(text) > MAX_SENTENCE_CHARACTERS for text in self.texts):
            raise ValueError("Training text must be non-empty and bounded.")
        return self


class PhonotacticScoreRequest(GenerationDomainModel):
    artifact: PhonotacticArtifact
    sequences: tuple[PhonemeSequence, ...] = Field(min_length=1, max_length=250)


class PhonotacticScoreResult(GenerationDomainModel):
    artifact_type: PhonotacticArtifactType
    scores: tuple[float, ...]

    @model_validator(mode="after")
    def validate_scores(self) -> Self:
        if any(not math.isfinite(score) or not 0.0 <= score <= 1.0 for score in self.scores):
            raise ValueError("Phonotactic scores must be finite values in [0, 1].")
        return self


class ReadabilityRequest(GenerationDomainModel):
    texts: tuple[str, ...] = Field(min_length=1, max_length=250)
    target_range: ReadabilityRange | None = None
    filter_range: ReadabilityRange | None = None

    @model_validator(mode="after")
    def validate_texts(self) -> Self:
        if any(len(text) > MAX_SENTENCE_CHARACTERS for text in self.texts):
            raise ValueError("Readability inputs must be bounded.")
        return self


class ReadabilityResult(GenerationDomainModel):
    text: str
    status: ReadabilityStatus
    flesch_reading_ease: float | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    accepted_by_filter: bool | None = None


class ReadabilityBatchResult(GenerationDomainModel):
    results: tuple[ReadabilityResult, ...]


def _require_unique_source_ids(source_ids: tuple[str, ...]) -> None:
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Repository source identifiers must be unique.")
    if any(_SAFE_SOURCE_ID.fullmatch(value) is None for value in source_ids):
        raise ValueError("Repository source identifiers must use the safe identifier grammar.")


def _canonical_payload(payload: dict[str, JsonValue]) -> bytes:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("Phonotactic artifact payload must be JSON-safe.") from None


__all__ = [
    "AcceptedCandidate",
    "CandidateScore",
    "CompositeScoringRequest",
    "CompositeScoringResult",
    "GenerationExecutionMode",
    "GenerationPhase",
    "GenerationProgress",
    "GenerationScoringOptions",
    "GenerationSourceKind",
    "GenerationStopReason",
    "GenerationStoppingCriteria",
    "GenerationTarget",
    "HuggingFaceRepository",
    "HuggingFaceRepositorySpec",
    "NgramConstraintTrainingRequest",
    "NgramScorerMode",
    "NgramScorerTrainingRequest",
    "PhonemeSequence",
    "PhonotacticArtifact",
    "PhonotacticArtifactType",
    "PhonotacticScoreRequest",
    "PhonotacticScoreResult",
    "PrephonemizedRepository",
    "RawTextCandidate",
    "RawTextRepository",
    "ReadabilityBatchResult",
    "ReadabilityRange",
    "ReadabilityRequest",
    "ReadabilityResult",
    "ReadabilityStatus",
    "RepositoryCandidate",
    "RepositoryGenerationRequest",
    "RepositoryGenerationResult",
    "RepositoryGenerationValidation",
    "RepositorySource",
    "ScoreWeights",
    "ScoringState",
]
