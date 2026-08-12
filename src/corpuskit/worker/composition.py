"""Fail-closed composition for profile-scoped durable worker capabilities."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from corpuskit import __version__ as corpuskit_version
from corpuskit.adapters.corpusgen.datg import CorpusgenDatgAdapter, OfflineSnapshotResolver
from corpuskit.adapters.corpusgen.generation import CorpusgenGenerationAdapter
from corpuskit.adapters.corpusgen.model_runtime import (
    CorpusgenModelRuntimeAdapter,
    EnvironmentSecretResolver,
    TransformersLocalModelLoader,
    validate_hosted_policy_secrets,
)
from corpuskit.adapters.corpusgen.phoible_provisioning import (
    PHOIBLE_COMMIT,
    PHOIBLE_SHA256,
    PhoibleSnapshotProvisioner,
)
from corpuskit.adapters.corpusgen.phon_rl import (
    CorpusgenPhonRlAdapter,
    OfflinePhonRlSnapshotResolver,
)
from corpuskit.config import Settings
from corpuskit.domain.artifacts import (
    ContentDigest,
    DatasetProvenance,
    DeterminismClass,
    ModelProvenance,
    PhoibleProvenance,
)
from corpuskit.domain.datg import (
    MAX_DATG_INDEX_BYTES,
    DatgGuidedGenerationRequest,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgSnapshotPin,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import EngineUnavailableError
from corpuskit.domain.generation import (
    GenerationExecutionMode,
    HuggingFaceRepository,
    RepositoryGenerationRequest,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    DEFAULT_HOSTED_PROMPT_TEMPLATE,
    HostedGenerationRequest,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import (
    MAX_RL_RESULT_BYTES,
    PhonRlRuntimePolicyEntry,
    PhonRlStaticPromptSource,
    PhonRlTrainingRequest,
    PhonRlWorkerProfile,
    prompt_source_sha256,
)
from corpuskit.domain.reproducibility import TrustedExecutionFacts
from corpuskit.domain.selection import MAX_SELECTION_RESULT_ARTIFACT_BYTES
from corpuskit.persistence.artifact_store import ConfiguredStagedArtifactWriter
from corpuskit.services.datg import DatgCoordinator, DatgRuntimePolicy
from corpuskit.services.generation_scoring import GenerationCoordinator
from corpuskit.services.model_runtime import ModelRuntimeCoordinator, ModelRuntimePolicy
from corpuskit.services.phon_rl import PhonRlRuntimePolicy, PhonRlTrainingCoordinator
from corpuskit.worker.datg_handler import MAX_DATG_RESULT_ARTIFACT_BYTES, build_datg_handlers
from corpuskit.worker.generation_handler import (
    MAX_REPOSITORY_RESULT_ARTIFACT_BYTES,
    RepositoryGenerationDurableHandler,
)
from corpuskit.worker.model_registry import build_model_handlers
from corpuskit.worker.phon_rl_registry import build_phon_rl_handlers
from corpuskit.worker.routing import PROFILE_RUN_KINDS, task_queue_for_kind
from corpuskit.workflows.handlers import (
    DurableRunHandler,
    HandlerRegistry,
    build_core_handler_registry,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SAFE_CACHE_ID = re.compile(r"^[a-z][a-z0-9._-]{1,63}$", re.ASCII)
_SUPPORTED_RL_STRATEGIES = frozenset({"missing-units-v1"})


@dataclass(frozen=True, slots=True)
class FilesystemDatgIndexCache:
    """Read one content-addressed, pre-provisioned DATG index from a read-only root."""

    root: Path

    def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
        if _SHA256.fullmatch(cache_key_sha256) is None:
            raise EngineUnavailableError("datg.index.cache_key")
        try:
            root = self.root.resolve(strict=True)
            candidate = root / f"{cache_key_sha256}.json"
            if not candidate.exists():
                return None
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                raise EngineUnavailableError("datg.index.cache_boundary")
            if resolved.stat().st_size > MAX_DATG_INDEX_BYTES:
                raise EngineUnavailableError("datg.index.cache_size")
            artifact = DatgIndexArtifact.model_validate_json(resolved.read_bytes(), strict=True)
            if artifact.identity.cache_key_sha256 != cache_key_sha256:
                raise EngineUnavailableError("datg.index.cache_identity")
            return artifact
        except EngineUnavailableError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.index.cache") from None


@dataclass(frozen=True, slots=True)
class EmptyDatgIndexCache:
    """Build-only cache implementation; generation is not registered on this profile."""

    def get(self, cache_key_sha256: str) -> None:
        del cache_key_sha256


@dataclass(frozen=True, slots=True)
class WorkerExecutionFactsFactory:
    """Author trusted run facts from worker settings and reviewed request DTOs."""

    settings: Settings
    corpusgen_version: str
    espeak_version: str | None
    phoible: PhoibleProvenance | None

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerExecutionFactsFactory:
        return cls(
            settings=settings,
            corpusgen_version=importlib.metadata.version("corpusgen"),
            espeak_version=_espeak_version(),
            phoible=_phoible_provenance(),
        )

    def for_run(
        self,
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> TrustedExecutionFacts:
        if task_queue_for_kind(kind) != self.settings.worker_profile:
            raise ValueError("run kind is not authorized on this worker profile")
        if self.settings.worker_image_digest is None:
            raise ValueError("worker image provenance is unavailable")
        model, attestations, determinism = self._operation_facts(kind, spec)
        dataset = self._dataset_facts(kind, spec)
        policy_bytes = _worker_policy_bytes(self.settings)
        return TrustedExecutionFacts(
            corpuskit_version=corpuskit_version,
            corpusgen_version=self.corpusgen_version,
            worker_profile=self.settings.worker_profile,
            worker_image_digest=self.settings.worker_image_digest,
            worker_policy=ContentDigest(
                name="worker-policy",
                sha256=hashlib.sha256(policy_bytes).hexdigest(),
                size_bytes=len(policy_bytes),
            ),
            espeak_version=self.espeak_version,
            phoible=self.phoible,
            model=model,
            dataset=dataset,
            input_artifact_ids=self._input_artifact_ids(kind, spec),
            input_attestations=attestations,
            determinism=determinism,
        )

    @staticmethod
    def _input_artifact_ids(
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> tuple[UUID, ...]:
        if kind is RunKind.TRAIN_PHON_RL:
            training_request = PhonRlTrainingRequest.model_validate(spec)
            if isinstance(training_request.prompt_source, PhonRlStaticPromptSource):
                return (training_request.prompt_source.artifact_id,)
        if kind is RunKind.GENERATE_LOCAL:
            local_request = LocalGenerationRequest.model_validate(spec)
            if local_request.phon_rl_adapter is not None:
                return (local_request.phon_rl_adapter.artifact_id,)
        return ()

    @staticmethod
    def _dataset_facts(
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> DatasetProvenance | None:
        if kind is not RunKind.GENERATE_REPOSITORY:
            return None
        request = RepositoryGenerationRequest.model_validate(spec)
        if not isinstance(request.source, HuggingFaceRepository):
            return None
        source = request.source.spec
        selector = _canonical_json(source.model_dump(mode="json"))
        return DatasetProvenance(
            name=source.dataset,
            config=source.config,
            split=source.split,
            revision=source.revision,
            selector_sha256=hashlib.sha256(selector).hexdigest(),
            content_sha256=None,
        )

    def _operation_facts(
        self,
        kind: RunKind,
        spec: Mapping[str, Any],
    ) -> tuple[ModelProvenance | None, tuple[ContentDigest, ...], DeterminismClass]:
        if kind is RunKind.GENERATE_LLM:
            hosted_request = HostedGenerationRequest.model_validate(spec)
            hosted_policy = ModelRuntimePolicy(
                hosted_models=self.settings.worker_hosted_model_policies
            )
            hosted_policy.validate_hosted(hosted_request)
            authorized_hosted = hosted_policy.authorize_hosted(hosted_request)
            prompt_policy = hosted_policy.authorize_hosted_prompt(hosted_request, authorized_hosted)
            if prompt_policy is None:
                prompt = DEFAULT_HOSTED_PROMPT_TEMPLATE.encode("utf-8")
                prompt_digest = hashlib.sha256(prompt).hexdigest()
                prompt_size = len(prompt)
            else:
                prompt_digest = prompt_policy.sha256
                prompt_size = prompt_policy.size_bytes
            return (
                ModelProvenance(
                    backend=f"hosted-{hosted_request.selection.provider}",
                    identifier=hosted_request.selection.model,
                    revision="provider-managed",
                ),
                (
                    ContentDigest(
                        name="prompt-template",
                        sha256=prompt_digest,
                        size_bytes=prompt_size,
                    ),
                ),
                DeterminismClass.NONREPRODUCIBLE,
            )
        if kind is RunKind.GENERATE_REPOSITORY:
            repository_request = RepositoryGenerationRequest.model_validate(spec)
            GenerationCoordinator(
                CorpusgenGenerationAdapter(),
                allowed_huggingface_sources=self.settings.worker_huggingface_repository_policies,
            ).validate(
                repository_request,
                execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
            )
            source_bytes = _canonical_json(repository_request.source.model_dump(mode="json"))
            return (
                None,
                (
                    ContentDigest(
                        name="repository-source",
                        sha256=hashlib.sha256(source_bytes).hexdigest(),
                        size_bytes=len(source_bytes),
                    ),
                ),
                (
                    DeterminismClass.BEST_EFFORT
                    if isinstance(repository_request.source, HuggingFaceRepository)
                    else DeterminismClass.EXACT
                ),
            )
        if kind in {RunKind.GENERATE_LOCAL, RunKind.PERPLEXITY}:
            if kind is RunKind.GENERATE_LOCAL:
                local_request = LocalGenerationRequest.model_validate(spec)
                local_selection = local_request.selection
            else:
                analysis_request = LanguageModelAnalysisRequest.model_validate(spec)
                local_selection = analysis_request.selection
            local_policy = ModelRuntimePolicy(
                local_models=self.settings.worker_local_model_policies,
                worker_profile=WorkerModelProfile.LOCAL_GPU,
            )
            if kind is RunKind.GENERATE_LOCAL:
                local_policy.validate_local(local_request)
            else:
                local_policy.validate_analysis(analysis_request)
            authorized_local = local_policy.authorize_local(
                local_selection.pin.model,
                local_selection.pin.revision,
            )
            return (
                ModelProvenance(
                    backend="transformers",
                    identifier=authorized_local.pin.model,
                    revision=authorized_local.pin.revision,
                    artifact_sha256=authorized_local.artifact_sha256,
                ),
                (),
                DeterminismClass.BEST_EFFORT,
            )
        if kind is RunKind.BUILD_DATG_INDEX:
            datg_build_request = DatgIndexBuildRequest.model_validate(spec)
            datg_build_policy = DatgRuntimePolicy(
                self.settings.worker_datg_runtime_policies,
                worker_profile=DatgWorkerProfile.LOCAL_CPU,
            )
            datg_build_entry = datg_build_policy.authorize(datg_build_request.runtime_id)
            datg_build_policy.validate_build(datg_build_request)
            return (_datg_model(datg_build_entry.model), (), DeterminismClass.EXACT)
        if kind is RunKind.GENERATE_DATG:
            datg_request = DatgGuidedGenerationRequest.model_validate(spec)
            datg_policy = DatgRuntimePolicy(
                self.settings.worker_datg_runtime_policies,
                worker_profile=DatgWorkerProfile.LOCAL_GPU,
            )
            datg_entry = datg_policy.authorize(datg_request.runtime_id)
            datg_policy.validate_generation(datg_request)
            index_root = _read_only_root(
                self.settings.worker_datg_index_cache_root,
                attested=self.settings.worker_datg_cache_mount_read_only,
                label="DATG index cache",
            )
            artifact = FilesystemDatgIndexCache(index_root).get(datg_request.index_cache_key_sha256)
            if artifact is None:
                raise ValueError("authorized DATG index is unavailable")
            content = _canonical_json(artifact.model_dump(mode="json"))
            return (
                _datg_model(datg_entry.model),
                (
                    ContentDigest(
                        name="datg-index",
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    ),
                ),
                DeterminismClass.BEST_EFFORT,
            )
        if kind is RunKind.TRAIN_PHON_RL:
            rl_request = PhonRlTrainingRequest.model_validate(spec)
            rl_policy = PhonRlRuntimePolicy(
                self.settings.worker_phon_rl_runtime_policies,
                worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
            )
            rl_entry = rl_policy.authorize(rl_request.runtime_id)
            rl_policy.validate(rl_request)
            prompt_bytes = _canonical_json(rl_request.prompt_source.model_dump(mode="json"))
            prompt_digest = prompt_source_sha256(rl_request.prompt_source)
            return (
                ModelProvenance(
                    backend="transformers-phon-rl",
                    identifier=rl_entry.model.repository_id,
                    revision=rl_entry.model.revision,
                    artifact_sha256=rl_entry.model.snapshot_sha256,
                ),
                (
                    ContentDigest(
                        name="prompt-source",
                        sha256=prompt_digest,
                        size_bytes=len(prompt_bytes),
                    ),
                ),
                DeterminismClass.BEST_EFFORT,
            )
        return None, (), DeterminismClass.EXACT


def build_profile_handler_registry(settings: Settings) -> HandlerRegistry:
    """Build the exact server-owned handler set for one deployment profile."""

    if settings.worker_profile == "interactive-cpu":
        raise RuntimeError("interactive-cpu does not run durable activities")
    _validate_image_identity(settings)
    stager = ConfiguredStagedArtifactWriter.from_settings(settings)

    if settings.worker_profile == "batch-cpu":
        _require_artifact_budget(settings, MAX_SELECTION_RESULT_ARTIFACT_BYTES, "selection")
        registry = build_core_handler_registry(settings, stager=stager)
        _reject_irrelevant_policies(settings, allowed="datg")
        if not settings.worker_datg_runtime_policies:
            return registry
        _require_artifact_budget(settings, MAX_DATG_RESULT_ARTIFACT_BYTES, "DATG")
        model_root = _read_only_root(
            settings.worker_datg_model_cache_root,
            attested=settings.worker_datg_cache_mount_read_only,
            label="DATG model cache",
        )
        batch_datg_coordinator = DatgCoordinator(
            DatgRuntimePolicy(
                settings.worker_datg_runtime_policies,
                worker_profile=DatgWorkerProfile.LOCAL_CPU,
            ),
            CorpusgenDatgAdapter(snapshot_resolver=OfflineSnapshotResolver(model_root)),
            EmptyDatgIndexCache(),
        )
        return registry.extended(build_datg_handlers("batch-cpu", batch_datg_coordinator, stager))

    if settings.worker_profile == "external-provider":
        _reject_irrelevant_policies(settings, allowed="external")
        if not (
            settings.worker_hosted_model_policies or settings.worker_huggingface_repository_policies
        ):
            raise RuntimeError("external-provider requires a hosted model or repository allowlist")
        external_handlers: list[DurableRunHandler] = []
        if settings.worker_hosted_model_policies:
            resolver = EnvironmentSecretResolver()
            for policy in settings.worker_hosted_model_policies:
                validate_hosted_policy_secrets(policy, resolver)
            hosted_coordinator = ModelRuntimeCoordinator(
                ModelRuntimePolicy(hosted_models=settings.worker_hosted_model_policies),
                CorpusgenModelRuntimeAdapter(secret_resolver=resolver),
            )
            external_handlers.extend(
                build_model_handlers("external-provider", hosted_coordinator, stager)
            )
        _require_artifact_budget(
            settings,
            MAX_REPOSITORY_RESULT_ARTIFACT_BYTES,
            "repository generation",
        )
        external_handlers.append(
            RepositoryGenerationDurableHandler(
                GenerationCoordinator(
                    CorpusgenGenerationAdapter(),
                    allowed_huggingface_sources=settings.worker_huggingface_repository_policies,
                ),
                stager,
            )
        )
        return HandlerRegistry(tuple(external_handlers))

    if settings.worker_profile == "gpu-inference":
        _reject_irrelevant_policies(settings, allowed="inference")
        if not settings.worker_local_model_policies and not settings.worker_datg_runtime_policies:
            raise RuntimeError("gpu-inference requires a local-model or DATG allowlist")
        handlers: list[DurableRunHandler] = []
        if settings.worker_local_model_policies:
            model_root = _read_only_root(
                settings.worker_model_cache_root,
                attested=settings.worker_model_cache_mount_read_only,
                label="local model cache",
            )
            model_coordinator = ModelRuntimeCoordinator(
                ModelRuntimePolicy(
                    local_models=settings.worker_local_model_policies,
                    worker_profile=WorkerModelProfile.LOCAL_GPU,
                ),
                CorpusgenModelRuntimeAdapter(
                    model_loader=TransformersLocalModelLoader(
                        approved_cache_root=model_root,
                    )
                ),
            )
            handlers.extend(build_model_handlers("gpu-inference", model_coordinator, stager))
        if settings.worker_datg_runtime_policies:
            _require_artifact_budget(settings, MAX_DATG_RESULT_ARTIFACT_BYTES, "DATG")
            model_root = _read_only_root(
                settings.worker_datg_model_cache_root,
                attested=settings.worker_datg_cache_mount_read_only,
                label="DATG model cache",
            )
            index_root = _read_only_root(
                settings.worker_datg_index_cache_root,
                attested=settings.worker_datg_cache_mount_read_only,
                label="DATG index cache",
            )
            datg_coordinator = DatgCoordinator(
                DatgRuntimePolicy(
                    settings.worker_datg_runtime_policies,
                    worker_profile=DatgWorkerProfile.LOCAL_GPU,
                ),
                CorpusgenDatgAdapter(snapshot_resolver=OfflineSnapshotResolver(model_root)),
                FilesystemDatgIndexCache(index_root),
            )
            handlers.extend(build_datg_handlers("gpu-inference", datg_coordinator, stager))
        return HandlerRegistry(tuple(handlers))

    if settings.worker_profile == "gpu-training":
        _reject_irrelevant_policies(settings, allowed="phon-rl")
        entries = settings.worker_phon_rl_runtime_policies
        if not entries:
            raise RuntimeError("gpu-training requires a Phon-RL allowlist")
        if any(
            not set(entry.allowed_prompt_strategies) <= _SUPPORTED_RL_STRATEGIES
            for entry in entries
        ):
            raise RuntimeError("Phon-RL prompt strategy is not implemented by this worker")
        _require_artifact_budget(settings, MAX_RL_RESULT_BYTES, "Phon-RL")
        roots = _phon_rl_roots(settings, entries)
        rl_coordinator = PhonRlTrainingCoordinator(
            PhonRlRuntimePolicy(entries, worker_profile=PhonRlWorkerProfile.LOCAL_GPU),
            CorpusgenPhonRlAdapter(
                snapshot_resolver=OfflinePhonRlSnapshotResolver(roots),
            ),
        )
        return HandlerRegistry(build_phon_rl_handlers("gpu-training", rl_coordinator, stager))

    raise RuntimeError("worker profile has no reviewed durable composition")


def worker_policy_sha256(settings: Settings) -> str:
    """Digest exact worker policy without exposing paths or secret references."""

    return hashlib.sha256(_worker_policy_bytes(settings)).hexdigest()


def _worker_policy_bytes(settings: Settings) -> bytes:
    policy = {
        "profile": settings.worker_profile,
        "activity_deadline_cap_seconds": settings.worker_activity_deadline_cap_seconds,
        "artifact_max_bytes": settings.artifact_max_bytes,
        "hosted": [item.model_dump(mode="json") for item in settings.worker_hosted_model_policies],
        "huggingface_repositories": [
            item.model_dump(mode="json") for item in settings.worker_huggingface_repository_policies
        ],
        "local": [item.model_dump(mode="json") for item in settings.worker_local_model_policies],
        "datg": [item.model_dump(mode="json") for item in settings.worker_datg_runtime_policies],
        "phon_rl": [
            item.model_dump(mode="json") for item in settings.worker_phon_rl_runtime_policies
        ],
        "model_cache_read_only": settings.worker_model_cache_mount_read_only,
        "datg_cache_read_only": settings.worker_datg_cache_mount_read_only,
        "phon_rl_cache_ids": sorted(settings.worker_phon_rl_cache_roots),
    }
    return _canonical_json(policy)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _datg_model(pin: DatgSnapshotPin) -> ModelProvenance:
    return ModelProvenance(
        backend="transformers-datg",
        identifier=pin.repository_id,
        revision=pin.revision,
        artifact_sha256=pin.snapshot_sha256,
    )


def _espeak_version() -> str | None:
    try:
        from phonemizer.backend import EspeakBackend  # type: ignore[import-untyped]

        return ".".join(str(item) for item in EspeakBackend.version())
    except Exception:
        return None


def _phoible_provenance() -> PhoibleProvenance | None:
    try:
        if not PhoibleSnapshotProvisioner().status().ready:
            return None
        return PhoibleProvenance(revision=PHOIBLE_COMMIT, sha256=PHOIBLE_SHA256)
    except Exception:
        return None


def _read_only_root(path: Path | None, *, attested: bool, label: str) -> Path:
    if path is None or not path.is_absolute() or not attested:
        raise RuntimeError(f"{label} requires an absolute read-only mounted root")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise RuntimeError(f"{label} is unavailable") from None
    if not resolved.is_dir():
        raise RuntimeError(f"{label} is not a directory")
    return resolved


def _phon_rl_roots(
    settings: Settings,
    entries: tuple[PhonRlRuntimePolicyEntry, ...],
) -> dict[str, Path]:
    identifiers = {entry.cache_root_id for entry in entries}
    configured = set(settings.worker_phon_rl_cache_roots)
    if identifiers != configured or any(
        _SAFE_CACHE_ID.fullmatch(identifier) is None for identifier in configured
    ):
        raise RuntimeError("Phon-RL cache-root mapping must exactly match the allowlist")
    return {
        identifier: _read_only_root(path, attested=True, label="Phon-RL model cache")
        for identifier, path in settings.worker_phon_rl_cache_roots.items()
    }


def _reject_irrelevant_policies(settings: Settings, *, allowed: str) -> None:
    configured = {
        "hosted": bool(settings.worker_hosted_model_policies),
        "repository": bool(settings.worker_huggingface_repository_policies),
        "local": bool(settings.worker_local_model_policies),
        "datg": bool(settings.worker_datg_runtime_policies),
        "phon-rl": bool(settings.worker_phon_rl_runtime_policies),
    }
    permitted = {allowed}
    if allowed == "inference":
        permitted = {"local", "datg"}
    elif allowed == "external":
        permitted = {"hosted", "repository"}
    if any(value and name not in permitted for name, value in configured.items()):
        raise RuntimeError("worker policy includes a capability for another profile")


def _require_artifact_budget(settings: Settings, minimum: int, label: str) -> None:
    if settings.artifact_max_bytes < minimum:
        raise RuntimeError(f"{label} result budget exceeds configured artifact storage")


def _validate_image_identity(settings: Settings) -> None:
    if settings.environment in {"staging", "production"} and settings.worker_image_digest is None:
        raise RuntimeError("deployed workers require an immutable image digest")


__all__ = [
    "PROFILE_RUN_KINDS",
    "EmptyDatgIndexCache",
    "FilesystemDatgIndexCache",
    "WorkerExecutionFactsFactory",
    "build_profile_handler_registry",
    "task_queue_for_kind",
    "worker_policy_sha256",
]
