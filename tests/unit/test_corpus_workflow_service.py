"""Application workflow validation and delegation tests."""

from __future__ import annotations

from typing import Any

import pytest

from corpuskit.config import Settings
from corpuskit.domain import (
    CoverageUnit,
    EvaluationTarget,
    InvalidRequestError,
    SelectionOptions,
)
from corpuskit.services import CorpusWorkflowService


class RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def phonemize(self, text: str, *, language: str = "en-us") -> Any:
        self.calls.append(("single", (text, language)))
        return "single"

    def phonemize_batch(self, texts: object, *, language: str = "en-us") -> Any:
        self.calls.append(("batch", (texts, language)))
        return ("batch",)

    def evaluate(self, sentences: object, **kwargs: object) -> Any:
        self.calls.append(("evaluate", (sentences, kwargs)))
        return "evaluation"

    def select(self, request: object) -> Any:
        self.calls.append(("select", request))
        return "selection"


def _service(engine: RecordingEngine | None = None, **settings: object) -> CorpusWorkflowService:
    return CorpusWorkflowService(
        engine or RecordingEngine(),  # type: ignore[arg-type]
        Settings(environment="test", **settings),
    )


def test_workflows_delegate_valid_bounded_inputs_and_preserve_blank_g2p() -> None:
    engine = RecordingEngine()
    service = _service(engine)

    assert service.phonemize("", language="en-us") == "single"
    assert service.phonemize_batch(("one", " "), language="fr-fr") == ("batch",)
    assert (
        service.evaluate(
            ("one",),
            language="en-us",
            unit=CoverageUnit.TRIPHONE,
            target=EvaluationTarget(),
        )
        == "evaluation"
    )
    assert (
        service.select(
            ("one", "two"),
            language="en-us",
            unit=CoverageUnit.PHONEME,
            target=EvaluationTarget(),
            options=SelectionOptions(max_sentences=1),
        )
        == "selection"
    )
    assert [call[0] for call in engine.calls] == ["single", "batch", "evaluate", "select"]


@pytest.mark.parametrize("language", ["", "e", "en_us", "en us", "x" * 33])
def test_language_validation_fails_before_engine(language: str) -> None:
    engine = RecordingEngine()
    with pytest.raises(InvalidRequestError):
        _service(engine).phonemize("text", language=language)
    assert engine.calls == []


def test_text_count_length_payload_and_selection_budget_are_bounded() -> None:
    service = _service(max_sentence_characters=3, max_upload_bytes=5)

    with pytest.raises(InvalidRequestError):
        service.phonemize("long", language="en-us")
    with pytest.raises(InvalidRequestError):
        service.phonemize_batch((), language="en-us")
    with pytest.raises(InvalidRequestError):
        service.phonemize_batch(("aaa", "bbb"), language="en-us")
    with pytest.raises(InvalidRequestError):
        service.evaluate(
            (" ",), language="en-us", unit=CoverageUnit.PHONEME, target=EvaluationTarget()
        )
    with pytest.raises(InvalidRequestError):
        service.select(
            ("one",),
            language="en-us",
            unit=CoverageUnit.PHONEME,
            target=EvaluationTarget(),
            options=SelectionOptions(max_sentences=2),
        )


def test_explicit_combinatorial_target_space_is_bounded() -> None:
    service = _service()
    target = EvaluationTarget(
        mode="explicit",
        phonemes=tuple(f"p{index}" for index in range(47)),
    )

    with pytest.raises(InvalidRequestError):
        service.evaluate(
            ("text",),
            language="en-us",
            unit=CoverageUnit.TRIPHONE,
            target=target,
        )
