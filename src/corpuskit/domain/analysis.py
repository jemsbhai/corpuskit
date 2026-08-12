"""Immutable deterministic-analysis inputs and results."""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field, model_validator

from corpuskit.domain.corpus import (
    CoverageUnit,
    DistributionMetrics,
    FrozenDomainModel,
    TextQualityMetrics,
    UnitCount,
)

MAX_ANALYSIS_ROWS = 500
MAX_TRAJECTORY_ROWS = 2_000
MAX_ANALYSIS_TARGET_UNITS = 100_000


class ReferenceWeight(FrozenDomainModel):
    unit: str = Field(min_length=1, max_length=64)
    weight: float = Field(ge=0.0, le=1_000_000.0, allow_inf_nan=False)


class DistributionAnalysisRequest(FrozenDomainModel):
    counts: tuple[UnitCount, ...] = Field(max_length=MAX_ANALYSIS_TARGET_UNITS)
    target_units: tuple[str, ...] = Field(max_length=MAX_ANALYSIS_TARGET_UNITS)
    reference_distribution: tuple[ReferenceWeight, ...] | None = Field(
        default=None,
        max_length=MAX_ANALYSIS_TARGET_UNITS,
    )

    @model_validator(mode="after")
    def validate_units(self) -> DistributionAnalysisRequest:
        _validate_unique_units(tuple(item.unit for item in self.counts), "count")
        _validate_unique_units(self.target_units, "target")
        if self.reference_distribution is not None:
            reference_units = tuple(item.unit for item in self.reference_distribution)
            _validate_unique_units(reference_units, "reference")
            target_set = set(self.target_units)
            if not any(
                item.weight > 0 and item.unit in target_set for item in self.reference_distribution
            ):
                raise ValueError(
                    "Reference distribution must positively weight at least one target unit."
                )
        return self


class TextQualityAnalysisRequest(FrozenDomainModel):
    sentences: tuple[str, ...] = Field(max_length=MAX_ANALYSIS_ROWS)
    phoneme_sequences: tuple[tuple[str, ...], ...] = Field(max_length=MAX_ANALYSIS_ROWS)

    @model_validator(mode="after")
    def validate_lengths(self) -> TextQualityAnalysisRequest:
        if len(self.sentences) != len(self.phoneme_sequences):
            raise ValueError("Sentences and phoneme sequences must have the same length.")
        return self


class ErrorRatesAnalysisRequest(FrozenDomainModel):
    references: tuple[str, ...] = Field(max_length=MAX_ANALYSIS_ROWS)
    hypotheses: tuple[str, ...] = Field(max_length=MAX_ANALYSIS_ROWS)
    reference_phonemes: tuple[tuple[str, ...], ...] | None = Field(
        default=None,
        max_length=MAX_ANALYSIS_ROWS,
    )
    hypothesis_phonemes: tuple[tuple[str, ...], ...] | None = Field(
        default=None,
        max_length=MAX_ANALYSIS_ROWS,
    )
    case_sensitive: bool = False

    @model_validator(mode="after")
    def validate_lengths(self) -> ErrorRatesAnalysisRequest:
        if len(self.references) != len(self.hypotheses):
            raise ValueError("References and hypotheses must have the same length.")
        if (self.reference_phonemes is None) != (self.hypothesis_phonemes is None):
            raise ValueError("Reference and hypothesis phonemes must be provided together.")
        if self.reference_phonemes is not None:
            if len(self.reference_phonemes) != len(self.references):
                raise ValueError("Reference phonemes must match the reference count.")
            if self.hypothesis_phonemes is None or len(self.hypothesis_phonemes) != len(
                self.hypotheses
            ):
                raise ValueError("Hypothesis phonemes must match the hypothesis count.")
        return self


class CoverageTrajectoryRequest(FrozenDomainModel):
    phoneme_sequences: tuple[tuple[str, ...], ...] = Field(max_length=MAX_TRAJECTORY_ROWS)
    target_units: tuple[str, ...] = Field(max_length=MAX_ANALYSIS_TARGET_UNITS)
    unit: CoverageUnit = CoverageUnit.PHONEME

    @model_validator(mode="after")
    def validate_targets(self) -> CoverageTrajectoryRequest:
        _validate_unique_units(self.target_units, "target")
        return self


class RateStatus(StrEnum):
    FINITE = "finite"
    POSITIVE_INFINITY = "positive_infinity"
    NOT_COMPUTED = "not_computed"


class RateValue(FrozenDomainModel):
    status: RateStatus
    value: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_status(self) -> RateValue:
        if self.status is RateStatus.FINITE:
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("A finite rate requires a finite value.")
        elif self.value is not None:
            raise ValueError("A non-finite or absent rate must use a null value.")
        return self


class SentenceErrorRate(FrozenDomainModel):
    index: int = Field(ge=0)
    reference: str
    hypothesis: str
    wer: RateValue
    cer: RateValue
    per: RateValue


class ErrorRatesAnalysis(FrozenDomainModel):
    wer: RateValue
    cer: RateValue
    per: RateValue
    ser: RateValue
    details: tuple[SentenceErrorRate, ...]


class CoverageSnapshot(FrozenDomainModel):
    sentence_index: int = Field(ge=0)
    coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    covered_count: int = Field(ge=0)
    new_units_count: int = Field(ge=0)
    new_units: tuple[str, ...]


class CoverageTrajectory(FrozenDomainModel):
    unit: CoverageUnit
    target_size: int = Field(ge=0)
    coverages: tuple[float, ...]
    gains: tuple[int, ...]
    snapshots: tuple[CoverageSnapshot, ...]


def _validate_unique_units(units: tuple[str, ...], label: str) -> None:
    if any(not unit.strip() or len(unit) > 64 for unit in units):
        raise ValueError(f"Every {label} unit must contain 1 to 64 characters.")
    if len(units) != len(set(units)):
        raise ValueError(f"{label.title()} units must be unique.")


__all__ = [
    "MAX_ANALYSIS_ROWS",
    "MAX_ANALYSIS_TARGET_UNITS",
    "MAX_TRAJECTORY_ROWS",
    "CoverageSnapshot",
    "CoverageTrajectory",
    "CoverageTrajectoryRequest",
    "DistributionAnalysisRequest",
    "DistributionMetrics",
    "ErrorRatesAnalysis",
    "ErrorRatesAnalysisRequest",
    "RateStatus",
    "RateValue",
    "ReferenceWeight",
    "SentenceErrorRate",
    "TextQualityAnalysisRequest",
    "TextQualityMetrics",
]
