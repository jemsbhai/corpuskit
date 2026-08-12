"""Killable, bounded child-process isolation for synchronous engine handlers."""

from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from multiprocessing.reduction import ForkingPickler
from typing import Any, cast

from corpuskit.domain.jobs import (
    MAX_RESULT_SUMMARY_BYTES,
    MAX_RUN_SPEC_BYTES,
    RunKind,
    normalize_result_summary,
)
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.progress import (
    MAX_DURABLE_PROGRESS_MESSAGES,
    MAX_PROCESS_PROGRESS_BYTES,
    DurableRunProgress,
    normalize_progress,
)

MAX_PROCESS_REQUEST_BYTES = MAX_RUN_SPEC_BYTES + 8_192
MAX_PROCESS_RESPONSE_BYTES = MAX_RESULT_SUMMARY_BYTES + 1_024
PROCESS_EXECUTION_TIMEOUT_SECONDS = 14 * 60.0
PROCESS_TERMINATION_GRACE_SECONDS = 2.0
_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "created_by",
        "organization_id",
        "project_id",
        "run_id",
        "tenant_id",
        "user_id",
    }
)
_PARENT_ONLY_ENVIRONMENT = (
    "CORPUSKIT_ADOPTION_DATABASE_URL",
    "CORPUSKIT_DATABASE_URL",
    "CORPUSKIT_METRICS_BEARER_TOKEN",
    "CORPUSKIT_TEMPORAL_API_KEY",
)


