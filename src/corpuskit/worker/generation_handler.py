"""Killable, deadline-bound repository generation activity handler."""

from __future__ import annotations

import hashlib
import multiprocessing
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, Protocol

from pydantic import ValidationError

from corpuskit.domain.artifacts import StagedArtifactResult
from corpuskit.domain.errors import (
    ApplicationError,
    ApplicationErrorCode,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
    InventoryDataUnavailableError,
    InventoryNotFoundError,
    LanguageNotSupportedError,
)
from corpuskit.domain.generation import (
    MAX_GENERATION_ITERATIONS,
    GenerationExecutionMode,
    GenerationPhase,
    GenerationProgress,
    RepositoryGenerationRequest,
    RepositoryGenerationResult,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.services.generation_scoring import GenerationCoordinator, ProgressSink
from corpuskit.workflows.handlers import ResultSummary
from corpuskit.workflows.progress import (
    MAX_DURABLE_PROGRESS_MESSAGES,
    DurableRunProgress,
    RunProgressPhase,
)

_POLL_SECONDS = 0.05
_TERMINATE_GRACE_SECONDS = 1.0
MAX_REPOSITORY_RESULT_ARTIFACT_BYTES = 4 * 1024 * 1024

MessageKind = Literal["progress", "result", "application_error", "engine_error"]
ActivityMessage = tuple[MessageKind, object]


class ActivityDeadlineExecutor(Protocol):
    """Executor contract isolated for deterministic handler tests."""

    def run(
        self,
        coordinator: GenerationCoordinator,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        timeout_seconds: float,
        emit: ProgressSink | None,
    ) -> RepositoryGenerationResult: ...


class RepositoryResultArtifactStager(Protocol):
    """Stage unowned bytes only; the durable parent assigns run and tenant authority."""

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str: ...


def _activity_process(
    connection: Connection,
    coordinator: GenerationCoordinator,
    request: RepositoryGenerationRequest,
    execution_mode: GenerationExecutionMode,
) -> None:
    """Child process target; only sanitized structured messages cross the pipe."""

    def emit(event: GenerationProgress) -> None:
        connection.send(("progress", event.model_dump(mode="json")))

    try:
        result = coordinator.execute(
            request,
            execution_mode=execution_mode,
            emit=emit,
        )
        connection.send(("result", result.model_dump(mode="json")))
    except ApplicationError as error:
        connection.send(
            (
                "application_error",
                {"code": error.code.value, "operation": error.operation},
            )
        )
    except Exception:
        connection.send(("engine_error", {"operation": "generation.activity"}))
    finally:
        connection.close()


class ProcessActivityDeadlineExecutor:
    """Run an activity in a process that can be terminated at its hard deadline."""

    def run(
        self,
        coordinator: GenerationCoordinator,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        timeout_seconds: float,
        emit: ProgressSink | None,
    ) -> RepositoryGenerationResult:
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(
            target=_activity_process,
            args=(sending, coordinator, request, execution_mode),
            daemon=True,
            name="corpuskit-generation-activity",
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            process.start()
        except Exception:
            receiving.close()
            sending.close()
            raise EngineUnavailableError("generation.activity.start") from None
        sending.close()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    raise EngineUnavailableError("generation.activity.timeout")
                if receiving.poll(min(_POLL_SECONDS, remaining)):
                    message = receiving.recv()
                    result = self._handle_message(message, emit)
                    if result is not None:
                        return result
                if not process.is_alive() and not receiving.poll():
                    raise EngineUnavailableError("generation.activity.process")
        except EOFError:
            raise EngineUnavailableError("generation.activity.process") from None
        finally:
            receiving.close()
            if process.is_alive():
                self._terminate(process)
            else:
                process.join(timeout=_TERMINATE_GRACE_SECONDS)

    @staticmethod
    def _handle_message(
        message: object,
        emit: ProgressSink | None,
    ) -> RepositoryGenerationResult | None:
        if not isinstance(message, tuple) or len(message) != 2:
            raise EngineContractError("generation.activity.message")
        kind, payload = message
        try:
            if kind == "progress":
                event = GenerationProgress.model_validate(payload)
                if emit is not None:
                    emit(event)
                return None
            if kind == "result":
                return RepositoryGenerationResult.model_validate(payload)
            if kind == "application_error":
                if not isinstance(payload, dict):
                    raise EngineContractError("generation.activity.message")
                code = ApplicationErrorCode(payload["code"])
                operation = payload["operation"]
                if not isinstance(operation, str):
                    raise EngineContractError("generation.activity.message")
                error_type = _ERROR_TYPES.get(code, EngineUnavailableError)
                raise error_type(operation)
            if kind == "engine_error":
                raise EngineUnavailableError("generation.activity")
        except ApplicationError:
            raise
        except (ValidationError, ValueError, TypeError, KeyError):
            raise EngineContractError("generation.activity.message") from None
        raise EngineContractError("generation.activity.message")

    @staticmethod
    def _terminate(process: BaseProcess) -> None:
        process.terminate()
        process.join(timeout=_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_TERMINATE_GRACE_SECONDS)


class RepositoryGenerationJobHandler:
    """Callable activity boundary; it does not claim workflow or durable-run completion."""

    def __init__(
        self,
        coordinator: GenerationCoordinator,
        executor: ActivityDeadlineExecutor | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._executor = executor or ProcessActivityDeadlineExecutor()

    def __call__(
        self,
        request: RepositoryGenerationRequest,
        emit: ProgressSink | None = None,
    ) -> RepositoryGenerationResult:
        self._coordinator.validate(
            request,
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
        last_sequence = -1
        last_phase: GenerationPhase | None = None

        def track(event: GenerationProgress) -> None:
            nonlocal last_phase, last_sequence
            if event.sequence <= last_sequence:
                raise EngineContractError("generation.activity.progress")
            last_sequence = event.sequence
            last_phase = event.phase
            if emit is not None:
                emit(event)

        try:
            return self._executor.run(
                self._coordinator,
                request,
                execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
                timeout_seconds=request.activity_timeout_seconds,
                emit=track,
            )
        except ApplicationError:
            if emit is not None and last_phase is not GenerationPhase.FAILED:
                emit(
                    GenerationProgress(
                        sequence=last_sequence + 1,
                        phase=GenerationPhase.FAILED,
                    )
                )
            raise
        except Exception:
            if emit is not None and last_phase is not GenerationPhase.FAILED:
                emit(
                    GenerationProgress(
                        sequence=last_sequence + 1,
                        phase=GenerationPhase.FAILED,
                    )
                )
            raise EngineUnavailableError("generation.activity") from None


@dataclass(frozen=True, slots=True)
class RepositoryGenerationDurableHandler:
    """Execute inline inside the platform's one killable child and stage the full result."""

    coordinator: GenerationCoordinator
    staging: RepositoryResultArtifactStager
    kind: RunKind = RunKind.GENERATE_REPOSITORY

    def execute(self, spec: Mapping[str, object]) -> ResultSummary:
        return self._execute(spec, emit=None)

    def execute_with_progress(
        self,
        spec: Mapping[str, object],
        emit: Callable[[DurableRunProgress], None],
    ) -> ResultSummary:
        return self._execute(spec, emit=emit)

    def _execute(
        self,
        spec: Mapping[str, object],
        *,
        emit: Callable[[DurableRunProgress], None] | None,
    ) -> ResultSummary:
        request = RepositoryGenerationRequest.model_validate(spec)
        sequence = 0
        last_phase: RunProgressPhase | None = None
        total_iterations = request.stopping.max_iterations or MAX_GENERATION_ITERATIONS
        candidate_ceiling = request.stopping.max_sentences or (
            total_iterations * request.candidates_per_iteration
        )
        candidate_budget = MAX_DURABLE_PROGRESS_MESSAGES - 8
        candidate_stride = max(1, (candidate_ceiling + candidate_budget - 1) // candidate_budget)

        def publish(
            phase: RunProgressPhase,
            *,
            completed: int | None = None,
            coverage: float | None = None,
            accepted_count: int | None = None,
        ) -> None:
            nonlocal last_phase, sequence
            if emit is None:
                return
            emit(
                DurableRunProgress(
                    sequence=sequence,
                    phase=phase,
                    completed=completed,
                    total=total_iterations if completed is not None else None,
                    coverage=coverage,
                    accepted_count=accepted_count,
                )
            )
            last_phase = phase
            sequence += 1

        def track(event: GenerationProgress) -> None:
            phase = RunProgressPhase(event.phase.value)
            if event.phase is GenerationPhase.CANDIDATE_ACCEPTED and not (
                event.accepted_count == 1 or event.accepted_count % candidate_stride == 0
            ):
                return
            if event.phase is GenerationPhase.FINISHED:
                phase = RunProgressPhase.STAGING_RESULT
            publish(
                phase,
                completed=(
                    min(event.iteration, total_iterations)
                    if event.phase
                    in {
                        GenerationPhase.GENERATING,
                        GenerationPhase.CANDIDATE_ACCEPTED,
                        GenerationPhase.FINISHED,
                    }
                    else None
                ),
                coverage=(
                    event.coverage
                    if event.phase in {GenerationPhase.CANDIDATE_ACCEPTED, GenerationPhase.FINISHED}
                    else None
                ),
                accepted_count=(
                    event.accepted_count
                    if event.phase in {GenerationPhase.CANDIDATE_ACCEPTED, GenerationPhase.FINISHED}
                    else None
                ),
            )

        try:
            result = self.coordinator.execute(
                request,
                execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
                emit=track,
            )
            payload = result.model_dump_json().encode("utf-8")
            if len(payload) > MAX_REPOSITORY_RESULT_ARTIFACT_BYTES:
                raise EngineContractError("generation.repository.artifact.size")
            digest = hashlib.sha256(payload).hexdigest()
            try:
                reference = self.staging.stage_model_result(
                    kind=self.kind,
                    payload=payload,
                    content_sha256=digest,
                )
            except Exception:
                raise EngineUnavailableError("generation.repository.artifact.staging") from None
            claim = StagedArtifactResult(
                staged_artifact_ref=reference,
                schema_id=result.schema_id,
                artifact_type="run-result",
                media_type="application/json",
                size_bytes=len(payload),
            )
            if claim.sha256 != digest:
                raise EngineContractError("generation.repository.artifact.staging_reference")
            publish(
                RunProgressPhase.FINISHED,
                completed=min(result.iterations, total_iterations),
                coverage=result.coverage,
                accepted_count=len(result.accepted),
            )
            return claim.model_dump(mode="json")
        except ValueError:
            publish(RunProgressPhase.FAILED)
            raise EngineContractError("generation.repository.artifact.staging_reference") from None
        except Exception:
            if last_phase is not RunProgressPhase.FAILED:
                publish(RunProgressPhase.FAILED)
            raise


_ERROR_TYPES: dict[ApplicationErrorCode, Callable[[str], ApplicationError]] = {
    ApplicationErrorCode.INVALID_REQUEST: InvalidRequestError,
    ApplicationErrorCode.LANGUAGE_NOT_SUPPORTED: LanguageNotSupportedError,
    ApplicationErrorCode.INVENTORY_NOT_FOUND: InventoryNotFoundError,
    ApplicationErrorCode.INVENTORY_DATA_UNAVAILABLE: InventoryDataUnavailableError,
    ApplicationErrorCode.DEPENDENCY_UNAVAILABLE: DependencyUnavailableError,
    ApplicationErrorCode.ENGINE_UNAVAILABLE: EngineUnavailableError,
    ApplicationErrorCode.ENGINE_CONTRACT_VIOLATION: EngineContractError,
}


__all__ = [
    "MAX_REPOSITORY_RESULT_ARTIFACT_BYTES",
    "ActivityDeadlineExecutor",
    "ProcessActivityDeadlineExecutor",
    "RepositoryGenerationDurableHandler",
    "RepositoryGenerationJobHandler",
    "RepositoryResultArtifactStager",
]
