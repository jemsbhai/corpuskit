from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

import corpuskit.worker.phon_rl_registry as registry_module
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError
from corpuskit.domain.jobs import RunKind, canonical_spec_sha256
from corpuskit.domain.phon_rl import (
    PhonRlCheckpointBundle,
    PhonRlCheckpointCompatibility,
    PhonRlCheckpointFile,
    PhonRlDynamicPromptSource,
    PhonRlProgressPoint,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlStaticPromptSource,
    PhonRlTrainingManifest,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
    PhonRlWorkerProfile,
)
from corpuskit.services.phon_rl import (
    PhonRlAuthorizedPromptReader,
    PhonRlRuntimePolicy,
    PhonRlTrainingCoordinator,
)
from corpuskit.worker.phon_rl_registry import (
    TrainPhonRlDurableHandler,
    build_phon_rl_handler_registry,
    build_phon_rl_handlers,
    phon_rl_activity_timeout_seconds,
)
from corpuskit.workflows.deadlines import activity_deadline_seconds
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.progress import DurableRunProgress, RunProgressPhase
from corpuskit.workflows.trusted_inputs import TrustedPromptInput, TrustedRunInputs

PIN = PhonRlSnapshotPin(
    repository_id="acme/tiny-rl",
    revision="a" * 40,
    snapshot_sha256="b" * 64,
)


def request() -> PhonRlTrainingRequest:
    return PhonRlTrainingRequest(
        runtime_id="tiny-rl-v1",
        target_phonemes=("a", "b"),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(
            seed=7,
            num_steps=1,
            batch_size=1,
            activity_timeout_seconds=12.5,
        ),
    )


def policy_entry() -> PhonRlRuntimePolicyEntry:
    return PhonRlRuntimePolicyEntry(
        runtime_id="tiny-rl-v1",
        model=PIN,
        tokenizer=PIN,
        cache_root_id="models-ro",
        cache_mount_read_only=True,
        allowed_prompt_strategies=("missing-units-v1",),
    )


def result(training_request: PhonRlTrainingRequest | None = None) -> PhonRlTrainingResult:
    content = b"weights"
    checkpoint_file = PhonRlCheckpointFile(
        path="model.safetensors",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_base64=base64.b64encode(content).decode(),
    )
    compatibility = PhonRlCheckpointCompatibility(
        base_model_id=PIN.repository_id,
        base_model_revision=PIN.revision,
        base_model_snapshot_sha256=PIN.snapshot_sha256,
        tokenizer_id=PIN.repository_id,
        tokenizer_revision=PIN.revision,
        tokenizer_snapshot_sha256=PIN.snapshot_sha256,
        corpusgen_version="0.1.7",
        torch_version="2.13.0",
        transformers_version="5.15.0",
        peft_adapter=False,
    )
    training_request = training_request or request()
    manifest = PhonRlTrainingManifest(
        runtime_id=training_request.runtime_id,
        model=PIN,
        tokenizer=PIN,
        language=training_request.language,
        unit=training_request.unit,
        target_sha256="c" * 64,
        prompt_source_kind=training_request.prompt_source.kind,
        prompt_source_sha256="d" * 64,
        parameters=training_request.parameters,
        corpusgen_version="0.1.7",
        torch_version="2.13.0",
        transformers_version="5.15.0",
    )
    return PhonRlTrainingResult(
        manifest=manifest,
        progress=(PhonRlProgressPoint(step=0, mean_reward=0.5, policy_loss=-0.1),),
        mean_rewards=(0.5,),
        total_steps=1,
        final_coverage=0.5,
        checkpoint=PhonRlCheckpointBundle.create(
            compatibility=compatibility,
            files=(checkpoint_file,),
        ),
        peft_inference_status="not_requested",
    )


class RecordingEngine:
    def __init__(self, value: PhonRlTrainingResult) -> None:
        self.value = value
        self.calls: list[tuple[PhonRlTrainingRequest, PhonRlRuntimePolicyEntry]] = []

    def train(
        self,
        training_request: PhonRlTrainingRequest,
        policy: PhonRlRuntimePolicyEntry,
        *,
        emit: Callable[[PhonRlProgressPoint], None] | None = None,
        prompt_reader: PhonRlAuthorizedPromptReader | None = None,
    ) -> PhonRlTrainingResult:
        self.calls.append((training_request, policy))
        if isinstance(training_request.prompt_source, PhonRlStaticPromptSource):
            assert prompt_reader is not None
            assert prompt_reader.read(training_request.prompt_source) == ("parent authorized",)
        if emit is not None:
            for point in self.value.progress:
                emit(point)
        return self.value


