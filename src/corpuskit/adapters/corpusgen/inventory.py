"""Typed CorpusGen boundary for PHOIBLE and eSpeak exploration."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from threading import Lock
from typing import Protocol, cast

from pydantic import ValidationError

from corpuskit.adapters.corpusgen.phoible_provisioning import (
    PHOIBLE_COMMIT,
    PHOIBLE_SHA256,
)
from corpuskit.domain import (
    EspeakMappingEntry,
    FeatureCatalog,
    InvalidRequestError,
    Inventory,
    InventoryDataUnavailableError,
    InventoryNotFoundError,
    LanguageSummary,
    PhoibleDatasetStats,
    PhoibleStatus,
    PhoneticFeature,
    Segment,
)
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError


class SegmentLike(Protocol):
    phoneme: str
    segment_class: str
    marginal: bool
    allophones: list[str]
    features: dict[str, str]
    glyph_id: str


class InventoryLike(Protocol):
    inventory_id: int
    language_name: str
    iso639_3: str
    glottocode: str
    specific_dialect: str | None
    source: str
    segments: list[SegmentLike]
    phonemes: list[str]
    consonants: list[str]
    vowels: list[str]
    tones: list[str]
    marginal_phonemes: list[str]
    size: int
    consonant_count: int
    vowel_count: int
    tone_count: int


class PhoibleDatasetLike(Protocol):
    csv_exists: bool
    is_loaded: bool
    inventory_count: int
    language_count: int
    segment_count: int

    def load(self) -> None: ...

    def search(self, name: str) -> list[dict[str, object]]: ...

    def available_languages(self) -> list[dict[str, object]]: ...

    def sources_for(self, identifier: str) -> list[str]: ...

    def get_inventory(self, identifier: str, source: str | None = None) -> InventoryLike: ...

    def get_all_inventories(self, identifier: str) -> list[InventoryLike]: ...

    def get_union_inventory(self, identifier: str) -> InventoryLike: ...


class EspeakMappingLike(Protocol):
    def to_iso(self, espeak_code: str) -> str: ...

    def to_espeak(self, iso_code: str) -> list[str]: ...

    def items(self) -> Iterator[tuple[str, str]]: ...


def _default_dataset_factory() -> PhoibleDatasetLike:
    from corpusgen.inventory import PhoibleDataset

    return cast(PhoibleDatasetLike, PhoibleDataset())


def _default_mapping_factory() -> EspeakMappingLike:
    from corpusgen.inventory import EspeakMapping

    return cast(EspeakMappingLike, EspeakMapping())


def _default_feature_names() -> tuple[str, ...]:
    from corpusgen.inventory import FEATURE_NAMES

    return tuple(FEATURE_NAMES)


class CorpusgenInventoryAdapter:
    """Normalize cached PHOIBLE and bundled mapping data."""

    def __init__(
        self,
        *,
        dataset_factory: Callable[[], PhoibleDatasetLike] | None = None,
        mapping_factory: Callable[[], EspeakMappingLike] | None = None,
        feature_names: tuple[str, ...] | None = None,
    ) -> None:
        self._dataset = (dataset_factory or _default_dataset_factory)()
        self._mapping = (mapping_factory or _default_mapping_factory)()
        self._feature_names = feature_names or _default_feature_names()
        self._dataset_lock = Lock()

    def status(self) -> PhoibleStatus:
        """Report cache readiness without loading or exposing its path."""

        with self._dataset_lock:
            return self._status_locked()

    def load(self) -> PhoibleStatus:
        """Explicitly load the already provisioned snapshot and return bounded statistics."""

        operation = "inventory.load"
        with self._dataset_lock:
            if not self._dataset.csv_exists:
                raise InventoryDataUnavailableError(operation)
            if not self._dataset.is_loaded:
                self._invoke_dataset(self._dataset.load, operation)
            if not self._dataset.is_loaded:
                raise EngineContractError(operation)
            return self._status_locked()

    def features(self) -> FeatureCatalog:
        """Expose the canonical PHOIBLE feature vocabulary."""

        try:
            return FeatureCatalog(names=self._feature_names)
        except (TypeError, ValidationError, ValueError):
            raise EngineContractError("inventory.features") from None

    def languages(self, query: str | None = None) -> tuple[LanguageSummary, ...]:
        """List or search cached PHOIBLE language metadata."""

        operation = "inventory.languages"
        with self._dataset_lock:
            raw = self._invoke_dataset(
                lambda: (
                    self._dataset.search(query)
                    if query is not None
                    else self._dataset.available_languages()
                ),
                operation,
            )
        try:
            return tuple(self._normalize_language(item) for item in raw)
        except (KeyError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    def mappings(self, query: str | None = None) -> tuple[EspeakMappingEntry, ...]:
        """List bundled eSpeak-to-ISO mappings with optional substring filtering."""

        normalized_query = query.casefold() if query is not None else None
        try:
            return tuple(
                EspeakMappingEntry(espeak_code=espeak, iso639_3=iso)
                for espeak, iso in self._mapping.items()
                if normalized_query is None
                or normalized_query in espeak.casefold()
                or normalized_query in iso.casefold()
            )
        except (TypeError, ValidationError, ValueError):
            raise EngineContractError("inventory.mappings") from None
        except Exception:
            raise EngineUnavailableError("inventory.mappings") from None

    def sources(self, identifier: str) -> tuple[str, ...]:
        operation = "inventory.sources"
        resolved = self._resolve_identifier(identifier)
        with self._dataset_lock:
            result = self._invoke_dataset(lambda: self._dataset.sources_for(resolved), operation)
        return tuple(result)

    def inventory(
        self,
        identifier: str,
        *,
        source: str | None = None,
        union: bool = False,
    ) -> Inventory:
        operation = "inventory.get"
        resolved = self._resolve_identifier(identifier)
        with self._dataset_lock:
            if source is not None and not union:
                sources = self._invoke_dataset(
                    lambda: self._dataset.sources_for(resolved), operation
                )
                if source not in sources:
                    raise InvalidRequestError("inventory.source")
            result = self._invoke_dataset(
                lambda: (
                    self._dataset.get_union_inventory(resolved)
                    if union
                    else self._dataset.get_inventory(resolved, source=source)
                ),
                operation,
            )
        return self._normalize_inventory(result, operation)

    def all_inventories(self, identifier: str) -> tuple[Inventory, ...]:
        operation = "inventory.all"
        resolved = self._resolve_identifier(identifier)
        with self._dataset_lock:
            results = self._invoke_dataset(
                lambda: self._dataset.get_all_inventories(resolved), operation
            )
        return tuple(self._normalize_inventory(item, operation) for item in results)

    def _status_locked(self) -> PhoibleStatus:
        loaded = self._dataset.is_loaded
        stats = (
            PhoibleDatasetStats(
                inventory_count=self._dataset.inventory_count,
                language_count=self._dataset.language_count,
                segment_count=self._dataset.segment_count,
            )
            if loaded
            else None
        )
        return PhoibleStatus(
            cache_available=self._dataset.csv_exists,
            loaded=loaded,
            revision=PHOIBLE_COMMIT,
            sha256=PHOIBLE_SHA256,
            stats=stats,
        )

    def _resolve_identifier(self, identifier: str) -> str:
        try:
            return self._mapping.to_iso(identifier)
        except KeyError:
            return identifier
        except Exception:
            raise EngineUnavailableError("inventory.resolve") from None

    @staticmethod
    def _invoke_dataset[T](call: Callable[[], T], operation: str) -> T:
        try:
            return call()
        except FileNotFoundError:
            raise InventoryDataUnavailableError(operation) from None
        except KeyError:
            raise InventoryNotFoundError(operation) from None
        except (RuntimeError, OSError):
            raise InventoryDataUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    @staticmethod
    def _normalize_language(item: dict[str, object]) -> LanguageSummary:
        sources = item["sources"]
        if not isinstance(sources, list) or not all(isinstance(value, str) for value in sources):
            raise TypeError("sources must be strings")
        return LanguageSummary(
            iso639_3=cast(str, item["iso639_3"]),
            glottocode=cast(str, item["glottocode"]),
            language_name=cast(str, item["language_name"]),
            inventory_count=cast(int, item["inventory_count"]),
            sources=tuple(sources),
        )

    @staticmethod
    def _normalize_inventory(result: InventoryLike, operation: str) -> Inventory:
        try:
            segments = tuple(
                Segment(
                    phoneme=segment.phoneme,
                    segment_class=segment.segment_class,
                    marginal=segment.marginal,
                    allophones=tuple(segment.allophones),
                    features=tuple(
                        PhoneticFeature(name=name, value=value)
                        for name, value in sorted(segment.features.items())
                    ),
                    glyph_id=segment.glyph_id,
                )
                for segment in result.segments
            )
            return Inventory(
                inventory_id=result.inventory_id,
                language_name=result.language_name,
                iso639_3=result.iso639_3,
                glottocode=result.glottocode,
                specific_dialect=result.specific_dialect,
                source=result.source,
                segments=segments,
                phonemes=tuple(result.phonemes),
                consonants=tuple(result.consonants),
                vowels=tuple(result.vowels),
                tones=tuple(result.tones),
                marginal_phonemes=tuple(result.marginal_phonemes),
                size=result.size,
                consonant_count=result.consonant_count,
                vowel_count=result.vowel_count,
                tone_count=result.tone_count,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None


__all__ = ["CorpusgenInventoryAdapter", "EspeakMappingLike", "PhoibleDatasetLike"]
