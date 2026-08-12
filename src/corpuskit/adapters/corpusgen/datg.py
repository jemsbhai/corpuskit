"""Offline, bounded CorpusGen Phon-DATG adapter."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError

from corpuskit.adapters.corpusgen.model_runtime import compute_snapshot_digest
from corpuskit.domain.datg import (
    DatgCacheIdentity,
    DatgGeneratedCandidate,
    DatgGuidanceManifest,
    DatgGuidedGenerationRequest,
    DatgGuidedGenerationResult,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexBuildResult,
    DatgIndexedToken,
    DatgLogitPreviewRequest,
    DatgLogitPreviewResult,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgUnit,
    DatgUnitTokenSet,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import (
    ApplicationError,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)


@dataclass(frozen=True, slots=True)
class SnapshotLocation:
    """Provisioned snapshot and the only root its files may resolve beneath."""

    snapshot: Path
    approved_cache_root: Path


class DatgSnapshotResolver(Protocol):
    def resolve(self, pin: DatgSnapshotPin) -> SnapshotLocation: ...


class DatgTokenizer(Protocol):
    def get_vocab(self) -> dict[str, int]: ...

    def decode(self, token_id: int, *, skip_special_tokens: bool) -> str: ...


class DatgTokenizerLoader(Protocol):
    def load(self, location: SnapshotLocation, pin: DatgSnapshotPin) -> DatgTokenizer: ...


@dataclass(frozen=True, slots=True)
class IndexMaps:
    unit_to_tokens: dict[str, set[int]]
    token_units: dict[int, set[str]]


@dataclass(frozen=True, slots=True)
class RawCandidate:
    text: str
    phonemes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GuidanceOutput:
    candidates: tuple[RawCandidate, ...]
    attribute_token_ids: tuple[int, ...]
    anti_attribute_token_ids: tuple[int, ...]


class DatgBindings(Protocol):
    """Test seam around public CorpusGen Phon-DATG contracts."""

    def build_index(
        self,
        tokenizer: DatgTokenizer,
        *,
        language: str,
        batch_size: int,
    ) -> IndexMaps: ...

    def generate(
        self,
        *,
        snapshot: Path,
        request: DatgGuidedGenerationRequest,
        artifact: DatgIndexArtifact,
    ) -> GuidanceOutput: ...

    def preview(self, request: DatgLogitPreviewRequest) -> DatgLogitPreviewResult: ...


class OfflineSnapshotResolver:
    """Resolve an exact Hub commit from the worker's local cache only."""

    def __init__(self, approved_cache_root: Path | None = None) -> None:
        self._approved_cache_root = (
            approved_cache_root.absolute() if approved_cache_root is not None else None
        )

    def resolve(self, pin: DatgSnapshotPin) -> SnapshotLocation:
        try:
            hub = importlib.import_module("huggingface_hub")
            snapshot_download = cast(Callable[..., str], hub.snapshot_download)
        except (ImportError, AttributeError):
            raise DependencyUnavailableError("datg.snapshot.dependency") from None
        try:
            options: dict[str, object] = {
                "repo_id": pin.repository_id,
                "revision": pin.revision,
                "local_files_only": True,
            }
            if self._approved_cache_root is not None:
                options["cache_dir"] = str(self._approved_cache_root)
            snapshot = Path(snapshot_download(**options)).absolute()
            if snapshot.name != pin.revision or snapshot.parent.name != "snapshots":
                raise EngineUnavailableError("datg.snapshot.layout")
            approved_root = self._approved_cache_root or snapshot.parent.parent
            resolved_root = approved_root.resolve(strict=True)
            resolved_snapshot = snapshot.resolve(strict=True)
            if not resolved_snapshot.is_relative_to(resolved_root):
                raise EngineUnavailableError("datg.snapshot.boundary")
            return SnapshotLocation(
                snapshot=resolved_snapshot,
                approved_cache_root=resolved_root,
            )
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.snapshot.resolve") from None


