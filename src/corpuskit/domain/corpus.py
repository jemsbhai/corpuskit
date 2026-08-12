"""Immutable domain contracts for CorpusGen-backed workflows."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenDomainModel(BaseModel):
    """Base configuration shared by externally stable domain DTOs."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CoverageUnit(StrEnum):
    """Phonetic unit levels supported by CorpusGen."""

    PHONEME = "phoneme"
    DIPHONE = "diphone"
    TRIPHONE = "triphone"


class EvaluationTargetMode(StrEnum):
    """How an evaluation target inventory is resolved."""

    DERIVED = "derived"
    EXPLICIT = "explicit"
    PHOIBLE = "phoible"


class EvaluationTarget(FrozenDomainModel):
    """Typed target selection for corpus evaluation."""

    mode: EvaluationTargetMode = EvaluationTargetMode.DERIVED
    phonemes: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_mode(self) -> EvaluationTarget:
        """Require symbols only for an explicit target."""

        has_phonemes = bool(self.phonemes)
        if self.mode is EvaluationTargetMode.EXPLICIT and not has_phonemes:
            raise ValueError("An explicit target requires at least one phoneme.")
        if self.mode is not EvaluationTargetMode.EXPLICIT and has_phonemes:
            raise ValueError("Only an explicit target accepts phonemes.")
        if any(not phoneme.strip() for phoneme in self.phonemes):
            raise ValueError("Target phonemes must be non-empty strings.")
        if any(len(phoneme) > 64 for phoneme in self.phonemes):
            raise ValueError("Target phonemes must not exceed 64 characters.")
        if len(set(self.phonemes)) != len(self.phonemes):
            raise ValueError("Target phonemes must be unique.")
        return self


class G2PTranscription(FrozenDomainModel):
    """Normalized grapheme-to-phoneme result."""

    text: str
    language: str
    ipa: str
    phonemes: tuple[str, ...]
    diphones: tuple[str, ...]
    triphones: tuple[str, ...]
    phoneme_count: int = Field(ge=0)
    unique_phonemes: tuple[str, ...]


class PhoneticFeature(FrozenDomainModel):
    """One PHOIBLE distinctive-feature value."""

    name: str = Field(min_length=1)
    value: str = Field(
        min_length=1,
        max_length=15,
        pattern=r"^[+\-0](?:,[+\-0])*$",
    )


class Segment(FrozenDomainModel):
    """Normalized phonological segment."""

    phoneme: str = Field(min_length=1)
    segment_class: Literal["consonant", "vowel", "tone"]
    marginal: bool
    allophones: tuple[str, ...]
    features: tuple[PhoneticFeature, ...]
    glyph_id: str


class Inventory(FrozenDomainModel):
    """Normalized PHOIBLE inventory with immutable nested data."""

    inventory_id: int = Field(ge=0)
    language_name: str = Field(min_length=1)
    iso639_3: str = Field(min_length=1)
    glottocode: str = Field(min_length=1)
    specific_dialect: str | None
    source: str = Field(min_length=1)
    segments: tuple[Segment, ...]
    phonemes: tuple[str, ...]
    consonants: tuple[str, ...]
    vowels: tuple[str, ...]
    tones: tuple[str, ...]
    marginal_phonemes: tuple[str, ...]
    size: int = Field(ge=0)
    consonant_count: int = Field(ge=0)
    vowel_count: int = Field(ge=0)
    tone_count: int = Field(ge=0)


class SentenceCoverage(FrozenDomainModel):
    """Per-sentence contribution to an evaluation result."""

    index: int = Field(ge=0)
    text: str
    phoneme_count: int = Field(ge=0)
    new_units: tuple[str, ...]
    all_phonemes: tuple[str, ...]


class UnitCount(FrozenDomainModel):
    """Occurrence count for one target unit."""

    unit: str = Field(min_length=1)
    count: int = Field(ge=0)


class UnitSources(FrozenDomainModel):
    """Sentence provenance for one target unit."""

    unit: str = Field(min_length=1)
    sentence_indices: tuple[int, ...]


class DistributionMetrics(FrozenDomainModel):
    """Normalized distribution-quality metrics."""

    entropy: float
    normalized_entropy: float
    jsd_uniform: float
    coefficient_of_variation: float
    min_count: int = Field(ge=0)
    max_count: int = Field(ge=0)
    count_ratio: float
    zero_count: int = Field(ge=0)
    pcd_uniform: float
    jsd_reference: float | None
    pearson_correlation: float | None


class TextQualityMetrics(FrozenDomainModel):
    """Normalized corpus text-quality metrics."""

    sentence_length_words_mean: float
    sentence_length_words_median: float
    sentence_length_words_std: float
    sentence_length_words_min: int = Field(ge=0)
    sentence_length_words_max: int = Field(ge=0)
    sentence_length_phonemes_mean: float
    sentence_length_phonemes_median: float
    sentence_length_phonemes_std: float
    sentence_length_phonemes_min: int = Field(ge=0)
    sentence_length_phonemes_max: int = Field(ge=0)
    total_words: int = Field(ge=0)
    unique_words: int = Field(ge=0)
    type_token_ratio: float
    hapax_ratio: float
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None


class CorpusEvaluation(FrozenDomainModel):
    """Stable evaluation result independent of CorpusGen's report model."""

    language: str = Field(min_length=1)
    unit: CoverageUnit
    target_mode: EvaluationTargetMode
    target_units: tuple[str, ...]
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    total_sentences: int = Field(ge=0)
    unit_counts: tuple[UnitCount, ...]
    sentence_details: tuple[SentenceCoverage, ...]
    unit_sources: tuple[UnitSources, ...]
    distribution: DistributionMetrics | None
    text_quality: TextQualityMetrics | None
