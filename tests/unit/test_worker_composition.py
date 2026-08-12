"""Profile composition, read-only cache, and durable routing contracts."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest
from temporalio.client import Client

from corpuskit.config import Settings
from corpuskit.domain.datg import (
    DatgCacheIdentity,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexedToken,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgUnit,
    DatgUnitTokenSet,
)
from corpuskit.domain.errors import EngineUnavailableError
from corpuskit.domain.generation import (
    GenerationStoppingCriteria,
    GenerationTarget,
    HuggingFaceRepository,
    HuggingFaceRepositorySpec,
    RepositoryGenerationRequest,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    AnalysisText,
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedPromptTemplatePolicy,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelQuantization,
    PhonRlAdapterSelection,
    SecretReference,
)
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlStaticPromptSource,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
)
from corpuskit.persistence.database import Database
from corpuskit.services.artifact_adoption import ArtifactAdoptionService
from corpuskit.services.reproducibility import RunManifestService
from corpuskit.worker import composition, runtime
from corpuskit.worker.composition import (
    EmptyDatgIndexCache,
    FilesystemDatgIndexCache,
    WorkerExecutionFactsFactory,
    build_profile_handler_registry,
    worker_policy_sha256,
)
from corpuskit.worker.routing import (
    PROFILE_RUN_KINDS,
    durable_task_queue_map,
    task_queue_for_kind,
)

WorkerProfile = Literal[
    "interactive-cpu",
    "batch-cpu",
    "external-provider",
    "gpu-inference",
    "gpu-training",
]


def _settings(tmp_path: Path, profile: WorkerProfile, **values: object) -> Settings:
    configured: dict[str, object] = {
        "environment": "test",
        "worker_profile": profile,
        "temporal_task_queue": profile,
        "artifact_root": tmp_path / "artifacts",
        "artifact_max_bytes": 100 * 1024 * 1024,
        "_env_file": None,
    }
    configured.update(values)
    return Settings.model_validate(configured)


def _hosted_policy() -> HostedModelPolicy:
    return HostedModelPolicy(
        provider="openai",
        model="openai/demo-model",
        connection_id="production",
        credential_ref=SecretReference(reference="secret://env/CORPUSKIT_TEST_PROVIDER_KEY"),
        input_cost_per_million_usd=Decimal("1"),
        output_cost_per_million_usd=Decimal("2"),
        max_output_tokens_per_request=128,
    )


def _hosted_policy_with_prompt() -> HostedModelPolicy:
    prompt = "Use {target_units} for {k} lines in {language}."
    return _hosted_policy().model_copy(
        update={
            "prompt_templates": (
                HostedPromptTemplatePolicy(
                    template_id="coverage-v1",
                    template_ref=SecretReference(
                        reference="secret://env/CORPUSKIT_TEST_PROMPT_TEMPLATE"
                    ),
                    sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    size_bytes=len(prompt.encode()),
                    max_rendered_bytes=1024,
                ),
            )
        }
    )


def _huggingface_policy() -> HuggingFaceRepositorySpec:
    return HuggingFaceRepositorySpec(
        dataset="acme/demo-corpus",
        config="default",
        split="train",
        text_column="text",
        revision="c" * 40,
        language="en-us",
        max_samples=100,
    )


def _local_policy() -> LocalModelPolicy:
    return LocalModelPolicy(
        pin=ImmutableModelPin(model="acme/tiny", revision="a" * 40),
        artifact_sha256="b" * 64,
        allowed_devices=(ModelDevice.CUDA,),
        allowed_quantizations=(ModelQuantization.NONE,),
    )


def _datg_policy() -> DatgRuntimePolicyEntry:
    pin = DatgSnapshotPin(
        repository_id="acme/tiny",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    return DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=pin,
        tokenizer=pin,
        allowed_quantizations=(DatgQuantization.NONE,),
    )


def _rl_policy(*, static: bool = False) -> PhonRlRuntimePolicyEntry:
    pin = PhonRlSnapshotPin(
        repository_id="acme/tiny-rl",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    return PhonRlRuntimePolicyEntry(
        runtime_id="tiny-rl",
        model=pin,
        tokenizer=pin,
        cache_root_id="models-ro",
        cache_mount_read_only=True,
        allow_static_prompts=static,
        allowed_prompt_strategies=("missing-units-v1",),
    )


def _index() -> DatgIndexArtifact:
    identity = DatgCacheIdentity.create(
        tokenizer=_datg_policy().tokenizer,
        language="en-us",
        unit=DatgUnit.PHONEME,
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    return DatgIndexArtifact.create(
        identity=identity,
        vocabulary_size=1,
        unit_to_tokens=(DatgUnitTokenSet(unit="p", token_ids=(0,)),),
        token_units=(DatgIndexedToken(token_id=0, decoded_text="pea", units=("p",)),),
    )


def test_server_routing_is_unique_for_admitted_kinds_and_has_no_fallback() -> None:
    mapping = durable_task_queue_map()
    assert set(mapping) == set(RunKind) - {RunKind.EXPORT}
    assert sum(len(kinds) for kinds in PROFILE_RUN_KINDS.values()) == len(RunKind) - 1
    assert mapping[RunKind.GENERATE_REPOSITORY] == "external-provider"
    assert mapping[RunKind.SELECT] == "batch-cpu"
    with pytest.raises(ValueError, match="no unique worker profile"):
        task_queue_for_kind(RunKind.EXPORT)
    assert mapping[RunKind.GENERATE_LLM] == "external-provider"
    assert mapping[RunKind.GENERATE_LOCAL] == "gpu-inference"
    assert mapping[RunKind.PERPLEXITY] == "gpu-inference"
    assert mapping[RunKind.BUILD_DATG_INDEX] == "batch-cpu"
    assert mapping[RunKind.GENERATE_DATG] == "gpu-inference"
    assert mapping[RunKind.TRAIN_PHON_RL] == "gpu-training"

    original = PROFILE_RUN_KINDS["gpu-training"]
    PROFILE_RUN_KINDS["gpu-training"] = original | {RunKind.GENERATE_LLM}
    try:
        with pytest.raises(ValueError, match="unique"):
            task_queue_for_kind(RunKind.GENERATE_LLM)
    finally:
        PROFILE_RUN_KINDS["gpu-training"] = original


def test_every_advanced_result_kind_requires_parent_side_adoption() -> None:
    for kind in (
        RunKind.SELECT,
        RunKind.GENERATE_LLM,
        RunKind.GENERATE_LOCAL,
        RunKind.PERPLEXITY,
        RunKind.BUILD_DATG_INDEX,
        RunKind.GENERATE_DATG,
        RunKind.TRAIN_PHON_RL,
    ):
        assert ArtifactAdoptionService.requires_adoption(kind) is True
    assert ArtifactAdoptionService.requires_adoption(RunKind.EVALUATE) is False


def test_batch_profile_keeps_core_and_adds_only_datg_index_when_configured(
    tmp_path: Path,
) -> None:
    core = build_profile_handler_registry(_settings(tmp_path, "batch-cpu"))
    assert core.kinds == {
        RunKind.PHONEMIZE,
        RunKind.EVALUATE,
        RunKind.DISTRIBUTION,
        RunKind.TRAJECTORY,
        RunKind.ERROR_RATES,
        RunKind.SELECT,
    }

    model_root = tmp_path / "models"
    model_root.mkdir()
    configured = build_profile_handler_registry(
        _settings(
            tmp_path,
            "batch-cpu",
            worker_datg_runtime_policies=(_datg_policy(),),
            worker_datg_model_cache_root=model_root,
            worker_datg_cache_mount_read_only=True,
        )
    )
    assert configured.kinds == core.kinds | {RunKind.BUILD_DATG_INDEX}
    ForkingPickler.dumps(configured)


def test_privileged_profiles_register_only_their_exact_configured_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORPUSKIT_TEST_PROVIDER_KEY", "not-returned-or-logged")
    hosted_settings = _settings(
        tmp_path,
        "external-provider",
        worker_hosted_model_policies=(_hosted_policy(),),
    )
    hosted = build_profile_handler_registry(hosted_settings)
    assert hosted.kinds == {RunKind.GENERATE_LLM, RunKind.GENERATE_REPOSITORY}
    assert "CORPUSKIT_TEST_PROVIDER_KEY" not in repr(hosted_settings)

    model_root = tmp_path / "models"
    index_root = tmp_path / "indexes"
    rl_root = tmp_path / "rl-models"
    for root in (model_root, index_root, rl_root):
        root.mkdir()
    inference = build_profile_handler_registry(
        _settings(
            tmp_path,
            "gpu-inference",
            worker_local_model_policies=(_local_policy(),),
            worker_model_cache_root=model_root,
            worker_model_cache_mount_read_only=True,
            worker_datg_runtime_policies=(_datg_policy(),),
            worker_datg_model_cache_root=model_root,
            worker_datg_index_cache_root=index_root,
            worker_datg_cache_mount_read_only=True,
        )
    )
    assert inference.kinds == {
        RunKind.GENERATE_LOCAL,
        RunKind.PERPLEXITY,
        RunKind.GENERATE_DATG,
    }
    ForkingPickler.dumps(inference)
    local_only = build_profile_handler_registry(
        _settings(
            tmp_path,
            "gpu-inference",
            worker_local_model_policies=(_local_policy(),),
            worker_model_cache_root=model_root,
            worker_model_cache_mount_read_only=True,
        )
    )
    assert local_only.kinds == {RunKind.GENERATE_LOCAL, RunKind.PERPLEXITY}
    datg_only = build_profile_handler_registry(
        _settings(
            tmp_path,
            "gpu-inference",
            worker_datg_runtime_policies=(_datg_policy(),),
            worker_datg_model_cache_root=model_root,
            worker_datg_index_cache_root=index_root,
            worker_datg_cache_mount_read_only=True,
        )
    )
    assert datg_only.kinds == {RunKind.GENERATE_DATG}

    training = build_profile_handler_registry(
        _settings(
            tmp_path,
            "gpu-training",
            worker_phon_rl_runtime_policies=(_rl_policy(),),
            worker_phon_rl_cache_roots={"models-ro": rl_root},
        )
    )
    assert training.kinds == {RunKind.TRAIN_PHON_RL}
    ForkingPickler.dumps(training)


def test_hosted_worker_validates_prompt_secret_integrity_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = "Use {target_units} for {k} lines in {language}."
    monkeypatch.setenv("CORPUSKIT_TEST_PROVIDER_KEY", "provider-value")
    monkeypatch.setenv("CORPUSKIT_TEST_PROMPT_TEMPLATE", prompt)
    configured = _settings(
        tmp_path,
        "external-provider",
        worker_hosted_model_policies=(_hosted_policy_with_prompt(),),
    )
    assert build_profile_handler_registry(configured).kinds == {
        RunKind.GENERATE_LLM,
        RunKind.GENERATE_REPOSITORY,
    }

    monkeypatch.setenv("CORPUSKIT_TEST_PROMPT_TEMPLATE", prompt + " tampered")
    with pytest.raises(EngineUnavailableError) as caught:
        build_profile_handler_registry(configured)
    assert caught.value.operation == "model_runtime.hosted.prompt_integrity"


def test_composition_rejects_missing_policies_cross_profile_and_mutable_cache_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="hosted model or repository allowlist"):
        build_profile_handler_registry(_settings(tmp_path, "external-provider"))

    repository_only = build_profile_handler_registry(
        _settings(
            tmp_path,
            "external-provider",
            worker_huggingface_repository_policies=(_huggingface_policy(),),
        )
    )
    assert repository_only.kinds == {RunKind.GENERATE_REPOSITORY}
    ForkingPickler.dumps(repository_only)
    with pytest.raises(RuntimeError, match="local-model or DATG"):
        build_profile_handler_registry(_settings(tmp_path, "gpu-inference"))
    with pytest.raises(RuntimeError, match="Phon-RL allowlist"):
        build_profile_handler_registry(_settings(tmp_path, "gpu-training"))
    with pytest.raises(RuntimeError, match="does not run durable"):
        build_profile_handler_registry(_settings(tmp_path, "interactive-cpu"))

    monkeypatch.setenv("CORPUSKIT_TEST_PROVIDER_KEY", "value")
    with pytest.raises(RuntimeError, match="another profile"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "external-provider",
                worker_hosted_model_policies=(_hosted_policy(),),
                worker_local_model_policies=(_local_policy(),),
            )
        )

    model_root = tmp_path / "models"
    model_root.mkdir()
    with pytest.raises(RuntimeError, match="read-only"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "gpu-inference",
                worker_local_model_policies=(_local_policy(),),
                worker_model_cache_root=model_root,
            )
        )
    missing_root = tmp_path / "missing-models"
    with pytest.raises(RuntimeError, match="unavailable"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "gpu-inference",
                worker_local_model_policies=(_local_policy(),),
                worker_model_cache_root=missing_root,
                worker_model_cache_mount_read_only=True,
            )
        )
    non_directory = tmp_path / "model-file"
    non_directory.write_text("not a cache", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a directory"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "gpu-inference",
                worker_local_model_policies=(_local_policy(),),
                worker_model_cache_root=non_directory,
                worker_model_cache_mount_read_only=True,
            )
        )
    with pytest.raises(RuntimeError, match="selection result budget"):
        build_profile_handler_registry(_settings(tmp_path, "batch-cpu", artifact_max_bytes=1024))
    with pytest.raises(RuntimeError, match="storage"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "batch-cpu",
                artifact_max_bytes=1024,
                worker_datg_runtime_policies=(_datg_policy(),),
            )
        )


def test_training_rejects_static_prompts_unknown_strategies_and_cache_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "rl"
    root.mkdir()
    static_registry = build_profile_handler_registry(
        _settings(
            tmp_path,
            "gpu-training",
            worker_phon_rl_runtime_policies=(_rl_policy(static=True),),
            worker_phon_rl_cache_roots={"models-ro": root},
        )
    )
    assert static_registry.kinds == {RunKind.TRAIN_PHON_RL}
    unsupported = _rl_policy().model_copy(
        update={"allowed_prompt_strategies": ("operator-callback",)}
    )
    with pytest.raises(RuntimeError, match="not implemented"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "gpu-training",
                worker_phon_rl_runtime_policies=(unsupported,),
                worker_phon_rl_cache_roots={"models-ro": root},
            )
        )
    with pytest.raises(RuntimeError, match="exactly match"):
        build_profile_handler_registry(
            _settings(
                tmp_path,
                "gpu-training",
                worker_phon_rl_runtime_policies=(_rl_policy(),),
                worker_phon_rl_cache_roots={"other-ro": root},
            )
        )


def test_datg_index_cache_is_content_addressed_bounded_and_schema_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _index()
    cache = FilesystemDatgIndexCache(tmp_path)
    assert cache.get(value.identity.cache_key_sha256) is None
    path = tmp_path / f"{value.identity.cache_key_sha256}.json"
    path.write_text(value.model_dump_json(), encoding="utf-8")
    assert cache.get(value.identity.cache_key_sha256) == value

    path.write_text(json.dumps({"schema_id": "wrong"}), encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as malformed:
        cache.get(value.identity.cache_key_sha256)
    assert malformed.value.operation == "datg.index.cache"
    with pytest.raises(EngineUnavailableError) as unsafe_key:
        cache.get("../private")
    assert unsafe_key.value.operation == "datg.index.cache_key"

    path.unlink()
    path.mkdir()
    with pytest.raises(EngineUnavailableError) as non_file:
        cache.get(value.identity.cache_key_sha256)
    assert non_file.value.operation == "datg.index.cache_boundary"
    path.rmdir()

    other_key = "f" * 64
    other_path = tmp_path / f"{other_key}.json"
    other_path.write_text(value.model_dump_json(), encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as wrong_identity:
        cache.get(other_key)
    assert wrong_identity.value.operation == "datg.index.cache_identity"

    monkeypatch.setattr(composition, "MAX_DATG_INDEX_BYTES", 1)
    with pytest.raises(EngineUnavailableError) as oversized:
        cache.get(other_key)
    assert oversized.value.operation == "datg.index.cache_size"
    EmptyDatgIndexCache().get(other_key)


def test_policy_digest_is_stable_sensitive_to_allowlist_and_never_returns_policy_text(
    tmp_path: Path,
) -> None:
    empty = _settings(tmp_path, "batch-cpu")
    first = worker_policy_sha256(empty)
    assert first == worker_policy_sha256(empty)
    assert len(first) == 64
    configured = _settings(
        tmp_path,
        "external-provider",
        worker_hosted_model_policies=(_hosted_policy(),),
    )
    second = worker_policy_sha256(configured)
    assert second != first
    assert "secret://" not in second
    assert "CORPUSKIT_TEST_PROVIDER_KEY" not in second


def test_parent_facts_factory_revalidates_allowlists_and_records_nonsecret_model_pins(
    tmp_path: Path,
) -> None:
    image = "sha256:" + ("f" * 64)
    hosted_settings = _settings(
        tmp_path,
        "external-provider",
        worker_image_digest=image,
        worker_hosted_model_policies=(_hosted_policy(),),
    )
    hosted_request = HostedGenerationRequest(
        selection={
            "provider": "openai",
            "model": "openai/demo-model",
            "connection_id": "production",
        },
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1,
        ),
        max_tokens_per_request=8,
        external_processing_confirmed=True,
    )
    hosted = WorkerExecutionFactsFactory(
        hosted_settings,
        "0.1.7",
        "1.52.0",
        None,
    ).for_run(RunKind.GENERATE_LLM, hosted_request.model_dump(mode="json"))
    assert hosted.model is not None
    assert hosted.model.backend == "hosted-openai"
    assert hosted.model.revision == "provider-managed"
    assert hosted.determinism.value == "nonreproducible"
    serialized = hosted.model_dump_json()
    assert "secret://" not in serialized
    assert "CORPUSKIT_TEST_PROVIDER_KEY" not in serialized

    repository_settings = _settings(
        tmp_path,
        "external-provider",
        worker_image_digest=image,
        worker_huggingface_repository_policies=(_huggingface_policy(),),
    )
    repository_request = RepositoryGenerationRequest(
        source=HuggingFaceRepository(spec=_huggingface_policy()),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1,
        ),
    )
    repository = WorkerExecutionFactsFactory(
        repository_settings,
        "0.1.7",
        "1.52.0",
        None,
    ).for_run(RunKind.GENERATE_REPOSITORY, repository_request.model_dump(mode="json"))
    assert repository.dataset is not None
    assert repository.dataset.name == "acme/demo-corpus"
    assert repository.dataset.config == "default"
    assert repository.dataset.split == "train"
    assert repository.dataset.revision == "c" * 40
    assert len(repository.dataset.selector_sha256) == 64
    assert repository.dataset.content_sha256 is None

    model_root = tmp_path / "models"
    model_root.mkdir()
    local_settings = _settings(
        tmp_path,
        "gpu-inference",
        worker_image_digest=image,
        worker_local_model_policies=(_local_policy(),),
        worker_model_cache_root=model_root,
        worker_model_cache_mount_read_only=True,
    )
    local_request = LocalGenerationRequest(
        selection=LocalModelSelection(
            pin=_local_policy().pin,
            device=ModelDevice.CUDA,
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1,
        ),
    )
    local = WorkerExecutionFactsFactory(
        local_settings,
        "0.1.7",
        None,
        None,
    ).for_run(RunKind.GENERATE_LOCAL, local_request.model_dump(mode="json"))
    assert local.model is not None
    assert local.model.revision == "a" * 40
    assert local.model.artifact_sha256 == "b" * 64

    adapter_artifact_id = uuid4()
    adapter_policy = _local_policy().model_copy(update={"allow_phon_rl_adapters": True})
    adapter_settings = _settings(
        tmp_path,
        "gpu-inference",
        worker_image_digest=image,
        worker_local_model_policies=(adapter_policy,),
        worker_model_cache_root=model_root,
        worker_model_cache_mount_read_only=True,
    )
    adapter_request = local_request.model_copy(
        update={
            "phon_rl_adapter": PhonRlAdapterSelection(
                artifact_id=adapter_artifact_id,
                artifact_sha256="c" * 64,
                checkpoint_sha256="d" * 64,
            )
        }
    )
    adapter_facts = WorkerExecutionFactsFactory(
        adapter_settings,
        "0.1.7",
        None,
        None,
    ).for_run(RunKind.GENERATE_LOCAL, adapter_request.model_dump(mode="json"))
    assert adapter_facts.input_artifact_ids == (adapter_artifact_id,)
    with pytest.raises(ValueError, match="not authorized"):
        WorkerExecutionFactsFactory(
            local_settings,
            "0.1.7",
            None,
            None,
        ).for_run(RunKind.GENERATE_LLM, hosted_request.model_dump(mode="json"))


def test_parent_facts_bind_datg_index_and_rl_prompt_strategy_digests(tmp_path: Path) -> None:
    image = "sha256:" + ("f" * 64)
    model_root = tmp_path / "models"
    index_root = tmp_path / "indexes"
    rl_root = tmp_path / "rl"
    for root in (model_root, index_root, rl_root):
        root.mkdir()
    index = _index()
    (index_root / f"{index.identity.cache_key_sha256}.json").write_text(
        index.model_dump_json(),
        encoding="utf-8",
    )
    datg_settings = _settings(
        tmp_path,
        "gpu-inference",
        worker_image_digest=image,
        worker_datg_runtime_policies=(_datg_policy(),),
        worker_datg_model_cache_root=model_root,
        worker_datg_index_cache_root=index_root,
        worker_datg_cache_mount_read_only=True,
    )
    datg_spec = {
        "runtime_id": "tiny-datg",
        "index_cache_key_sha256": index.identity.cache_key_sha256,
        "target_phonemes": ["p"],
        "target_units": ["p"],
        "candidates": 1,
        "max_new_tokens": 8,
    }
    datg = WorkerExecutionFactsFactory(
        datg_settings,
        "0.1.7",
        "1.52.0",
        None,
    ).for_run(RunKind.GENERATE_DATG, datg_spec)
    assert datg.model is not None
    assert datg.input_attestations[0].name == "datg-index"

    rl_settings = _settings(
        tmp_path,
        "gpu-training",
        worker_image_digest=image,
        worker_phon_rl_runtime_policies=(_rl_policy(),),
        worker_phon_rl_cache_roots={"models-ro": rl_root},
    )
    rl_request = PhonRlTrainingRequest(
        runtime_id="tiny-rl",
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(seed=7, num_steps=1, batch_size=1),
    )
    rl = WorkerExecutionFactsFactory(
        rl_settings,
        "0.1.7",
        "1.52.0",
        None,
    ).for_run(RunKind.TRAIN_PHON_RL, rl_request.model_dump(mode="json"))
    assert rl.model is not None
    assert rl.input_attestations[0].name == "prompt-source"

    prompt_artifact_id = uuid4()
    static_settings = _settings(
        tmp_path,
        "gpu-training",
        worker_image_digest=image,
        worker_phon_rl_runtime_policies=(_rl_policy(static=True),),
        worker_phon_rl_cache_roots={"models-ro": rl_root},
    )
    static_request = rl_request.model_copy(
        update={
            "prompt_source": PhonRlStaticPromptSource(
                artifact_id=prompt_artifact_id,
                content_sha256="c" * 64,
                prompt_count=2,
            )
        }
    )
    static_facts = WorkerExecutionFactsFactory(
        static_settings,
        "0.1.7",
        "1.52.0",
        None,
    ).for_run(RunKind.TRAIN_PHON_RL, static_request.model_dump(mode="json"))
    assert static_facts.input_artifact_ids == (prompt_artifact_id,)


def test_parent_facts_cover_analysis_build_core_and_missing_provenance_guards(
    tmp_path: Path,
) -> None:
    image = "sha256:" + ("f" * 64)
    local_settings = _settings(
        tmp_path,
        "gpu-inference",
        worker_image_digest=image,
        worker_local_model_policies=(_local_policy(),),
    )
    analysis = LanguageModelAnalysisRequest(
        selection=LocalModelSelection(
            pin=_local_policy().pin,
            device=ModelDevice.CUDA,
        ),
        texts=(AnalysisText(source_id="sentence-1", text="A bounded sentence."),),
    )
    analysis_facts = WorkerExecutionFactsFactory(
        local_settings,
        "0.1.7",
        None,
        None,
    ).for_run(RunKind.PERPLEXITY, analysis.model_dump(mode="json"))
    assert analysis_facts.model is not None
    assert analysis_facts.model.identifier == "acme/tiny"

    batch_settings = _settings(
        tmp_path,
        "batch-cpu",
        worker_image_digest=image,
        worker_datg_runtime_policies=(_datg_policy(),),
    )
    batch_factory = WorkerExecutionFactsFactory(
        batch_settings,
        "0.1.7",
        "1.52.0",
        None,
    )
    build_facts = batch_factory.for_run(
        RunKind.BUILD_DATG_INDEX,
        DatgIndexBuildRequest(runtime_id="tiny-datg").model_dump(mode="json"),
    )
    assert build_facts.model is not None
    assert build_facts.model.backend == "transformers-datg"
    assert build_facts.determinism.value == "exact"

    core_settings = _settings(
        tmp_path,
        "batch-cpu",
        worker_image_digest=image,
    )
    core_facts = WorkerExecutionFactsFactory(
        core_settings,
        "0.1.7",
        None,
        None,
    ).for_run(RunKind.PHONEMIZE, {"language": "en-us", "text": "hello"})
    assert core_facts.model is None
    assert core_facts.input_attestations == ()
    assert core_facts.determinism.value == "exact"

    with pytest.raises(ValueError, match="image provenance"):
        WorkerExecutionFactsFactory(
            _settings(tmp_path, "batch-cpu"),
            "0.1.7",
            None,
            None,
        ).for_run(RunKind.PHONEMIZE, {})


def test_parent_facts_require_preprovisioned_datg_index_and_factory_probes_runtime(
    tmp_path: Path,
) -> None:
    image = "sha256:" + ("f" * 64)
    missing_index_root = tmp_path / "indexes"
    missing_index_root.mkdir()
    settings = _settings(
        tmp_path,
        "gpu-inference",
        worker_image_digest=image,
        worker_datg_runtime_policies=(_datg_policy(),),
        worker_datg_index_cache_root=missing_index_root,
        worker_datg_cache_mount_read_only=True,
    )
    request = {
        "runtime_id": "tiny-datg",
        "index_cache_key_sha256": "d" * 64,
        "target_phonemes": ["p"],
        "target_units": ["p"],
        "candidates": 1,
        "max_new_tokens": 8,
    }
    with pytest.raises(ValueError, match="index is unavailable"):
        WorkerExecutionFactsFactory(
            settings,
            "0.1.7",
            None,
            None,
        ).for_run(RunKind.GENERATE_DATG, request)

    probed = WorkerExecutionFactsFactory.from_settings(
        _settings(tmp_path, "batch-cpu", worker_image_digest=image)
    )
    assert probed.corpusgen_version == "0.1.7"
    assert probed.settings.worker_profile == "batch-cpu"


def test_deployed_and_unknown_worker_profiles_fail_closed(tmp_path: Path) -> None:
    deployed = _settings(tmp_path, "batch-cpu")
    object.__setattr__(deployed, "environment", "production")
    with pytest.raises(RuntimeError, match="immutable image digest"):
        build_profile_handler_registry(deployed)

    unknown = _settings(tmp_path, "batch-cpu", worker_image_digest="sha256:" + ("f" * 64))
    object.__setattr__(unknown, "worker_profile", "unknown-profile")
    with pytest.raises(RuntimeError, match="no reviewed durable composition"):
        build_profile_handler_registry(unknown)


@pytest.mark.asyncio
async def test_runtime_wires_parent_manifest_lifecycle_before_temporal_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_worker(client: object, **options: object) -> object:
        captured["client"] = client
        captured.update(options)
        return object()

    monkeypatch.setattr(runtime, "Worker", fake_worker)
    settings = _settings(
        tmp_path,
        "batch-cpu",
        worker_image_digest="sha256:" + ("f" * 64),
    )
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'runtime.db').as_posix()}")
    adoption_database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'runtime-adoption.db').as_posix()}"
    )
    client = cast(Client, object())
    try:
        worker = runtime.build_worker(
            client,
            database,
            settings,
            adoption_database=adoption_database,
        )
    finally:
        await adoption_database.dispose()
        await database.dispose()

    assert worker is not None
    assert captured["client"] is client
    assert captured["task_queue"] == "batch-cpu"
    activities = cast(list[Any], captured["activities"])
    owner = activities[0].__self__
    assert all(activity.__self__ is owner for activity in activities)
    assert isinstance(owner._manifest_recorder, RunManifestService)
    assert isinstance(owner._execution_facts, WorkerExecutionFactsFactory)
    assert owner._artifact_adopter is not None
    assert owner._manifest_recorder._worker_database is database
    assert owner._manifest_recorder._adoption_database is adoption_database
    assert owner._artifact_adopter._runs.database is database
    assert owner._artifact_adopter._adoption_runs.database is adoption_database


@pytest.mark.asyncio
async def test_runtime_requires_explicit_adoption_authority_for_manifest_publication(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        "batch-cpu",
        worker_image_digest="sha256:" + ("f" * 64),
    )
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'runtime.db').as_posix()}")
    try:
        with pytest.raises(RuntimeError, match="distinct adoption database"):
            runtime.build_worker(cast(Client, object()), database, settings)
    finally:
        await database.dispose()
