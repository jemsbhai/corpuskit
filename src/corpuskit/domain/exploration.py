"""Immutable inventory-exploration contracts."""

from __future__ import annotations

from pydantic import Field

from corpuskit.domain.corpus import FrozenDomainModel, Inventory, Segment


class PhoibleDatasetStats(FrozenDomainModel):
    inventory_count: int = Field(ge=0)
    language_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class PhoibleStatus(FrozenDomainModel):
    cache_available: bool
    loaded: bool
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stats: PhoibleDatasetStats | None


class LanguageSummary(FrozenDomainModel):
    iso639_3: str = Field(min_length=1, max_length=16)
    glottocode: str = Field(min_length=1, max_length=32)
    language_name: str = Field(min_length=1, max_length=256)
    inventory_count: int = Field(ge=1)
    sources: tuple[str, ...]


class EspeakMappingEntry(FrozenDomainModel):
    espeak_code: str = Field(min_length=1, max_length=64)
    iso639_3: str = Field(min_length=1, max_length=16)


class InventorySources(FrozenDomainModel):
    identifier: str
    sources: tuple[str, ...]


class FeatureCatalog(FrozenDomainModel):
    names: tuple[str, ...]


class LanguagePage(FrozenDomainModel):
    items: tuple[LanguageSummary, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)


class EspeakMappingPage(FrozenDomainModel):
    items: tuple[EspeakMappingEntry, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)


class InventoryPage(FrozenDomainModel):
    items: tuple[Inventory, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)


class SegmentPage(FrozenDomainModel):
    items: tuple[Segment, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
