"""Profile-gated DATG handlers that run inside the existing outer process boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from corpuskit.domain.artifacts import StagedArtifactResult
from corpuskit.domain.datg import (
    DatgGuidedGenerationRequest,
    DatgIndexBuildRequest,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError
from corpuskit.domain.jobs import RunKind
from corpuskit.services.datg import DatgCoordinator
from corpuskit.workflows.handlers import DurableRunHandler, HandlerRegistry, ResultSummary

MAX_DATG_RESULT_ARTIFACT_BYTES = 36 * 1024 * 1024


class DatgResultStager(Protocol):
    """Stage unowned bytes; only trusted parent code may adopt them into a run."""

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class BuildDatgIndexDurableHandler:
    coordinator: DatgCoordinator
    staging: DatgResultStager
    kind: RunKind = RunKind.BUILD_DATG_INDEX

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = DatgIndexBuildRequest.model_validate(spec)
        result = self.coordinator.build_index(request)
        return _stage(
            self.staging,
            self.kind,
            _canonical_payload(result.model_dump(mode="json")),
            schema_id=result.schema_id,
        )


@dataclass(frozen=True, slots=True)
class GenerateDatgDurableHandler:
    coordinator: DatgCoordinator
    staging: DatgResultStager
    kind: RunKind = RunKind.GENERATE_DATG

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        request = DatgGuidedGenerationRequest.model_validate(spec)
        result = self.coordinator.generate(request)
        return _stage(
            self.staging,
            self.kind,
            _canonical_payload(result.model_dump(mode="json")),
            schema_id=result.schema_id,
        )


def build_datg_handler_registry(
    deployment_profile: str,
    coordinator: DatgCoordinator,
    staging: DatgResultStager,
) -> HandlerRegistry:
    """Build an isolated registry; production registration awaits parent adoption wiring."""

    return HandlerRegistry(build_datg_handlers(deployment_profile, coordinator, staging))


def build_datg_handlers(
    deployment_profile: str,
    coordinator: DatgCoordinator,
    staging: DatgResultStager,
) -> tuple[DurableRunHandler, ...]:
    """Return the exact operations allowed on a deployment profile."""

    if deployment_profile == "batch-cpu":
        if coordinator.policy.worker_profile is not DatgWorkerProfile.LOCAL_CPU:
            raise RuntimeError("The DATG policy does not match the batch CPU profile.")
        return (BuildDatgIndexDurableHandler(coordinator, staging),)
    if deployment_profile == "gpu-inference":
        if coordinator.policy.worker_profile is not DatgWorkerProfile.LOCAL_GPU:
            raise RuntimeError("The DATG policy does not match the GPU profile.")
        return (GenerateDatgDurableHandler(coordinator, staging),)
    raise RuntimeError("The deployment profile does not permit DATG operations.")


def datg_activity_timeout_seconds(kind: RunKind, spec: Mapping[str, Any]) -> float:
    """Parse bounded metadata for the parent's one killable ProcessExecutionRunner."""

    if kind is RunKind.BUILD_DATG_INDEX:
        return DatgIndexBuildRequest.model_validate(spec).activity_timeout_seconds
    if kind is RunKind.GENERATE_DATG:
        return DatgGuidedGenerationRequest.model_validate(spec).activity_timeout_seconds
    raise RuntimeError("The run kind has no DATG activity deadline contract.")


def _stage(
    stager: DatgResultStager,
    kind: RunKind,
    payload: bytes,
    *,
    schema_id: str,
) -> ResultSummary:
    if not payload or len(payload) > MAX_DATG_RESULT_ARTIFACT_BYTES:
        raise EngineContractError("datg.staging.size")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        reference = stager.stage_model_result(
            kind=kind,
            payload=payload,
            content_sha256=digest,
        )
    except Exception:
        raise EngineUnavailableError("datg.staging.write") from None
    try:
        claim = StagedArtifactResult(
            staged_artifact_ref=reference,
            schema_id=schema_id,
            artifact_type="run-result",
            media_type="application/json",
            size_bytes=len(payload),
        )
    except ValueError:
        raise EngineContractError("datg.staging.reference") from None
    if claim.sha256 != digest:
        raise EngineContractError("datg.staging.reference")
    return claim.model_dump(mode="json")


def _canonical_payload(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "MAX_DATG_RESULT_ARTIFACT_BYTES",
    "BuildDatgIndexDurableHandler",
    "DatgResultStager",
    "GenerateDatgDurableHandler",
    "build_datg_handler_registry",
    "build_datg_handlers",
    "datg_activity_timeout_seconds",
]
