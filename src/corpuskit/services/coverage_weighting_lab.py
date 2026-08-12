"""Bounded application service for coverage, reports, G2P, and weighting."""

from __future__ import annotations

import re
from typing import Protocol

from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.corpus import EvaluationTargetMode
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.domain.lab import (
    CoverageLabRequest,
    CoverageLabResult,
    ExportedReport,
    ExportReportRequest,
    G2PLanguages,
    G2PVariants,
    G2PVariantsRequest,
    RenderedReport,
    RenderReportRequest,
    RuntimeOverview,
    TargetSpaceEstimate,
    TargetSpaceRequest,
    WeightComputeRequest,
    WeightSet,
    WeightValidationRequest,
    WeightValidationResult,
)

_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")


class LabEngine(Protocol):
    @staticmethod
    def installed_version() -> str | None: ...

    @staticmethod
    def expected_version() -> str: ...

    def g2p_languages(self) -> G2PLanguages: ...

    def g2p_variants(self, text: str, language: str) -> G2PVariants: ...

    def coverage(self, request: CoverageLabRequest) -> CoverageLabResult: ...

    def render_report(self, request: RenderReportRequest) -> RenderedReport: ...

    def export_report(self, request: ExportReportRequest) -> ExportedReport: ...

    def compute_weights(self, request: WeightComputeRequest) -> WeightSet: ...

    def validate_weights(self, request: WeightValidationRequest) -> WeightValidationResult: ...


class CapabilityReporter(Protocol):
    def report(self, *, force: bool = False) -> CapabilityReport: ...


class CoverageWeightingLabService:
    """Validate synchronous lab work before invoking CorpusGen."""

    def __init__(self, engine: LabEngine, reporter: CapabilityReporter, settings: Settings) -> None:
        self._engine = engine
        self._reporter = reporter
        self._max_sentence_characters = settings.max_sentence_characters
        self._max_payload_bytes = settings.max_upload_bytes

    def runtime(self, *, force: bool = False) -> RuntimeOverview:
        installed = self._engine.installed_version()
        expected = self._engine.expected_version()
        return RuntimeOverview(
            expected_corpusgen_version=expected,
            installed_corpusgen_version=installed,
            compatible=installed == expected,
            capabilities=self._reporter.report(force=force),
        )

    def g2p_languages(self) -> G2PLanguages:
        return self._engine.g2p_languages()

    def g2p_variants(self, request: G2PVariantsRequest) -> G2PVariants:
        self._validate_language(request.language, "lab.g2p.variants")
        self._validate_texts((request.text,), "lab.g2p.variants", allow_empty=True)
        return self._engine.g2p_variants(request.text, request.language)

    @staticmethod
    def estimate(request: TargetSpaceRequest) -> TargetSpaceEstimate:
        exponent = {
            "phoneme": 1,
            "diphone": 2,
            "triphone": 3,
        }[request.unit.value]
        size = len(request.target_phonemes) ** exponent
        return TargetSpaceEstimate(
            phoneme_count=len(request.target_phonemes),
            unit=request.unit,
            exponent=exponent,
            estimated_target_size=size,
            max_target_size=request.max_target_size,
            within_limit=size <= request.max_target_size,
        )

    def coverage(self, request: CoverageLabRequest) -> CoverageLabResult:
        if not self.estimate(request).within_limit:
            raise InvalidRequestError("lab.coverage")
        return self._engine.coverage(request)

    def render_report(self, request: RenderReportRequest) -> RenderedReport:
        self._validate_report(request, "lab.report.render")
        return self._engine.render_report(request)

    def export_report(self, request: ExportReportRequest) -> ExportedReport:
        self._validate_report(request, "lab.report.export")
        return self._engine.export_report(request)

    def compute_weights(self, request: WeightComputeRequest) -> WeightSet:
        return self._engine.compute_weights(request)

    def validate_weights(self, request: WeightValidationRequest) -> WeightValidationResult:
        return self._engine.validate_weights(request)

    def _validate_report(
        self,
        request: RenderReportRequest | ExportReportRequest,
        operation: str,
    ) -> None:
        self._validate_language(request.language, operation)
        self._validate_texts(request.sentences, operation, allow_empty=False)
        if request.target.mode is EvaluationTargetMode.EXPLICIT:
            estimate = self.estimate(
                TargetSpaceRequest(
                    target_phonemes=request.target.phonemes,
                    unit=request.unit,
                )
            )
            if not estimate.within_limit:
                raise InvalidRequestError(operation)

    @staticmethod
    def _validate_language(language: str, operation: str) -> None:
        if _LANGUAGE_PATTERN.fullmatch(language) is None:
            raise InvalidRequestError(operation)

    def _validate_texts(
        self,
        texts: tuple[str, ...],
        operation: str,
        *,
        allow_empty: bool,
    ) -> None:
        total = 0
        for value in texts:
            if len(value) > self._max_sentence_characters:
                raise InvalidRequestError(operation)
            if not allow_empty and not value.strip():
                raise InvalidRequestError(operation)
            total += len(value.encode("utf-8"))
            if total > self._max_payload_bytes:
                raise InvalidRequestError(operation)


__all__ = ["CapabilityReporter", "CoverageWeightingLabService", "LabEngine"]
