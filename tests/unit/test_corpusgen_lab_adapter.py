"""Contract and golden-vector tests for the CorpusGen lab adapter."""

# ruff: noqa: RUF012

from __future__ import annotations

import json
from typing import Any

import pytest

from corpuskit.adapters.corpusgen.lab import CorpusgenLabAdapter
from corpuskit.domain.corpus import CoverageUnit, EvaluationTarget, EvaluationTargetMode
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
    LanguageNotSupportedError,
)
from corpuskit.domain.lab import (
    CoverageLabRequest,
    ExportReportRequest,
    RenderReportRequest,
    ReportExportFormat,
    ReportVerbosity,
    WeightComputeRequest,
    WeightStrategy,
    WeightValidationKind,
    WeightValidationRequest,
    WeightValue,
)


class FakeG2PResult:
    text = "tomato"
    ipa = "t É™ m"
    phonemes = ["t", "É™", "m"]
    language = "en-us"
    diphones = ["t-É™", "É™-m"]
    triphones = ["t-É™-m"]
    phoneme_count = 3
    unique_phonemes = {"m", "É™", "t"}


class FakeG2P:
    backend = "fake-espeak"

    def supported_languages(self) -> list[str]:
        return ["en-us", "en-gb", "en-us"]

    def phonemize_variants(self, text: str, language: str = "en-us") -> list[FakeG2PResult]:
        assert text == "tomato"
        assert language == "en-us"
        return [FakeG2PResult()]


def test_g2p_languages_and_variants_are_normalized_deterministically() -> None:
    adapter = CorpusgenLabAdapter(g2p_factory=FakeG2P)

    languages = adapter.g2p_languages()
    variants = adapter.g2p_variants("tomato", "en-us")

    assert languages.backend == "fake-espeak"
    assert languages.languages == ("en-gb", "en-us")
    assert variants.variants[0].unique_phonemes == ("m", "t", "É™")
    assert variants.variants[0].diphones == ("t-É™", "É™-m")


@pytest.mark.parametrize("unit", list(CoverageUnit))
def test_real_tracker_updates_counts_provenance_priority_and_reset(unit: CoverageUnit) -> None:
    target = ("a", "b")
    separator = {
        CoverageUnit.PHONEME: "a",
        CoverageUnit.DIPHONE: "a-b",
        CoverageUnit.TRIPHONE: "a-b-a",
    }[unit]
    result = CorpusgenLabAdapter().coverage(
        CoverageLabRequest(
            target_phonemes=target,
            unit=unit,
            phoneme_sequences=(("a", "b", "a"), ("a", "b")),
            weights=(WeightValue(unit=separator, weight=5),),
            next_targets_limit=10,
        )
    )

    assert result.steps[0].new_units
    assert result.final.covered_count == len(result.final.covered_units)
    assert result.final.unit_counts
    assert result.final.unit_sources
    assert result.after_reset.covered_count == 0
    assert result.after_reset.unit_counts == ()
    assert result.after_reset.unit_sources == ()
    assert result.after_reset.target_units == result.final.target_units
    assert result.next_targets == tuple(sorted(result.next_targets))


def test_weight_priority_is_descending_then_lexicographic() -> None:
    result = CorpusgenLabAdapter().coverage(
        CoverageLabRequest(
            target_phonemes=("a", "b", "c"),
            phoneme_sequences=(),
            weights=(
                WeightValue(unit="b", weight=3),
                WeightValue(unit="c", weight=3),
            ),
            next_targets_limit=3,
        )
    )
    assert result.next_targets == ("b", "c", "a")


def test_report_render_and_export_forward_contract_and_canonicalize_json() -> None:
    seen: list[Any] = []

    def render(request: RenderReportRequest) -> str:
        seen.append(request)
        return "report"

    def export(request: ExportReportRequest) -> dict[str, object]:
        seen.append(request)
        return {"z": 1, "a": {"value": "É™"}}

    adapter = CorpusgenLabAdapter(report_renderer=render, report_exporter=export)
    rendered = adapter.render_report(
        RenderReportRequest(sentences=("hello",), verbosity=ReportVerbosity.VERBOSE)
    )
    exported = adapter.export_report(
        ExportReportRequest(
            sentences=("hello",),
            format=ReportExportFormat.JSON_LD,
        )
    )

    assert rendered.content == "report"
    assert rendered.verbosity is ReportVerbosity.VERBOSE
    assert exported.media_type == "application/ld+json"
    assert exported.canonical_json == '{"a":{"value":"É™"},"z":1}'
    assert json.loads(exported.canonical_json)["z"] == 1
    assert len(seen) == 2


@pytest.mark.parametrize("verbosity", list(ReportVerbosity))
def test_all_report_verbosity_values_are_forwarded(verbosity: ReportVerbosity) -> None:
    adapter = CorpusgenLabAdapter(report_renderer=lambda request: request.verbosity.value)
    result = adapter.render_report(RenderReportRequest(sentences=("x",), verbosity=verbosity))
    assert result.content == verbosity.value