class TransformersTokenizerLoader:
    """Load a tokenizer without downloads or executable repository code."""

    def load(self, location: SnapshotLocation, pin: DatgSnapshotPin) -> DatgTokenizer:
        try:
            transformers = importlib.import_module("transformers")
            auto_tokenizer = cast(Any, transformers.AutoTokenizer)
        except (ImportError, AttributeError):
            raise DependencyUnavailableError("datg.tokenizer.dependency") from None
        try:
            return cast(
                DatgTokenizer,
                auto_tokenizer.from_pretrained(
                    str(location.snapshot.resolve(strict=True)),
                    revision=pin.revision,
                    local_files_only=True,
                    trust_remote_code=False,
                ),
            )
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.tokenizer.load") from None


class CorpusgenDatgBindings:
    """Use only public CorpusGen constructors, methods, and copied properties."""

    def build_index(
        self,
        tokenizer: DatgTokenizer,
        *,
        language: str,
        batch_size: int,
    ) -> IndexMaps:
        from corpusgen.generate.phon_datg.attribute_words import AttributeWordIndex

        index = AttributeWordIndex(language=language, batch_size=batch_size)
        index.build(tokenizer)
        return IndexMaps(
            unit_to_tokens=index.unit_to_tokens,
            token_units=index.token_units,
        )

    def generate(
        self,
        *,
        snapshot: Path,
        request: DatgGuidedGenerationRequest,
        artifact: DatgIndexArtifact,
    ) -> GuidanceOutput:
        from corpusgen.generate.backends.local import LocalBackend
        from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory
        from corpusgen.generate.phon_datg.graph import DATGStrategy

        targets = PhoneticTargetInventory(
            target_phonemes=list(request.target_phonemes),
            unit=request.unit.value,
        )
        for sequence_index, sequence in enumerate(request.coverage_sequences):
            targets.update(list(sequence.phonemes), sequence_index)
        index = _ArtifactWordIndex(artifact)
        strategy = DATGStrategy(
            targets=targets,
            language=request.language,
            boost_strength=request.guidance.boost_strength,
            penalty_strength=request.guidance.penalty_strength,
            anti_attribute_mode=request.guidance.anti_attribute_mode.value,
            frequency_threshold=request.guidance.frequency_threshold,
            attribute_word_index=cast(Any, index),
        )
        _seed_transformers(request.seed)
        quantization = (
            None if request.quantization is DatgQuantization.NONE else request.quantization.value
        )
        backend = LocalBackend(
            model_name=str(snapshot),
            language=request.language,
            device="cuda",
            quantization=quantization,
            guidance_strategy=strategy,
            max_new_tokens=request.max_new_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            do_sample=request.do_sample,
            model_kwargs={
                "revision": artifact.identity.tokenizer_revision,
                "local_files_only": True,
                "trust_remote_code": False,
                "use_safetensors": True,
            },
        )
        raw = backend.generate(list(request.target_units), k=request.candidates)
        candidates = _raw_candidates(raw)
        return GuidanceOutput(
            candidates=candidates,
            attribute_token_ids=tuple(sorted(strategy.current_attribute_tokens)),
            anti_attribute_token_ids=tuple(sorted(strategy.current_anti_attribute_tokens)),
        )

    def preview(self, request: DatgLogitPreviewRequest) -> DatgLogitPreviewResult:
        from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory
        from corpusgen.generate.phon_datg.graph import DATGStrategy

        targets = PhoneticTargetInventory(
            target_phonemes=list(request.target_phonemes),
            unit=request.artifact.identity.unit.value,
        )
        for sequence_index, sequence in enumerate(request.coverage_sequences):
            targets.update(list(sequence.phonemes), sequence_index)
        strategy = DATGStrategy(
            targets=targets,
            language=request.artifact.identity.language,
            boost_strength=request.guidance.boost_strength,
            penalty_strength=request.guidance.penalty_strength,
            anti_attribute_mode=request.guidance.anti_attribute_mode.value,
            frequency_threshold=request.guidance.frequency_threshold,
            attribute_word_index=cast(Any, _ArtifactWordIndex(request.artifact)),
        )
        strategy.prepare(list(request.target_units), model=None, tokenizer=None)
        tensor = _Matrix(request.logits)
        modified = strategy.modify_logits(input_ids=None, logits=tensor)
        if not isinstance(modified, _Matrix):
            raise EngineContractError("datg.logits.result")
        return DatgLogitPreviewResult(
            original_logits=request.logits,
            modified_logits=modified.rows,
            attribute_token_ids=tuple(sorted(strategy.current_attribute_tokens)),
            anti_attribute_token_ids=tuple(sorted(strategy.current_anti_attribute_tokens)),
        )


