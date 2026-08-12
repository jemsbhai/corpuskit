"""Typed, extensible handlers for side-effect-free core CorpusGen execution."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from corpuskit.adapters.corpusgen.analysis import CorpusgenAnalysisAdapter
from corpuskit.adapters.corpusgen.client import CorpusgenAdapter
from corpuskit.config import Settings
from corpuskit.domain.analysis import (
    CoverageTrajectoryRequest,
    DistributionAnalysisRequest,
    ErrorRatesAnalysisRequest,
)
from corpuskit.domain.artifacts import StagedArtifactResult
from corpuskit.domain.corpus import CoverageUnit, EvaluationTarget
from corpuskit.domain.errors import (
    ApplicationError,
    ApplicationErrorCode,
    EngineContractError,
    EngineUnavailableError,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.selection import (
    MAX_SELECTION_RESULT_ARTIFACT_BYTES,
    SELECTION_ARTIFACT_SCHEMA_ID,
    CorpusSelectionArtifactV1,
    SelectionAlgorithm,
    SelectionOptions,
)
from corpuskit.services.corpus_workflows import CorpusWorkflowService
from corpuskit.services.exploration_analysis import AnalysisService
from corpuskit.workflows.progress import DurableRunProgress

ResultSummary = dict[str, Any]


class SelectionResultArtifactStager(Protocol):
    """Stage authority-free selection bytes for trusted parent adoption."""

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str: ...


class DurableRunHandler(Protocol):
    """Extension point for one allowlisted durable run kind."""

    @property
    def kind(self) -> RunKind: ...

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary: ...


@runtime_checkable
class ProgressAwareDurableRunHandler(Protocol):
    """Optional handler extension for strict child-to-parent progress."""

    def execute_with_progress(
        self,
        spec: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None],
    ) -> ResultSummary: ...


@runtime_checkable
class TrustedInputAwareDurableRunHandler(Protocol):
    """Optional handler seam for one parent-authored, non-durable IPC claim."""

    def execute_with_trusted_inputs(
        self,
        spec: Mapping[str, Any],
        trusted_inputs: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None] | None,
    ) -> ResultSummary: ...


class RunExecutionError(RuntimeError):
    """Internal failure with only a stable code and retry classification."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhonemizeRunSpec(_Spec):
    text: str | None = None
    texts: tuple[str, ...] = Field(default=(), max_length=500)
    language: str = Field(default="en-us", min_length=2, max_length=32)

    @model_validator(mode="after")
    def exactly_one_input(self) -> PhonemizeRunSpec:
        if (self.text is None) == (not self.texts):
            raise ValueError("provide exactly one of text or texts")
        return self


class EvaluateRunSpec(_Spec):
    sentences: tuple[str, ...] = Field(min_length=1, max_length=500)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: CoverageUnit = CoverageUnit.PHONEME
    target: EvaluationTarget = EvaluationTarget()


class SelectRunSpec(_Spec):
    candidates: tuple[str, ...] = Field(min_length=1, max_length=2_000)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: CoverageUnit = CoverageUnit.PHONEME
    target: EvaluationTarget = EvaluationTarget()
    options: SelectionOptions = SelectionOptions()

    @model_validator(mode="after")
    def require_replayable_random_seed(self) -> SelectRunSpec:
        if (
            self.options.algorithm in {SelectionAlgorithm.STOCHASTIC, SelectionAlgorithm.NSGA2}
            and self.options.seed is None
        ):
            raise ValueError("durable stochastic selection requires a seed")
        return self


@dataclass(frozen=True, slots=True)
class PhonemizeHandler:
    service: CorpusWorkflowService
    kind: RunKind = RunKind.PHONEMIZE

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = PhonemizeRunSpec.model_validate(spec)
        results = (
            (self.service.phonemize(request.text, language=request.language),)
            if request.text is not None
            else self.service.phonemize_batch(request.texts, language=request.language)
        )
        unique = {phoneme for result in results for phoneme in result.unique_phonemes}
        return {
            "item_count": len(results),
            "language": request.language,
            "phoneme_count": sum(result.phoneme_count for result in results),
            "unique_phoneme_count": len(unique),
            "result_sha256": _semantic_result_sha256(results),
        }


