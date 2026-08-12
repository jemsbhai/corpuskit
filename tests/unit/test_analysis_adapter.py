"""Golden contracts for deterministic CorpusGen analysis adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from corpuskit.adapters.corpusgen import CorpusgenAnalysisAdapter
from corpuskit.domain import (
    CoverageTrajectoryRequest,
    CoverageUnit,
    DistributionAnalysisRequest,
    EngineContractError,
    ErrorRatesAnalysisRequest,
    RateStatus,
    ReferenceWeight,
    TextQualityAnalysisRequest,
    UnitCount,
)


def test_distribution_metrics_match_hand_computed_golden_vector() -> None:
    result = CorpusgenAnalysisAdapter().distribution(
        DistributionAnalysisRequest(
            counts=(UnitCount(unit="a", count=3), UnitCount(unit="b", count=1)),
            target_units=("a", "b"),
            reference_distribution=(
                ReferenceWeight(unit="a", weight=3),
                ReferenceWeight(unit="b", weight=1),
            ),
        )
    )

    assert result.entropy == pytest.approx(0.8112781244591328)
    assert result.normalized_entropy == pytest.approx(0.8112781244591328)
    assert result.jsd_uniform == pytest.approx(0.0487949406953985)
    assert result.coefficient_of_variation == pytest.approx(0.5)
    assert (result.min_count, result.max_count, result.count_ratio) == (1, 3, 1 / 3)
    assert result.zero_count == 0
    assert result.pcd_uniform == pytest.approx(1 - result.jsd_uniform)
    assert result.jsd_reference == pytest.approx(0.0)
    assert result.pearson_correlation == pytest.approx(1.0)


def test_text_quality_metrics_match_reference_statistics() -> None:
    result = CorpusgenAnalysisAdapter().text_quality(
        TextQualityAnalysisRequest(
            sentences=("One two.", "Three."),
            phoneme_sequences=(("w", "ʌ"), ("θ",)),
        )
    )

    assert result.sentence_length_words_mean == 1.5
    assert result.sentence_length_words_median == 1.5
    assert result.sentence_length_words_std == 0.5
    assert result.sentence_length_phonemes_mean == 1.5
    assert result.total_words == 3
    assert result.unique_words == 3
    assert result.type_token_ratio == 1.0
    assert result.hapax_ratio == 1.0
    assert result.flesch_reading_ease is not None

    non_latin = CorpusgenAnalysisAdapter().text_quality(
        TextQualityAnalysisRequest(
            sentences=("مرحبا بالعالم",),
            phoneme_sequences=(("m", "a", "r"),),
        )
    )
    assert non_latin.flesch_reading_ease is None
    assert non_latin.flesch_kincaid_grade is None


def test_error_rates_are_micro_averaged_and_infinity_is_explicitly_json_safe() -> None:
    result = CorpusgenAnalysisAdapter().error_rates(
        ErrorRatesAnalysisRequest(
            references=("a b", ""),
            hypotheses=("a c", "x"),
            reference_phonemes=(("a", "b"), ()),
            hypothesis_phonemes=(("a", "c"), ("x",)),
        )
    )

    assert result.wer.value == 1.0
    assert result.cer.value == pytest.approx(2 / 3)
    assert result.per.value == 1.0
    assert result.ser.value == 1.0
    assert result.details[1].wer.status is RateStatus.POSITIVE_INFINITY
    assert result.details[1].wer.value is None
    assert result.details[1].cer.status is RateStatus.POSITIVE_INFINITY
    assert result.details[1].per.status is RateStatus.POSITIVE_INFINITY
    assert "Infinity" not in result.model_dump_json()


def test_absent_phonemes_use_distinct_not_computed_status() -> None:
    result = CorpusgenAnalysisAdapter().error_rates(
        ErrorRatesAnalysisRequest(references=("same",), hypotheses=("same",))
    )

    assert result.per.status is RateStatus.NOT_COMPUTED
    assert result.details[0].per.status is RateStatus.NOT_COMPUTED


@pytest.mark.parametrize(
    ("unit", "targets", "expected_gains"),
    [
        (CoverageUnit.PHONEME, ("a", "b", "c"), (2, 1)),
        (CoverageUnit.DIPHONE, ("a-b", "b-c"), (1, 1)),
        (CoverageUnit.TRIPHONE, ("a-b-c",), (0, 1)),
    ],
)
def test_coverage_trajectory_golden_vectors_for_all_units(
    unit: CoverageUnit,
    targets: tuple[str, ...],
    expected_gains: tuple[int, ...],
) -> None:
    result = CorpusgenAnalysisAdapter().trajectory(
        CoverageTrajectoryRequest(
            phoneme_sequences=(("a", "b"), ("a", "b", "c")),
            target_units=targets,
            unit=unit,
        )
    )

    assert result.gains == expected_gains
    assert result.coverages[-1] == 1.0
    assert result.snapshots[1].covered_count == len(targets)


def test_analysis_requests_reject_ambiguous_or_unbounded_shapes() -> None:
    with pytest.raises(ValidationError):
        DistributionAnalysisRequest(
            counts=(UnitCount(unit="a", count=1), UnitCount(unit="a", count=2)),
            target_units=("a",),
        )
    with pytest.raises(ValidationError):
        DistributionAnalysisRequest(
            counts=(),
            target_units=("a",),
            reference_distribution=(ReferenceWeight(unit="a", weight=0),),
        )
    with pytest.raises(ValidationError):
        DistributionAnalysisRequest(
            counts=(),
            target_units=("a",),
            reference_distribution=(),
        )
    with pytest.raises(ValidationError):
        ErrorRatesAnalysisRequest(references=("one",), hypotheses=())
    with pytest.raises(ValidationError):
        TextQualityAnalysisRequest(sentences=("one",), phoneme_sequences=())


def test_nonfinite_or_inconsistent_engine_results_fail_closed() -> None:
    invalid_rates = SimpleNamespace(wer=float("nan"), cer=0.0, per=None, ser=0.0, details=[])
    with pytest.raises(EngineContractError):
        CorpusgenAnalysisAdapter(
            error_rates_computer=lambda *args, **kwargs: invalid_rates
        ).error_rates(ErrorRatesAnalysisRequest(references=(), hypotheses=()))

    invalid_trajectory = SimpleNamespace(unit="phoneme", target_size=2, snapshots=[])
    with pytest.raises(EngineContractError):
        CorpusgenAnalysisAdapter(
            trajectory_computer=lambda *args, **kwargs: invalid_trajectory
        ).trajectory(
            CoverageTrajectoryRequest(
                phoneme_sequences=(("a",),), target_units=("a",), unit="phoneme"
            )
        )
