"""Profile-specific durable handlers for model operations inside the existing process runner."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from corpuskit.domain.artifacts import StagedArtifactResult
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError
from corpuskit.domain.jobs import RunKind, canonical_spec_sha256
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    WorkerModelProfile,
)
from corpuskit.services.model_runtime import ModelRuntimeCoordinator
from corpuskit.workflows.handlers import DurableRunHandler, HandlerRegistry, ResultSummary
from corpuskit.workflows.progress import DurableRunProgress
from corpuskit.workflows.trusted_inputs import (
    claim_trusted_input,
    default_trusted_input_root,
    parse_trusted_run_inputs,
    read_materialized_peft_manifest,
)

MAX_MODEL_RESULT_ARTIFACT_BYTES = 4 * 1024 * 1024


class ModelResultArtifactStager(Protocol):
    """Stage unowned bytes only; authoritative parent-side code must adopt them."""

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class HostedGenerationDurableHandler:
    coordinator: ModelRuntimeCoordinator
    staging: ModelResultArtifactStager
    kind: RunKind = RunKind.GENERATE_LLM

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = HostedGenerationRequest.model_validate(spec)
        result = self.coordinator.run_hosted(request)
        return _stage(
            self.staging,
            self.kind,
            result.model_dump_json().encode(),
            schema_id=result.schema_id,
        )


@dataclass(frozen=True, slots=True)
class LocalGenerationDurableHandler:
    coordinator: ModelRuntimeCoordinator
    staging: ModelResultArtifactStager
    trusted_input_root: Path = field(
        default_factory=lambda: default_trusted_input_root("gpu-inference")
    )
    kind: RunKind = RunKind.GENERATE_LOCAL

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = LocalGenerationRequest.model_validate(spec)
        if request.phon_rl_adapter is not None:
            raise EngineContractError("model_runtime.local.phon_rl_materialization_required")
        result = self.coordinator.run_local(request)
        return _stage(
            self.staging,
            self.kind,
            result.model_dump_json().encode(),
            schema_id=result.schema_id,
        )

    def execute_with_trusted_inputs(
        self,
        spec: Mapping[str, Any],
        trusted_inputs: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None] | None,
    ) -> ResultSummary:
        del emit
        request = LocalGenerationRequest.model_validate(spec)
        try:
            trusted = parse_trusted_run_inputs(trusted_inputs)
        except ValueError:
            raise EngineContractError("model_runtime.local.phon_rl_claim") from None
        if trusted.spec_sha256 != canonical_spec_sha256(dict(spec)):
            raise EngineContractError("model_runtime.local.phon_rl_claim")
        directory = claim_trusted_input(
            trusted,
            root=self.trusted_input_root,
            expected_kind=self.kind,
        )
        adapter = request.phon_rl_adapter
        if trusted.peft_adapter is None or adapter is None:
            raise EngineContractError("model_runtime.local.phon_rl_claim")
        if (
            adapter.artifact_id != trusted.peft_adapter.artifact_id
            or adapter.artifact_sha256 != trusted.peft_adapter.artifact_sha256
            or adapter.checkpoint_sha256 != trusted.peft_adapter.checkpoint_sha256
        ):
            raise EngineContractError("model_runtime.local.phon_rl_claim")
        adapter_root, manifest = read_materialized_peft_manifest(
            directory,
            trusted.peft_adapter,
        )
        result = self.coordinator.run_local_phon_rl(
            request,
            adapter_root=adapter_root,
            compatibility=manifest.compatibility,
        )
        return _stage(
            self.staging,
            self.kind,
            result.model_dump_json().encode(),
            schema_id=result.schema_id,
        )


@dataclass(frozen=True, slots=True)
class LanguageModelAnalysisDurableHandler:
    coordinator: ModelRuntimeCoordinator
    staging: ModelResultArtifactStager
    kind: RunKind = RunKind.PERPLEXITY

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = LanguageModelAnalysisRequest.model_validate(spec)
        result = self.coordinator.analyze(request)
        return _stage(
            self.staging,
            self.kind,
            result.model_dump_json().encode(),
            schema_id=result.schema_id,
        )


def build_model_handler_registry(
    deployment_profile: str,
    coordinator: ModelRuntimeCoordinator,
    staging: ModelResultArtifactStager,
    trusted_input_root: Path | None = None,
) -> HandlerRegistry:
    """Map hyphenated deployment profiles to an exact, minimal model-operation allowlist."""

    return HandlerRegistry(
        build_model_handlers(
            deployment_profile,
            coordinator,
            staging,
            trusted_input_root=trusted_input_root,
        )
    )


def build_model_handlers(
    deployment_profile: str,
    coordinator: ModelRuntimeCoordinator,
    staging: ModelResultArtifactStager,
    *,
    trusted_input_root: Path | None = None,
) -> tuple[DurableRunHandler, ...]:
    """Build handlers for extension into an existing batch registry without another process."""

    if deployment_profile == "external-provider":
        return (HostedGenerationDurableHandler(coordinator, staging),)
    if deployment_profile == "batch-cpu":
        _require_policy_profile(coordinator, WorkerModelProfile.LOCAL_CPU)
        return (
            LocalGenerationDurableHandler(
                coordinator,
                staging,
                trusted_input_root or default_trusted_input_root(deployment_profile),
            ),
            LanguageModelAnalysisDurableHandler(coordinator, staging),
        )
    if deployment_profile == "gpu-inference":
        _require_policy_profile(coordinator, WorkerModelProfile.LOCAL_GPU)
        return (
            LocalGenerationDurableHandler(
                coordinator,
                staging,
                trusted_input_root or default_trusted_input_root(deployment_profile),
            ),
            LanguageModelAnalysisDurableHandler(coordinator, staging),
        )
    raise RuntimeError("The worker profile does not permit model inference handlers.")


def model_activity_timeout_seconds(kind: RunKind, spec: Mapping[str, Any]) -> float:
    """Parse only bounded metadata needed by the parent to kill the single child on time."""

    if kind is RunKind.GENERATE_LLM:
        return HostedGenerationRequest.model_validate(spec).activity_timeout_seconds
    if kind is RunKind.GENERATE_LOCAL:
        return LocalGenerationRequest.model_validate(spec).activity_timeout_seconds
    if kind is RunKind.PERPLEXITY:
        return LanguageModelAnalysisRequest.model_validate(spec).activity_timeout_seconds
    raise RuntimeError("The run kind has no model activity deadline contract.")


def _require_policy_profile(
    coordinator: ModelRuntimeCoordinator,
    expected: WorkerModelProfile,
) -> None:
    if coordinator.policy.worker_profile is not expected:
        raise RuntimeError("The model policy does not match the deployment worker profile.")


def _stage(
    stager: ModelResultArtifactStager,
    kind: RunKind,
    payload: bytes,
    *,
    schema_id: str,
) -> ResultSummary:
    if len(payload) > MAX_MODEL_RESULT_ARTIFACT_BYTES:
        raise EngineContractError("model_runtime.artifact.size")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        reference = stager.stage_model_result(
            kind=kind,
            payload=payload,
            content_sha256=digest,
        )
    except Exception:
        raise EngineUnavailableError("model_runtime.artifact.staging") from None
    try:
        claim = StagedArtifactResult(
            staged_artifact_ref=reference,
            schema_id=schema_id,
            artifact_type="run-result",
            media_type="application/json",
            size_bytes=len(payload),
        )
    except ValueError:
        raise EngineContractError("model_runtime.artifact.staging_reference") from None
    if claim.sha256 != digest:
        raise EngineContractError("model_runtime.artifact.staging_reference")
    return claim.model_dump(mode="json")


__all__ = [
    "HostedGenerationDurableHandler",
    "LanguageModelAnalysisDurableHandler",
    "LocalGenerationDurableHandler",
    "ModelResultArtifactStager",
    "build_model_handler_registry",
    "build_model_handlers",
    "model_activity_timeout_seconds",
]
