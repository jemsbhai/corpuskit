"""Tenant authority and one-use materialization acceptance for Phon-RL inputs."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind
from corpuskit.domain.errors import EngineContractError, ResourceNotFoundError
from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    ImmutableModelPin,
    LocalGenerationRequest,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelQuantization,
    PhonRlAdapterSelection,
)
from corpuskit.domain.phon_rl import (
    PhonRlCheckpointBundle,
    PhonRlCheckpointCompatibility,
    PhonRlCheckpointFile,
    PhonRlDynamicPromptSource,
    PhonRlProgressPoint,
    PhonRlPromptArtifact,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlStaticPromptSource,
    PhonRlTrainingManifest,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
    prompt_source_sha256,
    target_sha256,
)
from corpuskit.persistence.artifact_store import InMemoryObjectStore
from corpuskit.persistence.database import Database
from corpuskit.services.artifacts import ArtifactActor, ArtifactService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.worker.phon_rl_registry import TrainPhonRlDurableHandler
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.handlers import RunExecutionError
from corpuskit.workflows.store import DurableRunStore, ExecutionRecord
from corpuskit.workflows.trusted_inputs import (
    TrustedRunInputMaterializer,
    claim_trusted_input,
    read_materialized_peft_manifest,
    read_materialized_prompts,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REVISION = "a" * 40
_MODEL_DIGEST = "b" * 64
_MODEL_ID = "acme/tiny-rl"


def _settings(tmp_path: Path) -> Settings:
    pin = ImmutableModelPin(model=_MODEL_ID, revision=_REVISION)
    rl_pin = PhonRlSnapshotPin(
        repository_id=_MODEL_ID,
        revision=_REVISION,
        snapshot_sha256=_MODEL_DIGEST,
    )
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / f'trusted-{uuid4()}.db').as_posix()}",
        artifact_max_bytes=100 * 1024 * 1024,
        artifact_download_chunk_bytes=16 * 1024,
        worker_local_model_policies=(
            LocalModelPolicy(
                pin=pin,
                artifact_sha256=_MODEL_DIGEST,
                allowed_devices=(ModelDevice.CPU,),
                allowed_quantizations=(ModelQuantization.NONE,),
                allow_phon_rl_adapters=True,
            ),
        ),
        worker_phon_rl_runtime_policies=(
            PhonRlRuntimePolicyEntry(
                runtime_id="tiny-rl-v1",
                model=rl_pin,
                tokenizer=rl_pin,
                cache_root_id="models-ro",
                cache_mount_read_only=True,
                allow_static_prompts=True,
                allow_peft=True,
                allowed_peft_ranks=(2,),
                allowed_peft_alphas=(4,),
                allowed_prompt_strategies=("missing-units-v1",),
            ),
        ),
        _env_file=None,
    )


async def _stack(
    tmp_path: Path,
) -> tuple[
    Settings,
    Database,
    InMemoryObjectStore,
    ArtifactService,
    JobControlPlane,
    JobActor,
]:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    objects = InMemoryObjectStore()
    artifacts = ArtifactService(database, objects, settings)
    actor = JobActor(
        subject=DEMO_PRINCIPAL.subject,
        organization_id=DEMO_PRINCIPAL.organization_id,
    )
    jobs = JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings))
    await jobs.bootstrap_demo(actor, environment="test")
    return settings, database, objects, artifacts, jobs, actor


def _artifact_actor(actor: JobActor) -> ArtifactActor:
    return ArtifactActor(
        subject=actor.subject,
        organization_id=actor.organization_id,
        request_id="trusted-input-test",
    )


def _reference(actor: JobActor, run_id: UUID, spec_sha256: str) -> RunWorkflowReference:
    return RunWorkflowReference(
        organization_id=str(actor.organization_id),
        run_id=str(run_id),
        spec_sha256=spec_sha256,
    )


async def test_static_prompts_are_parent_authorized_one_use_and_never_enter_run_history(
    tmp_path: Path,
) -> None:
    settings, database, objects, artifacts, jobs, actor = await _stack(tmp_path)
    prompt_canary = "confidential prompt canary: cover /p/"
    prompt_artifact = PhonRlPromptArtifact(prompts=(prompt_canary, "second safe prompt"))
    content = prompt_artifact.canonical_bytes()
    try:
        created = await artifacts.create(
            _artifact_actor(actor),
            project_id=DEMO_PROJECT_ID,
            run_id=None,
            kind=ArtifactKind.PROMPT_SET,
            content=content,
            expected_sha256=prompt_artifact.sha256,
            media_type="application/json",
            filename="training-prompts.json",
        )
        request = PhonRlTrainingRequest(
            runtime_id="tiny-rl-v1",
            target_phonemes=("p",),
            prompt_source=PhonRlStaticPromptSource(
                artifact_id=created.artifact.id,
                content_sha256=prompt_artifact.sha256,
                prompt_count=2,
            ),
            parameters=PhonRlTrainingParameters(
                seed=7,
                num_steps=1,
                batch_size=1,
                activity_timeout_seconds=30,
            ),
        )
        submitted = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.TRAIN_PHON_RL,
                spec=request.model_dump(mode="json"),
            ),
            idempotency_key=f"static-prompts-{uuid4()}",
        )
        persisted = await jobs.get(actor, submitted.run.id)
        events = await jobs.events(actor, submitted.run.id)
        public_bytes = json.dumps(
            {
                "spec": persisted.spec,
                "result": persisted.result_summary,
                "events": [event.payload for event in events],
            },
            default=str,
            sort_keys=True,
        )
        assert prompt_canary not in public_bytes

        record = await DurableRunStore(database).execution_record(
            _reference(actor, submitted.run.id, submitted.run.spec_sha256)
        )
        root = tmp_path / "trusted-prompts"
        materializer = TrustedRunInputMaterializer(
            database,
            objects,
            root=root,
            local_policies=settings.worker_local_model_policies,
            chunk_bytes=settings.artifact_download_chunk_bytes,
        )
        directory: Path | None = None
        async with materializer.materialize(record) as trusted:
            assert trusted is not None
            assert trusted.prompt is not None
            handler = TrainPhonRlDurableHandler(
                cast(Any, object()),
                cast(Any, object()),
                root,
            )
            with pytest.raises(EngineContractError) as mismatched_spec:
                handler.execute_with_trusted_inputs(
                    {**record.spec, "language": "fr"},
                    trusted.model_dump(mode="json"),
                    None,
                )
            assert mismatched_spec.value.operation == "phon_rl.prompt_artifact.claim"
            directory = claim_trusted_input(
                trusted,
                root=root,
                expected_kind=RunKind.TRAIN_PHON_RL,
            )
            assert read_materialized_prompts(directory, trusted.prompt) == (
                prompt_canary,
                "second safe prompt",
            )
            with pytest.raises(RunExecutionError) as replayed:
                claim_trusted_input(
                    trusted,
                    root=root,
                    expected_kind=RunKind.TRAIN_PHON_RL,
                )
            assert replayed.value.code == "trusted_input_claim"
        assert directory is not None
        assert not directory.exists()
    finally:
        await database.dispose()


async def test_static_prompt_submission_rejects_unowned_or_digest_mismatched_artifact(
    tmp_path: Path,
) -> None:
    _, database, _, _, jobs, actor = await _stack(tmp_path)
    try:
        request = PhonRlTrainingRequest(
            runtime_id="tiny-rl-v1",
            target_phonemes=("p",),
            prompt_source=PhonRlStaticPromptSource(
                artifact_id=uuid4(),
                content_sha256="f" * 64,
                prompt_count=1,
            ),
            parameters=PhonRlTrainingParameters(seed=1, num_steps=1, batch_size=1),
        )
        with pytest.raises(ResourceNotFoundError) as denied:
            await jobs.submit(
                actor,
                RunSubmission(
                    project_id=DEMO_PROJECT_ID,
                    kind=RunKind.TRAIN_PHON_RL,
                    spec=request.model_dump(mode="json"),
                ),
                idempotency_key=f"unowned-prompts-{uuid4()}",
            )
        assert denied.value.operation == "run.input_artifact"
    finally:
        await database.dispose()


async def test_peft_result_is_bound_to_successful_training_run_and_materialized_read_only(
    tmp_path: Path,
) -> None:
    settings, database, objects, artifacts, jobs, actor = await _stack(tmp_path)
    dynamic = PhonRlDynamicPromptSource(strategy_id="missing-units-v1")
    parameters = PhonRlTrainingParameters(
        seed=9,
        num_steps=1,
        batch_size=1,
        use_peft=True,
        peft_rank=2,
        peft_alpha=4,
        activity_timeout_seconds=30,
    )
    training_request = PhonRlTrainingRequest(
        runtime_id="tiny-rl-v1",
        target_phonemes=("p",),
        prompt_source=dynamic,
        parameters=parameters,
    )
    try:
        training = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.TRAIN_PHON_RL,
                spec=training_request.model_dump(mode="json"),
            ),
            idempotency_key=f"peft-train-{uuid4()}",
        )
        reference = _reference(actor, training.run.id, training.run.spec_sha256)
        runs = DurableRunStore(database)
        assert await runs.begin_execution(reference)
        payload, checkpoint_sha256 = _training_result(training_request)
        result_artifact = await artifacts.create(
            _artifact_actor(actor),
            project_id=DEMO_PROJECT_ID,
            run_id=training.run.id,
            kind=ArtifactKind.RUN_RESULT,
            content=payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            media_type="application/json",
            filename="phon-rl-training-result.json",
        )
        assert await runs.complete(reference, {"test_fixture": True})

        local_request = LocalGenerationRequest(
            selection=LocalModelSelection(
                pin=ImmutableModelPin(model=_MODEL_ID, revision=_REVISION)
            ),
            target=GenerationTarget(phonemes=("p",)),
            stopping=GenerationStoppingCriteria(
                max_sentences=1,
                max_iterations=1,
                timeout_seconds=10,
            ),
            phon_rl_adapter=PhonRlAdapterSelection(
                artifact_id=result_artifact.artifact.id,
                artifact_sha256=result_artifact.artifact.sha256,
                checkpoint_sha256=checkpoint_sha256,
            ),
            activity_timeout_seconds=30,
        )
        generation = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.GENERATE_LOCAL,
                spec=local_request.model_dump(mode="json"),
            ),
            idempotency_key=f"peft-infer-{uuid4()}",
        )
        record = await runs.execution_record(
            _reference(actor, generation.run.id, generation.run.spec_sha256)
        )
        root = tmp_path / "trusted-adapter"
        materializer = TrustedRunInputMaterializer(
            database,
            objects,
            root=root,
            local_policies=settings.worker_local_model_policies,
            chunk_bytes=settings.artifact_download_chunk_bytes,
        )
        directory: Path | None = None
        async with materializer.materialize(record) as trusted:
            assert trusted is not None
            assert trusted.peft_adapter is not None
            directory = claim_trusted_input(
                trusted,
                root=root,
                expected_kind=RunKind.GENERATE_LOCAL,
            )
            adapter_root, manifest = read_materialized_peft_manifest(
                directory,
                trusted.peft_adapter,
            )
            assert manifest.checkpoint_sha256 == checkpoint_sha256
            assert {item.name for item in adapter_root.iterdir()} == {
                "adapter_config.json",
                "adapter_model.safetensors",
            }
            assert not any(
                item.name.endswith((".bin", ".pt", ".pth")) for item in adapter_root.iterdir()
            )
        assert directory is not None
        assert not directory.exists()
    finally:
        await database.dispose()


async def test_materialization_failure_and_body_exception_always_clean_ephemeral_root(
    tmp_path: Path,
) -> None:
    settings, database, objects, artifacts, jobs, actor = await _stack(tmp_path)
    prompt = PhonRlPromptArtifact(prompts=("cleanup canary",))
    try:
        created = await artifacts.create(
            _artifact_actor(actor),
            project_id=DEMO_PROJECT_ID,
            run_id=None,
            kind=ArtifactKind.PROMPT_SET,
            content=prompt.canonical_bytes(),
            expected_sha256=prompt.sha256,
            media_type="application/json",
            filename="cleanup.json",
        )
        request = PhonRlTrainingRequest(
            runtime_id="tiny-rl-v1",
            target_phonemes=("p",),
            prompt_source=PhonRlStaticPromptSource(
                artifact_id=created.artifact.id,
                content_sha256=prompt.sha256,
                prompt_count=1,
            ),
            parameters=PhonRlTrainingParameters(
                seed=2,
                num_steps=1,
                batch_size=1,
                activity_timeout_seconds=30,
            ),
        )
        submitted = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.TRAIN_PHON_RL,
                spec=request.model_dump(mode="json"),
            ),
            idempotency_key=f"cleanup-{uuid4()}",
        )
        record = await DurableRunStore(database).execution_record(
            _reference(actor, submitted.run.id, submitted.run.spec_sha256)
        )
        root = tmp_path / "cleanup-root"
        materializer = TrustedRunInputMaterializer(
            database,
            objects,
            root=root,
            local_policies=settings.worker_local_model_policies,
            chunk_bytes=settings.artifact_download_chunk_bytes,
        )
        with pytest.raises(RuntimeError, match="simulated cancellation"):
            await _raise_inside_materialization(materializer, record)
        assert tuple(root.iterdir()) == ()

        keys = await objects.list_keys("artifacts/v1/", limit=10)
        assert len(keys) == 1
        objects.corrupt(keys[0], b"corrupt")
        with pytest.raises(RunExecutionError) as corrupt:
            async with materializer.materialize(record):
                raise AssertionError("corrupt bytes must fail before yielding")
        assert corrupt.value.code == "trusted_input_integrity"
        assert tuple(root.iterdir()) == ()
    finally:
        await database.dispose()


def _training_result(request: PhonRlTrainingRequest) -> tuple[bytes, str]:
    versions = {
        name: importlib.metadata.version(name)
        for name in ("corpusgen", "torch", "transformers", "peft")
    }
    config = json.dumps(
        {
            "auto_mapping": None,
            "base_model_name_or_path": _MODEL_ID,
            "bias": "none",
            "inference_mode": True,
            "lora_alpha": 4,
            "lora_dropout": 0.0,
            "peft_type": "LORA",
            "r": 2,
            "revision": _REVISION,
            "target_modules": ["c_attn"],
            "task_type": "CAUSAL_LM",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    weights = b"test-only-safetensors-placeholder"
    compatibility = PhonRlCheckpointCompatibility(
        base_model_id=_MODEL_ID,
        base_model_revision=_REVISION,
        base_model_snapshot_sha256=_MODEL_DIGEST,
        tokenizer_id=_MODEL_ID,
        tokenizer_revision=_REVISION,
        tokenizer_snapshot_sha256=_MODEL_DIGEST,
        corpusgen_version=versions["corpusgen"],
        torch_version=versions["torch"],
        transformers_version=versions["transformers"],
        peft_version=versions["peft"],
        peft_adapter=True,
    )
    files = tuple(
        sorted(
            (
                _checkpoint_file("adapter_config.json", config),
                _checkpoint_file("adapter_model.safetensors", weights),
            ),
            key=lambda item: item.path,
        )
    )
    checkpoint = PhonRlCheckpointBundle.create(
        compatibility=compatibility,
        files=files,
    )
    pin = PhonRlSnapshotPin(
        repository_id=_MODEL_ID,
        revision=_REVISION,
        snapshot_sha256=_MODEL_DIGEST,
    )
    result = PhonRlTrainingResult(
        manifest=PhonRlTrainingManifest(
            runtime_id=request.runtime_id,
            model=pin,
            tokenizer=pin,
            language=request.language,
            unit=request.unit,
            target_sha256=target_sha256(request.target_phonemes, request.unit),
            prompt_source_kind=request.prompt_source.kind,
            prompt_source_sha256=prompt_source_sha256(request.prompt_source),
            parameters=request.parameters,
            corpusgen_version=versions["corpusgen"],
            torch_version=versions["torch"],
            transformers_version=versions["transformers"],
            peft_version=versions["peft"],
        ),
        progress=(PhonRlProgressPoint(step=0, mean_reward=0.5, policy_loss=0.1),),
        mean_rewards=(0.5,),
        total_steps=1,
        final_coverage=0.5,
        checkpoint=checkpoint,
        peft_inference_status="application_loader_ready",
    )
    return result.model_dump_json().encode(), checkpoint.content_sha256


async def _raise_inside_materialization(
    materializer: TrustedRunInputMaterializer,
    record: ExecutionRecord,
) -> None:
    async with materializer.materialize(record) as trusted:
        assert trusted is not None
        raise RuntimeError("simulated cancellation")


def _checkpoint_file(path: str, content: bytes) -> PhonRlCheckpointFile:
    return PhonRlCheckpointFile(
        path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_base64=base64.b64encode(content).decode(),
    )
