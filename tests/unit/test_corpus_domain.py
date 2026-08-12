"""Corpus normalization, validation, and hashing contracts."""

from __future__ import annotations

import pytest

from corpuskit.domain.corpora import (
    CorpusImportLimits,
    CorpusImportRequest,
    normalize_sentence,
    prepare_corpus,
)


def test_normalize_sentence_preserves_unicode_but_collapses_whitespace() -> None:
    assert normalize_sentence("  cafe\u0301\r\n\tworld  ") == "café world"


def test_prepare_corpus_is_deterministic_and_deduplicates_normalized_text() -> None:
    limits = CorpusImportLimits(max_sentences=10, max_sentence_characters=100)
    request = CorpusImportRequest(
        language="EN_us",
        sentences=("  Hello   world ", "Hello world", "", "Second sentence."),
    )

    first = prepare_corpus(request, limits)
    second = prepare_corpus(request, limits)

    assert first == second
    assert first.language == "en-us"
    assert [item.ordinal for item in first.sentences] == [0, 1]
    assert [item.normalized_text for item in first.sentences] == [
        "Hello world",
        "Second sentence.",
    ]
    assert len(first.content_sha256) == 64


def test_hash_changes_with_language_or_order() -> None:
    limits = CorpusImportLimits(max_sentences=10, max_sentence_characters=100)

    english = prepare_corpus(
        CorpusImportRequest(language="en-us", sentences=("One", "Two")), limits
    )
    french = prepare_corpus(CorpusImportRequest(language="fr-fr", sentences=("One", "Two")), limits)
    reversed_corpus = prepare_corpus(
        CorpusImportRequest(language="en-us", sentences=("Two", "One")), limits
    )

    assert len({english.content_sha256, french.content_sha256, reversed_corpus.content_sha256}) == 3


@pytest.mark.parametrize(
    ("sentences", "limits", "message"),
    [
        (("", "  "), CorpusImportLimits(max_sentences=2, max_sentence_characters=10), "at least"),
        (("one", "two"), CorpusImportLimits(max_sentences=1, max_sentence_characters=10), "count"),
        (
            ("too long",),
            CorpusImportLimits(max_sentences=1, max_sentence_characters=3),
            "character",
        ),
    ],
)
def test_prepare_corpus_rejects_invalid_content(
    sentences: tuple[str, ...], limits: CorpusImportLimits, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        prepare_corpus(CorpusImportRequest(sentences=sentences), limits)


def test_language_cannot_be_blank() -> None:
    with pytest.raises(ValueError, match="language"):
        CorpusImportRequest(language=" _ ", sentences=("hello",))
