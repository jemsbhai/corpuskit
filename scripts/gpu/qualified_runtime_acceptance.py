"""Run real CUDA inference or one phase of the durable PEFT acceptance chain.

This command runs either inside an exact CorpusKit GPU worker image with
``--gpus all --network none`` or in the checked, isolated Windows CUDA profile.
It never downloads a model: the deterministic GPT-2-shaped fixture is generated
as safetensors inside a temporary cache. The PEFT commands fail unless trainer output
crosses the exact training-runtime to inference-runtime boundary, parent adoption,
one-use read-only materialization, safe-merge generation, second parent adoption, and
cancellation under one exact candidate revision.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import hmac
import importlib
import importlib.metadata
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from corpuskit.adapters.corpusgen.datg import (
    CorpusgenDatgAdapter,
    SnapshotLocation,
)
from corpuskit.adapters.corpusgen.model_runtime import (
    CachedLocalModelLoader,
    CorpusgenModelRuntimeAdapter,
    TransformersLocalModelLoader,
    compute_snapshot_digest,
)
from corpuskit.adapters.corpusgen.phon_rl import (
    CorpusgenPhonRlAdapter,
    CorpusgenPhonRlTrainingBindings,
    PhonRlSnapshotLocation,
    validate_checkpoint_compatibility,
)
from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind
from corpuskit.domain.datg import (
    DatgGuidedGenerationRequest,
    DatgIndexBuildRequest,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgWorkerProfile,
)
from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.model_runtime import (
    AnalysisText,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    LocalGenerationResult,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelQuantization,
    PhonRlAdapterSelection,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
    PhonRlWorkerProfile,
)
from corpuskit.persistence.artifact_store import (
    ConfiguredStagedArtifactWriter,
    ObjectStore,
    build_object_store,
)
from corpuskit.persistence.database import Database
from corpuskit.services.artifact_adoption import ArtifactAdoptionService
from corpuskit.services.artifacts import ArtifactActor, ArtifactService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.services.model_runtime import ModelRuntimeCoordinator, ModelRuntimePolicy
from corpuskit.services.phon_rl import PhonRlRuntimePolicy, PhonRlTrainingCoordinator
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.worker.model_registry import build_model_handler_registry
from corpuskit.worker.phon_rl_registry import (
    TrainPhonRlDurableHandler,
    build_phon_rl_handler_registry,
)
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.handlers import HandlerRegistry, RunExecutionError
from corpuskit.workflows.process_runner import ProcessExecutionRunner
from corpuskit.workflows.progress import DurableRunProgress
from corpuskit.workflows.store import DurableRunStore
from corpuskit.workflows.trusted_inputs import (
    TrustedRunInputMaterializer,
    read_materialized_peft_manifest,
)

EVIDENCE_SCHEMA = "corpuskit.qualified-gpu-acceptance.v3"
PEFT_CHAIN_MODE = "peft-chain"
PEFT_TRAIN_MODE = "peft-train"
PEFT_INFER_MODE = "peft-infer"
PEFT_CHAIN_CONTRACT = "cuda-peft-train-adopt-materialize-generate-v1"
PEFT_TRAIN_RECEIPT_SCHEMA = "corpuskit.qualified-gpu-peft-train-receipt.v1"
PEFT_STATE_SCHEMA = "corpuskit.qualified-gpu-peft-state.v1"
_PEFT_TRAIN_RECEIPT = "peft-train-receipt.json"
_MAX_PHASE_RECEIPT_BYTES = 32 * 1024
REVISION = "a" * 40
MODEL_ID = "corpuskit/tiny-offline-causal"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNKNOWN_TOKEN = "<unk>"  # noqa: S105 - tokenizer vocabulary sentinel, not a credential
_PAD_TOKEN = "<pad>"  # noqa: S105 - tokenizer vocabulary sentinel, not a credential
_EOS_TOKEN = "<eos>"  # noqa: S105 - tokenizer vocabulary sentinel, not a credential
_TORCH_WHEEL_URL = (
    "https://download-r2.pytorch.org/whl/cu132/torch-2.13.0%2Bcu132-cp312-cp312-win_amd64.whl"
)
_TORCH_WHEEL_SHA256 = "0bcf7ae00b2e20ef2b53af2e764a4fd8646b913bfaaeba2b9c975e672e8c7902"
_TORCH_WHEEL_RECORD_SHA256 = "f8b0f86cacb13585da12fec801316550b82f45863b80117de148593c9f02d8d1"
_TORCH_RECORD_CANONICAL_SHA256 = "bcca40a4130fe52ab0acdbdd96498217d6acb7f3a948455fd4172df401ca7907"
_UV_TORCH_RECORD_ADDITIONS = frozenset(
    {
        "../../Scripts/torchfrtrace.exe",
        "../../Scripts/torchrun.exe",
        "torch-2.13.0+cu132.dist-info/INSTALLER",
        "torch-2.13.0+cu132.dist-info/REQUESTED",
        "torch-2.13.0+cu132.dist-info/direct_url.json",
    }
)


@dataclass(frozen=True, slots=True)
class TinyRuntime:
    root: Path
    snapshot: Path
    digest: str
    pin: ImmutableModelPin


@dataclass(frozen=True, slots=True)
class PinnedSnapshotResolver:
    snapshot: Path
    pin: ImmutableModelPin

    def __call__(self, requested: ImmutableModelPin) -> Path:
        if requested != self.pin:
            raise RuntimeError("unexpected model pin")
        return self.snapshot


@dataclass(frozen=True, slots=True)
class StaticDatgResolver:
    snapshot: Path
    root: Path

    def resolve(self, pin: DatgSnapshotPin) -> SnapshotLocation:
        del pin
        return SnapshotLocation(snapshot=self.snapshot, approved_cache_root=self.root)


@dataclass(frozen=True, slots=True)
class StaticPhonRlResolver:
    snapshot: Path
    root: Path

    def resolve(
        self,
        pin: PhonRlSnapshotPin,
        *,
        cache_root_id: str,
    ) -> PhonRlSnapshotLocation:
        del pin
        if cache_root_id != "models-ro":
            raise RuntimeError("unexpected cache root")
        return PhonRlSnapshotLocation(snapshot=self.snapshot, approved_cache_root=self.root)


@dataclass(frozen=True, slots=True)
class ForbiddenResultStager:
    marker: Path

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        del kind, payload
        self.marker.write_text("late staging is forbidden", encoding="utf-8")
        return f"staged-artifact://sha256/{content_sha256}"


def _dependencies() -> tuple[Any, Any, Any]:
    torch = importlib.import_module("torch")
    tokenizers = importlib.import_module("tokenizers")
    transformers = importlib.import_module("transformers")
    return torch, tokenizers, transformers


def create_tiny_runtime(root: Path) -> TinyRuntime:
    """Create deterministic local tokenizer/config/safetensors without a network call."""

    torch, tokenizers, transformers = _dependencies()
    snapshot = root / "models--corpuskit--tiny-offline-causal" / "snapshots" / REVISION
    snapshot.mkdir(parents=True)
    vocabulary = {
        _PAD_TOKEN: 0,
        _EOS_TOKEN: 1,
        _UNKNOWN_TOKEN: 2,
        "pea": 3,
        "p": 4,
        ".": 5,
        "Write": 6,
        "one": 7,
        "short": 8,
        "natural": 9,
        "sentence": 10,
        "containing": 11,
        "these": 12,
        "sounds": 13,
        "A": 14,
        "fluent": 15,
        "second": 16,
    }
    backend = tokenizers.Tokenizer(
        tokenizers.models.WordLevel(vocab=vocabulary, unk_token=_UNKNOWN_TOKEN)
    )
    backend.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tokenizer = transformers.PreTrainedTokenizerFast(
        tokenizer_object=backend,
        unk_token=_UNKNOWN_TOKEN,
        pad_token=_PAD_TOKEN,
        eos_token=_EOS_TOKEN,
        bos_token=_EOS_TOKEN,
    )
    tokenizer.save_pretrained(snapshot)
    config = transformers.GPT2Config(
        vocab_size=len(vocabulary),
        n_positions=64,
        n_ctx=64,
        n_embd=16,
        n_layer=1,
        n_head=1,
        bos_token_id=1,
        eos_token_id=1,
        pad_token_id=0,
        tie_word_embeddings=False,
    )
    model = transformers.GPT2LMHeadModel(config)
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.zero_()
        model.transformer.ln_f.bias.fill_(1.0)
        model.lm_head.weight[3].fill_(1.0)
    model.save_pretrained(snapshot, safe_serialization=True)
    if tuple(snapshot.glob("*.bin")) or not tuple(snapshot.glob("*.safetensors")):
        raise RuntimeError("tiny model did not serialize exclusively as safetensors")
    digest = compute_snapshot_digest(snapshot, approved_cache_root=root)
    return TinyRuntime(
        root=root,
        snapshot=snapshot,
        digest=digest,
        pin=ImmutableModelPin(model=MODEL_ID, revision=REVISION),
    )


def cuda_evidence(torch: Any, runtime: TinyRuntime) -> dict[str, object]:
    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError("qualified acceptance requires an available CUDA device")
    tensor = torch.arange(1, 5, dtype=torch.float32, device="cuda")
    squared_sum = float((tensor * tensor).sum().item())
    if tensor.device.type != "cuda" or squared_sum != 30.0:
        raise RuntimeError("CUDA tensor arithmetic proof failed")
    properties = torch.cuda.get_device_properties(0)
    return {
        "device_name": torch.cuda.get_device_name(0),
        "device_count": torch.cuda.device_count(),
        "compute_capability": list(torch.cuda.get_device_capability(0)),
        "total_memory_bytes": int(properties.total_memory),
        "driver_version": _nvidia_driver_version(),
        "torch_version": importlib.metadata.version("torch"),
        "torch_cuda_runtime": str(torch.version.cuda),
        "cudnn_version": torch.backends.cudnn.version(),
        "actual_cuda_tensor_device": str(tensor.device),
        "actual_cuda_tensor_squared_sum": squared_sum,
        "model_snapshot_sha256": runtime.digest,
    }


def _nvidia_driver_version() -> str:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        raise RuntimeError("nvidia-smi is unavailable inside the qualified runtime")
    result = subprocess.run(  # noqa: S603 - executable is resolved, argv is constant
        [executable, "--query-gpu=driver_version", "--format=csv,noheader"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    version = result.stdout.strip().splitlines()[0]
    if re.fullmatch(r"[0-9]{3,4}\.[0-9]{1,3}", version) is None:
        raise RuntimeError("NVIDIA driver version had an unexpected format")
    return version


def run_inference(runtime: TinyRuntime, torch: Any, transformers: Any) -> dict[str, object]:
    loader = CachedLocalModelLoader(
        TransformersLocalModelLoader(
            PinnedSnapshotResolver(runtime.snapshot, runtime.pin),
            approved_cache_root=runtime.root,
        ),
        max_entries=1,
    )
    policy = LocalModelPolicy(
        pin=runtime.pin,
        artifact_sha256=runtime.digest,
        allowed_devices=(ModelDevice.CUDA,),
        allowed_quantizations=(ModelQuantization.NONE,),
    )
    selection = LocalModelSelection(pin=runtime.pin, device=ModelDevice.CUDA)
    bundle = loader.load(
        runtime.pin,
        device=ModelDevice.CUDA,
        quantization=ModelQuantization.NONE,
        artifact_sha256=runtime.digest,
    )
    model = cast(Any, bundle.model)
    parameter_device = str(next(model.parameters()).device)
    if not parameter_device.startswith("cuda"):
        raise RuntimeError("local model parameters were not placed on CUDA")
    adapter = CorpusgenModelRuntimeAdapter(model_loader=loader)
    generated = adapter.run_local(
        LocalGenerationRequest(
            selection=selection,
            target=GenerationTarget(phonemes=("p",)),
            stopping=GenerationStoppingCriteria(
                target_coverage=1.0,
                max_sentences=1,
                max_iterations=1,
                timeout_seconds=30.0,
            ),
            candidates_per_iteration=1,
            max_new_tokens=3,
            do_sample=False,
            seed=1729,
            activity_timeout_seconds=60.0,
        ),
        policy,
        WorkerModelProfile.LOCAL_GPU,
    )
    if generated.coverage != 1.0 or not generated.accepted:
        raise RuntimeError("real CUDA local generation did not cover the target")
    analysis = adapter.analyze_language_model(
        LanguageModelAnalysisRequest(
            selection=selection,
            texts=(
                AnalysisText(source_id="one", text="A fluent sentence."),
                AnalysisText(source_id="two", text="A second fluent sentence."),
            ),
            batch_size=2,
            max_length=32,
            activity_timeout_seconds=60.0,
        ),
        policy,
        WorkerModelProfile.LOCAL_GPU,
    )
    perplexities = tuple(analysis.perplexity.per_sentence)
    if (
        not analysis.shared_model_instance
        or len(perplexities) != 2
        or not all(math.isfinite(value) and value > 0 for value in perplexities)
    ):
        raise RuntimeError("real CUDA shared-model perplexity contract failed")
    loader.clear()
    torch.cuda.empty_cache()

    datg_pin = DatgSnapshotPin(
        repository_id=MODEL_ID,
        revision=REVISION,
        snapshot_sha256=runtime.digest,
    )
    datg_policy = DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=datg_pin,
        tokenizer=datg_pin,
        allowed_quantizations=(DatgQuantization.NONE,),
    )
    datg = CorpusgenDatgAdapter(
        snapshot_resolver=StaticDatgResolver(runtime.snapshot, runtime.root)
    )
    index = datg.build_index(
        DatgIndexBuildRequest(
            runtime_id="tiny-datg",
            batch_size=8,
            max_vocabulary_size=64,
            activity_timeout_seconds=60.0,
        ),
        datg_policy,
    ).artifact
    if not any(item.unit == "p" for item in index.unit_to_tokens):
        raise RuntimeError("real DATG index did not map the target phoneme")
    guided = datg.generate(
        DatgGuidedGenerationRequest(
            runtime_id="tiny-datg",
            index_cache_key_sha256=index.identity.cache_key_sha256,
            target_phonemes=("p",),
            target_units=("p",),
            quantization=DatgQuantization.NONE,
            candidates=1,
            max_new_tokens=3,
            do_sample=False,
            seed=1729,
            activity_timeout_seconds=60.0,
        ),
        datg_policy,
        DatgWorkerProfile.LOCAL_GPU,
        index,
    )
    if not guided.candidates or not guided.attribute_token_ids:
        raise RuntimeError("real CUDA DATG generation did not apply target guidance")
    quantized_generation = {
        quantization.value: _run_quantized_generation(runtime, torch, quantization)
        for quantization in (ModelQuantization.FOUR_BIT, ModelQuantization.EIGHT_BIT)
    }
    validate_quantized_generation_evidence(quantized_generation)
    torch.cuda.synchronize()
    return {
        "model_parameter_device": parameter_device,
        "generated_text_sha256": hashlib.sha256(generated.accepted[0].text.encode()).hexdigest(),
        "generation_coverage": generated.coverage,
        "perplexity_count": len(perplexities),
        "perplexity_all_finite": True,
        "shared_model_instance": analysis.shared_model_instance,
        "datg_index_sha256": index.content_sha256,
        "datg_indexed_token_count": index.indexed_token_count,
        "datg_attribute_token_count": len(guided.attribute_token_ids),
        "datg_candidate_count": len(guided.candidates),
        "quantized_generation": quantized_generation,
        "transformers_version": importlib.metadata.version("transformers"),
        "safetensors_version": importlib.metadata.version("safetensors"),
        "tokenizer_class": type(
            transformers.AutoTokenizer.from_pretrained(
                runtime.snapshot,
                local_files_only=True,
                trust_remote_code=False,
            )
        ).__name__,
    }


def _run_quantized_generation(
    runtime: TinyRuntime,
    torch: Any,
    quantization: ModelQuantization,
) -> dict[str, object]:
    if quantization not in {ModelQuantization.FOUR_BIT, ModelQuantization.EIGHT_BIT}:
        raise RuntimeError("qualified quantized generation received an unsupported mode")
    loader = CachedLocalModelLoader(
        TransformersLocalModelLoader(
            PinnedSnapshotResolver(runtime.snapshot, runtime.pin),
            approved_cache_root=runtime.root,
        ),
        max_entries=1,
    )
    policy = LocalModelPolicy(
        pin=runtime.pin,
        artifact_sha256=runtime.digest,
        allowed_devices=(ModelDevice.CUDA,),
        allowed_quantizations=(quantization,),
    )
    selection = LocalModelSelection(
        pin=runtime.pin,
        device=ModelDevice.CUDA,
        quantization=quantization,
    )
    bundle = loader.load(
        runtime.pin,
        device=ModelDevice.CUDA,
        quantization=quantization,
        artifact_sha256=runtime.digest,
    )
    model = cast(Any, bundle.model)
    parameter_devices = sorted({str(parameter.device) for parameter in model.parameters()})
    quantized_module_types = sorted(
        {
            type(module).__name__
            for module in model.modules()
            if type(module).__name__ in {"Linear4bit", "Linear8bitLt"}
        }
    )
    adapter = CorpusgenModelRuntimeAdapter(model_loader=loader)
    generated = adapter.run_local(
        LocalGenerationRequest(
            selection=selection,
            target=GenerationTarget(phonemes=("p",)),
            stopping=GenerationStoppingCriteria(
                target_coverage=1.0,
                max_sentences=1,
                max_iterations=1,
                timeout_seconds=30.0,
            ),
            candidates_per_iteration=1,
            max_new_tokens=3,
            do_sample=False,
            seed=1729,
            activity_timeout_seconds=60.0,
        ),
        policy,
        WorkerModelProfile.LOCAL_GPU,
    )
    if generated.coverage != 1.0 or not generated.accepted:
        raise RuntimeError("qualified quantized generation did not cover the target")
    evidence: dict[str, object] = {
        "mode": quantization.value,
        "manifest_quantization": generated.model.quantization.value,
        "parameter_devices": parameter_devices,
        "quantized_module_types": quantized_module_types,
        "loaded_in_4bit": bool(getattr(model, "is_loaded_in_4bit", False)),
        "loaded_in_8bit": bool(getattr(model, "is_loaded_in_8bit", False)),
        "accepted_count": len(generated.accepted),
        "generation_coverage": generated.coverage,
        "generated_text_sha256": hashlib.sha256(
            generated.accepted[0].text.encode("utf-8")
        ).hexdigest(),
        "bitsandbytes_version": importlib.metadata.version("bitsandbytes"),
    }
    loader.clear()
    del adapter, bundle, model
    torch.cuda.empty_cache()
    return evidence


def validate_quantized_generation_evidence(
    evidence: Mapping[str, object],
) -> None:
    """Reject a baseline artifact unless both real bitsandbytes modes ran on CUDA."""

    if set(evidence) != {ModelQuantization.FOUR_BIT.value, ModelQuantization.EIGHT_BIT.value}:
        raise RuntimeError("qualified inference must attest both quantization modes")
    expected_modules = {
        ModelQuantization.FOUR_BIT.value: "Linear4bit",
        ModelQuantization.EIGHT_BIT.value: "Linear8bitLt",
    }
    for mode, expected_module in expected_modules.items():
        result_value = evidence.get(mode)
        if not isinstance(result_value, Mapping):
            raise RuntimeError(f"qualified {mode} generation evidence is incomplete")
        result = cast(Mapping[str, object], result_value)
        devices = result.get("parameter_devices")
        modules = result.get("quantized_module_types")
        accepted_count = result.get("accepted_count")
        coverage = result.get("generation_coverage")
        expected_four_bit = mode == ModelQuantization.FOUR_BIT.value
        expected_eight_bit = mode == ModelQuantization.EIGHT_BIT.value
        digest = result.get("generated_text_sha256")
        if (
            result.get("mode") != mode
            or result.get("manifest_quantization") != mode
            or not isinstance(devices, list)
            or not devices
            or any(
                not isinstance(device, str) or not device.startswith("cuda") for device in devices
            )
            or modules != [expected_module]
            or result.get("loaded_in_4bit") is not expected_four_bit
            or result.get("loaded_in_8bit") is not expected_eight_bit
            or isinstance(accepted_count, bool)
            or accepted_count != 1
            or isinstance(coverage, bool)
            or coverage != 1.0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or result.get("bitsandbytes_version") != importlib.metadata.version("bitsandbytes")
        ):
            raise RuntimeError(f"qualified {mode} generation evidence is incomplete")


def _rl_contract(
    runtime: TinyRuntime,
) -> tuple[
    PhonRlRuntimePolicyEntry,
    PhonRlRuntimePolicy,
    PhonRlTrainingRequest,
    CorpusgenPhonRlAdapter,
]:
    pin = PhonRlSnapshotPin(
        repository_id=MODEL_ID,
        revision=REVISION,
        snapshot_sha256=runtime.digest,
    )
    entry = PhonRlRuntimePolicyEntry(
        runtime_id="tiny-rl",
        model=pin,
        tokenizer=pin,
        cache_root_id="models-ro",
        cache_mount_read_only=True,
        allow_peft=True,
        allowed_peft_ranks=(2,),
        allowed_peft_alphas=(4,),
        allowed_prompt_strategies=("missing-units-v1",),
    )
    policy = PhonRlRuntimePolicy((entry,), worker_profile=PhonRlWorkerProfile.LOCAL_GPU)
    request = PhonRlTrainingRequest(
        runtime_id="tiny-rl",
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(
            num_steps=2,
            batch_size=1,
            max_new_tokens=2,
            temperature=0.8,
            seed=1729,
            use_peft=True,
            peft_rank=2,
            peft_alpha=4,
            activity_timeout_seconds=120.0,
        ),
    )
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticPhonRlResolver(runtime.snapshot, runtime.root),
        training_bindings=CorpusgenPhonRlTrainingBindings(_device="cuda"),
    )
    return entry, policy, request, adapter


@dataclass(frozen=True, slots=True)
class DurableAcceptanceStack:
    settings: Settings
    database: Database
    objects: ObjectStore
    artifacts: ArtifactService
    runs: DurableRunStore
    adopter: ArtifactAdoptionService
    stager: ConfiguredStagedArtifactWriter
    jobs: JobControlPlane
    actor: JobActor
    artifact_actor: ArtifactActor
    local_policy: LocalModelPolicy


def _local_policy(runtime: TinyRuntime) -> LocalModelPolicy:
    return LocalModelPolicy(
        pin=runtime.pin,
        artifact_sha256=runtime.digest,
        allowed_devices=(ModelDevice.CUDA,),
        allowed_quantizations=(ModelQuantization.NONE,),
        allow_phon_rl_adapters=True,
    )


async def _open_durable_stack(
    runtime: TinyRuntime,
    state_root: Path,
    entry: PhonRlRuntimePolicyEntry,
    *,
    worker_profile: Literal["gpu-training", "gpu-inference"],
    create_schema: bool,
) -> DurableAcceptanceStack:
    local_policy = _local_policy(runtime)
    settings = Settings(
        environment="test",
        runtime_role="worker",
        temporal_task_queue=worker_profile,
        worker_profile=worker_profile,
        database_url=f"sqlite+aiosqlite:///{(state_root / 'acceptance.db').as_posix()}",
        artifact_root=(state_root / "artifacts").absolute(),
        artifact_max_bytes=100 * 1024 * 1024,
        artifact_download_chunk_bytes=16 * 1024,
        worker_local_model_policies=(local_policy,),
        worker_model_cache_root=runtime.root,
        worker_model_cache_mount_read_only=True,
        worker_phon_rl_runtime_policies=(entry,),
        worker_phon_rl_cache_roots={entry.cache_root_id: runtime.root},
        _env_file=None,
    )
    database = Database(settings.database_url)
    if create_schema:
        await database.create_schema()
    elif not (state_root / "acceptance.db").is_file():
        raise RuntimeError("qualified PEFT state is missing its durable database")
    objects = build_object_store(settings)
    runs = DurableRunStore(database)
    actor = JobActor(
        subject="qualified-gpu-user",
        organization_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    return DurableAcceptanceStack(
        settings=settings,
        database=database,
        objects=objects,
        artifacts=ArtifactService(database, objects, settings),
        runs=runs,
        adopter=ArtifactAdoptionService(runs, objects, settings),
        stager=ConfiguredStagedArtifactWriter.from_settings(settings),
        jobs=JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings)),
        actor=actor,
        artifact_actor=ArtifactActor(
            subject=actor.subject,
            organization_id=actor.organization_id,
            request_id="qualified-gpu-peft-chain",
        ),
        local_policy=local_policy,
    )


def _inspect_training_result(
    payload: bytes,
    entry: PhonRlRuntimePolicyEntry,
) -> tuple[PhonRlTrainingResult, dict[str, Any], int]:
    result = PhonRlTrainingResult.model_validate_json(payload, strict=True)
    if result.total_steps != 2 or tuple(point.step for point in result.progress) != (0, 1):
        raise RuntimeError("real CUDA Phon-RL did not complete exactly two PPO steps")
    validate_checkpoint_compatibility(result.checkpoint, entry, require_peft=True)
    if result.peft_inference_status != "application_loader_ready":
        raise RuntimeError("qualified Phon-RL trainer did not produce a usable PEFT adapter")
    checkpoint_files = {item.path: item for item in result.checkpoint.files}
    if set(checkpoint_files) != {"adapter_config.json", "adapter_model.safetensors"}:
        raise RuntimeError("qualified Phon-RL PEFT checkpoint layout is not exact")
    weights = base64.b64decode(
        checkpoint_files["adapter_model.safetensors"].content_base64,
        validate=True,
    )
    adapter_tensors = importlib.import_module("safetensors.torch").load(weights)
    if not adapter_tensors or not all("lora_" in name for name in adapter_tensors):
        raise RuntimeError("qualified Phon-RL checkpoint lacks real LoRA tensors")
    return result, checkpoint_files, len(adapter_tensors)


async def run_peft_train_phase(
    runtime: TinyRuntime,
    torch: Any,
    state_root: Path,
    execution: Mapping[str, object],
    cuda: Mapping[str, object],
) -> dict[str, object]:
    entry, policy, request, adapter = _rl_contract(runtime)
    if (
        not request.parameters.use_peft
        or not entry.allow_peft
        or request.parameters.peft_rank not in entry.allowed_peft_ranks
        or request.parameters.peft_alpha not in entry.allowed_peft_alphas
    ):
        raise RuntimeError("qualified Phon-RL acceptance requires exact PEFT policy")
    stack = await _open_durable_stack(
        runtime,
        state_root,
        entry,
        worker_profile="gpu-training",
        create_schema=True,
    )
    coordinator = PhonRlTrainingCoordinator(policy, adapter)
    progress_steps: list[int] = []
    receipt_payload: dict[str, object] | None = None

    async def tick() -> None:
        return None

    try:
        await stack.jobs.bootstrap_demo(stack.actor, environment="test")
        training = await stack.jobs.submit(
            stack.actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.TRAIN_PHON_RL,
                spec=request.model_dump(mode="json"),
            ),
            idempotency_key=f"qualified-peft-training-{uuid4()}",
        )
        reference = _run_reference(stack.actor, training.run.id, training.run.spec_sha256)
        await stack.runs.prepare(reference)
        if not await stack.runs.begin_execution(reference):
            raise RuntimeError("qualified PEFT training did not enter running state")

        async def capture_progress(progress: DurableRunProgress) -> None:
            if not await stack.runs.record_progress(reference, progress, activity_attempt=1):
                raise RuntimeError("qualified PEFT training progress was not durable")
            if progress.completed is not None:
                progress_steps.append(progress.completed)

        runner = ProcessExecutionRunner(
            build_phon_rl_handler_registry("gpu-training", coordinator, stack.stager),
            hard_timeout_seconds=150.0,
        )
        summary = await runner.execute(
            RunKind.TRAIN_PHON_RL,
            request.model_dump(mode="json"),
            tick=tick,
            tick_seconds=0.10,
            timeout_seconds=120.0,
            on_progress=capture_progress,
        )
        commit = await stack.adopter.adopt(reference, summary)
        if (
            commit.state is not RunState.SUCCEEDED
            or commit.artifact_id is None
            or runner.active_pids
        ):
            raise RuntimeError("qualified PEFT training result was not parent-adopted")
        training_run = await stack.jobs.get(stack.actor, training.run.id)
        if training_run.state is not RunState.SUCCEEDED or training_run.result_summary is None:
            raise RuntimeError("qualified PEFT training did not commit durable success")
        artifact = await stack.artifacts.get(
            stack.artifact_actor,
            project_id=DEMO_PROJECT_ID,
            artifact_id=commit.artifact_id,
        )
        payload = await _download_artifact(stack.artifacts, stack.artifact_actor, artifact.id)
        if (
            artifact.kind is not ArtifactKind.RUN_RESULT
            or artifact.run_id != training.run.id
            or hashlib.sha256(payload).hexdigest() != artifact.sha256
            or UUID(str(training_run.result_summary.get("artifact_id"))) != artifact.id
            or training_run.result_summary.get("sha256") != artifact.sha256
        ):
            raise RuntimeError("qualified PEFT training artifact integrity failed")
        result, checkpoint_files, tensor_count = _inspect_training_result(payload, entry)

        marker = state_root / "cancelled-run-must-not-stage.json"
        cancellation_runner = ProcessExecutionRunner(
            HandlerRegistry(
                (TrainPhonRlDurableHandler(coordinator, ForbiddenResultStager(marker)),)
            ),
            hard_timeout_seconds=30.0,
        )

        async def cancel() -> None:
            raise RunExecutionError("run_cancelled", retryable=False)

        cancellation_code: str | None = None
        try:
            await cancellation_runner.execute(
                RunKind.TRAIN_PHON_RL,
                request.model_dump(mode="json"),
                tick=cancel,
                tick_seconds=0.10,
            )
        except RunExecutionError as error:
            cancellation_code = error.code
        if (
            cancellation_code != "run_cancelled"
            or cancellation_runner.active_pids
            or marker.exists()
        ):
            raise RuntimeError("Phon-RL cancellation did not stop cleanly before staging")
        await asyncio.sleep(0.25)
        if marker.exists():
            raise RuntimeError("cancelled Phon-RL child produced a late staged result")

        training_events = await stack.jobs.events(stack.actor, training.run.id)
        durable_history = json.dumps(
            {
                "spec": training_run.spec,
                "summary": training_run.result_summary,
                "events": [event.payload for event in training_events],
            },
            allow_nan=False,
            sort_keys=True,
        )
        if (
            "Write one short, natural sentence containing these sounds: p." in durable_history
            or checkpoint_files["adapter_model.safetensors"].content_base64 in durable_history
        ):
            raise RuntimeError("qualified PEFT training history exposed child payloads")
        training_facts: dict[str, object] = {
            "contract": PEFT_CHAIN_CONTRACT,
            "peft_requested": True,
            "training_device": "cuda",
            "training_handler_profile": "gpu-training",
            "ppo_steps": result.total_steps,
            "progress_steps": [point.step for point in result.progress],
            "durable_progress_completed": sorted(set(progress_steps)),
            "checkpoint_sha256": result.checkpoint.content_sha256,
            "checkpoint_bytes": result.checkpoint.total_size_bytes,
            "checkpoint_files": sorted(checkpoint_files),
            "checkpoint_compatibility": result.checkpoint.compatibility.model_dump(mode="json"),
            "checkpoint_safetensors_files": sum(
                item.path.endswith(".safetensors") for item in result.checkpoint.files
            ),
            "adapter_tensor_count": tensor_count,
            "adapter_tensors_are_lora": True,
            "peft_compatibility_validated": True,
            "training_run_state": training_run.state.value,
            "training_result_adopted": True,
            "training_artifact_id": str(artifact.id),
            "training_artifact_sha256": artifact.sha256,
            "training_artifact_integrity": True,
            "training_history_sensitive_payload_absent": True,
            "cancellation_code": cancellation_code,
            "active_child_pids_after_cancellation": len(cancellation_runner.active_pids),
            "late_staging_prevented": not marker.exists(),
        }
        receipt_payload = {
            "schema_version": PEFT_TRAIN_RECEIPT_SCHEMA,
            "recorded_at": datetime.now(UTC).isoformat(),
            "phase": PEFT_TRAIN_MODE,
            "execution": dict(execution),
            "cuda": dict(cuda),
            "training": training_facts,
            "state": {
                "schema_id": PEFT_STATE_SCHEMA,
                "organization_id": str(stack.actor.organization_id),
                "project_id": str(DEMO_PROJECT_ID),
                "training_run_id": str(training.run.id),
                "training_spec_sha256": training.run.spec_sha256,
                "training_artifact_id": str(artifact.id),
                "training_artifact_sha256": artifact.sha256,
                "checkpoint_sha256": result.checkpoint.content_sha256,
                "model_snapshot_sha256": runtime.digest,
            },
        }
        validate_peft_training_receipt(receipt_payload)
        torch.cuda.empty_cache()
    finally:
        await stack.database.dispose()
    if receipt_payload is None:
        raise RuntimeError("qualified PEFT training did not produce a phase receipt")
    return _write_training_receipt(state_root, receipt_payload)


async def run_peft_infer_phase(
    runtime: TinyRuntime,
    torch: Any,
    state_root: Path,
    execution: Mapping[str, object],
    cuda: Mapping[str, object],
) -> dict[str, object]:
    receipt, receipt_sha256 = _read_training_receipt(state_root)
    training_execution = _evidence_mapping(receipt, "execution")
    if training_execution.get("runtime_kind") == "container-image":
        expected_digest = os.environ.get("CORPUSKIT_ACCEPTANCE_TRAINING_IMAGE_DIGEST", "")
        if (
            _SHA256.fullmatch(expected_digest) is None
            or training_execution.get("image_digest") != expected_digest
        ):
            raise RuntimeError("qualified PEFT inference did not receive the exact training image")
    training_cuda = _evidence_mapping(receipt, "cuda")
    if training_cuda.get("model_snapshot_sha256") != runtime.digest:
        raise RuntimeError("qualified PEFT inference model does not match the training phase")
    entry, _, _, _ = _rl_contract(runtime)
    stack = await _open_durable_stack(
        runtime,
        state_root,
        entry,
        worker_profile="gpu-inference",
        create_schema=False,
    )
    receipt_training = _evidence_mapping(receipt, "training")
    receipt_state = _evidence_mapping(receipt, "state")

    async def tick() -> None:
        return None

    try:
        training_run_id = _canonical_uuid(receipt_state.get("training_run_id"), "training run")
        training_artifact_id = _canonical_uuid(
            receipt_state.get("training_artifact_id"),
            "training artifact",
        )
        training_run = await stack.jobs.get(stack.actor, training_run_id)
        artifact = await stack.artifacts.get(
            stack.artifact_actor,
            project_id=DEMO_PROJECT_ID,
            artifact_id=training_artifact_id,
        )
        payload = await _download_artifact(stack.artifacts, stack.artifact_actor, artifact.id)
        if (
            training_run.state is not RunState.SUCCEEDED
            or training_run.result_summary is None
            or training_run.spec_sha256 != receipt_state.get("training_spec_sha256")
            or artifact.kind is not ArtifactKind.RUN_RESULT
            or artifact.run_id != training_run.id
            or artifact.sha256 != receipt_state.get("training_artifact_sha256")
            or hashlib.sha256(payload).hexdigest() != artifact.sha256
            or UUID(str(training_run.result_summary.get("artifact_id"))) != artifact.id
            or training_run.result_summary.get("sha256") != artifact.sha256
        ):
            raise RuntimeError("qualified PEFT inference rejected persisted training state")
        result, checkpoint_files, tensor_count = _inspect_training_result(payload, entry)
        if (
            result.checkpoint.content_sha256 != receipt_state.get("checkpoint_sha256")
            or result.checkpoint.content_sha256 != receipt_training.get("checkpoint_sha256")
            or result.checkpoint.total_size_bytes != receipt_training.get("checkpoint_bytes")
            or tensor_count != receipt_training.get("adapter_tensor_count")
            or result.checkpoint.compatibility.model_dump(mode="json")
            != receipt_training.get("checkpoint_compatibility")
        ):
            raise RuntimeError("qualified PEFT inference rejected the training phase receipt")

        generation_request = LocalGenerationRequest(
            selection=LocalModelSelection(pin=runtime.pin, device=ModelDevice.CUDA),
            target=GenerationTarget(phonemes=("p",)),
            stopping=GenerationStoppingCriteria(
                target_coverage=1.0,
                max_sentences=1,
                max_iterations=1,
                timeout_seconds=30.0,
            ),
            candidates_per_iteration=1,
            max_new_tokens=3,
            do_sample=False,
            seed=1729,
            phon_rl_adapter=PhonRlAdapterSelection(
                artifact_id=artifact.id,
                artifact_sha256=artifact.sha256,
                checkpoint_sha256=result.checkpoint.content_sha256,
            ),
            activity_timeout_seconds=60.0,
        )
        generation = await stack.jobs.submit(
            stack.actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.GENERATE_LOCAL,
                spec=generation_request.model_dump(mode="json"),
            ),
            idempotency_key=f"qualified-peft-generation-{uuid4()}",
        )
        reference = _run_reference(stack.actor, generation.run.id, generation.run.spec_sha256)
        await stack.runs.prepare(reference)
        if not await stack.runs.begin_execution(reference):
            raise RuntimeError("qualified PEFT generation did not enter running state")
        record = await stack.runs.execution_record(reference)
        trusted_root = (state_root / "trusted-inputs").absolute()
        materializer = TrustedRunInputMaterializer(
            stack.database,
            stack.objects,
            root=trusted_root,
            local_policies=(stack.local_policy,),
            chunk_bytes=stack.settings.artifact_download_chunk_bytes,
        )
        model_loader = CachedLocalModelLoader(
            TransformersLocalModelLoader(
                PinnedSnapshotResolver(runtime.snapshot, runtime.pin),
                approved_cache_root=runtime.root,
            ),
            max_entries=1,
        )
        coordinator = ModelRuntimeCoordinator(
            ModelRuntimePolicy(
                local_models=(stack.local_policy,),
                worker_profile=WorkerModelProfile.LOCAL_GPU,
            ),
            CorpusgenModelRuntimeAdapter(model_loader=model_loader),
        )
        runner = ProcessExecutionRunner(
            build_model_handler_registry(
                "gpu-inference",
                coordinator,
                stack.stager,
                trusted_input_root=trusted_root,
            ),
            hard_timeout_seconds=90.0,
        )
        materialized_directory: Path | None = None
        materialized_files: list[str] = []
        materialized_read_only = False
        async with materializer.materialize(record) as trusted:
            if trusted is None or trusted.peft_adapter is None:
                raise RuntimeError("qualified PEFT adapter was not parent-materialized")
            materialized_directory = trusted_root / trusted.token
            adapter_root, manifest = read_materialized_peft_manifest(
                materialized_directory,
                trusted.peft_adapter,
            )
            materialized_paths = (
                adapter_root,
                *(adapter_root / item.path for item in manifest.files),
            )
            materialized_files = [item.path for item in manifest.files]
            materialized_read_only = all(
                path.stat().st_mode & 0o222 == 0 for path in materialized_paths
            )
            if (
                manifest.checkpoint_sha256 != result.checkpoint.content_sha256
                or set(materialized_files) != {"adapter_config.json", "adapter_model.safetensors"}
                or not materialized_read_only
            ):
                raise RuntimeError("qualified PEFT materialization was not exact and read-only")
            summary = await runner.execute(
                RunKind.GENERATE_LOCAL,
                generation_request.model_dump(mode="json"),
                tick=tick,
                tick_seconds=0.10,
                timeout_seconds=60.0,
                trusted_inputs=trusted.model_dump(mode="json"),
            )
        materialization_cleaned = (
            materialized_directory is not None and not materialized_directory.exists()
        )
        if not materialization_cleaned or tuple(trusted_root.iterdir()) or runner.active_pids:
            raise RuntimeError("qualified PEFT materialization was not one-use and fully cleaned")
        commit = await stack.adopter.adopt(reference, summary)
        if commit.state is not RunState.SUCCEEDED or commit.artifact_id is None:
            raise RuntimeError("qualified PEFT generation result was not parent-adopted")
        generation_run = await stack.jobs.get(stack.actor, generation.run.id)
        generation_artifact = await stack.artifacts.get(
            stack.artifact_actor,
            project_id=DEMO_PROJECT_ID,
            artifact_id=commit.artifact_id,
        )
        generation_payload = await _download_artifact(
            stack.artifacts,
            stack.artifact_actor,
            generation_artifact.id,
        )
        if (
            generation_run.state is not RunState.SUCCEEDED
            or generation_run.result_summary is None
            or generation_artifact.kind is not ArtifactKind.RUN_RESULT
            or generation_artifact.run_id != generation.run.id
            or hashlib.sha256(generation_payload).hexdigest() != generation_artifact.sha256
            or UUID(str(generation_run.result_summary.get("artifact_id"))) != generation_artifact.id
            or generation_run.result_summary.get("sha256") != generation_artifact.sha256
        ):
            raise RuntimeError("qualified PEFT generation artifact integrity failed")
        generated = LocalGenerationResult.model_validate_json(generation_payload, strict=True)
        if (
            generated.model.device is not ModelDevice.CUDA
            or generated.model.guidance_strategy != "phon_rl"
            or generated.model.adapter_artifact_sha256 != artifact.sha256
            or generated.model.adapter_checkpoint_sha256 != result.checkpoint.content_sha256
            or generated.coverage != 1.0
            or not generated.accepted
        ):
            raise RuntimeError("qualified PEFT generation provenance did not close the chain")

        training_events = await stack.jobs.events(stack.actor, training_run.id)
        generation_events = await stack.jobs.events(stack.actor, generation.run.id)
        durable_history = json.dumps(
            {
                "training": {
                    "spec": training_run.spec,
                    "summary": training_run.result_summary,
                    "events": [event.payload for event in training_events],
                },
                "generation": {
                    "spec": generation_run.spec,
                    "summary": generation_run.result_summary,
                    "events": [event.payload for event in generation_events],
                },
            },
            allow_nan=False,
            sort_keys=True,
        )
        sensitive_values = (
            "Write one short, natural sentence containing these sounds: p.",
            *(candidate.text for candidate in generated.accepted),
            checkpoint_files["adapter_model.safetensors"].content_base64,
        )
        if any(value in durable_history for value in sensitive_values):
            raise RuntimeError("qualified PEFT durable history exposed sensitive child payloads")

        training_facts = dict(receipt_training)
        training_facts.update(
            {
                "training_phase_receipt_verified": True,
                "training_phase_receipt_sha256": receipt_sha256,
                "trusted_materialization_authorized": True,
                "trusted_materialization_read_only": materialized_read_only,
                "trusted_materialization_one_use": True,
                "trusted_materialization_cleaned": materialization_cleaned,
                "trusted_materialization_files": sorted(materialized_files),
                "generation_device": generated.model.device.value,
                "generation_handler_profile": "gpu-inference",
                "generation_run_state": generation_run.state.value,
                "generation_result_adopted": True,
                "generation_artifact_id": str(generation_artifact.id),
                "generation_artifact_sha256": generation_artifact.sha256,
                "generation_artifact_integrity": True,
                "generation_output_sha256": hashlib.sha256(
                    generated.accepted[0].text.encode("utf-8")
                ).hexdigest(),
                "generation_coverage": generated.coverage,
                "generation_model_id": generated.model.model,
                "generation_model_revision": generated.model.revision,
                "generation_model_snapshot_sha256": generated.model.artifact_sha256,
                "guidance_strategy": generated.model.guidance_strategy,
                "adapter_artifact_sha256": generated.model.adapter_artifact_sha256,
                "adapter_checkpoint_sha256": generated.model.adapter_checkpoint_sha256,
                "durable_history_sensitive_payload_absent": True,
            }
        )
        evidence: dict[str, object] = {
            "schema_version": EVIDENCE_SCHEMA,
            "recorded_at": datetime.now(UTC).isoformat(),
            "mode": PEFT_CHAIN_MODE,
            "phases": {
                "training": {
                    "execution": dict(training_execution),
                    "cuda": dict(training_cuda),
                },
                "inference": {
                    "execution": dict(execution),
                    "cuda": dict(cuda),
                },
            },
            "training": training_facts,
        }
        validate_peft_chain_evidence(evidence)
        torch.cuda.empty_cache()
        return evidence
    finally:
        await stack.database.dispose()


def _write_training_receipt(
    state_root: Path,
    payload: Mapping[str, object],
) -> dict[str, object]:
    unsigned = dict(payload)
    signature = hmac.new(_phase_key(), _canonical_json(unsigned), hashlib.sha256).hexdigest()
    receipt = {**unsigned, "hmac_sha256": signature}
    encoded = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_PHASE_RECEIPT_BYTES:
        raise RuntimeError("qualified PEFT training receipt exceeds its bound")
    path = state_root / _PEFT_TRAIN_RECEIPT
    temporary = state_root / f".{_PEFT_TRAIN_RECEIPT}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    path.chmod(0o400)
    return receipt


def _read_training_receipt(state_root: Path) -> tuple[dict[str, object], str]:
    path = state_root / _PEFT_TRAIN_RECEIPT
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_PHASE_RECEIPT_BYTES:
        raise RuntimeError("qualified PEFT training receipt is missing or unsafe")
    encoded = path.read_bytes()
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("qualified PEFT training receipt is invalid") from None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "recorded_at",
        "phase",
        "execution",
        "cuda",
        "training",
        "state",
        "hmac_sha256",
    }:
        raise RuntimeError("qualified PEFT training receipt has an unsupported shape")
    receipt = cast(dict[str, object], value)
    signature = receipt.pop("hmac_sha256")
    expected = hmac.new(_phase_key(), _canonical_json(receipt), hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise RuntimeError("qualified PEFT training receipt authentication failed")
    validate_peft_training_receipt(receipt)
    receipt["hmac_sha256"] = signature
    return receipt, hashlib.sha256(encoded).hexdigest()


def _phase_key() -> bytes:
    value = os.environ.get("CORPUSKIT_ACCEPTANCE_PHASE_KEY", "")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RuntimeError("qualified PEFT phases require an ephemeral 256-bit handoff key")
    return bytes.fromhex(value)


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_uuid(value: object, label: str) -> UUID:
    try:
        parsed = UUID(cast(str, value))
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError(f"qualified PEFT receipt has an invalid {label} ID") from None
    if str(parsed) != value:
        raise RuntimeError(f"qualified PEFT receipt has a non-canonical {label} ID")
    return parsed


def _run_reference(actor: JobActor, run_id: UUID, spec_sha256: str) -> RunWorkflowReference:
    return RunWorkflowReference(
        organization_id=str(actor.organization_id),
        run_id=str(run_id),
        spec_sha256=spec_sha256,
    )


async def _download_artifact(
    artifacts: ArtifactService,
    actor: ArtifactActor,
    artifact_id: UUID,
) -> bytes:
    download = await artifacts.download(
        actor,
        project_id=DEMO_PROJECT_ID,
        artifact_id=artifact_id,
    )
    return b"".join([chunk async for chunk in download.chunks])


def _validate_phase_identity(identity: Mapping[str, object], expected_role: str) -> None:
    source_revision = identity.get("source_revision")
    if (
        identity.get("phase_role") != expected_role
        or not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
        or not isinstance(identity.get("corpusgen_version"), str)
        or not identity.get("corpusgen_version")
    ):
        raise RuntimeError("qualified PEFT phase identity is not exact")
    runtime_kind = identity.get("runtime_kind")
    if runtime_kind == "container-image":
        image_digest = identity.get("image_digest")
        if (
            identity.get("network") != "none"
            or not isinstance(image_digest, str)
            or _SHA256.fullmatch(image_digest) is None
        ):
            raise RuntimeError("qualified PEFT container phase is not immutable and offline")
    elif runtime_kind == "isolated-windows-lock":
        lock_digest = identity.get("profile_lock_sha256")
        if (
            identity.get("network") != "offline-local-only"
            or not isinstance(lock_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", lock_digest) is None
        ):
            raise RuntimeError("qualified PEFT Windows phase is not exact and offline")
    else:
        raise RuntimeError("qualified PEFT phase has an unsupported runtime identity")


def _validate_cuda_proof(cuda: Mapping[str, object]) -> str:
    model_digest = cuda.get("model_snapshot_sha256")
    if (
        not isinstance(model_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", model_digest) is None
        or not str(cuda.get("actual_cuda_tensor_device", "")).startswith("cuda")
        or cuda.get("actual_cuda_tensor_squared_sum") != 30.0
    ):
        raise RuntimeError("qualified PEFT phase lacks an actual CUDA proof")
    return model_digest


def _validate_training_facts(
    training: Mapping[str, object],
    execution: Mapping[str, object],
    cuda: Mapping[str, object],
) -> None:
    required_true = (
        "peft_requested",
        "adapter_tensors_are_lora",
        "peft_compatibility_validated",
        "training_result_adopted",
        "training_artifact_integrity",
        "training_history_sensitive_payload_absent",
        "late_staging_prevented",
    )
    if any(training.get(field) is not True for field in required_true):
        raise RuntimeError("qualified PEFT training facts omit a fail-closed proof")
    if (
        training.get("contract") != PEFT_CHAIN_CONTRACT
        or training.get("training_device") != "cuda"
        or training.get("training_handler_profile") != "gpu-training"
        or training.get("training_run_state") != RunState.SUCCEEDED.value
        or training.get("ppo_steps") != 2
        or training.get("progress_steps") != [0, 1]
        or training.get("durable_progress_completed") != [1, 2]
        or training.get("checkpoint_files") != ["adapter_config.json", "adapter_model.safetensors"]
        or training.get("checkpoint_safetensors_files") != 1
        or training.get("cancellation_code") != "run_cancelled"
        or training.get("active_child_pids_after_cancellation") != 0
    ):
        raise RuntimeError("qualified PEFT training facts do not attest the exact phase")
    tensor_count = training.get("adapter_tensor_count")
    checkpoint_bytes = training.get("checkpoint_bytes")
    if (
        isinstance(tensor_count, bool)
        or not isinstance(tensor_count, int)
        or tensor_count < 1
        or isinstance(checkpoint_bytes, bool)
        or not isinstance(checkpoint_bytes, int)
        or checkpoint_bytes < 1
    ):
        raise RuntimeError("qualified PEFT training facts have invalid tensor or size evidence")
    model_digest = _validate_cuda_proof(cuda)
    compatibility = _evidence_mapping(training, "checkpoint_compatibility")
    if (
        compatibility.get("peft_adapter") is not True
        or compatibility.get("base_model_id") != MODEL_ID
        or compatibility.get("base_model_revision") != REVISION
        or compatibility.get("base_model_snapshot_sha256") != model_digest
        or compatibility.get("tokenizer_id") != MODEL_ID
        or compatibility.get("tokenizer_revision") != REVISION
        or compatibility.get("tokenizer_snapshot_sha256") != model_digest
        or compatibility.get("corpusgen_version") != execution.get("corpusgen_version")
        or compatibility.get("torch_version") != cuda.get("torch_version")
        or compatibility.get("corpusgen_version") != importlib.metadata.version("corpusgen")
        or compatibility.get("torch_version") != importlib.metadata.version("torch")
        or compatibility.get("transformers_version") != importlib.metadata.version("transformers")
        or compatibility.get("peft_version") != importlib.metadata.version("peft")
    ):
        raise RuntimeError("qualified PEFT training compatibility is incomplete")
    for field in ("checkpoint_sha256", "training_artifact_sha256"):
        digest = training.get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError("qualified PEFT training facts contain an invalid digest")
    _canonical_uuid(training.get("training_artifact_id"), "training artifact")


def validate_peft_training_receipt(receipt: Mapping[str, object]) -> None:
    """Validate the bounded training-phase handoff before it can cross an image boundary."""

    if (
        receipt.get("schema_version") != PEFT_TRAIN_RECEIPT_SCHEMA
        or receipt.get("phase") != PEFT_TRAIN_MODE
    ):
        raise RuntimeError("qualified PEFT training receipt has the wrong schema or phase")
    execution = _evidence_mapping(receipt, "execution")
    cuda = _evidence_mapping(receipt, "cuda")
    training = _evidence_mapping(receipt, "training")
    state = _evidence_mapping(receipt, "state")
    _validate_phase_identity(execution, "gpu-training")
    _validate_training_facts(training, execution, cuda)
    if (
        state.get("schema_id") != PEFT_STATE_SCHEMA
        or state.get("organization_id") != "00000000-0000-4000-8000-000000000001"
        or state.get("project_id") != str(DEMO_PROJECT_ID)
        or state.get("training_artifact_id") != training.get("training_artifact_id")
        or state.get("training_artifact_sha256") != training.get("training_artifact_sha256")
        or state.get("checkpoint_sha256") != training.get("checkpoint_sha256")
        or state.get("model_snapshot_sha256") != cuda.get("model_snapshot_sha256")
        or not isinstance(state.get("training_spec_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", cast(str, state.get("training_spec_sha256"))) is None
    ):
        raise RuntimeError("qualified PEFT training receipt does not bind its durable state")
    _canonical_uuid(state.get("training_run_id"), "training run")
    _canonical_uuid(state.get("training_artifact_id"), "training artifact")


def validate_peft_chain_evidence(evidence: Mapping[str, object]) -> None:
    """Reject incomplete or internally inconsistent qualified PEFT-chain evidence."""

    if evidence.get("schema_version") != EVIDENCE_SCHEMA or evidence.get("mode") != PEFT_CHAIN_MODE:
        raise RuntimeError("qualified PEFT evidence has the wrong schema or mode")
    phases = _evidence_mapping(evidence, "phases")
    if set(phases) != {"training", "inference"}:
        raise RuntimeError("qualified PEFT evidence must contain exactly two phases")
    training_phase = _evidence_mapping(phases, "training")
    inference_phase = _evidence_mapping(phases, "inference")
    if set(training_phase) != {"execution", "cuda"} or set(inference_phase) != {
        "execution",
        "cuda",
    }:
        raise RuntimeError("qualified PEFT evidence has an unsupported phase shape")
    training_execution = _evidence_mapping(training_phase, "execution")
    inference_execution = _evidence_mapping(inference_phase, "execution")
    training_cuda = _evidence_mapping(training_phase, "cuda")
    inference_cuda = _evidence_mapping(inference_phase, "cuda")
    _validate_phase_identity(training_execution, "gpu-training")
    _validate_phase_identity(inference_execution, "gpu-inference")
    model_snapshot_sha256 = _validate_cuda_proof(training_cuda)
    if _validate_cuda_proof(inference_cuda) != model_snapshot_sha256:
        raise RuntimeError("qualified PEFT phases used different model snapshots")
    if training_execution.get("source_revision") != inference_execution.get(
        "source_revision"
    ) or training_execution.get("runtime_kind") != inference_execution.get("runtime_kind"):
        raise RuntimeError("qualified PEFT phases do not identify the same candidate runtime")
    runtime_kind = training_execution.get("runtime_kind")
    if runtime_kind == "container-image":
        # Each role is independently attested. The target images can legitimately have
        # the same content digest when their exact dependency closures are identical.
        if not all(
            isinstance(identity.get("image_digest"), str)
            and _SHA256.fullmatch(cast(str, identity.get("image_digest"))) is not None
            for identity in (training_execution, inference_execution)
        ):
            raise RuntimeError("qualified PEFT evidence does not bind both exact images")
    elif runtime_kind == "isolated-windows-lock":
        if training_execution.get("profile_id") != inference_execution.get(
            "profile_id"
        ) or training_execution.get("profile_lock_sha256") != inference_execution.get(
            "profile_lock_sha256"
        ):
            raise RuntimeError("qualified PEFT Windows phases did not use one exact profile")
    else:  # guarded independently above; retained as a fail-closed invariant
        raise RuntimeError("qualified PEFT evidence has an unsupported runtime identity")

    training = _evidence_mapping(evidence, "training")
    _validate_training_facts(training, training_execution, training_cuda)
    required_true = (
        "training_phase_receipt_verified",
        "trusted_materialization_authorized",
        "trusted_materialization_read_only",
        "trusted_materialization_one_use",
        "trusted_materialization_cleaned",
        "generation_result_adopted",
        "generation_artifact_integrity",
        "durable_history_sensitive_payload_absent",
    )
    if any(training.get(field) is not True for field in required_true):
        raise RuntimeError("qualified PEFT evidence is missing a required fail-closed proof")
    if (
        training.get("generation_device") != "cuda"
        or training.get("generation_handler_profile") != "gpu-inference"
        or training.get("generation_run_state") != RunState.SUCCEEDED.value
        or training.get("trusted_materialization_files")
        != ["adapter_config.json", "adapter_model.safetensors"]
        or training.get("generation_model_id") != MODEL_ID
        or training.get("generation_model_revision") != REVISION
        or training.get("generation_model_snapshot_sha256") != model_snapshot_sha256
        or training.get("guidance_strategy") != "phon_rl"
        or training.get("generation_coverage") != 1.0
    ):
        raise RuntimeError("qualified PEFT evidence does not attest the complete chain")

    compatibility = _evidence_mapping(training, "checkpoint_compatibility")
    if (
        compatibility.get("corpusgen_version") != importlib.metadata.version("corpusgen")
        or compatibility.get("torch_version") != importlib.metadata.version("torch")
        or compatibility.get("transformers_version") != importlib.metadata.version("transformers")
        or compatibility.get("peft_version") != importlib.metadata.version("peft")
        or inference_execution.get("corpusgen_version") != compatibility.get("corpusgen_version")
        or inference_cuda.get("torch_version") != compatibility.get("torch_version")
    ):
        raise RuntimeError("qualified PEFT inference image is incompatible with the checkpoint")

    training_artifact_sha256 = training.get("training_artifact_sha256")
    checkpoint_sha256 = training.get("checkpoint_sha256")
    generation_artifact_sha256 = training.get("generation_artifact_sha256")
    generation_output_sha256 = training.get("generation_output_sha256")
    for digest in (
        training_artifact_sha256,
        checkpoint_sha256,
        generation_artifact_sha256,
        generation_output_sha256,
        training.get("training_phase_receipt_sha256"),
    ):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError("qualified PEFT evidence contains an invalid digest")
    if (
        training.get("adapter_artifact_sha256") != training_artifact_sha256
        or training.get("adapter_checkpoint_sha256") != checkpoint_sha256
    ):
        raise RuntimeError("qualified PEFT evidence does not bind generation to training")
    _canonical_uuid(training.get("generation_artifact_id"), "generation artifact")


def _evidence_mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise RuntimeError(f"qualified PEFT evidence is missing {field}")
    return cast(Mapping[str, object], nested)


def _execution_identity(phase_role: str | None = None) -> dict[str, object]:
    image_digest = os.environ.get("CORPUSKIT_ACCEPTANCE_IMAGE_DIGEST", "")
    source_revision = os.environ.get("CORPUSKIT_ACCEPTANCE_SOURCE_REVISION", "")
    if phase_role is not None:
        if phase_role not in {"gpu-training", "gpu-inference"}:
            raise RuntimeError("qualified acceptance received an unsupported phase role")
        if os.environ.get("CORPUSKIT_ACCEPTANCE_PHASE_ROLE") != phase_role:
            raise RuntimeError("qualified acceptance phase role does not match its command")
        if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
            raise RuntimeError("qualified PEFT acceptance requires an exact source revision")
    if (
        source_revision != "local-uncommitted"
        and re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
    ):
        raise RuntimeError("qualified acceptance requires an exact source revision")
    if image_digest:
        if _SHA256.fullmatch(image_digest) is None:
            raise RuntimeError("qualified acceptance requires an exact image digest")
        if os.environ.get("CORPUSKIT_ACCEPTANCE_NETWORK") != "none":
            raise RuntimeError("container acceptance requires Docker network disabled")
        identity: dict[str, object] = {
            "runtime_kind": "container-image",
            "image_digest": image_digest,
            "source_revision": source_revision,
            "network": "none",
            "corpusgen_version": importlib.metadata.version("corpusgen"),
        }
        if phase_role is not None:
            identity["phase_role"] = phase_role
        return identity

    if os.environ.get("CORPUSKIT_ACCEPTANCE_NETWORK") != "offline-local-only":
        raise RuntimeError("isolated acceptance requires the offline local-only policy")
    lock_path = Path(os.environ.get("CORPUSKIT_ACCEPTANCE_PROFILE_LOCK", ""))
    expected_lock_digest = os.environ.get("CORPUSKIT_ACCEPTANCE_PROFILE_LOCK_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{64}", expected_lock_digest) is None:
        raise RuntimeError("isolated acceptance requires an exact profile-lock digest")
    lock_bytes = lock_path.read_bytes()
    actual_lock_digest = hashlib.sha256(lock_bytes).hexdigest()
    if actual_lock_digest != expected_lock_digest:
        raise RuntimeError("isolated profile lock digest does not match")
    package_count = _validate_windows_profile(lock_bytes.decode("utf-8"))
    identity = {
        "runtime_kind": "isolated-windows-lock",
        "profile_id": "windows-x64-python312-torch213-cu132-v1",
        "profile_lock_sha256": actual_lock_digest,
        "locked_package_count": package_count,
        "source_revision": source_revision,
        "network": "offline-local-only",
        "corpusgen_version": importlib.metadata.version("corpusgen"),
    }
    if phase_role is not None:
        identity["phase_role"] = phase_role
    return identity


def _validate_windows_profile(lock_text: str) -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("the isolated CUDA profile is Windows x64 only")
    if platform.python_version() != "3.12.12":
        raise RuntimeError("the isolated CUDA profile requires exact Python 3.12.12")
    expected = parse_windows_profile_lock(lock_text)
    lock_lines = {line.strip() for line in lock_text.splitlines()}
    if (
        f"# torch-wheel-sha256={_TORCH_WHEEL_SHA256}" not in lock_lines
        or f"# torch-wheel-record-sha256={_TORCH_WHEEL_RECORD_SHA256}" not in lock_lines
        or f"# torch-record-canonical-sha256={_TORCH_RECORD_CANONICAL_SHA256}" not in lock_lines
        or f"torch @ {_TORCH_WHEEL_URL}#sha256={_TORCH_WHEEL_SHA256}" not in lock_lines
    ):
        raise RuntimeError("isolated profile is missing an approved Torch artifact pin")
    torch_distribution = importlib.metadata.distribution("torch")
    record = next(
        (
            item
            for item in (torch_distribution.files or ())
            if str(item).endswith(".dist-info/RECORD")
        ),
        None,
    )
    if record is None:
        raise RuntimeError("installed Torch distribution has no RECORD manifest")
    record_path = Path(str(torch_distribution.locate_file(record)))
    _validate_uv_torch_install_metadata(record_path)
    actual_record_digest = canonical_torch_record_sha256(record_path.read_text(encoding="utf-8"))
    if actual_record_digest != _TORCH_RECORD_CANONICAL_SHA256:
        raise RuntimeError("installed Torch RECORD digest does not match the approved wheel")
    installed = {
        re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(): distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata["Name"]
    }
    mismatches = {
        name: {"expected": version, "installed": installed.get(name)}
        for name, version in expected.items()
        if installed.get(name) != version
    }
    if mismatches:
        raise RuntimeError(f"isolated profile package mismatch: {mismatches}")
    return len(expected)


def canonical_torch_record_sha256(record_text: str) -> str:
    """Normalize only uv's path-dependent rows from the hash-pinned wheel manifest."""

    try:
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except (csv.Error, UnicodeError):
        raise RuntimeError("installed Torch RECORD is malformed") from None
    if (
        not rows
        or any(len(row) != 3 or not row[0] for row in rows)
        or len({row[0] for row in rows}) != len(rows)
        or not _UV_TORCH_RECORD_ADDITIONS.issubset({row[0] for row in rows})
    ):
        raise RuntimeError("installed Torch RECORD has an unsupported uv layout")
    wheel_rows = sorted(
        (row for row in rows if row[0] not in _UV_TORCH_RECORD_ADDITIONS),
        key=lambda row: row[0],
    )
    normalized = io.StringIO(newline="")
    csv.writer(normalized, lineterminator="\n").writerows(wheel_rows)
    return hashlib.sha256(normalized.getvalue().encode("utf-8")).hexdigest()