class RecordingStager:
    def __init__(self) -> None:
        self.calls: list[tuple[RunKind, bytes, str]] = []
        self.fail = False
        self.reference: str | None = None

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        self.calls.append((kind, payload, content_sha256))
        if self.fail:
            raise OSError("C:/secret/path")
        return self.reference or f"staged-artifact://sha256/{content_sha256}"


def coordinator() -> tuple[PhonRlTrainingCoordinator, RecordingEngine]:
    engine = RecordingEngine(result())
    runtime_policy = PhonRlRuntimePolicy(
        (policy_entry(),),
        worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
    )
    return PhonRlTrainingCoordinator(runtime_policy, engine), engine


def test_handler_returns_exact_canonical_staged_artifact_claim() -> None:
    value, engine = coordinator()
    stager = RecordingStager()
    handler = TrainPhonRlDurableHandler(value, stager)
    summary = handler.execute(request().model_dump(mode="json"))
    assert summary == {
        "contract": "corpuskit.staged-artifact-result.v1",
        "staged_artifact_ref": f"staged-artifact://sha256/{stager.calls[0][2]}",
        "schema_id": "corpuskit.phon-rl-training-result.v1",
        "artifact_type": "run-result",
        "media_type": "application/json",
        "size_bytes": len(stager.calls[0][1]),
    }
    assert stager.calls[0][0] is RunKind.TRAIN_PHON_RL
    assert hashlib.sha256(stager.calls[0][1]).hexdigest() == stager.calls[0][2]
    assert stager.calls[0][1] == json.dumps(
        result().model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert b"organization_id" not in stager.calls[0][1]
    assert b"project_id" not in stager.calls[0][1]
    assert b"run_id" not in stager.calls[0][1]
    assert len(engine.calls) == 1


def test_handler_emits_bounded_sanitized_training_progress() -> None:
    value, _ = coordinator()
    events: list[DurableRunProgress] = []

    TrainPhonRlDurableHandler(value, RecordingStager()).execute_with_progress(
        request().model_dump(mode="json"),
        events.append,
    )

    assert [event.sequence for event in events] == [0, 1, 2, 3]
    assert [event.phase for event in events] == [
        RunProgressPhase.PREPARING_TRAINING,
        RunProgressPhase.TRAINING,
        RunProgressPhase.STAGING_RESULT,
        RunProgressPhase.FINISHED,
    ]
    assert events[1].completed == events[1].total == 1
    serialized = json.dumps([event.model_dump(mode="json") for event in events])
    assert "mean_reward" not in serialized
    assert "policy_loss" not in serialized
    assert "prompt" not in serialized


def test_static_prompts_execute_only_from_matching_parent_authorized_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id = uuid4()
    prompt_sha256 = "c" * 64
    training_request = request().model_copy(
        update={
            "prompt_source": PhonRlStaticPromptSource(
                artifact_id=artifact_id,
                content_sha256=prompt_sha256,
                prompt_count=1,
            )
        }
    )
    spec = training_request.model_dump(mode="json")
    trusted = TrustedRunInputs(
        token="1" * 64,
        run_binding_sha256="2" * 64,
        spec_sha256=canonical_spec_sha256(spec),
        run_kind=RunKind.TRAIN_PHON_RL,
        prompt=TrustedPromptInput(
            artifact_id=artifact_id,
            content_sha256=prompt_sha256,
            prompt_count=1,
        ),
    )
    engine = RecordingEngine(result(training_request))
    runtime = PhonRlTrainingCoordinator(
        PhonRlRuntimePolicy(
            (policy_entry().model_copy(update={"allow_static_prompts": True}),),
            worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
        ),
        engine,
    )
    monkeypatch.setattr(registry_module, "claim_trusted_input", lambda *_, **__: tmp_path)
    monkeypatch.setattr(
        registry_module,
        "read_materialized_prompts",
        lambda *_: ("parent authorized",),
    )
    events: list[DurableRunProgress] = []

    summary = TrainPhonRlDurableHandler(
        runtime,
        RecordingStager(),
        tmp_path,
    ).execute_with_trusted_inputs(
        spec,
        trusted.model_dump(mode="json"),
        events.append,
    )

    assert summary["schema_id"] == "corpuskit.phon-rl-training-result.v1"
    assert [event.phase for event in events] == [
        RunProgressPhase.PREPARING_TRAINING,
        RunProgressPhase.TRAINING,
        RunProgressPhase.STAGING_RESULT,
        RunProgressPhase.FINISHED,
    ]
    assert len(engine.calls) == 1


def test_handler_staging_failure_reference_and_size_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, _ = coordinator()
    stager = RecordingStager()
    stager.fail = True
    with pytest.raises(EngineUnavailableError) as write:
        TrainPhonRlDurableHandler(value, stager).execute(request().model_dump(mode="json"))
    assert write.value.operation == "phon_rl.staging.write"
    assert "secret" not in str(write.value)

    stager.fail = False
    stager.reference = "staged://invalid"
    with pytest.raises(EngineContractError) as invalid_reference:
        TrainPhonRlDurableHandler(value, stager).execute(request().model_dump(mode="json"))
    assert invalid_reference.value.operation == "phon_rl.staging.reference"

    stager.reference = "staged-artifact://sha256/" + ("0" * 64)
    with pytest.raises(EngineContractError) as reference:
        TrainPhonRlDurableHandler(value, stager).execute(request().model_dump(mode="json"))
    assert reference.value.operation == "phon_rl.staging.reference"

    monkeypatch.setattr(registry_module, "MAX_RL_RESULT_BYTES", 1)
    with pytest.raises(EngineContractError) as size:
        TrainPhonRlDurableHandler(value, RecordingStager()).execute(
            request().model_dump(mode="json")
        )
    assert size.value.operation == "phon_rl.staging.size"


def test_registry_is_exactly_profile_gated_and_has_no_nested_runner() -> None:
    value, _ = coordinator()
    stager = RecordingStager()
    handlers = build_phon_rl_handlers("gpu-training", value, stager)
    registry = build_phon_rl_handler_registry("gpu-training", value, stager)
    assert len(handlers) == 1
    assert handlers[0].kind is RunKind.TRAIN_PHON_RL
    assert registry.kinds == frozenset({RunKind.TRAIN_PHON_RL})
    with pytest.raises(RuntimeError, match="does not permit"):
        build_phon_rl_handlers("gpu-inference", value, stager)

    object.__setattr__(value.policy, "_worker_profile", "invalid")
    with pytest.raises(RuntimeError, match="does not match"):
        build_phon_rl_handlers("gpu-training", value, stager)

    source = Path(registry_module.__file__).read_text(encoding="utf-8")
    assert "ProcessExecutionRunner(" not in source
    assert "Thread(" not in source
    assert "Process(" not in source


def test_registry_normalizes_invalid_specs_and_rejects_duplicates() -> None:
    value, _ = coordinator()
    handler = TrainPhonRlDurableHandler(value, RecordingStager())
    registry = HandlerRegistry((handler,))
    with pytest.raises(RunExecutionError) as invalid:
        registry.execute(RunKind.TRAIN_PHON_RL, {"runtime_id": "invalid"})
    assert invalid.value.code == "invalid_run_spec"
    assert invalid.value.retryable is False
    with pytest.raises(ValueError, match="duplicate"):
        HandlerRegistry((handler, handler))


def test_activity_deadline_uses_authoritative_typed_request() -> None:
    spec = request().model_dump(mode="json")
    assert RunKind("train-phon-rl") is RunKind.TRAIN_PHON_RL
    assert phon_rl_activity_timeout_seconds(RunKind.TRAIN_PHON_RL, spec) == 12.5
    assert activity_deadline_seconds(RunKind.TRAIN_PHON_RL, spec) == 12.5
    assert (
        activity_deadline_seconds(
            RunKind.TRAIN_PHON_RL,
            spec,
            server_cap_seconds=10,
        )
        == 10
    )
    with pytest.raises(RuntimeError, match="no Phon-RL"):
        phon_rl_activity_timeout_seconds(RunKind.EVALUATE, spec)
    spec["parameters"]["activity_timeout_seconds"] = float("inf")
    with pytest.raises(ValueError, match="finite number"):
        phon_rl_activity_timeout_seconds(RunKind.TRAIN_PHON_RL, spec)