class CorpusgenDatgAdapter:
    """Production engine enforcing pins, cache identity, bounds, and sanitized contracts."""

    def __init__(
        self,
        *,
        snapshot_resolver: DatgSnapshotResolver | None = None,
        tokenizer_loader: DatgTokenizerLoader | None = None,
        bindings: DatgBindings | None = None,
        clock: Callable[[], float] = time.monotonic,
        corpusgen_version: str | None = None,
        espeak_version: str | None = None,
    ) -> None:
        self._resolver = snapshot_resolver or OfflineSnapshotResolver()
        self._tokenizer_loader = tokenizer_loader or TransformersTokenizerLoader()
        self._bindings = bindings or CorpusgenDatgBindings()
        self._clock = clock
        self._corpusgen_version = corpusgen_version or _corpusgen_version()
        self._espeak_version = espeak_version or _espeak_version()

    def build_index(
        self,
        request: DatgIndexBuildRequest,
        policy: DatgRuntimePolicyEntry,
    ) -> DatgIndexBuildResult:
        started = self._clock()
        try:
            if request.runtime_id != policy.runtime_id:
                raise InvalidRequestError("datg.runtime.policy")
            location = self._verified_snapshot(policy.tokenizer)
            tokenizer = self._tokenizer_loader.load(location, policy.tokenizer)
            vocabulary = _validated_vocabulary(tokenizer, request.max_vocabulary_size)
            maps = self._bindings.build_index(
                tokenizer,
                language=request.language,
                batch_size=request.batch_size,
            )
            artifact = _artifact_from_maps(
                tokenizer=tokenizer,
                vocabulary=vocabulary,
                maps=maps,
                identity=DatgCacheIdentity.create(
                    tokenizer=policy.tokenizer,
                    language=request.language,
                    unit=request.unit,
                    corpusgen_version=self._corpusgen_version,
                    espeak_version=self._espeak_version,
                ),
            )
            elapsed = self._elapsed(started, request.activity_timeout_seconds)
            return DatgIndexBuildResult(artifact=artifact, elapsed_seconds=elapsed)
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.index.build") from None

    def generate(
        self,
        request: DatgGuidedGenerationRequest,
        policy: DatgRuntimePolicyEntry,
        profile: DatgWorkerProfile,
        artifact: DatgIndexArtifact,
    ) -> DatgGuidedGenerationResult:
        started = self._clock()
        try:
            self._validate_generation(request, policy, profile, artifact)
            location = self._verified_snapshot(policy.model)
            output = self._bindings.generate(
                snapshot=location.snapshot.resolve(strict=True),
                request=request,
                artifact=artifact,
            )
            if len(output.candidates) > request.candidates:
                raise EngineContractError("datg.generation.candidate_count")
            candidates = _generated_candidates(output.candidates)
            elapsed = self._elapsed(started, request.activity_timeout_seconds)
            return DatgGuidedGenerationResult(
                manifest=DatgGuidanceManifest(
                    runtime_id=policy.runtime_id,
                    model_id=policy.model.repository_id,
                    model_revision=policy.model.revision,
                    model_snapshot_sha256=policy.model.snapshot_sha256,
                    tokenizer_id=policy.tokenizer.repository_id,
                    tokenizer_revision=policy.tokenizer.revision,
                    tokenizer_snapshot_sha256=policy.tokenizer.snapshot_sha256,
                    index_cache_key_sha256=artifact.identity.cache_key_sha256,
                    index_content_sha256=artifact.content_sha256,
                    language=request.language,
                    unit=request.unit,
                    guidance=request.guidance,
                    quantization=request.quantization,
                    seed=request.seed,
                    sampling_enabled=request.do_sample,
                    corpusgen_version=self._corpusgen_version,
                    espeak_version=self._espeak_version,
                ),
                candidates=candidates,
                attribute_token_ids=_valid_token_ids(output.attribute_token_ids, artifact),
                anti_attribute_token_ids=_valid_token_ids(
                    output.anti_attribute_token_ids, artifact
                ),
                elapsed_seconds=elapsed,
            )
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.generation.execute") from None

    def preview_logits(self, request: DatgLogitPreviewRequest) -> DatgLogitPreviewResult:
        try:
            return self._bindings.preview(request)
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.logits.preview") from None

    def _verified_snapshot(self, pin: DatgSnapshotPin) -> SnapshotLocation:
        location = self._resolver.resolve(pin)
        digest = compute_snapshot_digest(
            location.snapshot,
            approved_cache_root=location.approved_cache_root,
        )
        if digest != pin.snapshot_sha256:
            raise EngineUnavailableError("datg.snapshot.digest")
        return location

    def _elapsed(self, started: float, limit: float) -> float:
        elapsed = self._clock() - started
        if elapsed < 0 or elapsed > limit:
            raise EngineUnavailableError("datg.activity.deadline")
        return elapsed

    def _validate_generation(
        self,
        request: DatgGuidedGenerationRequest,
        policy: DatgRuntimePolicyEntry,
        profile: DatgWorkerProfile,
        artifact: DatgIndexArtifact,
    ) -> None:
        if profile is not DatgWorkerProfile.LOCAL_GPU:
            raise InvalidRequestError("datg.runtime.worker_profile")
        if request.runtime_id != policy.runtime_id:
            raise InvalidRequestError("datg.runtime.policy")
        if request.quantization not in policy.allowed_quantizations:
            raise InvalidRequestError("datg.runtime.quantization")
        expected = DatgCacheIdentity.create(
            tokenizer=policy.tokenizer,
            language=request.language,
            unit=request.unit,
            corpusgen_version=self._corpusgen_version,
            espeak_version=self._espeak_version,
        )
        if (
            artifact.identity != expected
            or request.index_cache_key_sha256 != expected.cache_key_sha256
        ):
            raise InvalidRequestError("datg.index.identity")