def _validate_uv_torch_install_metadata(record_path: Path) -> None:
    dist_info = record_path.parent
    try:
        installer = (dist_info / "INSTALLER").read_bytes()
        requested = (dist_info / "REQUESTED").read_bytes()
        direct_url = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("installed Torch uv provenance is missing or malformed") from None
    if installer != b"uv" or requested != b"" or not isinstance(direct_url, dict):
        raise RuntimeError("installed Torch does not have the approved uv provenance")
    archive_info = direct_url.get("archive_info")
    if direct_url.get("url") != _TORCH_WHEEL_URL or not isinstance(archive_info, dict):
        raise RuntimeError("installed Torch direct artifact provenance does not match")
    recorded_hashes = archive_info.get("hashes", {})
    recorded_hash = archive_info.get("hash")
    approved_archive_provenance: tuple[tuple[dict[str, str], str | None], ...] = (
        ({}, None),
        (
            {"sha256": _TORCH_WHEEL_SHA256},
            f"sha256={_TORCH_WHEEL_SHA256}",
        ),
    )
    if (recorded_hashes, recorded_hash) not in approved_archive_provenance:
        raise RuntimeError("installed Torch direct artifact hash does not match")


def parse_windows_profile_lock(lock_text: str) -> dict[str, str]:
    """Parse only exact package pins and the one approved CUDA wheel artifact."""

    expected: dict[str, str] = {}
    for raw_line in lock_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " @ " in line:
            name, url = line.split(" @ ", maxsplit=1)
            if name != "torch" or url != f"{_TORCH_WHEEL_URL}#sha256={_TORCH_WHEEL_SHA256}":
                raise RuntimeError("unsupported direct artifact in isolated profile")
            version = "2.13.0+cu132"
        elif "==" in line:
            name, version = line.split("==", maxsplit=1)
        else:
            raise RuntimeError("isolated profile entries must be exact pins")
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in expected or not version:
            raise RuntimeError("isolated profile contains an invalid duplicate pin")
        expected[normalized_name] = version
    if len(expected) < 70:
        raise RuntimeError("isolated profile is unexpectedly incomplete")
    return expected


