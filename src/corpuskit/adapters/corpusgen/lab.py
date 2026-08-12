"""CorpusGen boundary for the Coverage and Weighting Lab."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from functools import partial
from importlib import metadata
from typing import Protocol, cast

from pydantic import ValidationError

from corpuskit.adapters.corpusgen.probe import CORPUSGEN_VERSION
from corpuskit.domain.corpus import (
    EvaluationTargetMode,
    G2PTranscription,
    UnitCount,
    UnitSources,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
    LanguageNotSupportedError,
)
from corpuskit.domain.lab import (
    CoverageLabRequest,
    CoverageLabResult,
    CoverageSnapshot,
    CoverageStep,
    ExportedReport,
    ExportReportRequest,
    G2PLanguages,
    G2PVariants,
    RenderedReport,
    RenderReportRequest,
    ReportExportFormat,
    WeightComputeRequest,
    WeightSet,
    WeightStrategy,
    WeightValidationKind,
    WeightValidationRequest,
    WeightValidationResult,
    WeightValue,
)


class G2PResultLike(Protocol):
    text: str
    ipa: str
    phonemes: list[str]
    language: str

    @property
    def diphones(self) -> list[str]: ...

    @property
    def triphones(self) -> list[str]: ...

    @property
    def phoneme_count(self) -> int: ...

    @property
    def unique_phonemes(self) -> set[str]: ...


class LabG2PManager(Protocol):
    backend: str

    def phonemize_variants(self, text: str, language: str = "en-us") -> list[G2PResultLike]: ...

    def supported_languages(self) -> list[str]: ...


class TrackerLike(Protocol):
    unit: str
    target_units: set[str]
    target_size: int
    covered_count: int
    coverage: float
    covered_units: set[str]
    missing: set[str]
    phoneme_counts: dict[str, int]
    phoneme_sources: dict[str, list[int]]


class TargetInventoryLike(Protocol):
    tracker: TrackerLike
    unit: str
    target_size: int
    target_units: set[str]
    covered_count: int
    covered_units: set[str]
    coverage: float
    missing: set[str]

    def next_targets(self, k: int) -> list[str]: ...

    def update(self, phonemes: list[str], sentence_index: int) -> None: ...

    def reset(self) -> None: ...


class TargetInventoryFactory(Protocol):
    def __call__(
        self,
        *,
        target_phonemes: list[str],
        unit: str,
        weights: dict[str, float] | None,
        max_target_size: int,
    ) -> TargetInventoryLike: ...


class ReportRenderer(Protocol):
    def __call__(self, request: RenderReportRequest) -> str: ...


class ReportExporter(Protocol):
    def __call__(self, request: ExportReportRequest) -> dict[str, object]: ...


class UniformWeights(Protocol):
    def __call__(self, target_units: set[str]) -> dict[str, float]: ...


class InverseWeights(Protocol):
    def __call__(
        self,
        target_units: set[str],
        corpus_phonemes: list[list[str]],
        unit: str = "phoneme",
    ) -> dict[str, float]: ...


class LinguisticWeights(Protocol):
    def __call__(
        self,
        target_units: set[str],
        class_weights: dict[str, float] | None = None,
    ) -> dict[str, float]: ...


class UnitWeightValidator(Protocol):
    def __call__(self, weights: dict[str, float] | None) -> None: ...


class ComponentWeightValidator(Protocol):
    def __call__(self, weights: dict[str, float]) -> None: ...


def _default_g2p_factory() -> LabG2PManager:
    from corpusgen.g2p import G2PManager

    return cast(LabG2PManager, G2PManager())


def _default_target_factory(
    *,
    target_phonemes: list[str],
    unit: str,
    weights: dict[str, float] | None,
    max_target_size: int,
) -> TargetInventoryLike:
    from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory

    return cast(
        TargetInventoryLike,
        PhoneticTargetInventory(
            target_phonemes=target_phonemes,
            unit=unit,
            weights=weights,
            max_target_size=max_target_size,
        ),
    )


def _target_argument(
    mode: EvaluationTargetMode,
    phonemes: tuple[str, ...],
) -> list[str] | str | None:
    if mode is EvaluationTargetMode.DERIVED:
        return None
    if mode is EvaluationTargetMode.PHOIBLE:
        return "phoible"
    return list(phonemes)


def _default_render(request: RenderReportRequest) -> str:
    from corpusgen import evaluate
    from corpusgen.evaluate.report import Verbosity

    report = evaluate(
        list(request.sentences),
        language=request.language,
        target_phonemes=_target_argument(request.target.mode, request.target.phonemes),
        unit=request.unit.value,
    )
    return cast(str, report.render(Verbosity(request.verbosity.value)))


def _default_export(request: ExportReportRequest) -> dict[str, object]:
    from corpusgen import evaluate

    report = evaluate(
        list(request.sentences),
        language=request.language,
        target_phonemes=_target_argument(request.target.mode, request.target.phonemes),
        unit=request.unit.value,
    )
    document = (
        report.to_jsonld_ex() if request.format is ReportExportFormat.JSON_LD else report.to_dict()
    )
    return cast(dict[str, object], document)


def _default_uniform(target_units: set[str]) -> dict[str, float]:
    from corpusgen.weights import uniform_weights

    return cast(dict[str, float], uniform_weights(target_units))


def _default_inverse(
    target_units: set[str],
    corpus_phonemes: list[list[str]],
    unit: str = "phoneme",
) -> dict[str, float]:
    from corpusgen.weights import frequency_inverse_weights

    return cast(
        dict[str, float],
        frequency_inverse_weights(target_units, corpus_phonemes, unit),
    )


def _default_linguistic(
    target_units: set[str],
    class_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    from corpusgen.weights import linguistic_class_weights

    return cast(dict[str, float], linguistic_class_weights(target_units, class_weights))


def _default_unit_validator(weights: dict[str, float] | None) -> None:
    from corpusgen.weights import validate_unit_weights

    validate_unit_weights(weights)


def _default_component_validator(weights: dict[str, float]) -> None:
    from corpusgen.weights import validate_component_weights

    validate_component_weights(weights)


class CorpusgenLabAdapter:
    """Run deterministic lab operations behind a safe typed boundary."""

    def __init__(
        self,
        *,
        g2p_factory: Callable[[], LabG2PManager] | None = None,
        target_factory: TargetInventoryFactory | None = None,
        report_renderer: ReportRenderer | None = None,
        report_exporter: ReportExporter | None = None,
        uniform_weights: UniformWeights | None = None,
        inverse_weights: InverseWeights | None = None,
        linguistic_weights: LinguisticWeights | None = None,
        unit_validator: UnitWeightValidator | None = None,
        component_validator: ComponentWeightValidator | None = None,
    ) -> None:
        self._g2p_factory = g2p_factory or _default_g2p_factory
        self._target_factory = target_factory or _default_target_factory
        self._report_renderer = report_renderer or _default_render
        self._report_exporter = report_exporter or _default_export
        self._uniform = uniform_weights or _default_uniform
        self._inverse = inverse_weights or _default_inverse
        self._linguistic = linguistic_weights or _default_linguistic
        self._unit_validator = unit_validator or _default_unit_validator
        self._component_validator = component_validator or _default_component_validator
        self._g2p: LabG2PManager | None = None

    @staticmethod
    def installed_version() -> str | None:
        try:
            return metadata.version("corpusgen")
        except metadata.PackageNotFoundError:
            return None

    @staticmethod
    def expected_version() -> str:
        return CORPUSGEN_VERSION

    def g2p_languages(self) -> G2PLanguages:
        operation = "lab.g2p.languages"
        manager = self._g2p_manager()
        languages = self._invoke(manager.supported_languages, operation)
        try:
            return G2PLanguages(
                backend=manager.backend,
                languages=tuple(sorted(set(languages))),
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    def g2p_variants(self, text: str, language: str) -> G2PVariants:
        operation = "lab.g2p.variants"
        manager = self._g2p_manager()
        results = self._invoke(
            lambda: manager.phonemize_variants(text, language=language),
            operation,
            key_is_language=True,
        )
        try:
            variants = tuple(self._normalize_g2p(item, operation) for item in results)
            if not variants:
                raise ValueError("engine returned no pronunciation variants")
            return G2PVariants(
                backend=manager.backend,
                requested_language=language,
                variants=variants,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    def coverage(self, request: CoverageLabRequest) -> CoverageLabResult:
        operation = "lab.coverage"
        weights = {item.unit: item.weight for item in request.weights} or None
        inventory = self._invoke(
            lambda: self._target_factory(
                target_phonemes=list(request.target_phonemes),
                unit=request.unit.value,
                weights=weights,
                max_target_size=request.max_target_size,
            ),
            operation,
        )
        self._validate_inventory(inventory, request, operation)
        steps: list[CoverageStep] = []
        previous: set[str] = set()
        for index, sequence in enumerate(request.phoneme_sequences):
            self._invoke(partial(inventory.update, list(sequence), index), operation)
            current = set(inventory.covered_units)
            steps.append(
                CoverageStep(
                    sentence_index=index,
                    coverage=inventory.coverage,
                    new_units=tuple(sorted(current - previous)),
                )
            )
            previous = current
        final = self._snapshot(inventory, operation)
        next_targets = tuple(
            self._invoke(lambda: inventory.next_targets(request.next_targets_limit), operation)
        )
        self._invoke(inventory.reset, operation)
        after_reset = self._snapshot(inventory, operation)
        if after_reset.covered_count or after_reset.unit_counts or after_reset.unit_sources:
            raise EngineContractError(operation)
        return CoverageLabResult(
            unit=request.unit,
            steps=tuple(steps),
            final=final,
            next_targets=next_targets,
            after_reset=after_reset,
        )

    def render_report(self, request: RenderReportRequest) -> RenderedReport:
        content = self._invoke(
            lambda: self._report_renderer(request),
            "lab.report.render",
            key_is_language=request.target.mode is EvaluationTargetMode.PHOIBLE,
        )
        try:
            return RenderedReport(verbosity=request.verbosity, content=content)
        except (TypeError, ValidationError, ValueError):
            raise EngineContractError("lab.report.render") from None

    def export_report(self, request: ExportReportRequest) -> ExportedReport:
        operation = "lab.report.export"
        document = self._invoke(
            lambda: self._report_exporter(request),
            operation,
            key_is_language=request.target.mode is EvaluationTargetMode.PHOIBLE,
        )
        try:
            canonical = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                indent=request.indent,
                separators=(",", ":") if request.indent is None else None,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise EngineContractError(operation) from None
        media_type = (
            "application/ld+json"
            if request.format is ReportExportFormat.JSON_LD
            else "application/json"
        )
        return ExportedReport(
            format=request.format,
            media_type=media_type,
            canonical_json=canonical,
        )

    def compute_weights(self, request: WeightComputeRequest) -> WeightSet:
        operation = "lab.weights.compute"
        targets = set(request.target_units)
        if request.strategy is WeightStrategy.UNIFORM:
            result = self._invoke(lambda: self._uniform(targets), operation)
        elif request.strategy is WeightStrategy.INVERSE_FREQUENCY:
            result = self._invoke(
                lambda: self._inverse(
                    targets,
                    [list(item) for item in request.corpus_phonemes],
                    request.unit.value,
                ),
                operation,
            )
        else:
            class_weights = {item.unit: item.weight for item in request.class_weights} or None
            result = self._invoke(lambda: self._linguistic(targets, class_weights), operation)
        return self._normalize_weights(result, targets, operation)

    def validate_weights(self, request: WeightValidationRequest) -> WeightValidationResult:
        operation = "lab.weights.validate"
        values = {item.unit: item.weight for item in request.weights}
        if request.kind is WeightValidationKind.UNIT:
            self._invoke(lambda: self._unit_validator(values), operation)
        else:
            self._invoke(lambda: self._component_validator(values), operation)
        return WeightValidationResult(kind=request.kind, count=len(values))

    def _g2p_manager(self) -> LabG2PManager:
        if self._g2p is None:
            self._g2p = self._invoke(self._g2p_factory, "lab.g2p.initialize")
        return self._g2p

    @staticmethod
    def _validate_inventory(
        inventory: TargetInventoryLike,
        request: CoverageLabRequest,
        operation: str,
    ) -> None:
        try:
            if inventory.unit != request.unit.value:
                raise EngineContractError(operation)
            if inventory.target_size != len(inventory.target_units):
                raise EngineContractError(operation)
            if inventory.target_size > request.max_target_size:
                raise EngineContractError(operation)
        except (AttributeError, TypeError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _snapshot(inventory: TargetInventoryLike, operation: str) -> CoverageSnapshot:
        try:
            tracker = inventory.tracker
            return CoverageSnapshot(
                coverage=inventory.coverage,
                target_size=inventory.target_size,
                covered_count=inventory.covered_count,
                target_units=tuple(sorted(inventory.target_units)),
                covered_units=tuple(sorted(inventory.covered_units)),
                missing_units=tuple(sorted(inventory.missing)),
                unit_counts=tuple(
                    UnitCount(unit=unit, count=count)
                    for unit, count in sorted(tracker.phoneme_counts.items())
                ),
                unit_sources=tuple(
                    UnitSources(unit=unit, sentence_indices=tuple(indices))
                    for unit, indices in sorted(tracker.phoneme_sources.items())
                ),
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _normalize_g2p(result: G2PResultLike, operation: str) -> G2PTranscription:
        try:
            return G2PTranscription(
                text=result.text,
                language=result.language,
                ipa=result.ipa,
                phonemes=tuple(result.phonemes),
                diphones=tuple(result.diphones),
                triphones=tuple(result.triphones),
                phoneme_count=result.phoneme_count,
                unique_phonemes=tuple(sorted(result.unique_phonemes)),
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _normalize_weights(
        result: dict[str, float],
        expected_units: set[str],
        operation: str,
    ) -> WeightSet:
        try:
            if set(result) != expected_units:
                raise ValueError("engine returned a different weight domain")
            if any(not math.isfinite(value) or value <= 0 for value in result.values()):
                raise ValueError("engine returned invalid weights")
            weights = tuple(
                WeightValue(unit=unit, weight=value) for unit, value in sorted(result.items())
            )
            total = sum(item.weight for item in weights)
            return WeightSet(
                weights=weights,
                count=len(weights),
                total=total,
                mean=total / len(weights) if weights else 0.0,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _invoke[T](
        call: Callable[[], T],
        operation: str,
        *,
        key_is_language: bool = False,
    ) -> T:
        try:
            return call()
        except KeyError:
            error = LanguageNotSupportedError if key_is_language else InvalidRequestError
            raise error(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except ValueError:
            raise InvalidRequestError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None


__all__ = [
    "CorpusgenLabAdapter",
    "LabG2PManager",
    "TargetInventoryFactory",
    "TargetInventoryLike",
]