class _ArtifactWordIndex:
    """Application-owned facade implementing the documented public index methods."""

    def __init__(self, artifact: DatgIndexArtifact) -> None:
        self._artifact = artifact
        self._unit_to_tokens = {item.unit: set(item.token_ids) for item in artifact.unit_to_tokens}
        self._token_units = {item.token_id: set(item.units) for item in artifact.token_units}

    @property
    def is_built(self) -> bool:
        return True

    def build(self, tokenizer: object) -> None:
        del tokenizer

    def get_attribute_tokens(self, target_units: list[str]) -> set[int]:
        return {
            token_id for unit in target_units for token_id in self._unit_to_tokens.get(unit, set())
        }

    def get_anti_attribute_tokens(
        self,
        covered_units: set[str],
        unit_level: str | None = None,
    ) -> set[int]:
        return {
            token_id
            for token_id, units in self._token_units.items()
            if (filtered := _filter_units(units, unit_level)) and filtered.issubset(covered_units)
        }

    def get_anti_attribute_tokens_by_frequency(
        self,
        unit_counts: dict[str, int],
        threshold: int,
        unit_level: str | None = None,
    ) -> set[int]:
        return {
            token_id
            for token_id, units in self._token_units.items()
            if (filtered := _filter_units(units, unit_level))
            and all(unit_counts.get(unit, 0) > threshold for unit in filtered)
        }


