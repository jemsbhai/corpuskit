"""Curated, bounded, failure-isolated multilingual CorpusGen demo."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from corpuskit.domain.corpus import (
    CorpusEvaluation,
    CoverageUnit,
    EvaluationTarget,
    G2PTranscription,
    Inventory,
)
from corpuskit.domain.errors import ApplicationError, EngineContractError
from corpuskit.domain.multilingual_demo import (
    DemoCaseId,
    DemoEvaluationSummary,
    DemoInventorySummary,
    DemoSelectionSummary,
    MultilingualDemoCaseResult,
    MultilingualDemoRequest,
    MultilingualDemoResult,
    WritingSystem,
)
from corpuskit.domain.selection import (
    CorpusSelection,
    SelectionAlgorithm,
    SelectionOptions,
    SelectionRequest,
)

CORPUSGEN_CONTRACT_VERSION = "0.1.7"


@dataclass(frozen=True, slots=True)
class _DemoSpec:
    case: DemoCaseId
    language: str
    writing_system: WritingSystem
    sentences: tuple[str, ...]


_DEMO_SPECS = (
    _DemoSpec(
        DemoCaseId.LATIN_ENGLISH,
        "en-us",
        WritingSystem.LATIN,
        (
            "Pack my box with five dozen liquor jugs.",
            "The quick brown fox jumps over the lazy dog.",
        ),
    ),
    _DemoSpec(
        DemoCaseId.ARABIC,
        "ar",
        WritingSystem.ARABIC,
        (
            "مرحبا بالعالم.",
            "هذا اختبار لمجموعة أصوات عربية.",
        ),
    ),
    _DemoSpec(
        DemoCaseId.INDIC_DEVANAGARI,
        "hi",
        WritingSystem.DEVANAGARI,
        (
            "नमस्ते दुनिया।",
            "यह वाक्य ध्वनियों की जाँच करता है।",
        ),
    ),
    _DemoSpec(
        DemoCaseId.CJK_MANDARIN,
        "cmn",
        WritingSystem.HAN,
        (
            "你好，世界。",  # noqa: RUF001 - native-script punctuation is intentional.
            "这是一个语音语料测试。",
        ),
    ),
    _DemoSpec(
        DemoCaseId.TONAL_VIETNAMESE,
        "vi",
        WritingSystem.LATIN_TONAL,
        (
            "Xin chào thế giới.",
            "Đây là một bài kiểm tra ngữ âm.",
        ),
    ),
)


class MultilingualDemoEngine(Protocol):
    def phonemize_batch(
        self,
        texts: Sequence[str],
        *,
        language: str = "en-us",
    ) -> tuple[G2PTranscription, ...]: ...

    def get_inventory(self, language: str, *, source: str | None = None) -> Inventory: ...

    def evaluate(
        self,
        sentences: Sequence[str],
        *,
        language: str = "en-us",
        unit: CoverageUnit = CoverageUnit.PHONEME,
        target: EvaluationTarget | None = None,
    ) -> CorpusEvaluation: ...

    def select(self, request: SelectionRequest) -> CorpusSelection: ...


class MultilingualDemoService:
    """Run fixed non-user-controlled examples and preserve per-language failures."""

    def __init__(self, engine: MultilingualDemoEngine) -> None:
        self._engine = engine

    def run(self, request: MultilingualDemoRequest) -> MultilingualDemoResult:
        requested = set(request.cases)
        specs = tuple(spec for spec in _DEMO_SPECS if not requested or spec.case in requested)
        results = tuple(self._run_case(spec) for spec in specs)
        passed_count = sum(result.passed for result in results)
        return MultilingualDemoResult(
            corpusgen_version=CORPUSGEN_CONTRACT_VERSION,
            passed=passed_count == len(results),
            case_count=len(results),
            passed_count=passed_count,
            cases=results,
        )

    def _run_case(self, spec: _DemoSpec) -> MultilingualDemoCaseResult:
        try:
            inventory = self._engine.get_inventory(spec.language)
            transcriptions = self._engine.phonemize_batch(
                spec.sentences,
                language=spec.language,
            )
            self._validate_transcriptions(spec, transcriptions)
            evaluation = self._engine.evaluate(
                spec.sentences,
                language=spec.language,
                unit=CoverageUnit.PHONEME,
                target=EvaluationTarget(),
            )
            selection = self._engine.select(
                SelectionRequest(
                    candidates=spec.sentences,
                    language=spec.language,
                    target=EvaluationTarget(),
                    options=SelectionOptions(
                        algorithm=SelectionAlgorithm.GREEDY,
                        max_sentences=len(spec.sentences),
                    ),
                )
            )
            self._validate_results(evaluation, selection)
        except ApplicationError as error:
            return MultilingualDemoCaseResult(
                case=spec.case,
                language=spec.language,
                writing_system=spec.writing_system,
                sentences=spec.sentences,
                passed=False,
                failure_code=error.code,
                disclosure=(
                    "The runtime could not complete this fixed case. This usually means the "
                    "eSpeak build lacks usable native-script support or the pinned PHOIBLE "
                    "snapshot cannot resolve the voice. No coverage claim is made."
                ),
            )

        unique_phonemes = tuple(
            sorted({phoneme for row in transcriptions for phoneme in row.phonemes})
        )
        return MultilingualDemoCaseResult(
            case=spec.case,
            language=spec.language,
            writing_system=spec.writing_system,
            sentences=spec.sentences,
            passed=True,
            inventory=DemoInventorySummary(
                language_name=inventory.language_name,
                iso639_3=inventory.iso639_3,
                source=inventory.source,
                segment_count=inventory.size,
                tone_count=inventory.tone_count,
            ),
            transcriptions=transcriptions,
            unique_phonemes=unique_phonemes,
            evaluation=DemoEvaluationSummary(
                coverage=evaluation.coverage,
                target_count=len(evaluation.target_units),
                covered_count=len(evaluation.covered_units),
                missing_count=len(evaluation.missing_units),
                sentence_count=evaluation.total_sentences,
            ),
            selection=DemoSelectionSummary(
                algorithm=selection.algorithm.value,
                coverage=selection.coverage,
                selected_indices=selection.selected_indices,
                selected_sentences=selection.selected_sentences,
                missing_count=len(selection.missing_units),
            ),
            disclosure=(
                "G2P evidence comes from the pinned eSpeak runtime; inventory counts come from "
                "the independently pinned PHOIBLE snapshot and are not asserted to match the "
                "observed sentence inventory."
            ),
        )

    @staticmethod
    def _validate_transcriptions(
        spec: _DemoSpec,
        transcriptions: tuple[G2PTranscription, ...],
    ) -> None:
        if len(transcriptions) != len(spec.sentences) or any(
            not row.ipa or not row.phonemes for row in transcriptions
        ):
            raise EngineContractError("demo.multilingual.g2p")

    @staticmethod
    def _validate_results(
        evaluation: CorpusEvaluation,
        selection: CorpusSelection,
    ) -> None:
        if (
            not evaluation.target_units
            or evaluation.coverage != 1.0
            or evaluation.missing_units
            or selection.coverage != 1.0
            or selection.missing_units
            or not selection.selected_sentences
        ):
            raise EngineContractError("demo.multilingual.coverage")


__all__ = [
    "CORPUSGEN_CONTRACT_VERSION",
    "MultilingualDemoEngine",
    "MultilingualDemoService",
]