class ProcessExecutionRunner:
    """Run one pure handler in a process that can be forcefully stopped."""

    def __init__(
        self,
        handlers: HandlerRegistry,
        *,
        context: SpawnContext | None = None,
        hard_timeout_seconds: float = PROCESS_EXECUTION_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(hard_timeout_seconds) or hard_timeout_seconds <= 0:
            raise ValueError("hard_timeout_seconds must be finite and positive")
        self._handlers = handlers
        self._context = context or multiprocessing.get_context("spawn")
        self._hard_timeout_seconds = hard_timeout_seconds
        self._active_pids: set[int] = set()

    @property
    def active_pids(self) -> frozenset[int]:
        """Expose only process IDs for shutdown diagnostics and termination tests."""

        return frozenset(self._active_pids)

    async def execute(
        self,
        kind: RunKind,
        spec: dict[str, Any],
        *,
        tick: Callable[[], Awaitable[None]],
        tick_seconds: float,
        timeout_seconds: float | None = None,
        on_progress: Callable[[DurableRunProgress], Awaitable[None]] | None = None,
        trusted_inputs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(tick_seconds) or tick_seconds <= 0:
            raise ValueError("tick_seconds must be finite and positive")
        resolved_timeout = (
            self._hard_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if not math.isfinite(resolved_timeout) or resolved_timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        resolved_timeout = min(resolved_timeout, self._hard_timeout_seconds)
        request = _request_bytes(kind, spec, trusted_inputs=trusted_inputs)
        _validate_spawn_contract(self._handlers)
        receive, send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_child_execute,
            args=(self._handlers, request, send),
            name=f"corpuskit-{kind.value}",
            daemon=True,
        )
        started = False
        try:
            try:
                process.start()
            except Exception:
                started = process.pid is not None
                raise RunExecutionError("process_start_failed", retryable=True) from None
            started = True
            send.close()
            if process.pid is None:
                raise RunExecutionError("process_start_failed", retryable=True)
            self._active_pids.add(process.pid)
            deadline = time.monotonic() + resolved_timeout
            next_tick = time.monotonic() + tick_seconds
            last_progress_sequence = -1
            progress_count = 0
            while True:
                if not process.is_alive() and not receive.poll(0):
                    raise RunExecutionError("worker_process_failed", retryable=True)
                now = time.monotonic()
                if now >= deadline:
                    raise RunExecutionError("execution_timeout", retryable=False)
                if now >= next_tick:
                    await tick()
                    next_tick = time.monotonic() + tick_seconds
                    continue
                if receive.poll(0):
                    try:
                        response = receive.recv_bytes(MAX_PROCESS_RESPONSE_BYTES)
                    except (EOFError, OSError):
                        raise RunExecutionError("worker_process_failed", retryable=True) from None
                    message_type, payload = _decode_process_message(response)
                    if message_type == "progress":
                        progress = cast(DurableRunProgress, payload)
                        progress_count += 1
                        if (
                            progress_count > MAX_DURABLE_PROGRESS_MESSAGES
                            or progress.sequence <= last_progress_sequence
                        ):
                            raise RunExecutionError("invalid_progress", retryable=False)
                        last_progress_sequence = progress.sequence
                        if on_progress is not None:
                            await on_progress(progress)
                        continue
                    return cast(dict[str, Any], payload)
                await asyncio.sleep(min(0.05, deadline - now, next_tick - now))
        finally:
            receive.close()
            send.close()
            if started:
                await _stop_process_safely(process)
            if process.pid is not None:
                self._active_pids.discard(process.pid)


def _request_bytes(
    kind: RunKind,
    spec: dict[str, Any],
    *,
    trusted_inputs: Mapping[str, Any] | None = None,
) -> bytes:
    try:
        request = json.dumps(
            {
                "kind": kind.value,
                "spec": spec,
                **({"trusted_inputs": dict(trusted_inputs)} if trusted_inputs is not None else {}),
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise RunExecutionError("invalid_run_spec", retryable=False) from None
    if len(request) > MAX_PROCESS_REQUEST_BYTES:
        raise RunExecutionError("invalid_run_spec", retryable=False)
    return request


def _validate_spawn_contract(handlers: HandlerRegistry) -> None:
    try:
        ForkingPickler.dumps(handlers)
    except Exception:
        raise RunExecutionError("process_start_failed", retryable=True) from None


def _child_execute(
    handlers: HandlerRegistry,
    request: bytes,
    send: Connection,
) -> None:
    """Child entry point; send only a JSON summary or a sanitized error code."""

    for name in _PARENT_ONLY_ENVIRONMENT:
        os.environ.pop(name, None)
    try:
        decoded = json.loads(request)
        if not isinstance(decoded, dict):
            raise ValueError("invalid request envelope")
        kind = RunKind(decoded["kind"])
        spec = decoded["spec"]
        if not isinstance(spec, dict):
            raise ValueError("invalid request spec")
        trusted_inputs = decoded.get("trusted_inputs")
        if trusted_inputs is not None and not isinstance(trusted_inputs, dict):
            raise ValueError("invalid trusted input envelope")
        last_progress_sequence = -1
        progress_count = 0

        def emit(progress: DurableRunProgress) -> None:
            nonlocal last_progress_sequence, progress_count
            try:
                normalized = normalize_progress(
                    cast(Mapping[str, Any] | DurableRunProgress, progress)
                )
            except ValueError:
                raise RunExecutionError("invalid_progress", retryable=False) from None
            progress_count += 1
            if (
                progress_count > MAX_DURABLE_PROGRESS_MESSAGES
                or normalized.sequence <= last_progress_sequence
            ):
                raise RunExecutionError("invalid_progress", retryable=False)
            last_progress_sequence = normalized.sequence
            encoded_progress = _encode_progress(normalized)
            try:
                send.send_bytes(encoded_progress)
            except (BrokenPipeError, EOFError, OSError):
                raise RunExecutionError("worker_process_failed", retryable=True) from None

        summary = handlers.execute(kind, spec, emit=emit, trusted_inputs=trusted_inputs)
        response: dict[str, Any] = {"status": "ok", "summary": summary}
    except RunExecutionError as exc:
        code = exc.code if _safe_code(exc.code) else "internal_error"
        retryable = exc.retryable if isinstance(exc.retryable, bool) else True
        response = {"code": code, "retryable": retryable, "status": "error"}
    except BaseException:
        response = {"code": "internal_error", "retryable": True, "status": "error"}
    try:
        encoded = json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_PROCESS_RESPONSE_BYTES:
            encoded = b'{"code":"invalid_result_summary","retryable":false,"status":"error"}'
        send.send_bytes(encoded)
    except BaseException:
        return
    finally:
        send.close()


def _decode_response(response: bytes) -> dict[str, Any]:
    """Backward-compatible final-response decoder used by contract tests."""

    message_type, payload = _decode_process_message(response)
    if message_type != "result":
        raise RunExecutionError("worker_process_contract", retryable=False)
    return cast(dict[str, Any], payload)


def _encode_progress(progress: DurableRunProgress) -> bytes:
    encoded = json.dumps(
        {"progress": progress.model_dump(mode="json"), "status": "progress"},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_PROCESS_PROGRESS_BYTES:
        raise RunExecutionError("invalid_progress", retryable=False)
    return encoded


def _decode_process_message(response: bytes) -> tuple[str, dict[str, Any] | DurableRunProgress]:
    try:
        decoded = json.loads(response)
        if not isinstance(decoded, dict):
            raise ValueError("invalid response")
        status = decoded.get("status")
        if status == "progress":
            if len(response) > MAX_PROCESS_PROGRESS_BYTES:
                raise RunExecutionError("invalid_progress", retryable=False)
            try:
                progress_value = decoded.get("progress")
                if not isinstance(progress_value, dict):
                    raise ValueError("invalid_progress")
                progress = normalize_progress(progress_value)
            except ValueError:
                raise RunExecutionError("invalid_progress", retryable=False) from None
            return "progress", progress
        if status == "error":
            code = decoded.get("code")
            retryable = decoded.get("retryable")
            if not isinstance(code, str) or not isinstance(retryable, bool):
                raise ValueError("invalid error response")
            raise RunExecutionError(code, retryable=retryable)
        if status != "ok" or not isinstance(decoded.get("summary"), dict):
            raise ValueError("invalid success response")
        normalized = normalize_result_summary(decoded["summary"])
        _reject_authority_fields(normalized)
        return "result", normalized
    except RunExecutionError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RunExecutionError("worker_process_contract", retryable=False) from None


def _stop_process(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
    process.join(PROCESS_TERMINATION_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(PROCESS_TERMINATION_GRACE_SECONDS)


async def _stop_process_safely(process: BaseProcess) -> None:
    cleanup = asyncio.create_task(asyncio.to_thread(_stop_process, process))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


def _safe_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 80
        and all(
            character.islower() or character.isdigit() or character == "_" for character in value
        )
    )


def _reject_authority_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _FORBIDDEN_AUTHORITY_KEYS:
                raise ValueError("child result contains authority")
            _reject_authority_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_authority_fields(child)


__all__ = ["PROCESS_EXECUTION_TIMEOUT_SECONDS", "ProcessExecutionRunner"]