@dataclass(frozen=True, slots=True)
class EvaluateHandler:
    service: CorpusWorkflowService
    kind: RunKind = RunKind.EVALUATE

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = EvaluateRunSpec.model_validate(spec)
        result = self.service.evaluate(
            request.sentences,
            language=request.language,
            unit=request.unit,
            target=request.target,
        )
        return {
            "coverage": result.coverage,
            "covered_unit_count": len(result.covered_units),
            "missing_unit_count": len(result.missing_units),
            "target_mode": result.target_mode.value,
            "total_sentences": result.total_sentences,
            "unit": result.unit.value,
            "result_sha256": _semantic_result_sha256(result),
        }


@dataclass(frozen=True, slots=True)
class DistributionHandler:
    service: AnalysisService
    kind: RunKind = RunKind.DISTRIBUTION

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = DistributionAnalysisRequest.model_validate(spec)
        result = self.service.distribution(request)
        return {
            "coefficient_of_variation": _finite(result.coefficient_of_variation),
            "jsd_uniform": _finite(result.jsd_uniform),
            "normalized_entropy": _finite(result.normalized_entropy),
            "pcd_uniform": _finite(result.pcd_uniform),
            "target_unit_count": len(request.target_units),
            "zero_count": result.zero_count,
            "result_sha256": _semantic_result_sha256(result),
        }


@dataclass(frozen=True, slots=True)
class TrajectoryHandler:
    service: AnalysisService
    kind: RunKind = RunKind.TRAJECTORY

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = CoverageTrajectoryRequest.model_validate(spec)
        result = self.service.trajectory(request)
        return {
            "final_coverage": result.coverages[-1] if result.coverages else 0.0,
            "sentence_count": len(result.snapshots),
            "target_size": result.target_size,
            "unit": result.unit.value,
            "result_sha256": _semantic_result_sha256(result),
        }


@dataclass(frozen=True, slots=True)
class ErrorRatesHandler:
    service: AnalysisService
    kind: RunKind = RunKind.ERROR_RATES

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = ErrorRatesAnalysisRequest.model_validate(spec)
        result = self.service.error_rates(request)
        return {
            "cer": result.cer.model_dump(mode="json"),
            "per": result.per.model_dump(mode="json"),
            "sentence_count": len(result.details),
            "ser": result.ser.model_dump(mode="json"),
            "wer": result.wer.model_dump(mode="json"),
            "result_sha256": _semantic_result_sha256(result),
        }


@dataclass(frozen=True, slots=True)
class SelectHandler:
    service: CorpusWorkflowService
    staging: SelectionResultArtifactStager
    kind: RunKind = RunKind.SELECT

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = SelectRunSpec.model_validate(spec)
        result = self.service.select(
            request.candidates,
            language=request.language,
            unit=request.unit,
            target=request.target,
            options=request.options,
        )
        try:
            payload = CorpusSelectionArtifactV1.from_selection(result).canonical_bytes()
        except (TypeError, ValueError):
            raise EngineContractError("selection.artifact.contract") from None
        if len(payload) > MAX_SELECTION_RESULT_ARTIFACT_BYTES:
            raise RunExecutionError("result_too_large", retryable=False)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            reference = self.staging.stage_model_result(
                kind=self.kind,
                payload=payload,
                content_sha256=digest,
            )
        except Exception:
            raise EngineUnavailableError("selection.artifact.staging") from None
        try:
            claim = StagedArtifactResult(
                staged_artifact_ref=reference,
                schema_id=SELECTION_ARTIFACT_SCHEMA_ID,
                artifact_type="run-result",
                media_type="application/json",
                size_bytes=len(payload),
            )
        except (TypeError, ValueError):
            raise EngineContractError("selection.artifact.staging_reference") from None
        if claim.sha256 != digest:
            raise EngineContractError("selection.artifact.staging_reference")
        return claim.model_dump(mode="json")


