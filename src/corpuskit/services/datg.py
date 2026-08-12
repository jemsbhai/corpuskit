"""Pure policy, inspection, and worker coordination for Phon-DATG."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from corpuskit.domain.datg import (
    DatgCoveredInspectionRequest,
    DatgFrequencyInspectionRequest,
    DatgGuidedGenerationRequest,
    DatgGuidedGenerationResult,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexBuildResult,
    DatgInspectionResult,
    DatgLogitPreviewRequest,
    DatgLogitPreviewResult,
    DatgRuntimePolicyEntry,
    DatgRuntimeValidationResult,
    DatgTargetInspectionRequest,
    DatgTokenMatch,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import ApplicationError, EngineUnavailableError, InvalidRequestError


class DatgEngine(Protocol):
    """Worker adapter. Methods may load local models and run G2P."""

    def build_index(
        self,
        request: DatgIndexBuildRequest,
        policy: DatgRuntimePolicyEntry,
    ) -> DatgIndexBuildResult: ...

    def generate(
        self,
        request: DatgGuidedGenerationRequest,
        policy: DatgRuntimePolicyEntry,
        profile: DatgWorkerProfile,
        artifact: DatgIndexArtifact,
    ) -> DatgGuidedGenerationResult: ...


class DatgIndexCache(Protocol):
    """Read-only cache lookup; writes belong to staged worker publication."""

    def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None: ...


class DatgIndexPublisher(Protocol):
    """Trusted parent-only publication boundary for one validated index."""

    def publish(self, artifact: DatgIndexArtifact) -> int: ...


class DatgLogitPreviewEngine(Protocol):
    """Calculation-only adapter boundary; it cannot load a model or generate text."""

    def preview_logits(self, request: DatgLogitPreviewRequest) -> DatgLogitPreviewResult: ...


class DatgRuntimePolicy:
    """Exact allowlist and profile checks safe to call from HTTP routes."""

    def __init__(
        self,
        entries: tuple[DatgRuntimePolicyEntry, ...],
        *,
        worker_profile: DatgWorkerProfile,
    ) -> None:
        identifiers = tuple(item.runtime_id for item in entries)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("DATG runtime policy IDs must be unique.")
        self._entries = entries
        self._worker_profile = worker_profile

    @property
    def worker_profile(self) -> DatgWorkerProfile:
        return self._worker_profile

    def authorize(self, runtime_id: str) -> DatgRuntimePolicyEntry:
        entry = next((item for item in self._entries if item.runtime_id == runtime_id), None)
        if entry is None:
            raise InvalidRequestError("datg.runtime.allowlist")
        return entry

    def validate_build(self, request: DatgIndexBuildRequest) -> DatgRuntimeValidationResult:
        self.authorize(request.runtime_id)
        return DatgRuntimeValidationResult(
            operation="build_index",
            runtime_id=request.runtime_id,
            required_deployment_profile="batch-cpu",
            activity_timeout_seconds=request.activity_timeout_seconds,
        )

    def validate_generation(
        self,
        request: DatgGuidedGenerationRequest,
    ) -> DatgRuntimeValidationResult:
        entry = self.authorize(request.runtime_id)
        if request.quantization not in entry.allowed_quantizations:
            raise InvalidRequestError("datg.runtime.quantization")
        if self._worker_profile is not DatgWorkerProfile.LOCAL_GPU:
            raise InvalidRequestError("datg.runtime.worker_profile")
        return DatgRuntimeValidationResult(
            operation="guided_generation",
            runtime_id=request.runtime_id,
            required_deployment_profile="gpu-inference",
            activity_timeout_seconds=request.activity_timeout_seconds,
        )


class DatgInspectionService:
    """Bounded, deterministic index queries that never load a model or use a network."""

    def __init__(self, cache: DatgIndexCache) -> None:
        self._cache = cache

    def target(self, request: DatgTargetInspectionRequest) -> DatgInspectionResult:
        artifact = self.artifact(request.cache_key_sha256)
        units = _unique_units(request.target_units)
        _ensure_unit_level(artifact, units)
        mapping = {entry.unit: entry.token_ids for entry in artifact.unit_to_tokens}
        token_ids = {token_id for unit in units for token_id in mapping.get(unit, ())}
        return _inspection_result(artifact, token_ids, request.max_results)

    def covered(self, request: DatgCoveredInspectionRequest) -> DatgInspectionResult:
        artifact = self.artifact(request.cache_key_sha256)
        covered = _unique_units(request.covered_units)
        _ensure_unit_level(artifact, covered)
        covered_set = set(covered)
        token_ids = {
            token.token_id
            for token in artifact.token_units
            if token.units and set(token.units).issubset(covered_set)
        }
        return _inspection_result(artifact, token_ids, request.max_results)

    def frequency(self, request: DatgFrequencyInspectionRequest) -> DatgInspectionResult:
        artifact = self.artifact(request.cache_key_sha256)
        units = tuple(item.unit for item in request.unit_counts)
        _ensure_unit_level(artifact, units)
        counts = {item.unit: item.count for item in request.unit_counts}
        token_ids = {
            token.token_id
            for token in artifact.token_units
            if token.units and all(counts.get(unit, 0) > request.threshold for unit in token.units)
        }
        return _inspection_result(artifact, token_ids, request.max_results)

    def artifact(self, cache_key_sha256: str) -> DatgIndexArtifact:
        """Fetch and revalidate an immutable content-addressed cache entry."""

        try:
            artifact = self._cache.get(cache_key_sha256)
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.index.cache") from None
        if artifact is None:
            raise InvalidRequestError("datg.index.not_found")
        if not isinstance(artifact, DatgIndexArtifact):
            raise EngineUnavailableError("datg.index.cache_contract")
        try:
            validated = DatgIndexArtifact.model_validate(artifact.model_dump(mode="json"))
        except Exception:
            raise EngineUnavailableError("datg.index.cache_contract") from None
        if validated.identity.cache_key_sha256 != cache_key_sha256:
            raise EngineUnavailableError("datg.index.cache_contract")
        return validated


class DatgCoordinator:
    """Worker-only execution boundary with application-safe failures."""

    def __init__(
        self,
        policy: DatgRuntimePolicy,
        engine: DatgEngine,
        cache: DatgIndexCache,
    ) -> None:
        self.policy = policy
        self._engine = engine
        self._cache = cache

    def build_index(self, request: DatgIndexBuildRequest) -> DatgIndexBuildResult:
        entry = self.policy.authorize(request.runtime_id)
        self.policy.validate_build(request)
        return _safe_call(
            "datg.index.build",
            lambda: self._engine.build_index(request, entry),
        )

    def generate(
        self,
        request: DatgGuidedGenerationRequest,
    ) -> DatgGuidedGenerationResult:
        entry = self.policy.authorize(request.runtime_id)
        self.policy.validate_generation(request)
        artifact = DatgInspectionService(self._cache).artifact(request.index_cache_key_sha256)
        return _safe_call(
            "datg.generation.execute",
            lambda: self._engine.generate(
                request,
                entry,
                self.policy.worker_profile,
                artifact,
            ),
        )


def _safe_call[T](operation: str, callback: Callable[[], T]) -> T:
    try:
        return callback()
    except ApplicationError:
        raise
    except Exception:
        raise EngineUnavailableError(operation) from None


def _unique_units(units: tuple[str, ...]) -> tuple[str, ...]:
    if len(units) != len(set(units)):
        raise InvalidRequestError("datg.inspect.units")
    return units


def _ensure_unit_level(artifact: DatgIndexArtifact, units: tuple[str, ...]) -> None:
    hyphens = {"phoneme": 0, "diphone": 1, "triphone": 2}[artifact.identity.unit.value]
    if any(not item.strip() or len(item) > 192 or item.count("-") != hyphens for item in units):
        raise InvalidRequestError("datg.inspect.unit_level")


def _inspection_result(
    artifact: DatgIndexArtifact,
    token_ids: set[int],
    maximum: int,
) -> DatgInspectionResult:
    ordered = tuple(sorted(token_ids))
    selected = ordered[:maximum]
    records = {item.token_id: item for item in artifact.token_units}
    matches = tuple(
        DatgTokenMatch(
            token_id=token_id,
            decoded_text=records[token_id].decoded_text,
            units=records[token_id].units,
        )
        for token_id in selected
        if token_id in records
    )
    if len(matches) != len(selected):
        raise EngineUnavailableError("datg.index.cache_contract")
    return DatgInspectionResult(
        cache_key_sha256=artifact.identity.cache_key_sha256,
        token_ids=selected,
        matches=matches,
        total_matches=len(ordered),
        truncated=len(ordered) > len(selected),
    )


__all__ = [
    "DatgCoordinator",
    "DatgEngine",
    "DatgIndexCache",
    "DatgIndexPublisher",
    "DatgInspectionService",
    "DatgLogitPreviewEngine",
    "DatgRuntimePolicy",
]
