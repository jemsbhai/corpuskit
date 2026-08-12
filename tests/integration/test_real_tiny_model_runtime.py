"""Offline real-Transformers acceptance for local generation and LM analysis."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import math
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner
from corpusgen.cli import main as corpusgen_cli
from temporalio.testing import ActivityEnvironment

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.adapters.corpusgen.model_runtime import (
    CachedLocalModelLoader,
    CorpusgenModelRuntimeAdapter,
    TransformersLocalModelLoader,
    compute_snapshot_digest,
)
from corpuskit.adapters.corpusgen.phon_rl import (
    CorpusgenPhonRlAdapter,
    CorpusgenPhonRlTrainingBindings,
    OfflinePhonRlSnapshotResolver,
)
from corpuskit.config import Settings
from corpuskit.domain.artifacts import ArtifactKind
from corpuskit.domain.cli_parity import CliGenerateRequest
from corpuskit.domain.generation import (
    CompositeScoringRequest,
    GenerationScoringOptions,
    GenerationStoppingCriteria,
    GenerationTarget,
    RepositoryCandidate,
    ScoreWeights,
)
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
    PerplexitySentenceStatus,
    PhonRlAdapterSelection,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import (
    PhonRlCheckpointCompatibility,
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
    build_object_store,
)
from corpuskit.persistence.database import Database
from corpuskit.services.artifact_adoption import ArtifactAdoptionService
from corpuskit.services.artifacts import ArtifactActor, ArtifactService
from corpuskit.services.cli_parity import CliParityService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.services.model_runtime import ModelRuntimeCoordinator, ModelRuntimePolicy
from corpuskit.services.phon_rl import PhonRlRuntimePolicy, PhonRlTrainingCoordinator
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.worker.model_registry import build_model_handler_registry
from corpuskit.worker.phon_rl_registry import build_phon_rl_handler_registry
from corpuskit.workflows.activities import CoreRunActivities
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.store import DurableRunStore
from corpuskit.workflows.trusted_inputs import TrustedRunInputMaterializer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REVISION = "a" * 40
_MODEL_ID = "corpuskit/tiny-offline-causal"


@dataclass(frozen=True, slots=True)
class _TinyRuntime:
    adapter: CorpusgenModelRuntimeAdapter
    pin: ImmutableModelPin
    policy: LocalModelPolicy
    snapshot: Path


@pytest.fixture(scope="module")
def tiny_runtime(tmp_path_factory: pytest.TempPathFactory) -> _TinyRuntime:
    """Create deterministic safetensors weights without network or remote code."""

    torch = pytest.importorskip("torch", reason="the local-model profile requires PyTorch")
    tokenizers = pytest.importorskip(
        "tokenizers",
        reason="the local-model profile requires Hugging Face tokenizers",
    )
    transformers = pytest.importorskip(
        "transformers",
        reason="the local-model profile requires Transformers",
    )
    root = tmp_path_factory.mktemp("tiny-model-cache")
    snapshot = root / "models--corpuskit--tiny-offline-causal" / "snapshots" / _REVISION
    snapshot.mkdir(parents=True)
    vocabulary = {
        "<pad>": 0,
        "<eos>": 1,
        "<unk>": 2,
        "pea": 3,
        "p": 4,
        ".": 5,
        "Generate": 6,
        "short": 7,
        "natural": 8,
        "sentences": 9,
        "containing": 10,
        "these": 11,
        "sounds": 12,
        "One": 13,
        "sentence": 14,
        "per": 15,
        "line": 16,
        "no": 17,
        "numbering": 18,
        "A": 19,
        "fluent": 20,
        "second": 21,
    }
    tokenizer_backend = tokenizers.Tokenizer(
        tokenizers.models.WordLevel(vocab=vocabulary, unk_token="<unk>")
    )
    tokenizer_backend.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tokenizer = transformers.PreTrainedTokenizerFast(
        tokenizer_object=tokenizer_backend,
        unk_token="<unk>",
        pad_token="<pad>",
        eos_token="<eos>",
        bos_token="<eos>",
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
        # A constant hidden state and one dominant safe token make greedy output
        # deterministic across CPU kernels and operating systems.
        model.transformer.ln_f.bias.fill_(1.0)
        model.lm_head.weight[3].fill_(1.0)
    model.save_pretrained(snapshot, safe_serialization=True)

    assert not tuple(snapshot.glob("*.bin"))
    assert tuple(snapshot.glob("*.safetensors"))
    digest = compute_snapshot_digest(snapshot, approved_cache_root=root)
    pin = ImmutableModelPin(model=_MODEL_ID, revision=_REVISION)
    loader = CachedLocalModelLoader(
        TransformersLocalModelLoader(
            lambda requested: snapshot if requested == pin else _unexpected_pin(requested),
            approved_cache_root=root,
        ),
        max_entries=1,
    )
    policy = LocalModelPolicy(
        pin=pin,
        artifact_sha256=digest,
        allowed_devices=(ModelDevice.CPU,),
        allowed_quantizations=(ModelQuantization.NONE,),
        allow_phon_rl_adapters=True,
    )
    return _TinyRuntime(
        adapter=CorpusgenModelRuntimeAdapter(model_loader=loader),
        pin=pin,
        policy=policy,
        snapshot=snapshot,
    )


def _unexpected_pin(pin: ImmutableModelPin) -> Path:
    raise AssertionError(f"unexpected immutable model pin: {pin.model}@{pin.revision}")


def test_real_offline_local_generation(tiny_runtime: _TinyRuntime) -> None:
    """A real AutoModel/AutoTokenizer pair must traverse CorpusGen generation."""

    request = LocalGenerationRequest(
        selection=LocalModelSelection(pin=tiny_runtime.pin),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            target_coverage=1.0,
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=30.0,
        ),
        scoring=GenerationScoringOptions(weights=ScoreWeights(coverage=1, fluency=1)),
        candidates_per_iteration=1,
        max_new_tokens=3,
        do_sample=False,
        seed=1729,
        activity_timeout_seconds=60.0,
    )

    result = tiny_runtime.adapter.run_local(
        request,
        tiny_runtime.policy,
        WorkerModelProfile.LOCAL_CPU,
    )

    assert result.coverage == 1.0
    assert result.missing_units == ()
    assert tuple(candidate.text for candidate in result.accepted) == ("pea pea pea",)
    assert result.model.local_files_only is True
    assert result.model.trust_remote_code is False
    assert result.model.safetensors_only is True
    assert result.model.revision == _REVISION
    assert result.model.fluency_scorer == "perplexity"


def test_real_offline_cpu_local_cli_shared_result_semantics_match_pinned_adapter(
    tiny_runtime: _TinyRuntime,
) -> None:
    """Compare the locked CLI's shared JSON fields after real CPU model execution."""

    transformers = pytest.importorskip(
        "transformers",
        reason="the local-model profile requires Transformers",
    )
    seed = 1729
    target = GenerationTarget(phonemes=CorpusgenAdapter().get_inventory("en-us").phonemes)
    stopping = GenerationStoppingCriteria(
        target_coverage=1.0,
        max_sentences=1,
        max_iterations=1,
        timeout_seconds=30.0,
    )
    request = LocalGenerationRequest(
        selection=LocalModelSelection(pin=tiny_runtime.pin),
        target=target,
        stopping=stopping,
        candidates_per_iteration=1,
        max_new_tokens=3,
        temperature=0.8,
        top_p=0.95,
        do_sample=False,
        seed=seed,
        activity_timeout_seconds=60.0,
    )
    adapter_result = tiny_runtime.adapter.run_local(
        request,
        tiny_runtime.policy,
        WorkerModelProfile.LOCAL_CPU,
    )

    preview = CliParityService().preview(
        CliGenerateRequest(
            backend="local",
            language="en-us",
            model=str(tiny_runtime.snapshot),
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=30,
            candidates_per_iteration=1,
            local_temperature=0.8,
            local_max_tokens=3,
            device="cpu",
            quantization="none",
        )
    )
    assert "seed, top-p, or sampling-mode" in " ".join(preview.warnings)
    # CorpusGen 0.1.7 has no CLI seed flag. Seeding the process immediately before
    # invocation makes this offline fixture deterministic while the preview warning
    # continues to disclose that a copied command cannot carry the seed.
    transformers.set_seed(seed, deterministic=False)
    cli_run = CliRunner().invoke(
        corpusgen_cli,
        list(preview.argv[1:]),
        env={"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        catch_exceptions=False,
    )
    assert cli_run.exit_code == 0
    # Transformers writes its model-loading meter to stderr; the CLI's stdout stays JSON-only.
    cli_result = json.loads(cli_run.stdout)

    assert tuple(cli_result["generated_sentences"]) == tuple(
        item.text for item in adapter_result.accepted
    )
    assert cli_result["num_generated"] == len(adapter_result.accepted)
    assert cli_result["coverage"] == adapter_result.coverage
    assert set(cli_result["covered_units"]) == set(adapter_result.covered_units)
    assert set(cli_result["missing_units"]) == set(adapter_result.missing_units)
    assert cli_result["iterations"] == adapter_result.iterations
    assert cli_result["stop_reason"] == adapter_result.stop_reason.value
    assert cli_result["backend"] == "local"
    assert cli_result["unit"] == target.unit.value
    assert adapter_result.model.revision == tiny_runtime.pin.revision
    assert adapter_result.model.artifact_sha256 == tiny_runtime.policy.artifact_sha256
    assert {"revision", "artifact_sha256", "seed", "manifest"}.isdisjoint(cli_result)


def test_real_shared_fluency_and_perplexity(tiny_runtime: _TinyRuntime) -> None:
    """Fluency and perplexity must share one cached, real local model bundle."""

    request = LanguageModelAnalysisRequest(
        selection=LocalModelSelection(pin=tiny_runtime.pin),
        texts=(
            AnalysisText(source_id="one", text="pea pea pea."),
            AnalysisText(source_id="two", text="A second fluent sentence."),
        ),
        batch_size=2,
        max_length=32,
        composite_scoring=CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p", "b")),
            candidates=(
                RepositoryCandidate(
                    source_id="one",
                    text="pea pea pea.",
                    phonemes=("p",),
                ),
                RepositoryCandidate(
                    source_id="two",
                    text="A second fluent sentence.",
                    phonemes=("b",),
                ),
            ),
            options=GenerationScoringOptions(weights=ScoreWeights(coverage=0, fluency=1)),
        ),
        activity_timeout_seconds=60.0,
    )

    result = tiny_runtime.adapter.analyze_language_model(
        request,
        tiny_runtime.policy,
        WorkerModelProfile.LOCAL_CPU,
    )

    assert result.shared_model_instance is True
    assert result.input_sentence_count == result.scored_sentence_count == 2
    assert tuple(item.source_id for item in result.fluency) == ("one", "two")
    assert all(math.isfinite(item.score) and 0.0 <= item.score <= 1.0 for item in result.fluency)
    assert all(math.isfinite(value) and value > 0.0 for value in result.perplexity.per_sentence)
    assert tuple(item.status for item in result.sentence_perplexities) == (
        PerplexitySentenceStatus.SCORED,
        PerplexitySentenceStatus.SCORED,
    )
    assert result.model.artifact_sha256 == tiny_runtime.policy.artifact_sha256
    assert result.model.fluency_scorer == "perplexity"
    assert result.composite_scoring is not None
    fluency_by_source = {item.source_id: item.score for item in result.fluency}
    assert [item.source_id for item in result.composite_scoring.ranked] == ["one", "two"]
    assert result.composite_scoring.ranked[0].fluency_score > (
        result.composite_scoring.ranked[1].fluency_score
    )
    for item in result.composite_scoring.ranked:
        assert item.fluency_score == pytest.approx(fluency_by_source[item.source_id])
        assert item.composite_score == pytest.approx(item.fluency_score)


