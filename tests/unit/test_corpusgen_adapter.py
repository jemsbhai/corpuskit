"""Contract tests for the typed CorpusGen adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.adapters.corpusgen.client import CorpusgenAdapter
from corpuskit.domain import (
    ApplicationError,
    CorpusEvaluation,
    CoverageUnit,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    EvaluationTarget,
    EvaluationTargetMode,
    G2PTranscription,
    InvalidRequestError,
    InventoryDataUnavailableError,
    InventoryNotFoundError,
    LanguageNotSupportedError,
)


class FakeG2PResult:
    """Small object implementing the public G2P result contract."""

    def __init__(self, text: str, language: str, phonemes: list[str]) -> None:
        self.text = text
        self.language = language
        self.ipa = " ".join(phonemes)
        self.phonemes = phonemes

    @property
    def diphones(self) -> list[str]:
        return [
            f"{left}-{right}" for left, right in zip(self.phonemes, self.phonemes[1:], strict=False)
        ]

    @property
    def triphones(self) -> list[str]:
        return [
            f"{first}-{second}-{third}"
            for first, second, third in zip(
                self.phonemes,
                self.phonemes[1:],
                self.phonemes[2:],
                strict=False,
            )
        ]

    @property
    def phoneme_count(self) -> int:
        return len(self.phonemes)

    @property
    def unique_phonemes(self) -> set[str]:
        return set(self.phonemes)


class FakeG2PManager:
    """Deterministic manager that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def phonemize(self, text: str, language: str = "en-us") -> FakeG2PResult:
        self.calls.append(((text,), language))
        return FakeG2PResult(text, language, ["k", "æ", "t"])

    def phonemize_batch(
        self,
        texts: list[str],
        language: str = "en-us",
    ) -> list[FakeG2PResult]:
        self.calls.append((tuple(texts), language))
        return [
            FakeG2PResult(text, language, ["t", str(index)]) for index, text in enumerate(texts)
        ]


def _distribution() -> SimpleNamespace:
    return SimpleNamespace(
        entropy=1.0,
        normalized_entropy=0.9,
        jsd_uniform=0.1,
        coefficient_of_variation=0.2,
        min_count=1,
        max_count=3,
        count_ratio=1 / 3,
        zero_count=1,
        pcd_uniform=0.6,
        jsd_reference=None,
        pearson_correlation=None,
    )


def _text_quality() -> SimpleNamespace:
    return SimpleNamespace(
        sentence_length_words_mean=2.0,
        sentence_length_words_median=2.0,
        sentence_length_words_std=0.0,
        sentence_length_words_min=2,
        sentence_length_words_max=2,
        sentence_length_phonemes_mean=3.0,
        sentence_length_phonemes_median=3.0,
        sentence_length_phonemes_std=0.0,
        sentence_length_phonemes_min=3,
        sentence_length_phonemes_max=3,
        total_words=2,
        unique_words=2,
        type_token_ratio=1.0,
        hapax_ratio=1.0,
        flesch_reading_ease=90.0,
        flesch_kincaid_grade=1.0,
    )


def _report(unit: str = "phoneme") -> SimpleNamespace:
    return SimpleNamespace(
        language="en-us",
        unit=unit,
        target_phonemes=["k", "æ", "t"],
        covered_phonemes={"æ", "k"},
        missing_phonemes={"t"},
        coverage=2 / 3,
        phoneme_counts={"æ": 1, "k": 2},
        total_sentences=1,
        sentence_details=[
            SimpleNamespace(
                index=0,
                text="A cat.",
                phoneme_count=3,
                new_phonemes=["k", "æ"],
                all_phonemes=["k", "æ", "t"],
            )
        ],
        phoneme_sources={"æ": [0], "k": [0]},
        distribution=_distribution(),
        text_quality=_text_quality(),
    )