class HandlerRegistry:
    """Explicit allowlist of run handlers; unknown kinds never execute."""

    def __init__(self, handlers: Sequence[DurableRunHandler]) -> None:
        self._handlers: dict[RunKind, DurableRunHandler] = {}
        for handler in handlers:
            if handler.kind in self._handlers:
                raise ValueError(f"duplicate durable handler for {handler.kind.value}")
            self._handlers[handler.kind] = handler

    @property
    def kinds(self) -> frozenset[RunKind]:
        return frozenset(self._handlers)

    def extended(self, handlers: Sequence[DurableRunHandler]) -> HandlerRegistry:
        """Return a new explicit registry; duplicate kinds remain a startup error."""

        return HandlerRegistry((*self._handlers.values(), *handlers))

    def execute(
        self,
        kind: RunKind,
        spec: Mapping[str, Any],
        *,
        emit: Callable[[DurableRunProgress], None] | None = None,
        trusted_inputs: Mapping[str, Any] | None = None,
    ) -> ResultSummary:
        handler = self._handlers.get(kind)
        if handler is None:
            raise RunExecutionError("unsupported_run_kind", retryable=False)
        try:
            if trusted_inputs is not None:
                if not isinstance(handler, TrustedInputAwareDurableRunHandler):
                    raise RunExecutionError("trusted_input_unsupported", retryable=False)
                summary = handler.execute_with_trusted_inputs(spec, trusted_inputs, emit)
            else:
                summary = (
                    handler.execute_with_progress(spec, emit)
                    if emit is not None and isinstance(handler, ProgressAwareDurableRunHandler)
                    else handler.execute(spec)
                )
            _validate_summary(summary)
            return summary
        except RunExecutionError:
            raise
        except ValidationError:
            raise RunExecutionError("invalid_run_spec", retryable=False) from None
        except ApplicationError as exc:
            raise RunExecutionError(
                exc.code.value,
                retryable=exc.code is ApplicationErrorCode.ENGINE_UNAVAILABLE,
            ) from None
        except (TypeError, ValueError):
            raise RunExecutionError("invalid_run_spec", retryable=False) from None
        except Exception:
            raise RunExecutionError("internal_error", retryable=True) from None


def build_core_handler_registry(
    settings: Settings,
    *,
    stager: SelectionResultArtifactStager | None = None,
) -> HandlerRegistry:
    """Construct only the six reviewed CPU handlers for the batch worker profile."""

    if settings.worker_profile != "batch-cpu":
        raise RuntimeError("core durable handlers require the batch-cpu worker profile")
    if stager is None:
        raise RuntimeError("core durable selection requires an artifact stager")
    workflow_service = CorpusWorkflowService(CorpusgenAdapter(), settings)
    analysis_service = AnalysisService(CorpusgenAnalysisAdapter(), settings)
    return HandlerRegistry(
        (
            PhonemizeHandler(workflow_service),
            EvaluateHandler(workflow_service),
            DistributionHandler(analysis_service),
            TrajectoryHandler(analysis_service),
            ErrorRatesHandler(analysis_service),
            SelectHandler(workflow_service, stager),
        )
    )


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _semantic_result_sha256(value: Any) -> str:
    """Hash complete semantic output while excluding nondeterministic wall-clock timings."""

    def normalize(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return normalize(item.model_dump(mode="json"))
        if isinstance(item, Mapping):
            return {
                str(key): normalize(child)
                for key, child in item.items()
                if key != "elapsed_seconds"
            }
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [normalize(child) for child in item]
        return item

    encoded = json.dumps(
        normalize(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_summary(summary: ResultSummary) -> None:
    """Reject large, nested, non-JSON, or credential-shaped handler output."""

    from corpuskit.domain.jobs import normalize_result_summary

    normalize_result_summary(summary)


__all__ = [
    "DurableRunHandler",
    "HandlerRegistry",
    "ProgressAwareDurableRunHandler",
    "RunExecutionError",
    "SelectionResultArtifactStager",
    "build_core_handler_registry",
]