def test_real_offline_peft_adapter_is_merged_and_generates_via_public_loop(
    tiny_runtime: _TinyRuntime,
    tmp_path: Path,
) -> None:
    """A real safetensors LoRA adapter must affect an app-owned, CorpusGen-loop backend."""

    peft = pytest.importorskip("peft", reason="the Phon-RL profile requires PEFT")
    transformers = pytest.importorskip(
        "transformers",
        reason="the Phon-RL profile requires Transformers",
    )
    base = transformers.AutoModelForCausalLM.from_pretrained(
        tiny_runtime.snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
    )
    configured = peft.get_peft_model(
        base,
        peft.LoraConfig(
            r=2,
            lora_alpha=4,
            target_modules=["c_attn"],
            task_type="CAUSAL_LM",
        ),
    )
    generated = tmp_path / "generated-adapter"
    configured.save_pretrained(generated, safe_serialization=True)
    adapter_root = tmp_path / "verified-adapter"
    adapter_root.mkdir()
    for name in ("adapter_config.json", "adapter_model.safetensors"):
        (adapter_root / name).write_bytes((generated / name).read_bytes())
    config_path = adapter_root / "adapter_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(
        base_model_name_or_path=tiny_runtime.pin.model,
        revision=tiny_runtime.pin.revision,
        auto_mapping=None,
    )
    config_path.write_text(
        json.dumps(config, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    for item in adapter_root.iterdir():
        item.chmod(0o400)
    adapter_root.chmod(0o500)

    compatibility = PhonRlCheckpointCompatibility(
        base_model_id=tiny_runtime.pin.model,
        base_model_revision=tiny_runtime.pin.revision,
        base_model_snapshot_sha256=tiny_runtime.policy.artifact_sha256,
        tokenizer_id=tiny_runtime.pin.model,
        tokenizer_revision=tiny_runtime.pin.revision,
        tokenizer_snapshot_sha256=tiny_runtime.policy.artifact_sha256,
        corpusgen_version=importlib.metadata.version("corpusgen"),
        torch_version=importlib.metadata.version("torch"),
        transformers_version=importlib.metadata.version("transformers"),
        peft_version=importlib.metadata.version("peft"),
        peft_adapter=True,
    )
    request = LocalGenerationRequest(
        selection=LocalModelSelection(pin=tiny_runtime.pin),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            target_coverage=1.0,
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=30,
        ),
        candidates_per_iteration=1,
        max_new_tokens=3,
        do_sample=False,
        seed=1729,
        phon_rl_adapter=PhonRlAdapterSelection(
            artifact_id=uuid4(),
            artifact_sha256="c" * 64,
            checkpoint_sha256="d" * 64,
        ),
        activity_timeout_seconds=60,
    )
    result = tiny_runtime.adapter.run_local_phon_rl(
        request,
        tiny_runtime.policy,
        WorkerModelProfile.LOCAL_CPU,
        adapter_root=adapter_root,
        compatibility=compatibility,
    )

    assert result.coverage == 1.0
    assert tuple(candidate.text for candidate in result.accepted) == ("pea pea pea",)
    assert result.model.guidance_strategy == "phon_rl"
    assert result.model.adapter_artifact_sha256 == "c" * 64
    assert result.model.adapter_checkpoint_sha256 == "d" * 64


@pytest.mark.asyncio
async def test_real_trainer_peft_output_crosses_adoption_and_trusted_generation_chain(
    tiny_runtime: _TinyRuntime,
    tmp_path: Path,
) -> None:
    """Carry actual trainer safetensors through both parent-owned durable executions."""

    cache_root = tiny_runtime.snapshot.parents[2]
    artifact_root = (tmp_path / "artifacts").absolute()
    trusted_root = (tmp_path / "trusted-inputs").absolute()
    rl_pin = PhonRlSnapshotPin(
        repository_id=tiny_runtime.pin.model,
        revision=tiny_runtime.pin.revision,
        snapshot_sha256=tiny_runtime.policy.artifact_sha256,
    )
    rl_entry = PhonRlRuntimePolicyEntry(
        runtime_id="tiny-offline-rl-v1",
        model=rl_pin,
        tokenizer=rl_pin,
        cache_root_id="models-ro",
        cache_mount_read_only=True,
        allow_peft=True,
        allowed_peft_ranks=(2,),
        allowed_peft_alphas=(4,),
        allowed_prompt_strategies=("missing-units-v1",),
    )
    settings = Settings(
        environment="test",
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / f'peft-chain-{uuid4()}.db').as_posix()}"),
        artifact_root=artifact_root,
        artifact_max_bytes=100 * 1024 * 1024,
        artifact_download_chunk_bytes=16 * 1024,
        worker_local_model_policies=(tiny_runtime.policy,),
        worker_model_cache_root=cache_root,
        worker_model_cache_mount_read_only=True,
        worker_phon_rl_runtime_policies=(rl_entry,),
        worker_phon_rl_cache_roots={"models-ro": cache_root},
        _env_file=None,
    )
    database = Database(settings.database_url)
    await database.create_schema()
    objects = build_object_store(settings)
    artifacts = ArtifactService(database, objects, settings)
    runs = DurableRunStore(database)
    adopter = ArtifactAdoptionService(runs, objects, settings)
    stager = ConfiguredStagedArtifactWriter.from_settings(settings)
    actor = JobActor(
        subject="demo-user",
        organization_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    artifact_actor = ArtifactActor(
        subject=actor.subject,
        organization_id=actor.organization_id,
        request_id="real-peft-chain",
    )
    jobs = JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings))
    await jobs.bootstrap_demo(actor, environment="test")

    training_request = PhonRlTrainingRequest(
        runtime_id=rl_entry.runtime_id,
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(
            seed=17,
            num_steps=2,
            batch_size=1,
            max_new_tokens=3,
            use_peft=True,
            peft_rank=2,
            peft_alpha=4,
            activity_timeout_seconds=90,
        ),
    )
    try:
        training = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.TRAIN_PHON_RL,
                spec=training_request.model_dump(mode="json"),
            ),
            idempotency_key=f"real-peft-training-{uuid4()}",
        )
        training_reference = _reference(actor, training.run.id, training.run.spec_sha256)
        training_engine = CorpusgenPhonRlAdapter(
            snapshot_resolver=OfflinePhonRlSnapshotResolver({"models-ro": cache_root}),
            training_bindings=CorpusgenPhonRlTrainingBindings(_device="cpu"),
        )
        training_coordinator = PhonRlTrainingCoordinator(
            PhonRlRuntimePolicy(
                (rl_entry,),
                worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
            ),
            training_engine,
        )
        training_activities = CoreRunActivities(
            runs,
            build_phon_rl_handler_registry(
                "gpu-training",
                training_coordinator,
                stager,
            ),
            heartbeat_seconds=0.05,
            artifact_adopter=adopter,
            activity_deadline_cap_seconds=90,
            process_hard_timeout_seconds=90,
        )
        await ActivityEnvironment().run(training_activities.execute_run, training_reference)

        training_run = await jobs.get(actor, training.run.id)
        assert training_run.state is RunState.SUCCEEDED
        assert training_run.result_summary is not None
        training_artifact_id = UUID(str(training_run.result_summary["artifact_id"]))
        training_artifact = await artifacts.get(
            artifact_actor,
            project_id=DEMO_PROJECT_ID,
            artifact_id=training_artifact_id,
        )
        assert training_artifact.kind is ArtifactKind.RUN_RESULT
        assert training_artifact.run_id == training.run.id
        assert training_artifact.sha256 == training_run.result_summary["sha256"]
        training_payload = await _download_artifact(
            artifacts,
            artifact_actor,
            training_artifact_id,
        )
        assert hashlib.sha256(training_payload).hexdigest() == training_artifact.sha256
        training_result = PhonRlTrainingResult.model_validate_json(
            training_payload,
            strict=True,
        )
        assert training_result.total_steps == 2
        assert tuple(point.step for point in training_result.progress) == (0, 1)
        assert training_result.checkpoint.compatibility.peft_adapter is True
        assert training_result.peft_inference_status == "application_loader_ready"
        checkpoint_files = {item.path: item for item in training_result.checkpoint.files}
        assert set(checkpoint_files) == {
            "adapter_config.json",
            "adapter_model.safetensors",
        }
        adapter_tensors = __import__(
            "safetensors.torch",
            fromlist=["load"],
        ).load(base64.b64decode(checkpoint_files["adapter_model.safetensors"].content_base64))
        assert adapter_tensors
        assert all("lora_" in name for name in adapter_tensors)
        assert str(tiny_runtime.snapshot) not in training_payload.decode("utf-8")

        generation_request = LocalGenerationRequest(
            selection=LocalModelSelection(pin=tiny_runtime.pin),
            target=GenerationTarget(phonemes=("p",)),
            stopping=GenerationStoppingCriteria(
                target_coverage=1.0,
                max_sentences=1,
                max_iterations=1,
                timeout_seconds=30,
            ),
            candidates_per_iteration=1,
            max_new_tokens=3,
            do_sample=False,
            seed=1729,
            phon_rl_adapter=PhonRlAdapterSelection(
                artifact_id=training_artifact_id,
                artifact_sha256=training_artifact.sha256,
                checkpoint_sha256=training_result.checkpoint.content_sha256,
            ),
            activity_timeout_seconds=60,
        )
        generation = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.GENERATE_LOCAL,
                spec=generation_request.model_dump(mode="json"),
            ),
            idempotency_key=f"real-peft-generation-{uuid4()}",
        )
        generation_reference = _reference(
            actor,
            generation.run.id,
            generation.run.spec_sha256,
        )
        local_engine = CorpusgenModelRuntimeAdapter(
            model_loader=CachedLocalModelLoader(
                TransformersLocalModelLoader(approved_cache_root=cache_root),
                max_entries=1,
            )
        )
        local_coordinator = ModelRuntimeCoordinator(
            ModelRuntimePolicy(
                local_models=settings.worker_local_model_policies,
                worker_profile=WorkerModelProfile.LOCAL_CPU,
            ),
            local_engine,
        )
        materializer = TrustedRunInputMaterializer(
            database,
            objects,
            root=trusted_root,
            local_policies=settings.worker_local_model_policies,
            chunk_bytes=settings.artifact_download_chunk_bytes,
        )
        generation_activities = CoreRunActivities(
            runs,
            build_model_handler_registry(
                "batch-cpu",
                local_coordinator,
                stager,
                trusted_input_root=trusted_root,
            ),
            heartbeat_seconds=0.05,
            artifact_adopter=adopter,
            activity_deadline_cap_seconds=60,
            process_hard_timeout_seconds=60,
            input_materializer=materializer,
        )
        await ActivityEnvironment().run(generation_activities.execute_run, generation_reference)

        generation_run = await jobs.get(actor, generation.run.id)
        assert generation_run.state is RunState.SUCCEEDED
        assert generation_run.result_summary is not None
        generation_artifact_id = UUID(str(generation_run.result_summary["artifact_id"]))
        generation_artifact = await artifacts.get(
            artifact_actor,
            project_id=DEMO_PROJECT_ID,
            artifact_id=generation_artifact_id,
        )
        assert generation_artifact.kind is ArtifactKind.RUN_RESULT
        assert generation_artifact.run_id == generation.run.id
        generation_payload = await _download_artifact(
            artifacts,
            artifact_actor,
            generation_artifact_id,
        )
        local_result = LocalGenerationResult.model_validate_json(
            generation_payload,
            strict=True,
        )
        assert local_result.coverage == 1.0
        assert tuple(candidate.text for candidate in local_result.accepted) == ("pea pea pea",)
        assert local_result.model.guidance_strategy == "phon_rl"
        assert local_result.model.adapter_artifact_sha256 == training_artifact.sha256
        assert (
            local_result.model.adapter_checkpoint_sha256
            == training_result.checkpoint.content_sha256
        )
        assert tuple(trusted_root.iterdir()) == ()

        training_events = await jobs.events(actor, training.run.id)
        generation_events = await jobs.events(actor, generation.run.id)
        durable_history = json.dumps(
            {
                "training": {
                    "spec": training_run.spec,
                    "result": training_run.result_summary,
                    "events": [event.payload for event in training_events],
                },
                "generation": {
                    "spec": generation_run.spec,
                    "result": generation_run.result_summary,
                    "events": [event.payload for event in generation_events],
                },
            },
            sort_keys=True,
        )
        for secret_text in (
            "Write one short, natural sentence containing these sounds: p.",
            "Write one short, natural sentence.",
            "pea pea pea",
            checkpoint_files["adapter_model.safetensors"].content_base64,
        ):
            assert secret_text not in durable_history
    finally:
        await database.dispose()


def _reference(actor: JobActor, run_id: UUID, spec_sha256: str) -> RunWorkflowReference:
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
