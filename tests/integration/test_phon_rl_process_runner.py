"""Outer-process cancellation acceptance for the production Phon-RL handler boundary."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from corpuskit.domain.jobs import RunKind
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlProgressPoint,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
    PhonRlWorkerProfile,
)
from corpuskit.services.phon_rl import PhonRlRuntimePolicy, PhonRlTrainingCoordinator
from corpuskit.worker.phon_rl_registry import TrainPhonRlDurableHandler
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.process_runner import ProcessExecutionRunner

_PIN = PhonRlSnapshotPin(
    repository_id="acme/tiny-rl",
    revision="a" * 40,
    snapshot_sha256="b" * 64,
)


@dataclass(frozen=True, slots=True)
class LateTrainingEngine:
    marker: Path
    delay_seconds: float

    def train(
        self,
        request: PhonRlTrainingRequest,
        policy: PhonRlRuntimePolicyEntry,
        *,
        emit: Callable[[PhonRlProgressPoint], None] | None = None,
    ) -> PhonRlTrainingResult:
        del request, policy, emit
        time.sleep(self.delay_seconds)
        self.marker.write_text("late training side effect", encoding="utf-8")
        raise AssertionError("the cancelled child must never reach this point")


@dataclass(frozen=True, slots=True)
class LateResultStager:
    marker: Path

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        del kind, payload
        self.marker.write_text("late staged write", encoding="utf-8")
        return f"staged-artifact://sha256/{content_sha256}"


def _request() -> PhonRlTrainingRequest:
    return PhonRlTrainingRequest(
        runtime_id="tiny-rl-v1",
        target_phonemes=("a", "b"),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(
            seed=7,
            num_steps=1,
            batch_size=1,
            activity_timeout_seconds=5.0,
        ),
    )


def _policy() -> PhonRlRuntimePolicy:
    return PhonRlRuntimePolicy(
        (
            PhonRlRuntimePolicyEntry(
                runtime_id="tiny-rl-v1",
                model=_PIN,
                tokenizer=_PIN,
                cache_root_id="models-ro",
                cache_mount_read_only=True,
                allowed_prompt_strategies=("missing-units-v1",),
            ),
        ),
        worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["cancellation", "deadline"])
async def test_outer_runner_termination_kills_training_and_prevents_late_staging(
    tmp_path: Path,
    termination: str,
) -> None:
    training_marker = tmp_path / "training-must-not-write.txt"
    staging_marker = tmp_path / "staging-must-not-write.txt"
    handler = TrainPhonRlDurableHandler(
        PhonRlTrainingCoordinator(_policy(), LateTrainingEngine(training_marker, 1.0)),
        LateResultStager(staging_marker),
    )
    runner = ProcessExecutionRunner(HandlerRegistry((handler,)), hard_timeout_seconds=5.0)

    async def tick() -> None:
        if termination == "cancellation":
            raise RunExecutionError("run_cancelled", retryable=False)

    with pytest.raises(RunExecutionError) as stopped:
        await runner.execute(
            RunKind.TRAIN_PHON_RL,
            _request().model_dump(mode="json"),
            tick=tick,
            tick_seconds=0.05,
            timeout_seconds=0.05 if termination == "deadline" else None,
        )
    expected_code = "run_cancelled" if termination == "cancellation" else "execution_timeout"
    assert stopped.value.code == expected_code

    assert runner.active_pids == frozenset()
    await asyncio.sleep(1.1)
    assert not training_marker.exists()
    assert not staging_marker.exists()
