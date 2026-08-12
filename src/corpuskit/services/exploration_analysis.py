"""Bounded services for inventory exploration and deterministic analysis."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from corpuskit.config import Settings
from corpuskit.domain import (
    CoverageTrajectory,
    CoverageTrajectoryRequest,
    DistributionAnalysisRequest,
    DistributionMetrics,
    ErrorRatesAnalysis,
    ErrorRatesAnalysisRequest,
    EspeakMappingEntry,
    EspeakMappingPage,
    FeatureCatalog,
    InvalidRequestError,
    Inventory,
    InventoryPage,
    InventorySources,
    LanguagePage,
    LanguageSummary,
    PhoibleStatus,
    SegmentPage,
    TextQualityAnalysisRequest,
    TextQualityMetrics,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
MAX_ANALYSIS_PHONEME_TOKENS = 250_000


class InventoryExplorationEngine(Protocol):
    def status(self) -> PhoibleStatus: ...

    def load(self) -> PhoibleStatus: ...

    def features(self) -> FeatureCatalog: ...

    def languages(self, query: str | None = None) -> tuple[LanguageSummary, ...]: ...

    def mappings(self, query: str | None = None) -> tuple[EspeakMappingEntry, ...]: ...

    def sources(self, identifier: str) -> tuple[str, ...]: ...

    def inventory(
        self, identifier: str, *, source: str | None = None, union: bool = False
    ) -> Inventory: ...

    def all_inventories(self, identifier: str) -> tuple[Inventory, ...]: ...


class AnalysisEngine(Protocol):
    def distribution(self, request: DistributionAnalysisRequest) -> DistributionMetrics: ...

    def text_quality(self, request: TextQualityAnalysisRequest) -> TextQualityMetrics: ...

    def error_rates(self, request: ErrorRatesAnalysisRequest) -> ErrorRatesAnalysis: ...

    def trajectory(self, request: CoverageTrajectoryRequest) -> CoverageTrajectory: ...


class InventoryExplorationService:
    def __init__(self, engine: InventoryExplorationEngine) -> None:
        self._engine = engine

    def status(self) -> PhoibleStatus:
        return self._engine.status()

    def load(self) -> PhoibleStatus:
        return self._engine.load()

    def features(self) -> FeatureCatalog:
        return self._engine.features()

    def languages(self, *, query: str | None, offset: int, limit: int) -> LanguagePage:
        normalized = self._query(query, "inventory.languages")
        items = self._engine.languages(normalized)
        return LanguagePage(
            items=items[offset : offset + limit],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    def mappings(self, *, query: str | None, offset: int, limit: int) -> EspeakMappingPage:
        normalized = self._query(query, "inventory.mappings")
        items = self._engine.mappings(normalized)
        return EspeakMappingPage(
            items=items[offset : offset + limit],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    def sources(self, identifier: str) -> InventorySources:
        self._identifier(identifier)
        return InventorySources(identifier=identifier, sources=self._engine.sources(identifier))

    def inventory(self, identifier: str, *, source: str | None, union: bool) -> Inventory:
        self._identifier(identifier)
        normalized_source = self._source(source, union)
        return self._engine.inventory(identifier, source=normalized_source, union=union)

    def all_inventories(self, identifier: str, *, offset: int, limit: int) -> InventoryPage:
        self._identifier(identifier)
        items = self._engine.all_inventories(identifier)
        return InventoryPage(
            items=items[offset : offset + limit],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    def segments(
        self,
        identifier: str,
        *,
        source: str | None,
        union: bool,
        segment_class: str | None,
        marginal: bool | None,
        feature_name: str | None,
        feature_value: str | None,
        offset: int,
        limit: int,
    ) -> SegmentPage:
        inventory = self.inventory(identifier, source=source, union=union)
        if (feature_name is None) != (feature_value is None):
            raise InvalidRequestError("inventory.segments")
        items = inventory.segments
        if segment_class is not None:
            items = tuple(item for item in items if item.segment_class == segment_class)
        if marginal is not None:
            items = tuple(item for item in items if item.marginal is marginal)
        if feature_name is not None and feature_value is not None:
            items = tuple(
                item
                for item in items
                if any(
                    feature.name == feature_name
                    and self._feature_sequence(feature.value)
                    == self._feature_sequence(feature_value)
                    for feature in item.features
                )
            )
        return SegmentPage(
            items=items[offset : offset + limit],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    @staticmethod
    def _query(value: str | None, operation: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 100:
            raise InvalidRequestError(operation)
        return normalized

    @staticmethod
    def _identifier(identifier: str) -> None:
        if _IDENTIFIER.fullmatch(identifier) is None:
            raise InvalidRequestError("inventory.identifier")

    @staticmethod
    def _source(source: str | None, union: bool) -> str | None:
        if source is None:
            return None
        normalized = source.strip()
        if union or not normalized or len(normalized) > 128:
            raise InvalidRequestError("inventory.get")
        return normalized

    @staticmethod
    def _feature_sequence(value: str) -> tuple[str, ...]:
        """Parse one ordered PHOIBLE contour-feature sequence."""

        return tuple(value.split(","))


class AnalysisService:
    def __init__(self, engine: AnalysisEngine, settings: Settings) -> None:
        self._engine = engine
        self._max_sentence_characters = settings.max_sentence_characters
        self._max_payload_bytes = settings.max_upload_bytes

    def distribution(self, request: DistributionAnalysisRequest) -> DistributionMetrics:
        return self._engine.distribution(request)

    def text_quality(self, request: TextQualityAnalysisRequest) -> TextQualityMetrics:
        self._validate_texts(request.sentences, "analysis.text_quality")
        self._validate_sequences(request.phoneme_sequences, "analysis.text_quality")
        return self._engine.text_quality(request)

    def error_rates(self, request: ErrorRatesAnalysisRequest) -> ErrorRatesAnalysis:
        self._validate_texts(
            (*request.references, *request.hypotheses),
            "analysis.error_rates",
        )
        if request.reference_phonemes is not None and request.hypothesis_phonemes is not None:
            self._validate_sequences(
                (*request.reference_phonemes, *request.hypothesis_phonemes),
                "analysis.error_rates",
            )
        return self._engine.error_rates(request)

    def trajectory(self, request: CoverageTrajectoryRequest) -> CoverageTrajectory:
        self._validate_sequences(request.phoneme_sequences, "analysis.coverage_trajectory")
        return self._engine.trajectory(request)

    def _validate_texts(self, values: Sequence[str], operation: str) -> None:
        total = 0
        for value in values:
            if len(value) > self._max_sentence_characters:
                raise InvalidRequestError(operation)
            total += len(value.encode("utf-8"))
            if total > self._max_payload_bytes:
                raise InvalidRequestError(operation)

    @staticmethod
    def _validate_sequences(values: Sequence[Sequence[str]], operation: str) -> None:
        total = 0
        for sequence in values:
            for token in sequence:
                if not token.strip() or len(token) > 64:
                    raise InvalidRequestError(operation)
                total += 1
                if total > MAX_ANALYSIS_PHONEME_TOKENS:
                    raise InvalidRequestError(operation)


__all__ = [
    "MAX_ANALYSIS_PHONEME_TOKENS",
    "AnalysisEngine",
    "AnalysisService",
    "InventoryExplorationEngine",
    "InventoryExplorationService",
]
