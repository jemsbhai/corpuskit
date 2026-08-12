"""Real cached-PHOIBLE smoke coverage through the typed adapter."""

from __future__ import annotations

import pytest

from corpuskit.adapters.corpusgen import CorpusgenAdapter, CorpusgenInventoryAdapter
from corpuskit.adapters.corpusgen.phoible_provisioning import (
    PHOIBLE_BYTES,
    PHOIBLE_COMMIT,
    PhoibleSnapshotProvisioner,
)
from corpuskit.domain import CoverageUnit, EvaluationTarget, EvaluationTargetMode


@pytest.mark.integration
def test_cached_phoible_language_mapping_inventory_and_union_smoke() -> None:
    provisioned = PhoibleSnapshotProvisioner().status()
    if not provisioned.ready:
        pytest.skip("the pinned PHOIBLE cache is not available")

    adapter = CorpusgenInventoryAdapter()
    loaded = adapter.load()
    languages = adapter.languages("English")
    inventory = adapter.inventory("en-us")
    union = adapter.inventory("en-us", union=True)
    all_inventories = adapter.all_inventories("en-us")
    sources = adapter.sources("en-us")
    source_specific = adapter.inventory("eng", source=sources[0])
    features = adapter.features()
    status_after_queries = adapter.status()
    espeak_forward = adapter.mappings("en-us")
    iso_reverse = adapter.mappings("eng")

    assert any(item.iso639_3 == "eng" for item in languages)
    assert inventory.iso639_3 == "eng"
    assert inventory.size == len(inventory.segments)
    assert inventory.phonemes
    assert union.inventory_id == 0
    assert union.source == "union"
    assert set(inventory.phonemes) <= set(union.phonemes)
    assert len(all_inventories) >= 2
    assert {item.source for item in all_inventories} == set(sources)
    assert sources
    assert source_specific.source == sources[0]
    assert len(features.names) == 38
    assert any(item.espeak_code == "en-us" and item.iso639_3 == "eng" for item in espeak_forward)
    assert any(item.espeak_code == "en-us" and item.iso639_3 == "eng" for item in iso_reverse)
    assert loaded.loaded is True
    assert loaded.stats is not None
    assert loaded.stats.inventory_count > 3_000
    assert status_after_queries == loaded
    assert provisioned.revision == PHOIBLE_COMMIT
    assert provisioned.actual_bytes == PHOIBLE_BYTES
    assert "path" not in provisioned.public_dict()


@pytest.mark.integration
def test_cached_phoible_target_evaluation_uses_real_espeak_and_inventory() -> None:
    provisioned = PhoibleSnapshotProvisioner().status()
    if not provisioned.ready:
        pytest.skip("the pinned PHOIBLE cache is not available")

    result = CorpusgenAdapter().evaluate(
        ("The quick brown fox jumps over the lazy dog.",),
        language="en-us",
        unit=CoverageUnit.PHONEME,
        target=EvaluationTarget(mode=EvaluationTargetMode.PHOIBLE),
    )

    assert result.target_mode is EvaluationTargetMode.PHOIBLE
    assert result.unit is CoverageUnit.PHONEME
    assert 0.0 < result.coverage < 1.0
    assert result.covered_units
    assert result.missing_units
    assert set(result.covered_units).isdisjoint(result.missing_units)
    assert set(result.target_units) == set(result.covered_units) | set(result.missing_units)
