"""HTTP acceptance tests for phonology exploration and deterministic analysis."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from corpuskit.adapters.corpusgen.phoible_provisioning import PHOIBLE_COMMIT, PHOIBLE_SHA256
from corpuskit.api.app import create_app
from corpuskit.api.exploration_analysis import InventoryHttpService
from corpuskit.config import Settings
from corpuskit.domain import (
    EspeakMappingEntry,
    FeatureCatalog,
    Inventory,
    InventoryDataUnavailableError,
    LanguageSummary,
    PhoibleDatasetStats,
    PhoibleStatus,
    PhoneticFeature,
    Segment,
)
from corpuskit.services import InventoryExplorationService


def _inventory(source: str = "upsid") -> Inventory:
    return Inventory(
        inventory_id=0 if source == "union" else 1,
        language_name="English",
        iso639_3="eng",
        glottocode="stan1293",
        specific_dialect=None,
        source=source,
        segments=(
            Segment(
                phoneme="m",
                segment_class="consonant",
                marginal=False,
                allophones=("ɱ",),
                features=(
                    PhoneticFeature(name="nasal", value="+"),
                    PhoneticFeature(name="tone", value="0"),
                ),
                glyph_id="006D",
            ),
            Segment(
                phoneme="a",
                segment_class="vowel",
                marginal=True,
                allophones=(),
                features=(
                    PhoneticFeature(name="nasal", value="-"),
                    PhoneticFeature(name="front", value="+,-"),
                    PhoneticFeature(name="tone", value="0"),
                ),
                glyph_id="0061",
            ),
        ),
        phonemes=("m", "a"),
        consonants=("m",),
        vowels=("a",),
        tones=(),
        marginal_phonemes=("a",),
        size=2,
        consonant_count=1,
        vowel_count=1,
        tone_count=0,
    )


class FakeInventoryEngine:
    unavailable = False
    loaded = False

    def status(self) -> PhoibleStatus:
        return PhoibleStatus(
            cache_available=True,
            loaded=self.loaded,
            revision=PHOIBLE_COMMIT,
            sha256=PHOIBLE_SHA256,
            stats=(
                PhoibleDatasetStats(inventory_count=2, language_count=1, segment_count=2)
                if self.loaded
                else None
            ),
        )

    def load(self) -> PhoibleStatus:
        self.loaded = True
        return self.status()

    def features(self) -> FeatureCatalog:
        return FeatureCatalog(names=("nasal", "tone"))

    def languages(self, query: str | None = None) -> tuple[LanguageSummary, ...]:
        if self.unavailable:
            raise InventoryDataUnavailableError("inventory.languages")
        item = LanguageSummary(
            iso639_3="eng",
            glottocode="stan1293",
            language_name="English",
            inventory_count=2,
            sources=("phoible", "upsid"),
        )
        return (item,) if query is None or query.casefold() in "english" else ()

    def mappings(self, query: str | None = None) -> tuple[EspeakMappingEntry, ...]:
        items = (
            EspeakMappingEntry(espeak_code="en-gb", iso639_3="eng"),
            EspeakMappingEntry(espeak_code="en-us", iso639_3="eng"),
        )
        if query is None:
            return items
        return tuple(
            item
            for item in items
            if query.casefold() in item.espeak_code or query.casefold() in item.iso639_3
        )

    def sources(self, identifier: str) -> tuple[str, ...]:
        del identifier
        return ("phoible", "upsid")

    def inventory(
        self, identifier: str, *, source: str | None = None, union: bool = False
    ) -> Inventory:
        del identifier
        return _inventory("union" if union else source or "upsid")

    def all_inventories(self, identifier: str) -> tuple[Inventory, ...]:
        del identifier
        return (_inventory("upsid"), _inventory("phoible"))


def _client(engine: FakeInventoryEngine, **settings: Any) -> httpx.AsyncClient:
    resolved = Settings(environment="test", **settings)

    def inventory_factory(_: Settings) -> InventoryHttpService:
        return InventoryExplorationService(engine)

    app = create_app(resolved, inventory_service_factory=inventory_factory)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_inventory_listing_mapping_lookup_union_and_segment_filters() -> None:
    engine = FakeInventoryEngine()
    async with _client(engine) as client:
        status = await client.get("/api/v1/phonology/status")
        features = await client.get("/api/v1/phonology/features")
        languages = await client.get(
            "/api/v1/phonology/languages", params={"query": "Eng", "limit": 1}
        )
        mappings = await client.get(
            "/api/v1/phonology/espeak-mappings", params={"query": "en", "offset": 1}
        )
        sources = await client.get("/api/v1/phonology/inventories/en-us/sources")
        inventory = await client.get(
            "/api/v1/phonology/inventories/eng", params={"source": "upsid"}
        )
        union = await client.get("/api/v1/phonology/inventories/eng", params={"union": "true"})
        all_items = await client.get("/api/v1/phonology/inventories/eng/all")
        segments = await client.get(
            "/api/v1/phonology/inventories/eng/segments",
            params={
                "segment_class": "consonant",
                "marginal": "false",
                "feature_name": "nasal",
                "feature_value": "+",
            },
        )
        contour_segments = await client.get(
            "/api/v1/phonology/inventories/eng/segments",
            params={"feature_name": "front", "feature_value": "+,-"},
        )

    assert status.json() == {
        "cache_available": True,
        "loaded": False,
        "revision": PHOIBLE_COMMIT,
        "sha256": PHOIBLE_SHA256,
        "stats": None,
    }
    assert features.json() == {"names": ["nasal", "tone"]}
    assert languages.json()["items"][0]["language_name"] == "English"
    assert languages.json()["total"] == 1
    assert mappings.json()["items"][0]["espeak_code"] == "en-us"
    assert sources.json()["sources"] == ["phoible", "upsid"]
    assert inventory.json()["source"] == "upsid"
    assert inventory.json()["segments"][0]["features"][0] == {"name": "nasal", "value": "+"}
    assert union.json()["source"] == "union"
    assert all_items.json()["total"] == 2
    assert [item["phoneme"] for item in segments.json()["items"]] == ["m"]
    assert [item["phoneme"] for item in contour_segments.json()["items"]] == ["a"]


@pytest.mark.asyncio
async def test_explicit_phoible_load_returns_statistics_and_is_idempotent() -> None:
    engine = FakeInventoryEngine()
    async with _client(engine) as client:
        first = await client.post("/api/v1/phonology/load")
        second = await client.post("/api/v1/phonology/load")

    assert first.status_code == 200
    assert (
        first.json()
        == second.json()
        == {
            "cache_available": True,
            "loaded": True,
            "revision": PHOIBLE_COMMIT,
            "sha256": PHOIBLE_SHA256,
            "stats": {"inventory_count": 2, "language_count": 1, "segment_count": 2},
        }
    )


@pytest.mark.asyncio
async def test_inventory_validation_and_no_cache_errors_are_safe() -> None:
    engine = FakeInventoryEngine()
    async with _client(engine) as client:
        invalid = await client.get(
            "/api/v1/phonology/inventories/eng",
            params={"source": "upsid", "union": "true"},
        )
        missing_pair = await client.get(
            "/api/v1/phonology/inventories/eng/segments",
            params={"feature_name": "nasal"},
        )
        engine.unavailable = True
        unavailable = await client.get("/api/v1/phonology/languages")

    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_request"
    assert missing_pair.status_code == 422
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "inventory_data_unavailable"
    assert "private" not in unavailable.text


@pytest.mark.asyncio
async def test_analysis_http_golden_contracts_and_nonfinite_encoding() -> None:
    async with _client(FakeInventoryEngine()) as client:
        distribution = await client.post(
            "/api/v1/analyses/distribution",
            json={
                "counts": [{"unit": "a", "count": 1}, {"unit": "b", "count": 1}],
                "target_units": ["a", "b"],
                "reference_distribution": None,
            },
        )
        quality = await client.post(
            "/api/v1/analyses/text-quality",
            json={"sentences": ["One two."], "phoneme_sequences": [["w", "ʌ", "n"]]},
        )
        rates = await client.post(
            "/api/v1/analyses/error-rates",
            json={"references": [""], "hypotheses": ["x"]},
        )
        trajectory = await client.post(
            "/api/v1/analyses/coverage-trajectory",
            json={
                "phoneme_sequences": [["a", "b"], ["b", "c"]],
                "target_units": ["a", "b", "c"],
                "unit": "phoneme",
            },
        )

    assert distribution.status_code == 200
    assert distribution.json()["normalized_entropy"] == 1.0
    assert quality.status_code == 200
    assert quality.json()["total_words"] == 2
    assert rates.status_code == 200
    assert rates.json()["wer"] == {"status": "positive_infinity", "value": None}
    assert rates.json()["per"] == {"status": "not_computed", "value": None}
    assert "Infinity" not in rates.text
    assert trajectory.status_code == 200
    assert trajectory.json()["gains"] == [2, 1]
    assert trajectory.json()["coverages"][-1] == 1.0


@pytest.mark.asyncio
async def test_analysis_validation_is_sanitized_and_text_is_bounded() -> None:
    async with _client(FakeInventoryEngine(), max_sentence_characters=3) as client:
        mismatch = await client.post(
            "/api/v1/analyses/error-rates",
            json={"references": ["private"], "hypotheses": []},
        )
        too_long = await client.post(
            "/api/v1/analyses/text-quality",
            json={"sentences": ["long"], "phoneme_sequences": [["l"]]},
        )

    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "validation_error"
    assert "private" not in mismatch.text
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "invalid_request"
