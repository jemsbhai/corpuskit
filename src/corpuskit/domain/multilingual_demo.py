"""Stable contracts for CorpusKit's curated multilingual acceptance demo."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from corpuskit.domain.corpus import FrozenDomainModel, G2PTranscription
from corpuskit.domain.errors import ApplicationErrorCode


class DemoCaseId(StrEnum):
    LATIN_ENGLISH = "latin-english"
    ARABIC = "arabic"
    INDIC_DEVANAGARI = "indic-devanagari"
    CJK_MANDARIN = "cjk-mandarin"
    TONAL_VIETNAMESE = "tonal-vietnamese"


class WritingSystem(StrEnum):
    LATIN = "latin"
    ARABIC = "arabic"
    DEVANAGARI = "devanagari"
    HAN = "han"
    LATIN_TONAL = "latin-tonal"


class MultilingualDemoRequest(FrozenDomainModel):
    """Select a stable ordered subset; an empty tuple means the full suite."""

    cases: tuple[DemoCaseId, ...] = Field(default=(), max_length=len(DemoCaseId))

    @model_validator(mode="after")
    def validate_unique_cases(self) -> MultilingualDemoRequest:
        if len(self.cases) != len(set(self.cases)):
            raise ValueError("Multilingual demo cases must be unique.")
        return self


class DemoInventorySummary(FrozenDomainModel):
    language_name: str = Field(min_length=1)
    iso639_3: str = Field(min_length=3, max_length=3)
    source: str = Field(min_length=1)
    segment_count: int = Field(ge=1)
    tone_count: int = Field(ge=0)


class DemoEvaluationSummary(FrozenDomainModel):
    coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    target_count: int = Field(ge=1)
    covered_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    sentence_count: int = Field(ge=1)


class DemoSelectionSummary(FrozenDomainModel):
    algorithm: str = "greedy"
    coverage: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    selected_indices: tuple[int, ...]
    selected_sentences: tuple[str, ...]
    missing_count: int = Field(ge=0)


class MultilingualDemoCaseResult(FrozenDomainModel):
    case: DemoCaseId
    language: str = Field(min_length=2, max_length=32)
    writing_system: WritingSystem
    sentences: tuple[str, ...] = Field(min_length=1, max_length=8)
    passed: bool
    inventory: DemoInventorySummary | None = None
    transcriptions: tuple[G2PTranscription, ...] = ()
    unique_phonemes: tuple[str, ...] = ()
    evaluation: DemoEvaluationSummary | None = None
    selection: DemoSelectionSummary | None = None
    failure_code: ApplicationErrorCode | None = None
    disclosure: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_outcome(self) -> MultilingualDemoCaseResult:
        complete = all(
            value is not None for value in (self.inventory, self.evaluation, self.selection)
        ) and bool(self.transcriptions and self.unique_phonemes)
        if self.passed != complete:
            raise ValueError("A passing demo case requires complete non-empty evidence.")
        if self.passed == (self.failure_code is not None):
            raise ValueError("Only a failed demo case has a failure code.")
        if self.passed and any(not row.phonemes for row in self.transcriptions):
            raise ValueError("Every passing demo sentence requires a phonetic transcription.")
        return self


class MultilingualDemoResult(FrozenDomainModel):
    corpusgen_version: str = Field(min_length=1, max_length=32)
    passed: bool
    case_count: int = Field(ge=1, le=len(DemoCaseId))
    passed_count: int = Field(ge=0, le=len(DemoCaseId))
    cases: tuple[MultilingualDemoCaseResult, ...] = Field(min_length=1, max_length=len(DemoCaseId))

    @model_validator(mode="after")
    def validate_summary(self) -> MultilingualDemoResult:
        actual = sum(item.passed for item in self.cases)
        if self.case_count != len(self.cases) or self.passed_count != actual:
            raise ValueError("Multilingual demo summary counts must match its cases.")
        if self.passed != (actual == len(self.cases)):
            raise ValueError("Multilingual demo status must match all case outcomes.")
        return self


__all__ = [
    "DemoCaseId",
    "DemoEvaluationSummary",
    "DemoInventorySummary",
    "DemoSelectionSummary",
    "MultilingualDemoCaseResult",
    "MultilingualDemoRequest",
    "MultilingualDemoResult",
    "WritingSystem",
]