def _inventory() -> SimpleNamespace:
    segments = [
        SimpleNamespace(
            phoneme="m",
            segment_class="consonant",
            marginal=False,
            allophones=["ɱ"],
            features={"nasal": "+", "tone": "0"},
            glyph_id="006D",
        ),
        SimpleNamespace(
            phoneme="a",
            segment_class="vowel",
            marginal=True,
            allophones=[],
            features={"nasal": "-", "tone": "0"},
            glyph_id="0061",
        ),
    ]
    return SimpleNamespace(
        inventory_id=1,
        language_name="Example",
        iso639_3="exm",
        glottocode="exam1234",
        specific_dialect=None,
        source="test",
        segments=segments,
        phonemes=["m", "a"],
        consonants=["m"],
        vowels=["a"],
        tones=[],
        marginal_phonemes=["a"],
        size=2,
        consonant_count=1,
        vowel_count=1,
        tone_count=0,
    )


def test_single_and_batch_g2p_are_normalized_and_immutable() -> None:
    manager = FakeG2PManager()
    adapter = CorpusgenAdapter(g2p_factory=lambda: manager)

    single = adapter.phonemize("cat", language="en-us")
    batch = adapter.phonemize_batch(["one", "two"], language="fr-fr")

    assert single == G2PTranscription(
        text="cat",
        language="en-us",
        ipa="k æ t",
        phonemes=("k", "æ", "t"),
        diphones=("k-æ", "æ-t"),
        triphones=("k-æ-t",),
        phoneme_count=3,
        unique_phonemes=("k", "t", "æ"),
    )
    assert tuple(item.text for item in batch) == ("one", "two")
    assert manager.calls == [(("cat",), "en-us"), (("one", "two"), "fr-fr")]
    with pytest.raises(ValidationError):
        single.text = "changed"


@pytest.mark.parametrize(
    ("mode", "unit", "expected_target"),
    [
        (EvaluationTargetMode.DERIVED, CoverageUnit.PHONEME, None),
        (EvaluationTargetMode.PHOIBLE, CoverageUnit.DIPHONE, "phoible"),
        (EvaluationTargetMode.EXPLICIT, CoverageUnit.TRIPHONE, ["k", "æ", "t"]),
    ],
)
def test_evaluation_target_modes_and_units_are_forwarded_and_normalized(
    mode: EvaluationTargetMode,
    unit: CoverageUnit,
    expected_target: list[str] | str | None,
) -> None:
    calls: list[dict[str, Any]] = []

    def evaluator(
        sentences: list[str],
        language: str = "en-us",
        target_phonemes: list[str] | str | None = None,
        unit: str = "phoneme",
    ) -> Any:
        calls.append(
            {
                "sentences": sentences,
                "language": language,
                "target": target_phonemes,
                "unit": unit,
            }
        )
        return _report(unit)

    target = (
        EvaluationTarget(mode=mode, phonemes=("k", "æ", "t"))
        if mode is EvaluationTargetMode.EXPLICIT
        else EvaluationTarget(mode=mode)
    )
    result = CorpusgenAdapter(evaluator=evaluator).evaluate(
        ["A cat."],
        language="en-us",
        unit=unit,
        target=target,
    )

    assert isinstance(result, CorpusEvaluation)
    assert calls == [
        {
            "sentences": ["A cat."],
            "language": "en-us",
            "target": expected_target,
            "unit": unit.value,
        }
    ]
    assert result.target_mode is mode
    assert result.unit is unit
    assert result.target_units == ("k", "t", "æ")
    assert result.covered_units == ("k", "æ")
    assert [(item.unit, item.count) for item in result.unit_counts] == [("k", 2), ("æ", 1)]
    assert result.sentence_details[0].new_units == ("k", "æ")
    assert result.distribution is not None
    assert result.distribution.pcd_uniform == 0.6
    assert result.text_quality is not None
    assert result.text_quality.total_words == 2


def test_evaluation_allows_absent_optional_metric_groups() -> None:
    report = _report()
    report.distribution = None
    report.text_quality = None

    result = CorpusgenAdapter(evaluator=lambda *args, **kwargs: report).evaluate(["text"])

    assert result.distribution is None
    assert result.text_quality is None


