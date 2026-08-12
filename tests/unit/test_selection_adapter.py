"""Selection contract tests at the CorpusGen boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.domain import (
    CoverageUnit,
    DependencyUnavailableError,
    EngineContractError,
    EvaluationTarget,
    EvaluationTargetMode,
    SelectionAlgorithm,
    SelectionOptions,
    SelectionRequest,
    UnitWeight,
)


def _result(algorithm: str, unit: str, metadata: dict[str, object] | None = None) -> Any:
    return SimpleNamespace(
        selected_indices=[1],
        selected_sentences=["beta"],
        coverage=0.5,
        covered_units={"b"},
        missing_units={"a"},
        unit=unit,
        algorithm=algorithm,
        elapsed_seconds=0.01,
        iterations=1,
        metadata=metadata or {},
    )


@pytest.mark.parametrize(
    ("algorithm", "options", "expected_kwargs"),
    [
        (SelectionAlgorithm.GREEDY, {}, {}),
        (SelectionAlgorithm.CELF, {}, {}),
        (SelectionAlgorithm.STOCHASTIC, {"epsilon": 0.2, "seed": 7}, {"epsilon": 0.2, "seed": 7}),
        (
            SelectionAlgorithm.DISTRIBUTION,
            {"target_distribution": (UnitWeight(unit="a", weight=2.0),)},
            {"target_distribution": {"a": 2.0}},
        ),
        (SelectionAlgorithm.ILP, {"ilp_time_limit_seconds": 3.0}, {"time_limit": 3.0}),
        (
            SelectionAlgorithm.NSGA2,
            {"population_size": 12, "generations": 8, "seed": 9},
            {
                "target_distribution": None,
                "population_size": 12,
                "n_generations": 8,
                "seed": 9,
            },
        ),
    ],
)
def test_all_selector_algorithms_use_the_public_dispatch_contract(
    algorithm: SelectionAlgorithm,
    options: dict[str, object],
    expected_kwargs: dict[str, object],
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def selector(*args: Any, **kwargs: object) -> Any:
        calls.append((args, kwargs))
        return _result(algorithm.value, CoverageUnit.DIPHONE.value)

    request = SelectionRequest(
        candidates=("alpha", "beta"),
        language="en-us",
        unit=CoverageUnit.DIPHONE,
        target=EvaluationTarget(
            mode=EvaluationTargetMode.EXPLICIT,
            phonemes=("a", "b"),
        ),
        options=SelectionOptions(
            algorithm=algorithm,
            max_sentences=1,
            target_coverage=0.75,
            weights=(UnitWeight(unit="a-b", weight=3.0),),
            **options,
        ),
    )

    result = CorpusgenAdapter(selector=selector).select(request)

    assert calls == [
        (
            (["alpha", "beta"],),
            {
                "language": "en-us",
                "target_phonemes": ["a", "b"],
                "unit": "diphone",
                "algorithm": algorithm.value,
                "max_sentences": 1,
                "target_coverage": 0.75,
                "weights": {"a-b": 3.0},
                **expected_kwargs,
            },
        )
    ]
    assert result.selected_indices == (1,)
    assert result.target_mode is EvaluationTargetMode.EXPLICIT
    assert result.algorithm is algorithm


@pytest.mark.parametrize(
    ("mode", "engine_target"),
    [
        (EvaluationTargetMode.DERIVED, None),
        (EvaluationTargetMode.PHOIBLE, "phoible"),
    ],
)
def test_selection_supports_derived_and_phoible_targets(
    mode: EvaluationTargetMode,
    engine_target: str | None,
) -> None:
    seen: list[object] = []

    def selector(*args: Any, **kwargs: object) -> Any:
        seen.append(kwargs["target_phonemes"])
        return _result("greedy", "phoneme")

    CorpusgenAdapter(selector=selector).select(
        SelectionRequest(
            candidates=("alpha", "beta"),
            language="en-us",
            target=EvaluationTarget(mode=mode),
        )
    )

    assert seen == [engine_target]


def test_selection_normalizes_algorithm_metadata() -> None:
    metadata: dict[str, object] = {
        "evaluations": 5,
        "epsilon": 0.1,
        "seed": 4,
        "sample_size": 2,
        "kl_divergence": 0.25,
        "solver_status": "Optimal",
        "pareto_front": [
            {
                "coverage": 0.8,
                "n_sentences": 2,
                "selected_indices": [0, 1],
                "kl_divergence": 0.2,
            }
        ],
    }
    adapter = CorpusgenAdapter(
        selector=lambda *args, **kwargs: _result("nsga2", "phoneme", metadata)
    )
    result = adapter.select(
        SelectionRequest(
            candidates=("alpha", "beta"),
            language="en-us",
            options=SelectionOptions(algorithm=SelectionAlgorithm.NSGA2),
        )
    )

    assert result.metadata.solver_status == "Optimal"
    assert result.metadata.pareto_front[0].selected_indices == (0, 1)


def test_optional_selector_failure_is_safe_and_contract_mismatch_is_typed() -> None:
    def unavailable(*args: Any, **kwargs: object) -> Any:
        raise ImportError("private package path")

    request = SelectionRequest(candidates=("alpha",), language="en-us")
    with pytest.raises(DependencyUnavailableError, match="required language-processing"):
        CorpusgenAdapter(selector=unavailable).select(request)

    with pytest.raises(EngineContractError):
        CorpusgenAdapter(selector=lambda *args, **kwargs: _result("celf", "phoneme")).select(
            request
        )


def test_explicit_target_symbol_count_and_length_are_bounded() -> None:
    with pytest.raises(ValidationError):
        EvaluationTarget(
            mode=EvaluationTargetMode.EXPLICIT,
            phonemes=tuple(f"p{index}" for index in range(257)),
        )
    with pytest.raises(ValidationError, match="64 characters"):
        EvaluationTarget(
            mode=EvaluationTargetMode.EXPLICIT,
            phonemes=("p" * 65,),
        )


@pytest.mark.parametrize(
    ("indices", "sentences", "covered", "missing"),
    [
        ([0, 0], ["alpha", "alpha"], {"a"}, set()),
        ([2], ["unknown"], {"a"}, set()),
        ([0], ["wrong"], {"a"}, set()),
        ([0], ["alpha"], {"a"}, {"a"}),
    ],
)
def test_inconsistent_selection_results_fail_closed(
    indices: list[int],
    sentences: list[str],
    covered: set[str],
    missing: set[str],
) -> None:
    result = _result("greedy", "phoneme")
    result.selected_indices = indices
    result.selected_sentences = sentences
    result.covered_units = covered
    result.missing_units = missing

    with pytest.raises(EngineContractError):
        CorpusgenAdapter(selector=lambda *args, **kwargs: result).select(
            SelectionRequest(candidates=("alpha", "beta"), language="en-us")
        )
