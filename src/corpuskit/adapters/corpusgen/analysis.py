"""Typed CorpusGen boundary for deterministic corpus analyses."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Protocol, cast

from pydantic import ValidationError

from corpuskit.domain import (
    CoverageSnapshot,
    CoverageTrajectory,
    CoverageTrajectoryRequest,
    DistributionAnalysisRequest,
    DistributionMetrics,
    EngineContractError,
    EngineUnavailableError,
    ErrorRatesAnalysis,
    ErrorRatesAnalysisRequest,
    InvalidRequestError,
    RateStatus,
    RateValue,
    SentenceErrorRate,
    TextQualityAnalysisRequest,
    TextQualityMetrics,
)


class DistributionLike(Protocol):
    entropy: float
    normalized_entropy: float
    jsd_uniform: float
    coefficient_of_variation: float
    min_count: int
    max_count: int
    count_ratio: float
    zero_count: int
    pcd_uniform: float
    jsd_reference: float | None
    pearson_correlation: float | None


class TextQualityLike(Protocol):
    sentence_length_words_mean: float
    sentence_length_words_median: float
    sentence_length_words_std: float
    sentence_length_words_min: int
    sentence_length_words_max: int
    sentence_length_phonemes_mean: float
    sentence_length_phonemes_median: float
    sentence_length_phonemes_std: float
    sentence_length_phonemes_min: int
    sentence_length_phonemes_max: int
    total_words: int
    unique_words: int
    type_token_ratio: float
    hapax_ratio: float
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None


class SentenceErrorLike(Protocol):
    index: int
    reference: str
    hypothesis: str
    wer: float
    cer: float
    per: float | None


class ErrorRatesLike(Protocol):
    wer: float
    cer: float
    per: float | None
    ser: float
    details: list[SentenceErrorLike]


class CoverageSnapshotLike(Protocol):
    sentence_index: int
    coverage: float
    covered_count: int
    new_units_count: int
    new_units: list[str]


class CoverageTrajectoryLike(Protocol):
    snapshots: list[CoverageSnapshotLike]
    unit: str
    target_size: int


class DistributionComputer(Protocol):
    def __call__(
        self,
        phoneme_counts: dict[str, int],
        target_phonemes: list[str],
        reference_distribution: dict[str, float] | None = None,
    ) -> DistributionLike: ...


class TextQualityComputer(Protocol):
    def __call__(
        self,
        sentences: list[str],
        phoneme_sequences: list[list[str]],
    ) -> TextQualityLike: ...


class ErrorRatesComputer(Protocol):
    def __call__(
        self,
        references: list[str],
        hypotheses: list[str],
        reference_phonemes: list[list[str]] | None = None,
        hypothesis_phonemes: list[list[str]] | None = None,
        case_sensitive: bool = False,
    ) -> ErrorRatesLike: ...


class TrajectoryComputer(Protocol):
    def __call__(
        self,
        phoneme_sequences: list[list[str]],
        target_units: set[str],
        unit: str = "phoneme",
    ) -> CoverageTrajectoryLike: ...


def _default_distribution(
    counts: dict[str, int],
    targets: list[str],
    reference_distribution: dict[str, float] | None = None,
) -> DistributionLike:
    from corpusgen.evaluate.distribution import compute_distribution_metrics

    return cast(
        DistributionLike,
        compute_distribution_metrics(counts, targets, reference_distribution),
    )


def _default_text_quality(
    sentences: list[str],
    phoneme_sequences: list[list[str]],
) -> TextQualityLike:
    from corpusgen.evaluate.text_quality import compute_text_quality_metrics

    return cast(TextQualityLike, compute_text_quality_metrics(sentences, phoneme_sequences))


def _default_error_rates(
    references: list[str],
    hypotheses: list[str],
    reference_phonemes: list[list[str]] | None = None,
    hypothesis_phonemes: list[list[str]] | None = None,
    case_sensitive: bool = False,
) -> ErrorRatesLike:
    from corpusgen.evaluate.error_rates import compute_error_rates

    return cast(
        ErrorRatesLike,
        compute_error_rates(
            references,
            hypotheses,
            reference_phonemes=reference_phonemes,
            hypothesis_phonemes=hypothesis_phonemes,
            case_sensitive=case_sensitive,
        ),
    )


def _default_trajectory(
    phoneme_sequences: list[list[str]],
    target_units: set[str],
    unit: str = "phoneme",
) -> CoverageTrajectoryLike:
    from corpusgen.evaluate.trajectory import compute_coverage_trajectory

    return cast(
        CoverageTrajectoryLike,
        compute_coverage_trajectory(phoneme_sequences, target_units, unit),
    )


class CorpusgenAnalysisAdapter:
    """Normalize deterministic CorpusGen metrics into JSON-safe DTOs."""

    def __init__(
        self,
        *,
        distribution_computer: DistributionComputer | None = None,
        text_quality_computer: TextQualityComputer | None = None,
        error_rates_computer: ErrorRatesComputer | None = None,
        trajectory_computer: TrajectoryComputer | None = None,
    ) -> None:
        self._distribution = distribution_computer or _default_distribution
        self._text_quality = text_quality_computer or _default_text_quality
        self._error_rates = error_rates_computer or _default_error_rates
        self._trajectory = trajectory_computer or _default_trajectory

    def distribution(self, request: DistributionAnalysisRequest) -> DistributionMetrics:
        operation = "analysis.distribution"
        reference = (
            {item.unit: item.weight for item in request.reference_distribution}
            if request.reference_distribution is not None
            else None
        )
        result = self._invoke(
            lambda: self._distribution(
                {item.unit: item.count for item in request.counts},
                list(request.target_units),
                reference,
            ),
            operation,
        )
        try:
            return DistributionMetrics(
                entropy=result.entropy,
                normalized_entropy=result.normalized_entropy,
                jsd_uniform=result.jsd_uniform,
                coefficient_of_variation=result.coefficient_of_variation,
                min_count=result.min_count,
                max_count=result.max_count,
                count_ratio=result.count_ratio,
                zero_count=result.zero_count,
                pcd_uniform=result.pcd_uniform,
                jsd_reference=result.jsd_reference,
                pearson_correlation=result.pearson_correlation,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    def text_quality(self, request: TextQualityAnalysisRequest) -> TextQualityMetrics:
        operation = "analysis.text_quality"
        result = self._invoke(
            lambda: self._text_quality(
                list(request.sentences),
                [list(sequence) for sequence in request.phoneme_sequences],
            ),
            operation,
        )
        try:
            return TextQualityMetrics(
                sentence_length_words_mean=result.sentence_length_words_mean,
                sentence_length_words_median=result.sentence_length_words_median,
                sentence_length_words_std=result.sentence_length_words_std,
                sentence_length_words_min=result.sentence_length_words_min,
                sentence_length_words_max=result.sentence_length_words_max,
                sentence_length_phonemes_mean=result.sentence_length_phonemes_mean,
                sentence_length_phonemes_median=result.sentence_length_phonemes_median,
                sentence_length_phonemes_std=result.sentence_length_phonemes_std,
                sentence_length_phonemes_min=result.sentence_length_phonemes_min,
                sentence_length_phonemes_max=result.sentence_length_phonemes_max,
                total_words=result.total_words,
                unique_words=result.unique_words,
                type_token_ratio=result.type_token_ratio,
                hapax_ratio=result.hapax_ratio,
                flesch_reading_ease=result.flesch_reading_ease,
                flesch_kincaid_grade=result.flesch_kincaid_grade,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    def error_rates(self, request: ErrorRatesAnalysisRequest) -> ErrorRatesAnalysis:
        operation = "analysis.error_rates"
        result = self._invoke(
            lambda: self._error_rates(
                list(request.references),
                list(request.hypotheses),
                (
                    [list(sequence) for sequence in request.reference_phonemes]
                    if request.reference_phonemes is not None
                    else None
                ),
                (
                    [list(sequence) for sequence in request.hypothesis_phonemes]
                    if request.hypothesis_phonemes is not None
                    else None
                ),
                request.case_sensitive,
            ),
            operation,
        )
        try:
            details = tuple(
                SentenceErrorRate(
                    index=item.index,
                    reference=item.reference,
                    hypothesis=item.hypothesis,
                    wer=self._rate(item.wer),
                    cer=self._rate(item.cer),
                    per=self._rate(item.per),
                )
                for item in result.details
            )
            if len(details) != len(request.references) or any(
                item.index != index for index, item in enumerate(details)
            ):
                raise ValueError("engine returned inconsistent error details")
            return ErrorRatesAnalysis(
                wer=self._rate(result.wer),
                cer=self._rate(result.cer),
                per=self._rate(result.per),
                ser=self._rate(result.ser),
                details=details,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    def trajectory(self, request: CoverageTrajectoryRequest) -> CoverageTrajectory:
        operation = "analysis.coverage_trajectory"
        result = self._invoke(
            lambda: self._trajectory(
                [list(sequence) for sequence in request.phoneme_sequences],
                set(request.target_units),
                request.unit.value,
            ),
            operation,
        )
        try:
            if result.unit != request.unit.value or result.target_size != len(request.target_units):
                raise ValueError("engine returned a different trajectory contract")
            snapshots = tuple(
                CoverageSnapshot(
                    sentence_index=item.sentence_index,
                    coverage=item.coverage,
                    covered_count=item.covered_count,
                    new_units_count=item.new_units_count,
                    new_units=tuple(item.new_units),
                )
                for item in result.snapshots
            )
            if len(snapshots) != len(request.phoneme_sequences) or any(
                item.sentence_index != index
                or item.new_units_count != len(item.new_units)
                or item.covered_count > result.target_size
                for index, item in enumerate(snapshots)
            ):
                raise ValueError("engine returned inconsistent trajectory snapshots")
            return CoverageTrajectory(
                unit=request.unit,
                target_size=result.target_size,
                coverages=tuple(item.coverage for item in snapshots),
                gains=tuple(item.new_units_count for item in snapshots),
                snapshots=snapshots,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _rate(value: float | None) -> RateValue:
        if value is None:
            return RateValue(status=RateStatus.NOT_COMPUTED, value=None)
        if math.isfinite(value):
            return RateValue(status=RateStatus.FINITE, value=value)
        if math.isinf(value) and value > 0:
            return RateValue(status=RateStatus.POSITIVE_INFINITY, value=None)
        raise ValueError("engine returned an invalid rate")

    @staticmethod
    def _invoke[T](call: Callable[[], T], operation: str) -> T:
        try:
            return call()
        except ValueError:
            raise InvalidRequestError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None


__all__ = ["CorpusgenAnalysisAdapter"]
