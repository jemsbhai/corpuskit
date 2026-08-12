"""Immutable corpus import contracts and normalization rules."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CorpusImportLimits(BaseModel):
    """Server-enforced import limits copied into validation context."""

    model_config = ConfigDict(frozen=True)

    max_sentences: int = Field(ge=1)
    max_sentence_characters: int = Field(ge=1)


class PreparedSentence(BaseModel):
    """A normalized sentence paired with its original user text."""

    model_config = ConfigDict(frozen=True)

    ordinal: int = Field(ge=0)
    original_text: str
    normalized_text: str


class PreparedCorpus(BaseModel):
    """Deterministic import payload ready for immutable persistence."""

    model_config = ConfigDict(frozen=True)

    language: str = Field(min_length=1, max_length=64)
    sentences: tuple[PreparedSentence, ...]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CorpusImportRequest(BaseModel):
    """External request accepted by corpus import services."""

    model_config = ConfigDict(frozen=True)

    language: str = Field(default="en-us", min_length=1, max_length=64)
    sentences: tuple[str, ...]

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        normalized = value.strip().lower().replace("_", "-")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", normalized):
            raise ValueError("language must be a valid eSpeak-style language code")
        return normalized


def normalize_sentence(text: str) -> str:
    """Normalize Unicode and whitespace without erasing the original text."""

    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return " ".join(normalized.split())


def prepare_corpus(request: CorpusImportRequest, limits: CorpusImportLimits) -> PreparedCorpus:
    """Validate, normalize, de-duplicate, and hash a corpus import deterministically."""

    if len(request.sentences) > limits.max_sentences:
        raise ValueError(f"sentence count exceeds the configured limit ({limits.max_sentences})")

    prepared: list[PreparedSentence] = []
    seen: set[str] = set()
    for original in request.sentences:
        normalized = normalize_sentence(original)
        if not normalized or normalized in seen:
            continue
        if len(normalized) > limits.max_sentence_characters:
            raise ValueError(
                "a sentence exceeds the configured character limit "
                f"({limits.max_sentence_characters})"
            )
        seen.add(normalized)
        prepared.append(
            PreparedSentence(
                ordinal=len(prepared),
                original_text=original,
                normalized_text=normalized,
            )
        )

    if not prepared:
        raise ValueError("a corpus must contain at least one non-blank sentence")

    canonical = {
        "language": request.language,
        "sentences": [sentence.normalized_text for sentence in prepared],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return PreparedCorpus(
        language=request.language,
        sentences=tuple(prepared),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )
