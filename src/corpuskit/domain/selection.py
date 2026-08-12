"""Immutable sentence-selection domain contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from enum import StrEnum
from itertools import product
from typing import Any, Literal

from pydantic import Field, model_validator

from corpuskit.domain.corpus import (
    CoverageUnit,
    EvaluationTarget,
    EvaluationTargetMode,
    FrozenDomainModel,
)
from corpuskit.domain.jobs import MAX_RUN_SPEC_BYTES

SELECTION_ARTIFACT_SCHEMA_ID = "corpuskit.corpus-selection.v1"
# This is an explicit result budget, not a proof that every admitted input can
# produce an artifact: G2P-derived n-gram strings can expand independently of the
# 256 KiB run-spec envelope. Pathological engine output fails as result_too_large.
# The 4 MiB budget accommodates a near-limit Unicode corpus and the largest
# configured NSGA-II Pareto shape while keeping staging and parsing tightly bounded.
MAX_SELECTION_RESULT_ARTIFACT_BYTES = 16 * MAX_RUN_SPEC_BYTES
MAX_SELECTION_TARGET_UNITS = 100_000


class SelectionAlgorithm(StrEnum):
    """CorpusGen selector algorithms supported by the application."""

    GREEDY = "greedy"
    CELF = "celf"
    STOCHASTIC = "stochastic"
    DISTRIBUTION = "distribution"
    ILP = "ilp"
    NSGA2 = "nsga2"


class UnitWeight(FrozenDomainModel):
    """Positive weight assigned to one phonetic unit."""

    unit: str = Field(min_length=1, max_length=64)
    weight: float = Field(gt=0.0, le=1_000_000.0)


class SelectionOptions(FrozenDomainModel):
    """Bounded, typed selector configuration."""

    algorithm: SelectionAlgorithm = SelectionAlgorithm.GREEDY
    max_sentences: int | None = Field(default=None, ge=1, le=2_000)
    target_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    weights: tuple[UnitWeight, ...] = Field(default=(), max_length=10_000)
    epsilon: float = Field(default=0.1, gt=0.0, le=1.0)
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    target_distribution: tuple[UnitWeight, ...] = Field(default=(), max_length=10_000)
    ilp_time_limit_seconds: float = Field(default=10.0, gt=0.0, le=30.0)
    population_size: int = Field(default=50, ge=2, le=200)
    generations: int = Field(default=100, ge=1, le=200)

    @model_validator(mode="after")
    def validate_algorithm_options(self) -> SelectionOptions:
        """Require unique units and distribution input when needed."""

        weight_units = [item.unit for item in self.weights]
        if len(weight_units) != len(set(weight_units)):
            raise ValueError("Selection weight units must be unique.")
        distribution_units = [item.unit for item in self.target_distribution]
        if len(distribution_units) != len(set(distribution_units)):
            raise ValueError("Target distribution units must be unique.")
        if self.algorithm is SelectionAlgorithm.DISTRIBUTION and not distribution_units:
            raise ValueError("The distribution algorithm requires a target distribution.")
        return self


class SelectionRequest(FrozenDomainModel):
    """Application-level selection input after transport validation."""

    candidates: tuple[str, ...]
    language: str
    unit: CoverageUnit = CoverageUnit.PHONEME
    target: EvaluationTarget = EvaluationTarget()
    options: SelectionOptions = SelectionOptions()


class ParetoSolution(FrozenDomainModel):
    """One normalized NSGA-II Pareto-front solution."""

    coverage: float = Field(ge=0.0, le=1.0)
    sentence_count: int = Field(ge=0)
    selected_indices: tuple[int, ...]
    kl_divergence: float | None = None


class SelectionMetadata(FrozenDomainModel):
    """Normalized union of selector-specific metadata."""

    evaluations: int | None = Field(default=None, ge=0)
    epsilon: float | None = None
    seed: int | None = None
    sample_size: int | None = Field(default=None, ge=0)
    kl_divergence: float | None = None
    solver_status: str | None = None
    pareto_front: tuple[ParetoSolution, ...] = ()


class CorpusSelection(FrozenDomainModel):
    """Stable normalized result from any selector implementation."""

    selected_indices: tuple[int, ...]
    selected_sentences: tuple[str, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    unit: CoverageUnit
    target_mode: EvaluationTargetMode
    algorithm: SelectionAlgorithm
    elapsed_seconds: float = Field(ge=0.0)
    iterations: int = Field(ge=0)
    metadata: SelectionMetadata


class CorpusSelectionArtifactV1(FrozenDomainModel):
    """Deterministic, complete semantic selection persisted as a run artifact.

    Wall-clock elapsed time deliberately remains on the synchronous DTO and is
    omitted here so deterministic replay produces identical content bytes.
    """

    schema_id: Literal["corpuskit.corpus-selection.v1"] = "corpuskit.corpus-selection.v1"
    selected_indices: tuple[int, ...] = Field(max_length=2_000)
    selected_sentences: tuple[str, ...] = Field(max_length=2_000)
    coverage: float = Field(ge=0.0, le=1.0)
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    unit: CoverageUnit
    target_mode: EvaluationTargetMode
    algorithm: SelectionAlgorithm
    iterations: int = Field(ge=0)
    metadata: SelectionMetadata

    @model_validator(mode="after")
    def validate_selection_shape(self) -> CorpusSelectionArtifactV1:
        if len(self.selected_indices) != len(self.selected_sentences):
            raise ValueError("selection indices and sentences must have equal length")
        if len(self.selected_indices) != len(set(self.selected_indices)) or any(
            index < 0 for index in self.selected_indices
        ):
            raise ValueError("selection indices must be unique and non-negative")
        covered = set(self.covered_units)
        missing = set(self.missing_units)
        if len(covered) != len(self.covered_units) or len(missing) != len(self.missing_units):
            raise ValueError("covered and missing units must each be unique")
        if covered & missing:
            raise ValueError("covered and missing units must be disjoint")
        target_size = len(covered) + len(missing)
        expected_coverage = len(covered) / target_size if target_size else 1.0
        if not math.isfinite(self.coverage) or not math.isclose(
            self.coverage,
            expected_coverage,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("coverage must match the covered and missing unit sets")
        return self

    @classmethod
    def from_selection(cls, selection: CorpusSelection) -> CorpusSelectionArtifactV1:
        return cls(
            selected_indices=selection.selected_indices,
            selected_sentences=selection.selected_sentences,
            coverage=selection.coverage,
            covered_units=selection.covered_units,
            missing_units=selection.missing_units,
            unit=selection.unit,
            target_mode=selection.target_mode,
            algorithm=selection.algorithm,
            iterations=selection.iterations,
            metadata=selection.metadata,
        )

    def to_selection(self, *, elapsed_seconds: float = 0.0) -> CorpusSelection:
        """Rehydrate the public DTO; elapsed time is caller-supplied provenance."""

        return CorpusSelection(
            selected_indices=self.selected_indices,
            selected_sentences=self.selected_sentences,
            coverage=self.coverage,
            covered_units=self.covered_units,
            missing_units=self.missing_units,
            unit=self.unit,
            target_mode=self.target_mode,
            algorithm=self.algorithm,
            elapsed_seconds=elapsed_seconds,
            iterations=self.iterations,
            metadata=self.metadata,
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def validate_run_spec(self, spec: Mapping[str, Any]) -> None:
        """Bind unowned child bytes to the authoritative persisted selection input."""

        candidates = spec.get("candidates")
        if (
            not isinstance(candidates, Sequence)
            or isinstance(candidates, (str, bytes, bytearray))
            or not all(isinstance(item, str) for item in candidates)
        ):
            raise ValueError("selection artifact has no authoritative candidates")
        try:
            expected_unit = CoverageUnit(spec.get("unit", CoverageUnit.PHONEME.value))
            target = spec.get("target", {})
            if not isinstance(target, Mapping):
                raise ValueError("invalid selection target")
            expected_target_mode = EvaluationTargetMode(
                target.get("mode", EvaluationTargetMode.DERIVED.value)
            )
            target_phonemes = target.get("phonemes", ())
            if (
                not isinstance(target_phonemes, Sequence)
                or isinstance(target_phonemes, (str, bytes, bytearray))
                or not all(isinstance(item, str) for item in target_phonemes)
                or len(target_phonemes) != len(set(target_phonemes))
            ):
                raise ValueError("invalid selection target phonemes")
            options = spec.get("options", {})
            if not isinstance(options, Mapping):
                raise ValueError("invalid selection options")
            expected_algorithm = SelectionAlgorithm(
                options.get("algorithm", SelectionAlgorithm.GREEDY.value)
            )
            max_sentences = options.get("max_sentences")
            if max_sentences is not None and (
                not isinstance(max_sentences, int) or isinstance(max_sentences, bool)
            ):
                raise ValueError("invalid selection limit")
        except (TypeError, ValueError):
            raise ValueError("invalid authoritative selection spec") from None

        if (
            self.unit is not expected_unit
            or self.target_mode is not expected_target_mode
            or self.algorithm is not expected_algorithm
            or any(index >= len(candidates) for index in self.selected_indices)
            or self.selected_sentences
            != tuple(candidates[index] for index in self.selected_indices)
            or (max_sentences is not None and len(self.selected_indices) > max_sentences)
        ):
            raise ValueError("selection artifact does not match its run spec")
        if expected_target_mode is EvaluationTargetMode.EXPLICIT:
            exponent = {
                CoverageUnit.PHONEME: 1,
                CoverageUnit.DIPHONE: 2,
                CoverageUnit.TRIPHONE: 3,
            }[expected_unit]
            if not target_phonemes or len(target_phonemes) ** exponent > MAX_SELECTION_TARGET_UNITS:
                raise ValueError("selection artifact target is outside the reviewed bound")
            expected_units = {
                "-".join(parts) for parts in product(target_phonemes, repeat=exponent)
            }
            if set(self.covered_units) | set(self.missing_units) != expected_units:
                raise ValueError("selection artifact coverage does not match its explicit target")
        elif target_phonemes:
            raise ValueError("selection artifact target is inconsistent with its mode")
