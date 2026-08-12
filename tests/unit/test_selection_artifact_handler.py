"""Deterministic and bounded durable selection artifact contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic import ValidationError

from corpuskit.domain.corpus import CoverageUnit, EvaluationTarget, EvaluationTargetMode
from corpuskit.domain.jobs import MAX_RUN_SPEC_BYTES, RunKind, normalize_run_spec
from corpuskit.domain.selection import (
    MAX_SELECTION_RESULT_ARTIFACT_BYTES,
    CorpusSelection,
    CorpusSelectionArtifactV1,
    ParetoSolution,
    SelectionAlgorithm,
    SelectionMetadata,
    SelectionOptions,
)
from corpuskit.services.corpus_workflows import CorpusWorkflowService
from corpuskit.workflows.handlers import RunExecutionError, SelectHandler


@dataclass
class StaticSelectionService:
    result: CorpusSelection

    def select(
        self,
        candidates: Sequence[str],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
        options: SelectionOptions,
    ) -> CorpusSelection:
        assert tuple(candidates) == self.result.selected_sentences
        assert language == "en-us"
        assert unit is self.result.unit
        assert target.mode is self.result.target_mode
        assert options.algorithm is self.result.algorithm
        return self.result


class RecordingStager:
    def __init__(self) -> None:
        self.calls: list[tuple[RunKind, bytes, str]] = []

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        assert hashlib.sha256(payload).hexdigest() == content_sha256
        self.calls.append((kind, payload, content_sha256))
        return f"staged-artifact://sha256/{content_sha256}"


def test_near_limit_high_unicode_spec_and_max_nsga2_shape_fit_explicit_budget() -> None:
    candidates = tuple(f"{index:04d}-{'界' * 35}" for index in range(2_000))
    indices = tuple(range(len(candidates)))
    pareto = tuple(
        ParetoSolution(
            coverage=1.0,
            sentence_count=len(indices),
            selected_indices=indices,
            kl_divergence=0.0,
        )
        for _ in range(200)
    )
    result = CorpusSelection(
        selected_indices=indices,
        selected_sentences=candidates,
        coverage=1.0,
        covered_units=tuple(f"unit-{index}" for index in range(10_000)),
        missing_units=(),
        unit=CoverageUnit.PHONEME,
        target_mode=EvaluationTargetMode.DERIVED,
        algorithm=SelectionAlgorithm.NSGA2,
        elapsed_seconds=123.456,
        iterations=200,
        metadata=SelectionMetadata(seed=7, pareto_front=pareto),
    )
    spec, _ = normalize_run_spec(
        {
            "candidates": list(candidates),
            "language": "en-us",
            "unit": "phoneme",
            "target": {"mode": "derived", "phonemes": []},
            "options": {
                "algorithm": "nsga2",
                "max_sentences": 2_000,
                "seed": 7,
                "population_size": 200,
                "generations": 200,
            },
        }
    )
    encoded_spec_size = len(
        json.dumps(
            spec,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    assert MAX_RUN_SPEC_BYTES * 4 // 5 < encoded_spec_size <= MAX_RUN_SPEC_BYTES

    stager = RecordingStager()
    handler = SelectHandler(cast(CorpusWorkflowService, StaticSelectionService(result)), stager)
    claim = handler.execute(spec)

    assert set(claim) == {
        "artifact_type",
        "contract",
        "media_type",
        "schema_id",
        "size_bytes",
        "staged_artifact_ref",
    }
    assert claim["size_bytes"] == len(stager.calls[0][1])
    assert len(stager.calls[0][1]) < MAX_SELECTION_RESULT_ARTIFACT_BYTES
    restored = CorpusSelectionArtifactV1.model_validate_json(stager.calls[0][1], strict=True)
    restored.validate_run_spec(spec)
    assert restored.to_selection().selected_sentences == candidates
    assert "elapsed_seconds" not in restored.model_dump(mode="json")


def test_elapsed_time_does_not_change_selection_artifact_digest() -> None:
    result = CorpusSelection(
        selected_indices=(0,),
        selected_sentences=("Pea.",),
        coverage=1.0,
        covered_units=("p",),
        missing_units=(),
        unit=CoverageUnit.PHONEME,
        target_mode=EvaluationTargetMode.EXPLICIT,
        algorithm=SelectionAlgorithm.GREEDY,
        elapsed_seconds=0.01,
        iterations=1,
        metadata=SelectionMetadata(evaluations=1),
    )
    spec = {
        "candidates": ["Pea."],
        "language": "en-us",
        "unit": "phoneme",
        "target": {"mode": "explicit", "phonemes": ["p"]},
        "options": {"algorithm": "greedy", "max_sentences": 1},
    }
    stager = RecordingStager()
    first = SelectHandler(
        cast(CorpusWorkflowService, StaticSelectionService(result)), stager
    ).execute(spec)
    second = SelectHandler(
        cast(
            CorpusWorkflowService,
            StaticSelectionService(result.model_copy(update={"elapsed_seconds": 99.9})),
        ),
        stager,
    ).execute(spec)

    assert first == second
    assert stager.calls[0][1:] == stager.calls[1][1:]


def test_pathological_g2p_expansion_fails_with_stable_result_budget_code() -> None:
    result = CorpusSelection(
        selected_indices=(0,),
        selected_sentences=("Pea.",),
        coverage=1.0,
        covered_units=tuple(f"{index:05d}-{'x' * 58}" for index in range(66_000)),
        missing_units=(),
        unit=CoverageUnit.PHONEME,
        target_mode=EvaluationTargetMode.DERIVED,
        algorithm=SelectionAlgorithm.GREEDY,
        elapsed_seconds=1.0,
        iterations=1,
        metadata=SelectionMetadata(),
    )
    handler = SelectHandler(
        cast(CorpusWorkflowService, StaticSelectionService(result)),
        RecordingStager(),
    )

    with pytest.raises(RunExecutionError) as caught:
        handler.execute(
            {
                "candidates": ["Pea."],
                "language": "en-us",
                "unit": "phoneme",
                "target": {"mode": "derived", "phonemes": []},
                "options": {"algorithm": "greedy", "max_sentences": 1},
            }
        )
    assert (caught.value.code, caught.value.retryable) == ("result_too_large", False)


@pytest.mark.parametrize(
    "changes",
    [
        {"covered_units": ("p", "p"), "coverage": 1.0},
        {"missing_units": ("b", "b"), "coverage": 0.5},
        {"covered_units": ("p",), "missing_units": ("p",), "coverage": 0.5},
        {"covered_units": ("p",), "missing_units": ("b",), "coverage": 0.75},
        {"covered_units": (), "missing_units": (), "coverage": 0.0},
    ],
)
def test_artifact_rejects_duplicate_overlapping_or_inconsistent_coverage(
    changes: dict[str, object],
) -> None:
    value = {
        "schema_id": "corpuskit.corpus-selection.v1",
        "selected_indices": [0],
        "selected_sentences": ["Pea."],
        "coverage": 0.5,
        "covered_units": ["p"],
        "missing_units": ["b"],
        "unit": "phoneme",
        "target_mode": "explicit",
        "algorithm": "greedy",
        "iterations": 1,
        "metadata": {},
        **changes,
    }
    with pytest.raises(ValidationError):
        CorpusSelectionArtifactV1.model_validate(value)

    empty = CorpusSelectionArtifactV1.model_validate(
        {**value, "covered_units": [], "missing_units": [], "coverage": 1.0}
    )
    assert empty.coverage == 1.0


@pytest.mark.parametrize(
    "changes",
    [
        {"selected_sentences": []},
        {"selected_indices": [0, 0], "selected_sentences": ["Pea.", "Pea."]},
        {"selected_indices": [-1]},
    ],
)
def test_artifact_rejects_inconsistent_selection_indices(
    changes: dict[str, object],
) -> None:
    value = {
        "selected_indices": [0],
        "selected_sentences": ["Pea."],
        "coverage": 1.0,
        "covered_units": ["p"],
        "missing_units": [],
        "unit": "phoneme",
        "target_mode": "explicit",
        "algorithm": "greedy",
        "iterations": 1,
        "metadata": {},
        **changes,
    }
    with pytest.raises(ValidationError):
        CorpusSelectionArtifactV1.model_validate(value)


@pytest.mark.parametrize(
    "spec",
    [
        {"candidates": "Pea."},
        {"candidates": [1]},
        {"candidates": ["Pea."], "target": []},
        {"candidates": ["Pea."], "target": {"phonemes": "p"}},
        {"candidates": ["Pea."], "target": {"phonemes": [1]}},
        {"candidates": ["Pea."], "target": {"phonemes": ["p", "p"]}},
        {"candidates": ["Pea."], "options": []},
        {"candidates": ["Pea."], "unit": "invalid"},
        {"candidates": ["Pea."], "target": {"mode": "invalid"}},
        {"candidates": ["Pea."], "options": {"algorithm": "invalid"}},
        {"candidates": ["Pea."], "options": {"max_sentences": True}},
    ],
)
def test_parent_binding_rejects_malformed_authoritative_specs(
    spec: dict[str, object],
) -> None:
    artifact = CorpusSelectionArtifactV1(
        selected_indices=(0,),
        selected_sentences=("Pea.",),
        coverage=1.0,
        covered_units=("p",),
        missing_units=(),
        unit=CoverageUnit.PHONEME,
        target_mode=EvaluationTargetMode.DERIVED,
        algorithm=SelectionAlgorithm.GREEDY,
        iterations=1,
        metadata=SelectionMetadata(),
    )
    with pytest.raises(ValueError, match=r"selection|authoritative"):
        artifact.validate_run_spec(spec)


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "diphone"},
        {"target": {"mode": "explicit", "phonemes": ["p"]}},
        {"options": {"algorithm": "celf", "max_sentences": 1}},
        {"candidates": ["foreign"]},
        {"candidates": [], "options": {"algorithm": "greedy", "max_sentences": None}},
        {"options": {"algorithm": "greedy", "max_sentences": 0}},
        {"target": {"mode": "derived", "phonemes": ["p"]}},
    ],
)
def test_parent_binding_rejects_semantically_mismatched_specs(
    changes: dict[str, object],
) -> None:
    artifact = CorpusSelectionArtifactV1(
        selected_indices=(0,),
        selected_sentences=("Pea.",),
        coverage=1.0,
        covered_units=("p",),
        missing_units=(),
        unit=CoverageUnit.PHONEME,
        target_mode=EvaluationTargetMode.DERIVED,
        algorithm=SelectionAlgorithm.GREEDY,
        iterations=1,
        metadata=SelectionMetadata(),
    )
    spec: dict[str, object] = {
        "candidates": ["Pea."],
        "unit": "phoneme",
        "target": {"mode": "derived", "phonemes": []},
        "options": {"algorithm": "greedy", "max_sentences": 1},
        **changes,
    }
    with pytest.raises(ValueError, match="selection"):
        artifact.validate_run_spec(spec)


def test_parent_binding_rejects_wrong_or_oversized_explicit_target_space() -> None:
    artifact = CorpusSelectionArtifactV1(
        selected_indices=(0,),
        selected_sentences=("Pea.",),
        coverage=1.0,
        covered_units=("p",),
        missing_units=(),
        unit=CoverageUnit.PHONEME,
        target_mode=EvaluationTargetMode.EXPLICIT,
        algorithm=SelectionAlgorithm.GREEDY,
        iterations=1,
        metadata=SelectionMetadata(),
    )
    base: dict[str, object] = {
        "candidates": ["Pea."],
        "unit": "phoneme",
        "options": {"algorithm": "greedy", "max_sentences": 1},
    }
    with pytest.raises(ValueError, match="explicit target"):
        artifact.validate_run_spec({**base, "target": {"mode": "explicit", "phonemes": ["p", "b"]}})

    triphone = artifact.model_copy(update={"unit": CoverageUnit.TRIPHONE})
    with pytest.raises(ValueError, match="reviewed bound"):
        triphone.validate_run_spec(
            {
                **base,
                "unit": "triphone",
                "target": {
                    "mode": "explicit",
                    "phonemes": [f"p{index}" for index in range(47)],
                },
            }
        )
