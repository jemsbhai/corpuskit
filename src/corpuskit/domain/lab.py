"""Immutable contracts for the Coverage and Weighting Lab."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.corpus import (
    CoverageUnit,
    EvaluationTarget,
    FrozenDomainModel,
    G2PTranscription,
    UnitCount,
    UnitSources,
)

MAX_LAB_TARGET_PHONEMES = 256
MAX_LAB_TARGET_UNITS = 20_000
MAX_LAB_SEQUENCES = 2_000
MAX_LAB_PHONEME_TOKENS = 250_000


class RuntimeOverview(FrozenDomainModel):
    expected_corpusgen_version: str
    installed_corpusgen_version: str | None
    compatible: bool
    capabilities: CapabilityReport


class G2PLanguages(FrozenDomainModel):
    backend: str
    languages: tuple[str, ...]


class G2PVariantsRequest(FrozenDomainModel):
    text: str
    language: str = Field(default="en-us", min_length=2, max_length=32)


class G2PVariants(FrozenDomainModel):
    backend: str
    requested_language: str
    variants: tuple[G2PTranscription, ...]


class TargetSpaceRequest(FrozenDomainModel):
    target_phonemes: tuple[str, ...] = Field(max_length=MAX_LAB_TARGET_PHONEMES)
    unit: CoverageUnit = CoverageUnit.PHONEME
    max_target_size: int = Field(default=MAX_LAB_TARGET_UNITS, ge=1, le=MAX_LAB_TARGET_UNITS)

    @model_validator(mode="after")
    def validate_targets(self) -> TargetSpaceRequest:
        _validate_units(self.target_phonemes, "target phoneme")
        return self


class TargetSpaceEstimate(FrozenDomainModel):
    phoneme_count: int = Field(ge=0)
    unit: CoverageUnit
    exponent: int = Field(ge=1, le=3)
    estimated_target_size: int = Field(ge=0)
    max_target_size: int = Field(ge=1)
    within_limit: bool


class WeightValue(FrozenDomainModel):
    unit: str = Field(min_length=1, max_length=64)
    weight: float = Field(ge=0.0, le=1_000_000.0, allow_inf_nan=False)


class CoverageLabRequest(TargetSpaceRequest):
    phoneme_sequences: tuple[tuple[str, ...], ...] = Field(max_length=MAX_LAB_SEQUENCES)
    weights: tuple[WeightValue, ...] = Field(default=(), max_length=MAX_LAB_TARGET_UNITS)
    next_targets_limit: int = Field(default=20, ge=0, le=1_000)

    @model_validator(mode="after")
    def validate_coverage_input(self) -> CoverageLabRequest:
        _validate_unique_weight_units(self.weights)
        if any(item.weight <= 0 for item in self.weights):
            raise ValueError("Coverage priority weights must be strictly positive.")
        _validate_sequences(self.phoneme_sequences)
        return self


class CoverageSnapshot(FrozenDomainModel):
    coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    target_size: int = Field(ge=0)
    covered_count: int = Field(ge=0)
    target_units: tuple[str, ...]
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    unit_counts: tuple[UnitCount, ...]
    unit_sources: tuple[UnitSources, ...]


class CoverageStep(FrozenDomainModel):
    sentence_index: int = Field(ge=0)
    coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    new_units: tuple[str, ...]


class CoverageLabResult(FrozenDomainModel):
    unit: CoverageUnit
    steps: tuple[CoverageStep, ...]
    final: CoverageSnapshot
    next_targets: tuple[str, ...]
    after_reset: CoverageSnapshot


class ReportVerbosity(StrEnum):
    MINIMAL = "minimal"
    NORMAL = "normal"
    VERBOSE = "verbose"


class ReportExportFormat(StrEnum):
    JSON = "json"
    JSON_LD = "jsonld"


class ReportEvaluationRequest(FrozenDomainModel):
    sentences: tuple[str, ...] = Field(min_length=1, max_length=500)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: CoverageUnit = CoverageUnit.PHONEME
    target: EvaluationTarget = EvaluationTarget()


class RenderReportRequest(ReportEvaluationRequest):
    verbosity: ReportVerbosity = ReportVerbosity.NORMAL


class ExportReportRequest(ReportEvaluationRequest):
    format: ReportExportFormat = ReportExportFormat.JSON
    indent: int | None = Field(default=None, ge=0, le=8)


class RenderedReport(FrozenDomainModel):
    verbosity: ReportVerbosity
    media_type: str = "text/plain; charset=utf-8"
    content: str


class ExportedReport(FrozenDomainModel):
    format: ReportExportFormat
    media_type: str
    canonical_json: str


class WeightStrategy(StrEnum):
    UNIFORM = "uniform"
    INVERSE_FREQUENCY = "inverse_frequency"
    LINGUISTIC_CLASS = "linguistic_class"


class WeightComputeRequest(FrozenDomainModel):
    strategy: WeightStrategy
    target_units: tuple[str, ...] = Field(max_length=MAX_LAB_TARGET_UNITS)
    unit: CoverageUnit = CoverageUnit.PHONEME
    corpus_phonemes: tuple[tuple[str, ...], ...] = Field(default=(), max_length=MAX_LAB_SEQUENCES)
    class_weights: tuple[WeightValue, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_strategy_inputs(self) -> WeightComputeRequest:
        _validate_units(self.target_units, "target unit")
        _validate_sequences(self.corpus_phonemes)
        _validate_unique_weight_units(self.class_weights)
        if self.strategy is WeightStrategy.INVERSE_FREQUENCY and not self.corpus_phonemes:
            raise ValueError("Inverse-frequency weighting requires corpus phonemes.")
        if self.strategy is not WeightStrategy.LINGUISTIC_CLASS and self.class_weights:
            raise ValueError("Class weights apply only to linguistic-class weighting.")
        if any(item.unit not in {"vowel", "consonant"} for item in self.class_weights):
            raise ValueError("Class weights accept only vowel and consonant keys.")
        if any(item.weight <= 0 for item in self.class_weights):
            raise ValueError("Linguistic class weights must be strictly positive.")
        return self


class WeightValidationKind(StrEnum):
    UNIT = "unit"
    COMPONENT = "component"


class WeightValidationRequest(FrozenDomainModel):
    kind: WeightValidationKind
    weights: tuple[WeightValue, ...] = Field(max_length=MAX_LAB_TARGET_UNITS)

    @model_validator(mode="after")
    def validate_units(self) -> WeightValidationRequest:
        _validate_unique_weight_units(self.weights)
        if self.kind is WeightValidationKind.UNIT and any(
            item.weight <= 0 for item in self.weights
        ):
            raise ValueError("Unit weights must be strictly positive.")
        return self


class WeightSet(FrozenDomainModel):
    weights: tuple[WeightValue, ...]
    count: int = Field(ge=0)
    total: float = Field(ge=0.0, allow_inf_nan=False)
    mean: float = Field(ge=0.0, allow_inf_nan=False)


class WeightValidationResult(FrozenDomainModel):
    kind: WeightValidationKind
    valid: bool = True
    count: int = Field(ge=0)


def _validate_units(units: tuple[str, ...], label: str) -> None:
    if any(not unit.strip() or len(unit) > 64 for unit in units):
        raise ValueError(f"Every {label} must contain 1 to 64 characters.")
    if len(units) != len(set(units)):
        raise ValueError(f"{label.title()} values must be unique.")


def _validate_unique_weight_units(weights: tuple[WeightValue, ...]) -> None:
    units = tuple(item.unit for item in weights)
    if len(units) != len(set(units)):
        raise ValueError("Weight units must be unique.")


def _validate_sequences(sequences: tuple[tuple[str, ...], ...]) -> None:
    token_count = 0
    for sequence in sequences:
        for token in sequence:
            if not token.strip() or len(token) > 64:
                raise ValueError("Phoneme tokens must contain 1 to 64 characters.")
            token_count += 1
            if token_count > MAX_LAB_PHONEME_TOKENS:
                raise ValueError("The phoneme token limit was exceeded.")


__all__ = [
    "MAX_LAB_PHONEME_TOKENS",
    "MAX_LAB_SEQUENCES",
    "MAX_LAB_TARGET_PHONEMES",
    "MAX_LAB_TARGET_UNITS",
    "CoverageLabRequest",
    "CoverageLabResult",
    "CoverageSnapshot",
    "CoverageStep",
    "ExportReportRequest",
    "ExportedReport",
    "G2PLanguages",
    "G2PVariants",
    "G2PVariantsRequest",
    "RenderReportRequest",
    "RenderedReport",
    "ReportEvaluationRequest",
    "ReportExportFormat",
    "ReportVerbosity",
    "RuntimeOverview",
    "TargetSpaceEstimate",
    "TargetSpaceRequest",
    "WeightComputeRequest",
    "WeightSet",
    "WeightStrategy",
    "WeightValidationKind",
    "WeightValidationRequest",
    "WeightValidationResult",
    "WeightValue",
]
