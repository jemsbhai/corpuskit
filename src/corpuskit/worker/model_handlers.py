"""Killable worker-side handlers for optional hosted and local model activities."""

from __future__ import annotations

import multiprocessing
import re
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, Protocol, cast

from pydantic import ValidationError

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
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    HostedGenerationResult,
    LanguageModelAnalysisRequest,
    LanguageModelAnalysisResult,
    LocalGenerationRequest,
    LocalGenerationResult,
)
from corpuskit.services.model_runtime import ModelRuntimeCoordinator

_POLL_SECONDS = 0.05
_TERMINATE_GRACE_SECONDS = 1.0
_SAFE_OPERATION = re.compile(r"^[a-z][a-z0-9_.]{0,79}$", re.ASCII)

ModelOperation = Literal["hosted_generation", "local_generation", "language_model_analysis"]
ModelRequest = HostedGenerationRequest | LocalGenerationRequest | LanguageModelAnalysisRequest
ModelResult = HostedGenerationResult | LocalGenerationResult | LanguageModelAnalysisResult
MessageKind = Literal["result", "application_error", "engine_error"]
ActivityMessage = tuple[MessageKind, object]


class ModelActivityDeadlineExecutor(Protocol):
    """Injectable process boundary for deterministic tests and Temporal integration."""

    def run(
        self,
        coordinator: ModelRuntimeCoordinator,
        operation: ModelOperation,
        request: ModelRequest,
        *,
        timeout_seconds: float,
    ) -> ModelResult: ...


def _model_activity_process(
    connection: Connection,
    coordinator: ModelRuntimeCoordinator,
    operation: ModelOperation,
    request: ModelRequest,
) -> None:
    try:
        result = _dispatch(coordinator, operation, request)
        connection.send(("result", result.model_dump(mode="json")))
    except ApplicationError as error:
        connection.send(
            (
                "application_error",
                {"code": error.code.value, "operation": error.operation},
            )
        )
    except Exception:
        connection.send(("engine_error", {"operation": "model_runtime.activity"}))
    finally:
        connection.close()


class ProcessModelActivityDeadlineExecutor:
    """Deadline covers secret resolution, network/model load, G2P, scoring and loop work."""

    def run(
        self,
        coordinator: ModelRuntimeCoordinator,
        operation: ModelOperation,
        request: ModelRequest,
        *,
        timeout_seconds: float,
    ) -> ModelResult:
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(
            target=_model_activity_process,
            args=(sending, coordinator, operation, request),
            daemon=True,
            name="corpuskit-model-activity",
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            process.start()
        except Exception:
            receiving.close()
            sending.close()
            raise EngineUnavailableError("model_runtime.activity.start") from None
        sending.close()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    raise EngineUnavailableError("model_runtime.activity.timeout")
                if receiving.poll(min(_POLL_SECONDS, remaining)):
                    return self._handle(receiving.recv(), operation)
                if not process.is_alive() and not receiving.poll():
                    raise EngineUnavailableError("model_runtime.activity.process")
        except EOFError:
            raise EngineUnavailableError("model_runtime.activity.process") from None
        finally:
            receiving.close()
            if process.is_alive():
                self._terminate(process)
            else:
                process.join(timeout=_TERMINATE_GRACE_SECONDS)

    @staticmethod
    def _handle(message: object, operation: ModelOperation) -> ModelResult:
        if not isinstance(message, tuple) or len(message) != 2:
            raise EngineContractError("model_runtime.activity.message")
        kind, payload = message
        try:
            if kind == "result":
                return _validate_result(operation, payload)
            if kind == "application_error":
                if not isinstance(payload, dict):
                    raise EngineContractError("model_runtime.activity.message")
                code = ApplicationErrorCode(payload["code"])
                error_operation = payload["operation"]
                if (
                    not isinstance(error_operation, str)
                    or _SAFE_OPERATION.fullmatch(error_operation) is None
                ):
                    raise EngineContractError("model_runtime.activity.message")
                error_type = _ERROR_TYPES.get(code, EngineUnavailableError)
                raise error_type(error_operation)
            if kind == "engine_error":
                raise EngineUnavailableError("model_runtime.activity")
        except ApplicationError:
            raise
        except (ValidationError, ValueError, TypeError, KeyError):
            raise EngineContractError("model_runtime.activity.message") from None
        raise EngineContractError("model_runtime.activity.message")

    @staticmethod
    def _terminate(process: BaseProcess) -> None:
        process.terminate()
        process.join(timeout=_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_TERMINATE_GRACE_SECONDS)


class _BaseModelJobHandler:
    def __init__(
        self,
        coordinator: ModelRuntimeCoordinator,
        executor: ModelActivityDeadlineExecutor | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._executor = executor or ProcessModelActivityDeadlineExecutor()

    def _run(
        self,
        operation: ModelOperation,
        request: ModelRequest,
        timeout_seconds: float,
    ) -> ModelResult:
        try:
            return self._executor.run(
                self._coordinator,
                operation,
                request,
                timeout_seconds=timeout_seconds,
            )
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("model_runtime.activity") from None


class HostedGenerationJobHandler(_BaseModelJobHandler):
    """Pure activity callable; a return value does not claim durable workflow completion."""

    def __call__(self, request: HostedGenerationRequest) -> HostedGenerationResult:
        self._coordinator.policy.validate_hosted(request)
        return cast(
            HostedGenerationResult,
            self._run("hosted_generation", request, request.activity_timeout_seconds),
        )


class LocalGenerationJobHandler(_BaseModelJobHandler):
    def __call__(self, request: LocalGenerationRequest) -> LocalGenerationResult:
        self._coordinator.policy.validate_local(request)
        return cast(
            LocalGenerationResult,
            self._run("local_generation", request, request.activity_timeout_seconds),
        )


class LanguageModelAnalysisJobHandler(_BaseModelJobHandler):
    def __call__(
        self,
        request: LanguageModelAnalysisRequest,
    ) -> LanguageModelAnalysisResult:
        self._coordinator.policy.validate_analysis(request)
        return cast(
            LanguageModelAnalysisResult,
            self._run("language_model_analysis", request, request.activity_timeout_seconds),
        )


def _dispatch(
    coordinator: ModelRuntimeCoordinator,
    operation: ModelOperation,
    request: ModelRequest,
) -> ModelResult:
    if operation == "hosted_generation" and isinstance(request, HostedGenerationRequest):
        return coordinator.run_hosted(request)
    if operation == "local_generation" and isinstance(request, LocalGenerationRequest):
        return coordinator.run_local(request)
    if operation == "language_model_analysis" and isinstance(
        request,
        LanguageModelAnalysisRequest,
    ):
        return coordinator.analyze(request)
    raise EngineContractError("model_runtime.activity.operation")


def _validate_result(operation: ModelOperation, payload: object) -> ModelResult:
    if operation == "hosted_generation":
        return HostedGenerationResult.model_validate(payload)
    if operation == "local_generation":
        return LocalGenerationResult.model_validate(payload)
    return LanguageModelAnalysisResult.model_validate(payload)


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
    "HostedGenerationJobHandler",
    "LanguageModelAnalysisJobHandler",
    "LocalGenerationJobHandler",
    "ModelActivityDeadlineExecutor",
    "ProcessModelActivityDeadlineExecutor",
]