def _qualified_state_root(path: Path | None, *, training: bool) -> Path:
    if path is None:
        raise RuntimeError("qualified PEFT phases require an explicit ephemeral state root")
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("qualified PEFT state root must be an existing real directory")
    root = path.resolve(strict=True)
    if training:
        if any(root.iterdir()):
            raise RuntimeError("qualified PEFT training requires an empty state root")
    elif not all(
        candidate.exists()
        for candidate in (
            root / _PEFT_TRAIN_RECEIPT,
            root / "acceptance.db",
            root / "artifacts",
        )
    ):
        raise RuntimeError("qualified PEFT inference received incomplete training state")
    return root


async def run(mode: str, *, state_root: Path | None = None) -> dict[str, object]:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    torch, _, transformers = _dependencies()
    with tempfile.TemporaryDirectory(prefix="corpuskit-qualified-gpu-") as temporary:
        work = Path(temporary)
        runtime = create_tiny_runtime(work / "model-cache")
        if mode == "inference":
            if state_root is not None:
                raise RuntimeError("baseline inference does not accept PEFT chain state")
            evidence: dict[str, object] = {
                "schema_version": EVIDENCE_SCHEMA,
                "recorded_at": datetime.now(UTC).isoformat(),
                "mode": mode,
                "execution": _execution_identity(),
                "cuda": cuda_evidence(torch, runtime),
            }
            evidence["inference"] = run_inference(runtime, torch, transformers)
            return evidence
        if mode == PEFT_TRAIN_MODE:
            root = _qualified_state_root(state_root, training=True)
            return await run_peft_train_phase(
                runtime,
                torch,
                root,
                _execution_identity("gpu-training"),
                cuda_evidence(torch, runtime),
            )
        if mode == PEFT_INFER_MODE:
            root = _qualified_state_root(state_root, training=False)
            return await run_peft_infer_phase(
                runtime,
                torch,
                root,
                _execution_identity("gpu-inference"),
                cuda_evidence(torch, runtime),
            )
        raise RuntimeError("unsupported qualified GPU acceptance mode")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("inference", PEFT_TRAIN_MODE, PEFT_INFER_MODE),
        required=True,
    )
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.mode == "inference":
        if arguments.output is None or arguments.state_root is not None:
            parser.error("inference requires --output and forbids --state-root")
    elif arguments.mode == PEFT_TRAIN_MODE:
        if arguments.state_root is None or arguments.output is not None:
            parser.error("peft-train requires --state-root and forbids --output")
    elif arguments.state_root is None or arguments.output is None:
        parser.error("peft-infer requires --state-root and --output")
    if arguments.output is not None and arguments.state_root is not None:
        output = arguments.output.resolve(strict=False)
        state = arguments.state_root.resolve(strict=True)
        if output == state or output.is_relative_to(state):
            parser.error("qualified evidence output must be outside ephemeral chain state")
    result = asyncio.run(run(arguments.mode, state_root=arguments.state_root))
    if arguments.mode == PEFT_TRAIN_MODE:
        sys.stdout.write('{"phase":"peft-train","receipt_written":true}\n')
        return 0
    if arguments.output is None:  # argparse invariants above, retained for type narrowing
        raise RuntimeError("qualified acceptance output is missing")
    evidence = result
    encoded = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    if arguments.output.is_symlink():
        raise RuntimeError("qualified acceptance output must not be a symbolic link")
    arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":  # pragma: no cover - executed inside qualified GPU images
    raise SystemExit(main())
