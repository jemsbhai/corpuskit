"""Unit and property tests for bounded corpus imports and deterministic exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from corpuskit.domain.corpora import (
    CorpusImportLimits,
    CorpusImportRequest,
    normalize_sentence,
    prepare_corpus,
)
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.domain.workspaces import CorpusExportFormat, CorpusFileFormat, CorpusUpload
from corpuskit.services.project_workspaces import (
    SentenceSnapshot,
    VersionSnapshot,
    build_export,
    parse_corpus_upload,
)

VERSION = VersionSnapshot(
    id=UUID("00000000-0000-4000-8000-000000000011"),
    corpus_id=UUID("00000000-0000-4000-8000-000000000010"),
    parent_version_id=None,
    version_number=1,
    language="en-us",
    sentence_count=2,
    content_sha256="a" * 64,
    corpusgen_version="0.1.7",
    created_at=datetime(2026, 8, 11, tzinfo=UTC),
)
ARABIC_HELLO = "\u0645\u0631\u062d\u0628\u0627"
RUSSIAN_HELLO = "\u041f\u0440\u0438\u0432\u0435\u0442"
PREPARATION_TEXT_ALPHABET = (
    " abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "\t\n\r.,!?'-_"
    "\u00e9\u00df\u03a9\u0416\u4e2d\u6587\u0645\u0631\u062d\u0628\u0627"
    "\u0301\u200d\u202e"
)


def _upload(
    content: bytes,
    *,
    file_format: CorpusFileFormat,
    filename: str | None = None,
    content_type: str | None = None,
    text_column: str | None = None,
) -> CorpusUpload:
    media_types = {
        CorpusFileFormat.TXT: "text/plain",
        CorpusFileFormat.CSV: "text/csv",
        CorpusFileFormat.JSON: "application/json",
    }
    return CorpusUpload(
        name="Imported",
        language="en-us",
        filename=filename or f"sentences.{file_format.value}",
        content_type=content_type or media_types[file_format],
        file_format=file_format,
        content=content,
        text_column=text_column,
    )


@pytest.mark.parametrize(
    ("upload", "expected"),
    [
        (
            _upload(f"Hello\n{ARABIC_HELLO}\n".encode(), file_format=CorpusFileFormat.TXT),
            ("Hello", ARABIC_HELLO),
        ),
        (
            _upload(
                "id,utterance\n1,Hello\n2,Привет\n".encode(),
                file_format=CorpusFileFormat.CSV,
                text_column="utterance",
            ),
            ("Hello", RUSSIAN_HELLO),
        ),
        (
            _upload(
                json.dumps({"sentences": ["Hello", "你好"]}, ensure_ascii=False).encode(),
                file_format=CorpusFileFormat.JSON,
            ),
            ("Hello", "你好"),
        ),
    ],
)
def test_parse_supported_utf8_formats(upload: CorpusUpload, expected: tuple[str, ...]) -> None:
    assert parse_corpus_upload(upload) == expected


@pytest.mark.parametrize(
    "upload",
    [
        _upload(b"hello", file_format=CorpusFileFormat.TXT, filename="corpus.csv"),
        _upload(
            b"hello", file_format=CorpusFileFormat.TXT, content_type="application/octet-stream"
        ),
        _upload(b"\xff", file_format=CorpusFileFormat.TXT),
        _upload(b"hello", file_format=CorpusFileFormat.TXT, filename="corpus.txt\x00ignored.txt"),
        _upload(b"hello", file_format=CorpusFileFormat.TXT, text_column="text"),
        _upload(b"text\nhello\n", file_format=CorpusFileFormat.CSV),
        _upload(
            b"text,text\nhello,world\n",
            file_format=CorpusFileFormat.CSV,
            text_column="text",
        ),
        _upload(
            b"text\nhello,extra\n",
            file_format=CorpusFileFormat.CSV,
            text_column="text",
        ),
        _upload(
            b'{"sentences":["hello"],"extra":true}',
            file_format=CorpusFileFormat.JSON,
        ),
        _upload(b'{"sentences":[1]}', file_format=CorpusFileFormat.JSON),
        _upload(b"not json", file_format=CorpusFileFormat.JSON),
        _upload(
            b'{"sentences":["hello"]}',
            file_format=CorpusFileFormat.JSON,
            text_column="text",
        ),
    ],
)
def test_parse_rejects_mismatched_or_invalid_uploads(upload: CorpusUpload) -> None:
    with pytest.raises(InvalidRequestError, match="request is not valid"):
        parse_corpus_upload(upload)


def test_csv_parser_rejects_malformed_quoting() -> None:
    upload = _upload(
        b'text\n"unterminated\n',
        file_format=CorpusFileFormat.CSV,
        text_column="text",
    )
    with pytest.raises(InvalidRequestError):
        parse_corpus_upload(upload)


def test_domain_rejects_invalid_language_and_pre_normalization_count() -> None:
    with pytest.raises(ValueError, match="language"):
        CorpusImportRequest(language="../private", sentences=("Hello",))
    request = CorpusImportRequest(sentences=("one", "two"))
    with pytest.raises(ValueError, match="sentence count"):
        prepare_corpus(
            request,
            CorpusImportLimits(max_sentences=1, max_sentence_characters=100),
        )


def test_exports_are_deterministic_unicode_safe_and_spreadsheet_safe() -> None:
    sentences = (
        SentenceSnapshot(ordinal=1, original_text=RUSSIAN_HELLO, normalized_text=RUSSIAN_HELLO),
        SentenceSnapshot(ordinal=0, original_text="=2+3", normalized_text="=2+3"),
    )
    exports = {
        format_: build_export(
            corpus_id=VERSION.corpus_id,
            corpus_name='研究 / "demo"',
            version=VERSION,
            sentences=sentences,
            export_format=format_,
        )
        for format_ in CorpusExportFormat
    }

    assert exports[CorpusExportFormat.TXT].content.decode() == f"=2+3\n{RUSSIAN_HELLO}\n"
    json_value = json.loads(exports[CorpusExportFormat.JSON].content)
    assert [item["text"] for item in json_value["sentences"]] == ["=2+3", RUSSIAN_HELLO]
    csv_rows = list(csv.reader(io.StringIO(exports[CorpusExportFormat.CSV].content.decode())))
    assert csv_rows == [["ordinal", "text"], ["0", "'=2+3"], ["1", RUSSIAN_HELLO]]

    for exported in exports.values():
        assert hashlib.sha256(exported.content).hexdigest() == exported.sha256
        assert exported.content_digest.startswith("sha-256=:")
        assert "\r" not in exported.content_disposition
        assert "\n" not in exported.content_disposition
        assert "filename*=UTF-8''" in exported.content_disposition
        repeated = build_export(
            corpus_id=VERSION.corpus_id,
            corpus_name='研究 / "demo"',
            version=VERSION,
            sentences=sentences,
            export_format=CorpusExportFormat(exported.filename.rsplit(".", 1)[1]),
        )
        assert repeated.content == exported.content
        assert repeated.sha256 == exported.sha256


def test_export_filename_strips_bidi_controls_but_preserves_rtl_letters() -> None:
    exported = build_export(
        corpus_id=VERSION.corpus_id,
        corpus_name=f"{ARABIC_HELLO}-report\u202etxt.exe",
        version=VERSION,
        sentences=(SentenceSnapshot(0, "Hello", "Hello"),),
        export_format=CorpusExportFormat.TXT,
    )
    assert "%E2%80%AE" not in exported.content_disposition.upper()
    assert "txt.exe-v1.txt" in exported.content_disposition
    assert "\r" not in exported.content_disposition
    assert "\n" not in exported.content_disposition


@given(
    st.lists(
        st.text(alphabet=PREPARATION_TEXT_ALPHABET, max_size=80),
        min_size=1,
        max_size=30,
    )
)
def test_preparation_preserves_first_occurrence_and_has_stable_digest(values: list[str]) -> None:
    request = CorpusImportRequest(sentences=tuple(values))
    limits = CorpusImportLimits(max_sentences=30, max_sentence_characters=2_000)
    normalized = []
    for value in values:
        candidate = normalize_sentence(value)
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        with pytest.raises(ValueError, match="at least one"):
            prepare_corpus(request, limits)
        return

    first = prepare_corpus(request, limits)
    second = prepare_corpus(request, limits)
    assert [sentence.normalized_text for sentence in first.sentences] == normalized
    assert [sentence.ordinal for sentence in first.sentences] == list(range(len(normalized)))
    assert first.content_sha256 == second.content_sha256
