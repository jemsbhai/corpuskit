"""Bounded application workflows for interactive CorpusGen operations."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from corpuskit.config import Settings
from corpuskit.domain import (
    CorpusEvaluation,
    CorpusSelection,
    CoverageUnit,
    EvaluationTarget,
    G2PTranscription,
    InvalidRequestError,
    SelectionOptions,
    SelectionRequest,
)

_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")
MAX_SYNC_G2P_ITEMS = 500
MAX_SYNC_EVALUATION_SENTENCES = 500
MAX_SYNC_SELECTION_CANDIDATES = 2_000
MAX_SYNC_TARGET_UNITS = 100_000


class CorpusWorkflowEngine(Protocol):
    """Adapter surface consumed by interactive application services."""

    def phonemize(self, text: str, *, language: str = "en-us") -> G2PTranscription: ...

    def phonemize_batch(
        self,
        texts: Sequence[str],
        *,
        language: str = "en-us",
    ) -> tuple[G2PTranscription, ...]: ...

    def evaluate(
        self,
        sentences: Sequence[str],
        *,
        language: str = "en-us",
        unit: CoverageUnit = CoverageUnit.PHONEME,
        target: EvaluationTarget | None = None,
    ) -> CorpusEvaluation: ...

    def select(self, request: SelectionRequest) -> CorpusSelection: ...


class CorpusWorkflowService:
    """Validate bounded synchronous work before entering the engine boundary."""

    def __init__(self, engine: CorpusWorkflowEngine, settings: Settings) -> None:
        self._engine = engine
        self._max_sentence_characters = settings.max_sentence_characters
        self._max_payload_characters = settings.max_upload_bytes

    def phonemize(self, text: str, *, language: str) -> G2PTranscription:
        self._validate_language(language, "g2p.phonemize")
        self._validate_texts((text,), "g2p.phonemize", allow_empty=True)
        return self._engine.phonemize(text, language=language)

    def phonemize_batch(
        self,
        texts: Sequence[str],
        *,
        language: str,
    ) -> tuple[G2PTranscription, ...]:
        operation = "g2p.phonemize_batch"
        self._validate_language(language, operation)
        values = tuple(texts)
        if not values or len(values) > MAX_SYNC_G2P_ITEMS:
            raise InvalidRequestError(operation)
        self._validate_texts(values, operation, allow_empty=True)
        return self._engine.phonemize_batch(values, language=language)

    def evaluate(
        self,
        sentences: Sequence[str],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
    ) -> CorpusEvaluation:
        operation = "corpus.evaluate"
        self._validate_language(language, operation)
        values = tuple(sentences)
        if not values or len(values) > MAX_SYNC_EVALUATION_SENTENCES:
            raise InvalidRequestError(operation)
        self._validate_texts(values, operation, allow_empty=False)
        self._validate_target_size(target, unit, operation)
        return self._engine.evaluate(values, language=language, unit=unit, target=target)

    def select(
        self,
        candidates: Sequence[str],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
        options: SelectionOptions,
    ) -> CorpusSelection:
        operation = "corpus.select"
        self._validate_language(language, operation)
        values = tuple(candidates)
        if not values or len(values) > MAX_SYNC_SELECTION_CANDIDATES:
            raise InvalidRequestError(operation)
        self._validate_texts(values, operation, allow_empty=False)
        self._validate_target_size(target, unit, operation)
        if options.max_sentences is not None and options.max_sentences > len(values):
            raise InvalidRequestError(operation)
        return self._engine.select(
            SelectionRequest(
                candidates=values,
                language=language,
                unit=unit,
                target=target,
                options=options,
            )
        )

    @staticmethod
    def _validate_language(language: str, operation: str) -> None:
        if len(language) > 32 or _LANGUAGE_PATTERN.fullmatch(language) is None:
            raise InvalidRequestError(operation)

    def _validate_texts(
        self,
        texts: Sequence[str],
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
            if total > self._max_payload_characters:
                raise InvalidRequestError(operation)

    @staticmethod
    def _validate_target_size(
        target: EvaluationTarget,
        unit: CoverageUnit,
        operation: str,
    ) -> None:
        if not target.phonemes:
            return
        exponent = {
            CoverageUnit.PHONEME: 1,
            CoverageUnit.DIPHONE: 2,
            CoverageUnit.TRIPHONE: 3,
        }[unit]
        if len(target.phonemes) ** exponent > MAX_SYNC_TARGET_UNITS:
            raise InvalidRequestError(operation)