class _MatrixSelection:
    def __init__(self, matrix: _Matrix, columns: list[int]) -> None:
        self._matrix = matrix
        self._columns = columns

    def __iadd__(self, amount: float) -> _MatrixSelection:
        for row in self._matrix._values:
            for column in self._columns:
                row[column] += amount
        return self


class _Matrix:
    """Minimal tensor used to test upstream modulation without requiring torch."""

    def __init__(self, rows: tuple[tuple[float, ...], ...]) -> None:
        self._values = [list(row) for row in rows]

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self._values), len(self._values[0]))

    @property
    def rows(self) -> tuple[tuple[float, ...], ...]:
        return tuple(tuple(row) for row in self._values)

    def clone(self) -> _Matrix:
        return _Matrix(self.rows)

    def __getitem__(self, key: tuple[slice, list[int]]) -> _MatrixSelection:
        rows, columns = key
        if rows != slice(None):
            raise TypeError("Only full row selection is supported.")
        return _MatrixSelection(self, columns)

    def __setitem__(self, key: tuple[slice, list[int]], value: object) -> None:
        del key
        if not isinstance(value, _MatrixSelection):
            raise TypeError("Only in-place matrix selection updates are supported.")


def _validated_vocabulary(
    tokenizer: DatgTokenizer,
    maximum: int,
) -> dict[str, int]:
    vocabulary = tokenizer.get_vocab()
    if (
        not isinstance(vocabulary, dict)
        or not vocabulary
        or len(vocabulary) > maximum
        or any(
            not isinstance(token, str)
            or not isinstance(token_id, int)
            or isinstance(token_id, bool)
            or token_id < 0
            or token_id > 10_000_000
            for token, token_id in vocabulary.items()
        )
        or len(set(vocabulary.values())) != len(vocabulary)
    ):
        raise InvalidRequestError("datg.index.vocabulary")
    return vocabulary


def _artifact_from_maps(
    *,
    tokenizer: DatgTokenizer,
    vocabulary: dict[str, int],
    maps: IndexMaps,
    identity: DatgCacheIdentity,
) -> DatgIndexArtifact:
    valid_ids = set(vocabulary.values())
    filtered: dict[int, tuple[str, ...]] = {}
    for token_id, units in maps.token_units.items():
        if token_id not in valid_ids:
            raise EngineContractError("datg.index.token_id")
        selected = tuple(sorted(_filter_units(units, identity.unit.value)))
        if selected:
            filtered[token_id] = selected
    derived_units = {
        unit: tuple(sorted(token_id for token_id, units in filtered.items() if unit in units))
        for unit in sorted({unit for units in filtered.values() for unit in units})
    }
    public_units = {
        unit: tuple(sorted(token_ids & valid_ids))
        for unit, token_ids in maps.unit_to_tokens.items()
        if _unit_matches(unit, identity.unit)
    }
    if public_units != derived_units:
        raise EngineContractError("datg.index.mapping")
    token_units = tuple(
        DatgIndexedToken(
            token_id=token_id,
            decoded_text=_decode(tokenizer, token_id),
            units=units,
        )
        for token_id, units in sorted(filtered.items())
    )
    unit_to_tokens = tuple(
        DatgUnitTokenSet(unit=unit, token_ids=token_ids)
        for unit, token_ids in derived_units.items()
    )
    return DatgIndexArtifact.create(
        identity=identity,
        vocabulary_size=len(vocabulary),
        unit_to_tokens=unit_to_tokens,
        token_units=token_units,
    )