def test_inventory_is_deeply_normalized_without_mutable_mappings() -> None:
    calls: list[tuple[str, str | None]] = []

    def loader(language: str, source: str | None = None) -> Any:
        calls.append((language, source))
        return _inventory()

    result = CorpusgenAdapter(inventory_loader=loader).get_inventory("exm", source="test")

    assert calls == [("exm", "test")]
    assert result.language_name == "Example"
    assert result.phonemes == ("m", "a")
    assert result.marginal_phonemes == ("a",)
    assert result.segments[0].allophones == ("ɱ",)
    assert [(item.name, item.value) for item in result.segments[0].features] == [
        ("nasal", "+"),
        ("tone", "0"),
    ]


@pytest.mark.parametrize(
    ("engine_error", "expected_type"),
    [
        (FileNotFoundError("C:/private/phoible.csv"), InventoryDataUnavailableError),
        (ImportError("private dependency"), DependencyUnavailableError),
        (ValueError("private validation"), InvalidRequestError),
        (RuntimeError("private runtime"), EngineUnavailableError),
        (OSError("private OS detail"), EngineUnavailableError),
        (Exception("private unknown"), EngineUnavailableError),
    ],
)
def test_engine_failures_are_typed_and_do_not_leak_raw_text(
    engine_error: Exception,
    expected_type: type[ApplicationError],
) -> None:
    def evaluator(*args: Any, **kwargs: Any) -> Any:
        raise engine_error

    with pytest.raises(expected_type) as captured:
        CorpusgenAdapter(evaluator=evaluator).evaluate(["text"])

    assert "private" not in str(captured.value)
    assert captured.value.operation == "corpus.evaluate"


def test_key_errors_are_specific_to_language_or_inventory() -> None:
    class BrokenManager(FakeG2PManager):
        def phonemize(self, text: str, language: str = "en-us") -> FakeG2PResult:
            raise KeyError("private voice")

    def missing_inventory(language: str, source: str | None = None) -> Any:
        raise KeyError("private inventory")

    with pytest.raises(LanguageNotSupportedError):
        CorpusgenAdapter(g2p_factory=BrokenManager).phonemize("text", language="xx")
    with pytest.raises(InventoryNotFoundError):
        CorpusgenAdapter(inventory_loader=missing_inventory).get_inventory("xxx")


def test_invalid_application_inputs_fail_before_engine_calls() -> None:
    with pytest.raises(InvalidRequestError):
        CorpusgenAdapter().phonemize("text", language=" ")
    with pytest.raises(InvalidRequestError):
        CorpusgenAdapter().evaluate([])
    with pytest.raises(InvalidRequestError):
        CorpusgenAdapter().get_inventory("eng", source="")
    with pytest.raises(ValidationError):
        EvaluationTarget(mode=EvaluationTargetMode.EXPLICIT)
    with pytest.raises(ValidationError):
        EvaluationTarget(mode=EvaluationTargetMode.DERIVED, phonemes=("p",))
    with pytest.raises(ValidationError):
        EvaluationTarget(mode=EvaluationTargetMode.EXPLICIT, phonemes=("p", "p"))


def test_contract_mismatches_become_stable_contract_errors() -> None:
    class ShortBatchManager(FakeG2PManager):
        def phonemize_batch(
            self,
            texts: list[str],
            language: str = "en-us",
        ) -> list[FakeG2PResult]:
            return []

    malformed = SimpleNamespace(text="text")
    wrong_unit = _report("triphone")
    invalid_inventory = _inventory()
    invalid_inventory.segments[0].features = {"nasal": "invalid"}

    with pytest.raises(EngineContractError):
        CorpusgenAdapter(g2p_factory=ShortBatchManager).phonemize_batch(["text"])
    with pytest.raises(EngineContractError):
        CorpusgenAdapter(
            g2p_factory=lambda: SimpleNamespace(phonemize=lambda *args, **kwargs: malformed)
        ).phonemize("text")
    with pytest.raises(EngineContractError):
        CorpusgenAdapter(evaluator=lambda *args, **kwargs: wrong_unit).evaluate(
            ["text"], unit=CoverageUnit.PHONEME
        )
    with pytest.raises(EngineContractError):
        CorpusgenAdapter(inventory_loader=lambda *args, **kwargs: invalid_inventory).get_inventory(
            "eng"
        )
