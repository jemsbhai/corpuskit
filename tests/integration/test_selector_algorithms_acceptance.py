"""Real eSpeak and optimization acceptance for every sentence selector."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.domain import (
    CorpusSelection,
    CorpusSelectionArtifactV1,
    EvaluationTarget,
    EvaluationTargetMode,
    SelectionAlgorithm,
    SelectionOptions,
    SelectionRequest,
    UnitWeight,
)

pytestmark = pytest.mark.integration

_CANDIDATES = (
    "Pea.",
    "Bee.",
    "Tea.",
    "Key.",
    "Pea bee tea key.",
)
_TARGET_UNITS = ("p", "b", "t", "k")
_TARGET = EvaluationTarget(
    mode=EvaluationTargetMode.EXPLICIT,
    phonemes=_TARGET_UNITS,
)
_UNIFORM_DISTRIBUTION = tuple(UnitWeight(unit=unit, weight=1.0) for unit in _TARGET_UNITS)


def _request(
    algorithm: SelectionAlgorithm,
    *,
    candidates: tuple[str, ...] = _CANDIDATES,
    max_sentences: int = 2,
    target_coverage: float = 1.0,
    seed: int | None = 7,
    epsilon: float = 0.2,
    target_distribution: tuple[UnitWeight, ...] = (),
) -> SelectionRequest:
    return SelectionRequest(
        candidates=candidates,
        language="en-us",
        target=_TARGET,
        options=SelectionOptions(
            algorithm=algorithm,
            max_sentences=max_sentences,
            target_coverage=target_coverage,
            epsilon=epsilon,
            seed=seed,
            target_distribution=target_distribution,
            ilp_time_limit_seconds=10.0,
            population_size=8,
            generations=5,
        ),
    )


@pytest.fixture(scope="module")
def six_algorithm_results() -> dict[SelectionAlgorithm, CorpusSelection]:
    """Run the same real-G2P problem through all public algorithms."""

    pytest.importorskip("pulp", reason="the optimization acceptance profile requires PuLP")
    pytest.importorskip("pymoo", reason="the optimization acceptance profile requires pymoo")
    adapter = CorpusgenAdapter()
    results: dict[SelectionAlgorithm, CorpusSelection] = {}
    for algorithm in SelectionAlgorithm:
        distribution = (
            _UNIFORM_DISTRIBUTION
            if algorithm in {SelectionAlgorithm.DISTRIBUTION, SelectionAlgorithm.NSGA2}
            else ()
        )
        results[algorithm] = adapter.select(_request(algorithm, target_distribution=distribution))
    return results


def test_greedy_golden_set_cover_and_celf_parity(
    six_algorithm_results: dict[SelectionAlgorithm, CorpusSelection],
) -> None:
    """Greedy and CELF must find the unique one-sentence complete cover."""

    greedy = six_algorithm_results[SelectionAlgorithm.GREEDY]
    celf = six_algorithm_results[SelectionAlgorithm.CELF]

    assert greedy.selected_indices == (4,)
    assert celf.selected_indices == greedy.selected_indices
    assert greedy.coverage == celf.coverage == 1.0
    assert greedy.missing_units == celf.missing_units == ()
    assert celf.metadata.evaluations == len(_CANDIDATES)


def test_stochastic_selector_seed_and_budget_are_reproducible() -> None:
    """A seeded bounded stochastic run must replay byte-for-byte at the DTO layer."""

    candidates = _CANDIDATES[:4]
    adapter = CorpusgenAdapter()
    request = _request(
        SelectionAlgorithm.STOCHASTIC,
        candidates=candidates,
        max_sentences=2,
        epsilon=0.9,
    )

    first = adapter.select(request)
    second = adapter.select(request)

    assert first.model_dump(exclude={"elapsed_seconds"}) == second.model_dump(
        exclude={"elapsed_seconds"}
    )
    assert first.selected_indices == (2, 0)
    assert len(first.selected_sentences) == 2
    assert first.coverage == 0.5
    assert first.metadata.seed == 7
    assert first.metadata.sample_size == 1


def test_distribution_selector_improves_kl_for_a_skewed_target() -> None:
    """A matching candidate must beat the best fallback distribution by KL divergence."""

    target_distribution = tuple(
        UnitWeight(unit=unit, weight=weight)
        for unit, weight in (("p", 7.0), ("b", 1.0), ("t", 1.0), ("k", 1.0))
    )
    matching_candidates = (
        "Pea pea pea pea.",
        "Bee.",
        "Tea.",
        "Key.",
        "Pea bee tea key.",
    )
    fallback_candidates = matching_candidates[1:]
    adapter = CorpusgenAdapter()

    matching = adapter.select(
        _request(
            SelectionAlgorithm.DISTRIBUTION,
            candidates=matching_candidates,
            max_sentences=1,
            target_coverage=0.25,
            target_distribution=target_distribution,
        )
    )
    fallback = adapter.select(
        _request(
            SelectionAlgorithm.DISTRIBUTION,
            candidates=fallback_candidates,
            max_sentences=1,
            target_coverage=0.25,
            target_distribution=target_distribution,
        )
    )

    assert matching.selected_sentences == ("Pea pea pea pea.",)
    assert matching.metadata.kl_divergence is not None
    assert fallback.metadata.kl_divergence is not None
    assert matching.metadata.kl_divergence < fallback.metadata.kl_divergence


def test_ilp_is_bruteforce_optimal(
    six_algorithm_results: dict[SelectionAlgorithm, CorpusSelection],
) -> None:
    """The ILP result must equal the only one-row full cover in the candidate powerset."""

    adapter = CorpusgenAdapter()
    one_row_cover_indices = tuple(
        index
        for index, sentence in enumerate(_CANDIDATES)
        if adapter.evaluate((sentence,), language="en-us", target=_TARGET).coverage == 1.0
    )
    result = six_algorithm_results[SelectionAlgorithm.ILP]

    assert one_row_cover_indices == (4,)
    assert result.selected_indices == one_row_cover_indices
    assert result.coverage == 1.0
    assert result.metadata.solver_status == "Optimal"


def _dominates(left: tuple[float, int, float], right: tuple[float, int, float]) -> bool:
    """Return whether left Pareto objectives dominate right."""

    left_coverage, left_count, left_kl = left
    right_coverage, right_count, right_kl = right
    no_worse = left_coverage >= right_coverage and left_count <= right_count and left_kl <= right_kl
    strictly_better = (
        left_coverage > right_coverage or left_count < right_count or left_kl < right_kl
    )
    return no_worse and strictly_better


def test_nsga2_front_is_nondominated(
    six_algorithm_results: dict[SelectionAlgorithm, CorpusSelection],
) -> None:
    """Every reported solution must survive a direct pairwise dominance check."""

    result = six_algorithm_results[SelectionAlgorithm.NSGA2]
    front = result.metadata.pareto_front
    objectives = tuple(
        (
            solution.coverage,
            solution.sentence_count,
            solution.kl_divergence if solution.kl_divergence is not None else float("inf"),
        )
        for solution in front
    )

    assert len(front) >= 2
    assert result.selected_indices == (4,)
    assert result.coverage == 1.0
    for index, objective in enumerate(objectives):
        competitors: Iterable[tuple[float, int, float]] = (
            candidate
            for competitor_index, candidate in enumerate(objectives)
            if competitor_index != index
        )
        assert not any(_dominates(competitor, objective) for competitor in competitors)


def test_all_six_algorithms_share_the_normalized_contract(
    six_algorithm_results: dict[SelectionAlgorithm, CorpusSelection],
) -> None:
    """The complete comparison surface must preserve algorithm identity and invariants."""

    assert set(six_algorithm_results) == set(SelectionAlgorithm)
    for algorithm, result in six_algorithm_results.items():
        artifact = CorpusSelectionArtifactV1.from_selection(result)
        assert (
            CorpusSelectionArtifactV1.model_validate_json(artifact.canonical_bytes(), strict=True)
            == artifact
        )
        assert result.algorithm is algorithm
        assert result.coverage == 1.0
        assert not result.missing_units
        assert 1 <= len(result.selected_sentences) <= 2
        assert tuple(_CANDIDATES[index] for index in result.selected_indices) == (
            result.selected_sentences
        )
