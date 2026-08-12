"""Curated multilingual demo domain, service, and HTTP contracts."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from corpuskit.api.multilingual_demo import multilingual_demo_router
from corpuskit.domain.corpus import (
    CorpusEvaluation,
    CoverageUnit,
    EvaluationTarget,
    EvaluationTargetMode,
    G2PTranscription,
    Inventory,
)
from corpuskit.domain.errors import ApplicationErrorCode, EngineUnavailableError
from corpuskit.domain.multilingual_demo import (
    DemoCaseId,
    MultilingualDemoCaseResult,
    MultilingualDemoRequest,
    MultilingualDemoResult,
    WritingSystem,
)
from corpuskit.domain.selection import (
    CorpusSelection,
    SelectionAlgorithm,
    SelectionMetadata,
    SelectionRequest,
)
from corpuskit.services.multilingual_demo import MultilingualDemoService


class FakeDemoEngine:
    def __init__(
        self,
        *,
        fail_language: str | None = None,
        empty_language: str | None = None,
        invalid_coverage_language: str | None = None,
    ) -> None:
        self.fail_language = fail_language
        self.empty_language = empty_language
        self.invalid_coverage_language = invalid_coverage_language
        self.languages: list[str] = []

    def phonemize_batch(
        self,
        texts: Sequence[str],
        *,
        language: str = "en-us",
    ) -> tuple[G2PTranscription, ...]:
        self.languages.append(language)
        if language == self.fail_language:
            raise EngineUnavailableError("demo.fake")
        empty = language == self.empty_language
        return tuple(
            G2PTranscription(
                text=text,
                language=language,
                ipa="" if empty else "p a",
                phonemes=() if empty else ("p", "a"),
                diphones=() if empty else ("p a",),
                triphones=(),
                phoneme_count=0 if empty else 2,
                unique_phonemes=() if empty else ("a", "p"),
            )
            for text in texts
        )

    @staticmethod
    def get_inventory(language: str, *, source: str | None = None) -> Inventory:
        del source
        tones = ("˥",) if language in {"cmn", "vi"} else ()
        return Inventory(
            inventory_id=1,
            language_name=f"Language {language}",
            iso639_3={
                "en-us": "eng",
                "ar": "arb",
                "hi": "hin",
                "cmn": "cmn",
                "vi": "vie",
            }[language],
            glottocode="test1234",
            specific_dialect=None,
            source="phoible",
            segments=(),
            phonemes=("p", "a", *tones),
            consonants=("p",),
            vowels=("a",),
            tones=tones,
            marginal_phonemes=(),
            size=2 + len(tones),
            consonant_count=1,
            vowel_count=1,
            tone_count=len(tones),
        )

    def evaluate(
        self,
        sentences: Sequence[str],
        *,
        language: str = "en-us",
        unit: CoverageUnit = CoverageUnit.PHONEME,
        target: EvaluationTarget | None = None,
    ) -> CorpusEvaluation:
        del target
        invalid = language == self.invalid_coverage_language
        return CorpusEvaluation(
            language=language,
            unit=unit,
            target_mode=EvaluationTargetMode.DERIVED,
            target_units=("a", "p"),
            covered_units=("a",) if invalid else ("a", "p"),
            missing_units=("p",) if invalid else (),
            coverage=0.5 if invalid else 1.0,
            total_sentences=len(sentences),
            unit_counts=(),
            sentence_details=(),
            unit_sources=(),
            distribution=None,
            text_quality=None,
        )

    def select(self, request: SelectionRequest) -> CorpusSelection:
        invalid = request.language == self.invalid_coverage_language
        return CorpusSelection(
            selected_indices=(0,),
            selected_sentences=(request.candidates[0],),
            coverage=0.5 if invalid else 1.0,
            covered_units=("a",) if invalid else ("a", "p"),
            missing_units=("p",) if invalid else (),
            unit=request.unit,
            target_mode=EvaluationTargetMode.DERIVED,
            algorithm=SelectionAlgorithm.GREEDY,
            elapsed_seconds=0,
            iterations=1,
            metadata=SelectionMetadata(),
        )


def test_full_suite_runs_all_writing_systems_and_tonal_inventories() -> None:
    engine = FakeDemoEngine()
    result = MultilingualDemoService(engine).run(MultilingualDemoRequest())

    assert result.passed is True
    assert result.case_count == result.passed_count == 5
    assert [item.case for item in result.cases] == list(DemoCaseId)
    assert {item.writing_system for item in result.cases} == set(WritingSystem)
    assert engine.languages == ["en-us", "ar", "hi", "cmn", "vi"]
    assert all(item.evaluation and item.evaluation.coverage == 1 for item in result.cases)
    assert all(item.selection and item.selection.coverage == 1 for item in result.cases)
    assert all(item.transcriptions and item.unique_phonemes for item in result.cases)
    tonal = {item.case: item.inventory for item in result.cases}
    assert tonal[DemoCaseId.CJK_MANDARIN].tone_count == 1  # type: ignore[union-attr]
    assert tonal[DemoCaseId.TONAL_VIETNAMESE].tone_count == 1  # type: ignore[union-attr]


def test_case_subset_preserves_catalogue_order_not_request_order() -> None:
    result = MultilingualDemoService(FakeDemoEngine()).run(
        MultilingualDemoRequest(
            cases=(DemoCaseId.TONAL_VIETNAMESE, DemoCaseId.ARABIC),
        )
    )

    assert [item.case for item in result.cases] == [
        DemoCaseId.ARABIC,
        DemoCaseId.TONAL_VIETNAMESE,
    ]


@pytest.mark.parametrize(
    ("engine", "case", "code"),
    [
        (
            FakeDemoEngine(fail_language="hi"),
            DemoCaseId.INDIC_DEVANAGARI,
            ApplicationErrorCode.ENGINE_UNAVAILABLE,
        ),
        (
            FakeDemoEngine(empty_language="ar"),
            DemoCaseId.ARABIC,
            ApplicationErrorCode.ENGINE_CONTRACT_VIOLATION,
        ),
        (
            FakeDemoEngine(invalid_coverage_language="cmn"),
            DemoCaseId.CJK_MANDARIN,
            ApplicationErrorCode.ENGINE_CONTRACT_VIOLATION,
        ),
    ],
)
def test_one_case_failure_is_sanitized_and_does_not_abort_suite(
    engine: FakeDemoEngine,
    case: DemoCaseId,
    code: ApplicationErrorCode,
) -> None:
    result = MultilingualDemoService(engine).run(MultilingualDemoRequest())
    failed = next(item for item in result.cases if item.case is case)

    assert result.passed is False
    assert result.passed_count == 4
    assert failed.passed is False
    assert failed.failure_code is code
    assert failed.inventory is None
    assert failed.transcriptions == ()
    assert "No coverage claim" in failed.disclosure
    assert all(item.passed for item in result.cases if item.case is not case)


def test_request_and_result_summary_invariants_fail_closed() -> None:
    with pytest.raises(ValidationError):
        MultilingualDemoRequest(cases=(DemoCaseId.ARABIC, DemoCaseId.ARABIC))
    with pytest.raises(ValidationError):
        MultilingualDemoCaseResult(
            case=DemoCaseId.ARABIC,
            language="ar",
            writing_system=WritingSystem.ARABIC,
            sentences=("مرحبا",),
            passed=True,
            disclosure="invalid",
        )
    with pytest.raises(ValidationError):
        MultilingualDemoResult(
            corpusgen_version="0.1.7",
            passed=True,
            case_count=2,
            passed_count=0,
            cases=(
                MultilingualDemoCaseResult(
                    case=DemoCaseId.ARABIC,
                    language="ar",
                    writing_system=WritingSystem.ARABIC,
                    sentences=("مرحبا",),
                    passed=False,
                    failure_code=ApplicationErrorCode.ENGINE_UNAVAILABLE,
                    disclosure="unavailable",
                ),
            ),
        )


def test_http_endpoint_accepts_only_catalogued_cases() -> None:
    app = FastAPI()
    app.include_router(
        multilingual_demo_router(MultilingualDemoService(FakeDemoEngine())),
        prefix="/api/v1",
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/labs/demos/multilingual",
        json={"cases": ["arabic"]},
    )
    invalid = client.post(
        "/api/v1/labs/demos/multilingual",
        json={"cases": ["arbitrary-user-text"]},
    )

    assert response.status_code == 200
    assert response.json()["cases"][0]["case"] == "arabic"
    assert invalid.status_code == 422