def test_json_export_media_type_indentation_and_contract_failure() -> None:
    request = ExportReportRequest(sentences=("x",), indent=2)
    good = CorpusgenLabAdapter(report_exporter=lambda _: {"b": 2, "a": 1})
    assert good.export_report(request).canonical_json.startswith('{\n  "a": 1')
    assert good.export_report(request).media_type == "application/json"

    bad = CorpusgenLabAdapter(report_exporter=lambda _: {"bad": float("nan")})
    with pytest.raises(EngineContractError):
        bad.export_report(request)


def test_weight_algorithms_match_golden_vectors_and_validate() -> None:
    adapter = CorpusgenLabAdapter()
    uniform = adapter.compute_weights(
        WeightComputeRequest(strategy=WeightStrategy.UNIFORM, target_units=("b", "a"))
    )
    inverse = adapter.compute_weights(
        WeightComputeRequest(
            strategy=WeightStrategy.INVERSE_FREQUENCY,
            target_units=("a", "b"),
            corpus_phonemes=(("a", "a", "b"),),
        )
    )
    linguistic = adapter.compute_weights(
        WeightComputeRequest(
            strategy=WeightStrategy.LINGUISTIC_CLASS,
            target_units=("a", "m", "a-m"),
            class_weights=(
                WeightValue(unit="vowel", weight=2),
                WeightValue(unit="consonant", weight=0.5),
            ),
        )
    )

    assert [(item.unit, item.weight) for item in uniform.weights] == [("a", 1), ("b", 1)]
    assert inverse.count == 2
    assert inverse.total == pytest.approx(2.0)
    assert inverse.weights[1].weight > inverse.weights[0].weight
    assert [(item.unit, item.weight) for item in linguistic.weights] == [
        ("a", 2),
        ("a-m", 1),
        ("m", 0.5),
    ]
    assert adapter.validate_weights(
        WeightValidationRequest(
            kind=WeightValidationKind.UNIT,
            weights=(WeightValue(unit="a", weight=1),),
        )
    ).valid
    assert adapter.validate_weights(
        WeightValidationRequest(
            kind=WeightValidationKind.COMPONENT,
            weights=(WeightValue(unit="coverage", weight=0),),
        )
    ).valid


@pytest.mark.parametrize("bad", [float("nan"), 0.0, -1.0])
def test_adapter_rejects_nonpositive_or_nonfinite_engine_weight_outputs(bad: float) -> None:
    adapter = CorpusgenLabAdapter(uniform_weights=lambda _: {"a": bad})
    with pytest.raises(EngineContractError):
        adapter.compute_weights(
            WeightComputeRequest(strategy=WeightStrategy.UNIFORM, target_units=("a",))
        )


def test_adapter_rejects_changed_weight_domain_and_inventory_contract() -> None:
    adapter = CorpusgenLabAdapter(uniform_weights=lambda _: {"other": 1.0})
    with pytest.raises(EngineContractError):
        adapter.compute_weights(
            WeightComputeRequest(strategy=WeightStrategy.UNIFORM, target_units=("a",))
        )

    class BadInventory:
        unit = "diphone"
        target_size = 0
        target_units: set[str] = set()

    bad_inventory = CorpusgenLabAdapter(target_factory=lambda **_: BadInventory())
    with pytest.raises(EngineContractError):
        bad_inventory.coverage(CoverageLabRequest(target_phonemes=("a",), phoneme_sequences=()))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ImportError("C:/secret/module"), DependencyUnavailableError),
        (ValueError("C:/secret/value"), InvalidRequestError),
        (RuntimeError("C:/secret/runtime"), EngineUnavailableError),
        (OSError("C:/secret/os"), EngineUnavailableError),
        (Exception("C:/secret/unknown"), EngineUnavailableError),
    ],
)
def test_engine_failures_are_typed_and_do_not_leak_raw_details(
    error: Exception, expected: type[Exception]
) -> None:
    def fail() -> FakeG2P:
        raise error

    adapter = CorpusgenLabAdapter(g2p_factory=fail)
    with pytest.raises(expected) as caught:
        adapter.g2p_languages()
    assert "secret" not in str(caught.value)


def test_language_key_error_and_empty_variants_have_stable_errors() -> None:
    class UnknownLanguage(FakeG2P):
        def phonemize_variants(self, text: str, language: str = "en-us") -> list[FakeG2PResult]:
            del text, language
            raise KeyError("C:/secret/language")

    with pytest.raises(LanguageNotSupportedError):
        CorpusgenLabAdapter(g2p_factory=UnknownLanguage).g2p_variants("tomato", "en-us")

    class Empty(FakeG2P):
        def phonemize_variants(self, text: str, language: str = "en-us") -> list[FakeG2PResult]:
            del text, language
            return []

    with pytest.raises(EngineContractError):
        CorpusgenLabAdapter(g2p_factory=Empty).g2p_variants("tomato", "en-us")


def test_report_target_modes_reach_renderer_as_typed_requests() -> None:
    targets = (
        EvaluationTarget(),
        EvaluationTarget(mode=EvaluationTargetMode.EXPLICIT, phonemes=("a",)),
        EvaluationTarget(mode=EvaluationTargetMode.PHOIBLE),
    )
    seen: list[EvaluationTargetMode] = []
    adapter = CorpusgenLabAdapter(
        report_renderer=lambda request: seen.append(request.target.mode) or "ok"
    )
    for target in targets:
        adapter.render_report(RenderReportRequest(sentences=("x",), target=target))
    assert seen == [item.mode for item in targets]
