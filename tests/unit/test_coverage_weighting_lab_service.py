"""Domain and service tests for the bounded Coverage and Weighting Lab."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.corpus import CoverageUnit, EvaluationTarget, EvaluationTargetMode
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.domain.lab import (
    CoverageLabRequest,
    G2PLanguages,
    G2PVariantsRequest,
    RenderReportRequest,
    TargetSpaceRequest,
    WeightComputeRequest,
    WeightStrategy,
    WeightValidationKind,
    WeightValidationRequest,
    WeightValue,
)
from corpuskit.services.coverage_weighting_lab import CoverageWeightingLabService


class RecordingEngine:
    """Small duck-typed engine whose calls are observable."""

    def __init__(self) -> None:
        self.coverage_calls = 0

    @staticmethod
    def installed_version() -> str:
        return "0.1.7"

    @staticmethod
    def expected_version() -> str:
        return "0.1.7"

    def g2p_languages(self) -> G2PLanguages:
        return G2PLanguages(backend="fake", languages=("en-us",))

    def g2p_variants(self, text: str, language: str) -> Any:
        return (text, language)

    def coverage(self, request: CoverageLabRequest) -> Any:
        self.coverage_calls += 1
        return request

    def render_report(self, request: RenderReportRequest) -> Any:
        return request

    def export_report(self, request: Any) -> Any:
        return request

    def compute_weights(self, request: WeightComputeRequest) -> Any:
        return request

    def validate_weights(self, request: WeightValidationRequest) -> Any:
        return request


class RecordingReporter:
    def __init__(self) -> None:
        self.forces: list[bool] = []

    def report(self, *, force: bool = False) -> CapabilityReport:
        self.forces.append(force)
        return CapabilityReport(
            checked_at=datetime(2026, 8, 11, tzinfo=UTC),
            checks=(),
            ready=True,
        )


def _service(**settings: Any) -> tuple[CoverageWeightingLabService, RecordingEngine]:
    engine = RecordingEngine()
    service = CoverageWeightingLabService(
        engine,
        RecordingReporter(),
        Settings(environment="test", **settings),
    )
    return service, engine


@pytest.mark.parametrize(
    ("unit", "size", "exponent"),
    [
        (CoverageUnit.PHONEME, 3, 1),
        (CoverageUnit.DIPHONE, 9, 2),
        (CoverageUnit.TRIPHONE, 27, 3),
    ],
)
def test_target_space_estimates_all_units(unit: CoverageUnit, size: int, exponent: int) -> None:
    estimate = CoverageWeightingLabService.estimate(
        TargetSpaceRequest(target_phonemes=("a", "b", "c"), unit=unit)
    )

    assert estimate.estimated_target_size == size
    assert estimate.exponent == exponent
    assert estimate.within_limit is True


def test_over_limit_target_is_rejected_before_engine_construction() -> None:
    service, engine = _service()
    request = CoverageLabRequest(
        target_phonemes=("a", "b", "c"),
        unit=CoverageUnit.TRIPHONE,
        max_target_size=26,
        phoneme_sequences=(),
    )

    with pytest.raises(InvalidRequestError):
        service.coverage(request)

    assert engine.coverage_calls == 0


def test_runtime_and_g2p_validate_bounded_inputs() -> None:
    reporter = RecordingReporter()
    service = CoverageWeightingLabService(
        RecordingEngine(),
        reporter,
        Settings(environment="test", max_sentence_characters=4, max_upload_bytes=5),
    )

    overview = service.runtime(force=True)
    assert overview.compatible is True
    assert overview.installed_corpusgen_version == "0.1.7"
    assert reporter.forces == [True]
    assert service.g2p_languages().languages == ("en-us",)
    assert service.g2p_variants(G2PVariantsRequest(text="", language="en-us")) == (
        "",
        "en-us",
    )

    for request in (
        G2PVariantsRequest(text="hello", language="en-us"),
        G2PVariantsRequest(text="ok", language="bad_language"),
        G2PVariantsRequest(text="\u00e9" * 3, language="en-us"),
    ):
        with pytest.raises(InvalidRequestError):
            service.g2p_variants(request)


def test_report_validation_rejects_blank_invalid_and_oversized_requests() -> None:
    service, _ = _service(max_sentence_characters=4, max_upload_bytes=5)

    requests = (
        RenderReportRequest(sentences=("",)),
        RenderReportRequest(sentences=("hello",)),
        RenderReportRequest(sentences=("\u00e9" * 3,)),
        RenderReportRequest(sentences=("ok",), language="bad_language"),
    )
    for request in requests:
        with pytest.raises(InvalidRequestError):
            service.render_report(request)

    explicit = RenderReportRequest(
        sentences=("ok",),
        unit=CoverageUnit.TRIPHONE,
        target=EvaluationTarget(
            mode=EvaluationTargetMode.EXPLICIT,
            phonemes=tuple(f"p{index}" for index in range(28)),
        ),
    )
    with pytest.raises(InvalidRequestError):
        service.render_report(explicit)


@pytest.mark.parametrize(
    "payload",
    [
        {"target_phonemes": ["a", "a"]},
        {"target_phonemes": [""]},
        {"target_phonemes": ["x" * 65]},
        {
            "target_phonemes": ["a"],
            "phoneme_sequences": [[""]],
        },
        {
            "target_phonemes": ["a"],
            "phoneme_sequences": [],
            "weights": [{"unit": "a", "weight": 0}],
        },
        {
            "target_phonemes": ["a"],
            "phoneme_sequences": [],
            "weights": [
                {"unit": "a", "weight": 1},
                {"unit": "a", "weight": 2},
            ],
        },
    ],
)
def test_coverage_domain_rejects_ambiguous_or_invalid_values(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CoverageLabRequest.model_validate(payload)


def test_weight_domain_enforces_strategy_and_finite_validation_contracts() -> None:
    with pytest.raises(ValidationError):
        WeightValue(unit="a", weight=float("nan"))
    with pytest.raises(ValidationError):
        WeightComputeRequest(
            strategy=WeightStrategy.INVERSE_FREQUENCY,
            target_units=("a",),
        )
    with pytest.raises(ValidationError):
        WeightComputeRequest(
            strategy=WeightStrategy.UNIFORM,
            target_units=("a",),
            class_weights=(WeightValue(unit="vowel", weight=2),),
        )
    with pytest.raises(ValidationError):
        WeightComputeRequest(
            strategy=WeightStrategy.LINGUISTIC_CLASS,
            target_units=("a",),
            class_weights=(WeightValue(unit="tone", weight=2),),
        )
    with pytest.raises(ValidationError):
        WeightValidationRequest(
            kind=WeightValidationKind.UNIT,
            weights=(WeightValue(unit="a", weight=0),),
        )

    component = WeightValidationRequest(
        kind=WeightValidationKind.COMPONENT,
        weights=(WeightValue(unit="coverage", weight=0),),
    )
    assert component.weights[0].weight == 0
