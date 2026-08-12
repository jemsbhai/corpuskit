"""Killable process and bounded IPC regression tests."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from corpuskit.domain.jobs import RunKind
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.process_runner import (
    MAX_PROCESS_REQUEST_BYTES,
    ProcessExecutionRunner,
    _decode_process_message,
    _decode_response,
    _request_bytes,
)
from corpuskit.workflows.progress import (
    MAX_DURABLE_PROGRESS_MESSAGES,
    MAX_DURABLE_PROGRESS_TOTAL,
    MAX_PROCESS_PROGRESS_BYTES,
    DurableRunProgress,
    RunProgressPhase,
)


@dataclass(frozen=True, slots=True)
class LateWriteHandler:
    marker: Path
    delay_seconds: float
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        time.sleep(self.delay_seconds)
        self.marker.write_text("late side effect", encoding="utf-8")
        return {"artifact_count": 1}


@dataclass(frozen=True, slots=True)
class ResultHandler:
    result: dict[str, Any]
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        return self.result


@dataclass(frozen=True, slots=True)
class ErrorHandler:
    code: str
    retryable: bool
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        raise RunExecutionError(self.code, retryable=self.retryable)


@dataclass(frozen=True, slots=True)
class FatalHandler:
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        raise KeyboardInterrupt


@dataclass(frozen=True, slots=True)
class UnpickleableHandler:
    callback: Callable[[], None]
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        self.callback()
        return {"artifact_count": 1}


@dataclass(frozen=True, slots=True)
class ParentEnvironmentProbeHandler:
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        names = (
            "CORPUSKIT_ADOPTION_DATABASE_URL",
            "CORPUSKIT_DATABASE_URL",
            "CORPUSKIT_METRICS_BEARER_TOKEN",
            "CORPUSKIT_TEMPORAL_API_KEY",
        )
        return {"inherited_parent_secrets": sum(name in os.environ for name in names)}


@dataclass(frozen=True, slots=True)
class ProgressThenResultHandler:
    delay_seconds: float = 0.2
    marker: Path | None = None
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        del spec
        raise AssertionError("progress-aware dispatch must use execute_with_progress")

    def execute_with_progress(
        self,
        spec: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None],
    ) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        emit(
            DurableRunProgress(
                sequence=0,
                phase=RunProgressPhase.GENERATING,
                completed=1,
                total=2,
            )
        )
        time.sleep(self.delay_seconds)
        if self.marker is not None:
            self.marker.write_text("late side effect", encoding="utf-8")
        return {"artifact_count": 1}


@dataclass(frozen=True, slots=True)
class InvalidProgressHandler:
    duplicate: bool = False
    kind: RunKind = RunKind.EXPORT

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        del spec
        raise AssertionError("progress-aware dispatch must use execute_with_progress")

    def execute_with_progress(
        self,
        spec: Mapping[str, Any],
        emit: Callable[[DurableRunProgress], None],
    ) -> dict[str, Any]:
        assert spec == {"artifact_ref": "opaque-id"}
        progress = DurableRunProgress(sequence=0, phase=RunProgressPhase.GENERATING)
        emit(progress)
        if self.duplicate:
            emit(progress)
        else:
            emit(
                cast(
                    DurableRunProgress,
                    {
                        "sequence": 1,
                        "phase": "generating",
                        "api_key": "super-secret-progress-canary",
                    },
                )
            )
        return {"artifact_count": 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancellation_terminates_blocking_child_and_prevents_late_side_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist.txt"
    runner = ProcessExecutionRunner(
        HandlerRegistry((LateWriteHandler(marker, 1.0),)),
        hard_timeout_seconds=5,
    )

    async def cancel() -> None:
        raise RunExecutionError("run_cancelled", retryable=False)

    with pytest.raises(RunExecutionError, match="run_cancelled"):
        await runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=cancel,
            tick_seconds=0.05,
        )

    assert runner.active_pids == frozenset()
    await asyncio.sleep(1.1)
    assert not marker.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_child_result_uses_json_contract_and_hard_timeout_is_enforced(
    tmp_path: Path,
) -> None:
    async def tick() -> None:
        return None

    successful = ProcessExecutionRunner(
        HandlerRegistry((ResultHandler({"artifact_count": 1}),)),
        hard_timeout_seconds=10,
    )
    assert await successful.execute(
        RunKind.EXPORT,
        {"artifact_ref": "opaque-id"},
        tick=tick,
        tick_seconds=0.05,
    ) == {"artifact_count": 1}

    marker = tmp_path / "timeout-must-not-write.txt"
    timed = ProcessExecutionRunner(
        HandlerRegistry((LateWriteHandler(marker, 1.0),)),
        hard_timeout_seconds=5,
    )
    with pytest.raises(RunExecutionError) as error:
        await timed.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
            timeout_seconds=0.05,
        )
    assert error.value.code == "execution_timeout"
    assert error.value.retryable is False
    assert timed.active_pids == frozenset()
    await asyncio.sleep(1.1)
    assert not marker.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_progress_crosses_ipc_before_child_completion() -> None:
    seen: list[DurableRunProgress] = []
    progress_visible = asyncio.Event()
    runner = ProcessExecutionRunner(
        HandlerRegistry((ProgressThenResultHandler(),)),
        hard_timeout_seconds=5,
    )

    async def tick() -> None:
        return None

    async def on_progress(progress: DurableRunProgress) -> None:
        seen.append(progress)
        progress_visible.set()

    execution = asyncio.create_task(
        runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
            on_progress=on_progress,
        )
    )
    await asyncio.wait_for(progress_visible.wait(), timeout=2)
    assert not execution.done()
    assert seen == [
        DurableRunProgress(
            sequence=0,
            phase=RunProgressPhase.GENERATING,
            completed=1,
            total=2,
        )
    ]
    assert await execution == {"artifact_count": 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_progress_triggered_cancellation_kills_child_before_late_write(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "progress-cancel-must-not-write.txt"
    runner = ProcessExecutionRunner(
        HandlerRegistry((ProgressThenResultHandler(delay_seconds=1.0, marker=marker),)),
        hard_timeout_seconds=5,
    )

    async def tick() -> None:
        return None

    async def cancel_on_progress(_: DurableRunProgress) -> None:
        raise RunExecutionError("run_cancelled", retryable=False)

    with pytest.raises(RunExecutionError) as stopped:
        await runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=1,
            on_progress=cancel_on_progress,
        )
    assert stopped.value.code == "run_cancelled"
    await asyncio.sleep(1.1)
    assert not marker.exists()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_malformed_secret_bearing_and_nonmonotonic_progress_fail_closed(
    duplicate: bool,
) -> None:
    runner = ProcessExecutionRunner(
        HandlerRegistry((InvalidProgressHandler(duplicate=duplicate),)),
        hard_timeout_seconds=5,
    )
    seen: list[DurableRunProgress] = []

    async def tick() -> None:
        return None

    async def on_progress(progress: DurableRunProgress) -> None:
        seen.append(progress)

    with pytest.raises(RunExecutionError) as stopped:
        await runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
            on_progress=on_progress,
        )
    assert stopped.value.code == "invalid_progress"
    assert "super-secret-progress-canary" not in repr(stopped.value)
    assert [item.sequence for item in seen] == [0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_child_process_does_not_inherit_parent_database_or_control_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "CORPUSKIT_ADOPTION_DATABASE_URL",
        "CORPUSKIT_DATABASE_URL",
        "CORPUSKIT_METRICS_BEARER_TOKEN",
        "CORPUSKIT_TEMPORAL_API_KEY",
    )
    for name in names:
        monkeypatch.setenv(name, f"{name.lower()}-canary")
    runner = ProcessExecutionRunner(
        HandlerRegistry((ParentEnvironmentProbeHandler(),)),
        hard_timeout_seconds=10,
    )

    async def tick() -> None:
        return None

    result = await runner.execute(
        RunKind.EXPORT,
        {"artifact_ref": "opaque-id"},
        tick=tick,
        tick_seconds=0.05,
    )

    assert result == {"inherited_parent_secrets": 0}
    assert all(name in os.environ for name in names)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_child_errors_are_retry_classified_and_unsafe_codes_are_sanitized() -> None:
    async def tick() -> None:
        return None

    retryable = ProcessExecutionRunner(
        HandlerRegistry((ErrorHandler("engine_unavailable", True),)),
        hard_timeout_seconds=10,
    )
    with pytest.raises(RunExecutionError) as known:
        await retryable.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
        )
    assert (known.value.code, known.value.retryable) == ("engine_unavailable", True)

    unsafe = ProcessExecutionRunner(
        HandlerRegistry((ErrorHandler("SECRET provider detail", False),)),
        hard_timeout_seconds=10,
    )
    with pytest.raises(RunExecutionError) as sanitized:
        await unsafe.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
        )
    assert (sanitized.value.code, sanitized.value.retryable) == ("internal_error", False)

    fatal = ProcessExecutionRunner(
        HandlerRegistry((FatalHandler(),)),
        hard_timeout_seconds=10,
    )
    with pytest.raises(RunExecutionError) as internal:
        await fatal.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
        )
    assert (internal.value.code, internal.value.retryable) == ("internal_error", True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_start_failure_is_sanitized_and_closes_all_handles() -> None:
    def local_callback() -> None:
        return None

    runner = ProcessExecutionRunner(
        HandlerRegistry((UnpickleableHandler(local_callback),)),
        hard_timeout_seconds=2,
    )

    async def tick() -> None:
        return None

    with pytest.raises(RunExecutionError) as failure:
        await runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.05,
        )
    assert (failure.value.code, failure.value.retryable) == ("process_start_failed", True)
    assert runner.active_pids == frozenset()


@pytest.mark.parametrize("invalid_timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_process_runner_rejects_invalid_hard_timeouts(invalid_timeout: float) -> None:
    with pytest.raises(ValueError, match="hard_timeout_seconds"):
        ProcessExecutionRunner(HandlerRegistry(()), hard_timeout_seconds=invalid_timeout)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_tick", [0.0, float("nan")])
async def test_process_runner_rejects_invalid_tick_intervals(invalid_tick: float) -> None:
    runner = ProcessExecutionRunner(HandlerRegistry(()), hard_timeout_seconds=1)

    async def tick() -> None:
        return None

    with pytest.raises(ValueError, match="tick_seconds"):
        await runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=invalid_tick,
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_timeout", [0.0, float("nan")])
async def test_process_runner_rejects_invalid_per_run_deadlines(invalid_timeout: float) -> None:
    runner = ProcessExecutionRunner(HandlerRegistry(()), hard_timeout_seconds=1)

    async def tick() -> None:
        return None

    with pytest.raises(ValueError, match="timeout_seconds"):
        await runner.execute(
            RunKind.EXPORT,
            {"artifact_ref": "opaque-id"},
            tick=tick,
            tick_seconds=0.1,
            timeout_seconds=invalid_timeout,
        )


def test_process_request_contract_rejects_non_json_and_oversized_specs() -> None:
    with pytest.raises(RunExecutionError) as non_json:
        _request_bytes(RunKind.EXPORT, {"value": object()})
    assert non_json.value.code == "invalid_run_spec"

    with pytest.raises(RunExecutionError) as oversized:
        _request_bytes(RunKind.EXPORT, {"value": "x" * MAX_PROCESS_REQUEST_BYTES})
    assert oversized.value.code == "invalid_run_spec"


@pytest.mark.parametrize(
    "response",
    [
        b"[]",
        b'{"code":"safe_code","retryable":"yes","status":"error"}',
        b'{"status":"ok","summary":[]}',
        b'{"status":"unknown"}',
        b'{"status":"ok","summary":{"value":NaN}}',
        b'{"status":"ok","summary":{"nested":{"organization_id":"forged"}}}',
        b"not-json",
    ],
)
def test_process_response_contract_rejects_malformed_or_unsafe_results(response: bytes) -> None:
    with pytest.raises(RunExecutionError) as error:
        _decode_response(response)
    assert error.value.code == "worker_process_contract"
    assert error.value.retryable is False


def test_oversized_progress_envelope_is_rejected_before_payload_parsing() -> None:
    response = (
        b'{"status":"progress","progress":{"sequence":0,"phase":"generating",'
        b'"unexpected":"' + b"x" * MAX_PROCESS_PROGRESS_BYTES + b'"}}'
    )
    with pytest.raises(RunExecutionError) as error:
        _decode_process_message(response)
    assert error.value.code == "invalid_progress"
    assert error.value.retryable is False


def test_progress_contract_enforces_exact_count_and_sequence_bounds() -> None:
    assert DurableRunProgress(
        sequence=MAX_DURABLE_PROGRESS_MESSAGES - 1,
        phase=RunProgressPhase.TRAINING,
        completed=MAX_DURABLE_PROGRESS_TOTAL,
        total=MAX_DURABLE_PROGRESS_TOTAL,
        accepted_count=MAX_DURABLE_PROGRESS_TOTAL,
        coverage=1.0,
    )
    for invalid in (
        {"sequence": MAX_DURABLE_PROGRESS_MESSAGES, "phase": "training"},
        {
            "sequence": 0,
            "phase": "training",
            "completed": MAX_DURABLE_PROGRESS_TOTAL + 1,
            "total": MAX_DURABLE_PROGRESS_TOTAL + 1,
        },
        {"sequence": 0, "phase": "training", "completed": 1},
        {"sequence": 0, "phase": "training", "completed": 2, "total": 1},
        {"sequence": 0, "phase": "training", "coverage": float("nan")},
    ):
        with pytest.raises(ValidationError):
            DurableRunProgress.model_validate(invalid)
