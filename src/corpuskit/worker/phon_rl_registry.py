"""GPU-training Phon-RL handler for the existing outer process runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from corpuskit.domain.artifacts import StagedArtifactResult
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError
from corpuskit.domain.jobs import RunKind, canonical_spec_sha256
from corpuskit.domain.phon_rl import (
    MAX_RL_RESULT_BYTES,
    PhonRlProgressPoint,
    PhonRlStaticPromptSource,
    PhonRlTrainingRequest,
    PhonRlWorkerProfile,
)
from corpuskit.services.phon_rl import PhonRlAuthorizedPromptReader, PhonRlTrainingCoordinator
from corpuskit.workflows.handlers import DurableRunHandler, HandlerRegistry, ResultSummary
from corpuskit.workflows.progress import (
    MAX_DURABLE_PROGRESS_MESSAGES,
    DurableRunProgress,
    RunProgressPhase,
)
from corpuskit.workflows.trusted_inputs import (
    TrustedPromptInput,
    claim_trusted_input,
    default_trusted_input_root,
    parse_trusted_run_inputs,
    read_materialized_prompts,
)

_RL_RESULT_SCHEMA = "corpuskit.phon-rl-training-result.v1"


class PhonRlResultStager(Protocol):
    """Authority-free child staging; tenant/run ownership is adopted by the parent."""

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TrainPhonRlDurableHandler:
    """Runs inline in one killable ProcessExecutionRunner child; never starts another."""

    coordinator: PhonRlTrainingCoordinator
    staging: PhonRlResultStager
    trusted_input_root: Path = field(
        default_factory=lambda: default_trusted_input_root("gpu-training")
    )
    kind: RunKind = RunKind.TRAIN_PHON_RL

    def execute(self, spec: Mapping[str, Any]) -> ResultSummary:
        return self._execute(spec, emit=None)

    def execute_with_progress(
        self,
        spec: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None],
    ) -> ResultSummary:
        return self._execute(spec, emit=emit)

    def execute_with_trusted_inputs(
        self,
        spec: Mapping[str, Any],
        trusted_inputs: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None] | None,
    ) -> ResultSummary:
        try:
            trusted = parse_trusted_run_inputs(trusted_inputs)
        except ValueError:
            raise EngineContractError("phon_rl.prompt_artifact.claim") from None
        if trusted.spec_sha256 != canonical_spec_sha256(dict(spec)):
            raise EngineContractError("phon_rl.prompt_artifact.claim")
        directory = claim_trusted_input(
            trusted,
            root=self.trusted_input_root,
            expected_kind=self.kind,
        )
        prompt_input = trusted.prompt
        if prompt_input is None:
            raise EngineContractError("phon_rl.prompt_artifact.claim")

        @dataclass(frozen=True, slots=True)
        class _Reader:
            directory: Path
            prompt_input: TrustedPromptInput

            def read(self, source: PhonRlStaticPromptSource) -> tuple[str, ...]:
                if (
                    source.artifact_id != self.prompt_input.artifact_id
                    or source.content_sha256 != self.prompt_input.content_sha256
                    or source.prompt_count != self.prompt_input.prompt_count
                ):
                    raise EngineContractError("phon_rl.prompt_artifact.claim")
                return read_materialized_prompts(self.directory, self.prompt_input)

        return self._execute(
            spec,
            emit=emit,
            prompt_reader=_Reader(directory, prompt_input),
        )

    def _execute(
        self,
        spec: Mapping[str, Any],
        *,
        emit: Callable[[DurableRunProgress], None] | None,
        prompt_reader: PhonRlAuthorizedPromptReader | None = None,
    ) -> ResultSummary:
        request = PhonRlTrainingRequest.model_validate(spec)
        total_steps = request.parameters.num_steps
        step_budget = MAX_DURABLE_PROGRESS_MESSAGES - 4
        step_stride = max(1, (total_steps + step_budget - 1) // step_budget)
        sequence = 0
        last_phase: RunProgressPhase | None = None

        def publish(phase: RunProgressPhase, *, completed: int | None = None) -> None:
            nonlocal last_phase, sequence
            if emit is None:
                return
            emit(
                DurableRunProgress(
                    sequence=sequence,
                    phase=phase,
                    completed=completed,
                    total=total_steps if completed is not None else None,
                )
            )
            last_phase = phase
            sequence += 1

        def track(point: PhonRlProgressPoint) -> None:
            completed = point.step + 1
            if completed == 1 or completed == total_steps or completed % step_stride == 0:
                publish(RunProgressPhase.TRAINING, completed=completed)

        publish(RunProgressPhase.PREPARING_TRAINING)
        try:
            result = self.coordinator.train(
                request,
                emit=track if emit is not None else None,
                prompt_reader=prompt_reader,
            )
            publish(RunProgressPhase.STAGING_RESULT, completed=total_steps)
            payload = json.dumps(
                result.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if not payload or len(payload) > MAX_RL_RESULT_BYTES:
                raise EngineContractError("phon_rl.staging.size")
            digest = hashlib.sha256(payload).hexdigest()
            try:
                reference = self.staging.stage_model_result(
                    kind=self.kind,
                    payload=payload,
                    content_sha256=digest,
                )
            except Exception:
                raise EngineUnavailableError("phon_rl.staging.write") from None
            claim = StagedArtifactResult(
                staged_artifact_ref=reference,
                schema_id=_RL_RESULT_SCHEMA,
                artifact_type="run-result",
                media_type="application/json",
                size_bytes=len(payload),
            )
            if claim.sha256 != digest:
                raise EngineContractError("phon_rl.staging.reference")
            publish(RunProgressPhase.FINISHED, completed=total_steps)
            return claim.model_dump(mode="json")
        except ValueError:
            publish(RunProgressPhase.FAILED)
            raise EngineContractError("phon_rl.staging.reference") from None
        except Exception:
            if last_phase is not RunProgressPhase.FAILED:
                publish(RunProgressPhase.FAILED)
            raise


def build_phon_rl_handlers(
    deployment_profile: str,
    coordinator: PhonRlTrainingCoordinator,
    staging: PhonRlResultStager,
    trusted_input_root: Path | None = None,
) -> tuple[DurableRunHandler, ...]:
    """Return the single operation permitted on an exact GPU-training profile."""

    if deployment_profile != "gpu-training":
        raise RuntimeError("The deployment profile does not permit Phon-RL training.")
    if coordinator.policy.worker_profile is not PhonRlWorkerProfile.LOCAL_GPU:
        raise RuntimeError("The Phon-RL policy does not match the GPU-training profile.")
    return (
        TrainPhonRlDurableHandler(
            coordinator,
            staging,
            trusted_input_root or default_trusted_input_root("gpu-training"),
        ),
    )


def build_phon_rl_handler_registry(
    deployment_profile: str,
    coordinator: PhonRlTrainingCoordinator,
    staging: PhonRlResultStager,
    trusted_input_root: Path | None = None,
) -> HandlerRegistry:
    """Build an isolated registry; production registration awaits parent schema adoption."""

    return HandlerRegistry(
        build_phon_rl_handlers(
            deployment_profile,
            coordinator,
            staging,
            trusted_input_root,
        )
    )


def phon_rl_activity_timeout_seconds(kind: RunKind, spec: Mapping[str, Any]) -> float:
    """Parse only the bounded authoritative DTO field used by the parent deadline."""

    if kind is not RunKind.TRAIN_PHON_RL:
        raise RuntimeError("The run kind has no Phon-RL activity deadline contract.")
    return PhonRlTrainingRequest.model_validate(spec).parameters.activity_timeout_seconds


__all__ = [
    "PhonRlResultStager",
    "TrainPhonRlDurableHandler",
    "build_phon_rl_handler_registry",
    "build_phon_rl_handlers",
    "phon_rl_activity_timeout_seconds",
]