def _decode(tokenizer: DatgTokenizer, token_id: int) -> str:
    value = tokenizer.decode(token_id, skip_special_tokens=True)
    if not isinstance(value, str) or len(value) > 512:
        raise EngineContractError("datg.index.decode")
    return value


def _filter_units(units: set[str], level: str | None) -> set[str]:
    if level == "phoneme":
        return {unit for unit in units if "-" not in unit}
    if level == "diphone":
        return {unit for unit in units if unit.count("-") == 1}
    if level == "triphone":
        return {unit for unit in units if unit.count("-") == 2}
    return set(units)


def _unit_matches(unit: str, level: DatgUnit) -> bool:
    return (
        unit.count("-")
        == {
            DatgUnit.PHONEME: 0,
            DatgUnit.DIPHONE: 1,
            DatgUnit.TRIPHONE: 2,
        }[level]
    )


def _raw_candidates(raw: object) -> tuple[RawCandidate, ...]:
    if not isinstance(raw, list):
        raise EngineContractError("datg.generation.candidates")
    candidates: list[RawCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            raise EngineContractError("datg.generation.candidates")
        text = item.get("text")
        phonemes = item.get("phonemes")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 4_000
            or not isinstance(phonemes, list)
            or not phonemes
            or len(phonemes) > 1_000
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 64
                for value in phonemes
            )
        ):
            raise EngineContractError("datg.generation.candidates")
        candidates.append(RawCandidate(text=text.strip(), phonemes=tuple(phonemes)))
    return tuple(candidates)


def _generated_candidates(
    raw: tuple[RawCandidate, ...],
) -> tuple[DatgGeneratedCandidate, ...]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    candidates: list[DatgGeneratedCandidate] = []
    for item in raw:
        key = (item.text.strip(), item.phonemes)
        if key in seen:
            continue
        seen.add(key)
        digest = hashlib.sha256(f"{key[0]}\0{'\0'.join(key[1])}".encode()).hexdigest()
        try:
            candidates.append(
                DatgGeneratedCandidate(
                    source_id=f"datg:{digest[:48]}",
                    text=key[0],
                    phonemes=key[1],
                )
            )
        except ValidationError:
            raise EngineContractError("datg.generation.candidates") from None
    if not candidates:
        raise EngineContractError("datg.generation.empty")
    return tuple(candidates)


def _valid_token_ids(token_ids: tuple[int, ...], artifact: DatgIndexArtifact) -> tuple[int, ...]:
    result = tuple(sorted(set(token_ids)))
    vocabulary_ids = {item.token_id for item in artifact.token_units}
    if any(
        not isinstance(item, int)
        or isinstance(item, bool)
        or item < 0
        or item not in vocabulary_ids
        for item in result
    ):
        raise EngineContractError("datg.generation.token_ids")
    return result


def _seed_transformers(seed: int) -> None:
    try:
        set_seed = cast(
            Callable[[int], None],
            importlib.import_module("transformers").set_seed,
        )
    except ImportError:
        raise DependencyUnavailableError("datg.generation.dependency") from None
    set_seed(seed)


def _corpusgen_version() -> str:
    try:
        return importlib.metadata.version("corpusgen")
    except importlib.metadata.PackageNotFoundError:
        raise DependencyUnavailableError("datg.corpusgen.dependency") from None


def _espeak_version() -> str:
    try:
        from phonemizer.backend import EspeakBackend  # type: ignore[import-untyped]

        value = EspeakBackend.version()
        return ".".join(str(part) for part in value)
    except Exception:
        raise DependencyUnavailableError("datg.espeak.dependency") from None


__all__ = [
    "CorpusgenDatgAdapter",
    "CorpusgenDatgBindings",
    "DatgBindings",
    "DatgSnapshotResolver",
    "DatgTokenizer",
    "DatgTokenizerLoader",
    "GuidanceOutput",
    "IndexMaps",
    "OfflineSnapshotResolver",
    "RawCandidate",
    "SnapshotLocation",
    "TransformersTokenizerLoader",
]
