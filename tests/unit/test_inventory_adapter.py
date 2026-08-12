"""Contract tests for cached PHOIBLE and eSpeak exploration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from corpuskit.adapters.corpusgen.inventory import CorpusgenInventoryAdapter
from corpuskit.domain import (
    EngineContractError,
    InvalidRequestError,
    InventoryDataUnavailableError,
    InventoryNotFoundError,
)


def _inventory(source: str = "upsid", phoneme: str = "m") -> Any:
    segment = SimpleNamespace(
        phoneme=phoneme,
        segment_class="consonant",
        marginal=False,
        allophones=["ɱ"],
        features={"nasal": "+", "tone": "0"},
        glyph_id="006D",
    )
    return SimpleNamespace(
        inventory_id=1,
        language_name="English",
        iso639_3="eng",
        glottocode="stan1293",
        specific_dialect=None,
        source=source,
        segments=[segment],
        phonemes=[phoneme],
        consonants=[phoneme],
        vowels=[],
        tones=[],
        marginal_phonemes=[],
        size=1,
        consonant_count=1,
        vowel_count=0,
        tone_count=0,
    )


class FakeMapping:
    def to_iso(self, code: str) -> str:
        if code.lower() == "en-us":
            return "eng"
        raise KeyError(code)

    def to_espeak(self, iso_code: str) -> list[str]:
        if iso_code == "eng":
            return ["en-gb", "en-us"]
        raise KeyError(iso_code)

    def items(self) -> Any:
        return iter((("en-gb", "eng"), ("en-us", "eng"), ("fr-fr", "fra")))


class FakeDataset:
    csv_exists = True
    is_loaded = False
    inventory_count = 2
    language_count = 1
    segment_count = 2

    def load(self) -> None:
        self.is_loaded = True

    def _languages(self) -> list[dict[str, object]]:
        self.is_loaded = True
        return [
            {
                "iso639_3": "eng",
                "glottocode": "stan1293",
                "language_name": "English",
                "inventory_count": 2,
                "sources": ["phoible", "upsid"],
            }
        ]

    def search(self, name: str) -> list[dict[str, object]]:
        return self._languages() if name.casefold() in "english" else []

    def available_languages(self) -> list[dict[str, object]]:
        return self._languages()

    def sources_for(self, identifier: str) -> list[str]:
        if identifier != "eng":
            raise KeyError(identifier)
        return ["phoible", "upsid"]

    def get_inventory(self, identifier: str, source: str | None = None) -> Any:
        if identifier != "eng" or source == "missing":
            raise KeyError(identifier)
        return _inventory(source or "upsid")

    def get_all_inventories(self, identifier: str) -> list[Any]:
        if identifier != "eng":
            raise KeyError(identifier)
        return [_inventory("upsid"), _inventory("phoible", "n")]

    def get_union_inventory(self, identifier: str) -> Any:
        if identifier != "eng":
            raise KeyError(identifier)
        result = _inventory("union")
        result.inventory_id = 0
        return result


def _adapter(dataset: FakeDataset | None = None) -> CorpusgenInventoryAdapter:
    resolved = dataset or FakeDataset()
    return CorpusgenInventoryAdapter(
        dataset_factory=lambda: resolved,
        mapping_factory=FakeMapping,
        feature_names=("nasal", "tone"),
    )


def test_status_does_not_load_data_and_stats_appear_after_query() -> None:
    adapter = _adapter()

    initial = adapter.status()
    languages = adapter.languages("Eng")
    loaded = adapter.status()

    assert initial.cache_available is True
    assert initial.loaded is False
    assert initial.stats is None
    assert languages[0].iso639_3 == "eng"
    assert loaded.loaded is True
    assert loaded.stats is not None
    assert loaded.stats.inventory_count == 2


def test_explicit_load_is_idempotent_and_returns_statistics_without_a_query() -> None:
    dataset = FakeDataset()
    adapter = _adapter(dataset)

    first = adapter.load()
    second = adapter.load()

    assert first == second
    assert first.loaded is True
    assert first.stats is not None
    assert first.stats.model_dump() == {
        "inventory_count": 2,
        "language_count": 1,
        "segment_count": 2,
    }


def test_mapping_inventory_source_all_union_and_feature_contracts() -> None:
    adapter = _adapter()

    assert [item.espeak_code for item in adapter.mappings("eng")] == ["en-gb", "en-us"]
    assert adapter.sources("en-us") == ("phoible", "upsid")
    assert adapter.inventory("en-us", source="upsid").source == "upsid"
    assert adapter.inventory("eng", union=True).inventory_id == 0
    assert len(adapter.all_inventories("eng")) == 2
    assert adapter.features().names == ("nasal", "tone")
    segment = adapter.inventory("eng").segments[0]
    assert segment.allophones == ("ɱ",)
    assert [(item.name, item.value) for item in segment.features] == [
        ("nasal", "+"),
        ("tone", "0"),
    ]


def test_cached_data_and_lookup_failures_are_safe_and_typed() -> None:
    missing = FakeDataset()

    def unavailable() -> list[dict[str, object]]:
        raise FileNotFoundError("C:/private/phoible.csv")

    missing.available_languages = unavailable  # type: ignore[method-assign]
    with pytest.raises(InventoryDataUnavailableError) as captured:
        _adapter(missing).languages()
    assert "private" not in str(captured.value)

    with pytest.raises(InventoryNotFoundError):
        _adapter().inventory("unknown")
    with pytest.raises(InvalidRequestError):
        _adapter().inventory("eng", source="missing")


def test_malformed_engine_inventory_and_language_metadata_fail_closed() -> None:
    dataset = FakeDataset()
    malformed = dataset._languages()[0]
    malformed["sources"] = [1]
    dataset.available_languages = lambda: [malformed]  # type: ignore[method-assign]
    with pytest.raises(EngineContractError):
        _adapter(dataset).languages()

    dataset = FakeDataset()
    invalid = _inventory()
    invalid.segments[0].features = {"nasal": "invalid"}
    dataset.get_inventory = lambda *args, **kwargs: invalid  # type: ignore[method-assign]
    with pytest.raises(EngineContractError):
        _adapter(dataset).inventory("eng")
