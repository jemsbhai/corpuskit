"""Production-safe advanced API composition and admission contracts."""

from __future__ import annotations

import socket
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from corpuskit.api.advanced_capabilities import advanced_capabilities
from corpuskit.api.app import create_app
from corpuskit.config import Settings
from corpuskit.domain.datg import (
    DatgCacheIdentity,
    DatgCoveredInspectionRequest,
    DatgFrequencyInspectionRequest,
    DatgGuidedGenerationRequest,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexedToken,
    DatgIndexPublication,
    DatgInspectionResult,
    DatgLogitDeltaPreviewRequest,
    DatgLogitDeltaPreviewResult,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgTargetInspectionRequest,
    DatgUnit,
    DatgUnitTokenSet,
)
from corpuskit.domain.errors import EngineUnavailableError, InvalidRequestError
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
    HostedModelSelection,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelQuantization,
    SecretReference,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlRuntimePolicyEntry,
    PhonRlSnapshotPin,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
)
from corpuskit.persistence.datg_cache import (
    ReadOnlyFilesystemDatgIndexCache,
    UnavailableDatgIndexCache,
    read_only_datg_cache_available,
)
from corpuskit.services.datg_catalog import DatgCatalogActor
from corpuskit.services.run_admission import ConfiguredRunAdmission, DenyAdvancedRunAdmission

REVISION = "a" * 40
DIGEST = "b" * 64


def _settings(**changes: object) -> Settings:
    local_pin = ImmutableModelPin(model="acme/tiny-model", revision=REVISION)
    datg_pin = DatgSnapshotPin(
        repository_id="acme/tiny-datg",
        revision=REVISION,
        snapshot_sha256=DIGEST,
    )
    rl_pin = PhonRlSnapshotPin(
        repository_id="acme/tiny-rl",
        revision=REVISION,
        snapshot_sha256=DIGEST,
    )
    values: dict[str, object] = {
        "environment": "test",
        "worker_hosted_model_policies": (
            HostedModelPolicy(
                provider="openai",
                model="openai/demo-model",
                connection_id="demo-provider",
                credential_ref=SecretReference(reference="secret://environment/provider-key"),
                input_cost_per_million_usd=Decimal("1"),
                output_cost_per_million_usd=Decimal("2"),
                max_output_tokens_per_request=128,
                request_delay_seconds=0.125,
            ),
        ),
        "worker_huggingface_repository_policies": (
            HuggingFaceRepositorySpec(
                dataset="acme/demo-corpus",
                config="default",
                split="train",
                text_column="text",
                revision=REVISION,
                language="en-us",
                max_samples=100,
            ),
        ),
        "worker_local_model_policies": (
            LocalModelPolicy(
                pin=local_pin,
                artifact_sha256=DIGEST,
                allowed_devices=(ModelDevice.CPU, ModelDevice.CUDA),
                allowed_quantizations=(ModelQuantization.NONE,),
                allow_phon_rl_adapters=True,
            ),
        ),
        "worker_datg_runtime_policies": (
            DatgRuntimePolicyEntry(
                runtime_id="tiny-datg",
                model=datg_pin,
                tokenizer=datg_pin,
                allowed_quantizations=(DatgQuantization.NONE,),
            ),
        ),
        "worker_phon_rl_runtime_policies": (
            PhonRlRuntimePolicyEntry(
                runtime_id="tiny-rl",
                model=rl_pin,
                tokenizer=rl_pin,
                cache_root_id="models-ro",
                cache_mount_read_only=True,
                allowed_prompt_strategies=("missing-units-v1",),
            ),
        ),
    }
    values.update(changes)
    return Settings.model_validate(values)


def _hosted() -> HostedGenerationRequest:
    return HostedGenerationRequest(
        selection=HostedModelSelection(
            provider="openai",
            model="openai/demo-model",
            connection_id="demo-provider",
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=2,
        ),
        max_tokens_per_request=64,
        external_processing_confirmed=True,
        activity_timeout_seconds=3,
    )


def _local() -> LocalGenerationRequest:
    return LocalGenerationRequest(
        selection=LocalModelSelection(
            pin=ImmutableModelPin(model="acme/tiny-model", revision=REVISION)
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=2,
        ),
        activity_timeout_seconds=3,
    )


def _analysis() -> LanguageModelAnalysisRequest:
    return LanguageModelAnalysisRequest(
        selection=_local().selection,
        texts=(AnalysisText(source_id="sentence-1", text="A complete sentence."),),
        activity_timeout_seconds=3,
    )


def _datg_generation() -> DatgGuidedGenerationRequest:
    return DatgGuidedGenerationRequest(
        runtime_id="tiny-datg",
        index_cache_key_sha256="c" * 64,
        target_phonemes=("p",),
        target_units=("p",),
        candidates=1,
        max_new_tokens=8,
        activity_timeout_seconds=3,
    )


def _training() -> PhonRlTrainingRequest:
    return PhonRlTrainingRequest(
        runtime_id="tiny-rl",
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(
            strategy_id="missing-units-v1",
            requested_prompts=1,
        ),
        parameters=PhonRlTrainingParameters(
            seed=7,
            num_steps=1,
            batch_size=1,
            max_new_tokens=8,
            activity_timeout_seconds=3,
        ),
    )


def _artifact() -> DatgIndexArtifact:
    identity = DatgCacheIdentity.create(
        tokenizer=DatgSnapshotPin(
            repository_id="acme/tiny-datg",
            revision=REVISION,
            snapshot_sha256=DIGEST,
        ),
        language="en-us",
        unit=DatgUnit.PHONEME,
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    return DatgIndexArtifact.create(
        identity=identity,
        vocabulary_size=1,
        unit_to_tokens=(DatgUnitTokenSet(unit="p", token_ids=(0,)),),
        token_units=(DatgIndexedToken(token_id=0, decoded_text="p", units=("p",)),),
    )


def _repository() -> RepositoryGenerationRequest:
    source_policy = _settings().worker_huggingface_repository_policies[0]
    return RepositoryGenerationRequest(
        source=HuggingFaceRepository(spec=source_policy),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1.0,
        ),
        activity_timeout_seconds=3,
    )


def test_configured_admission_validates_every_advanced_kind_and_denies_unknown_policy() -> None:
    policy = ConfiguredRunAdmission.from_settings(_settings())
    assert policy.model_runtime.worker_profile is WorkerModelProfile.LOCAL_GPU
    assert policy.huggingface_repositories == _settings().worker_huggingface_repository_policies
    requests = {
        RunKind.GENERATE_REPOSITORY: _repository(),
        RunKind.GENERATE_LLM: _hosted(),
        RunKind.GENERATE_LOCAL: _local(),
        RunKind.PERPLEXITY: _analysis(),
        RunKind.BUILD_DATG_INDEX: DatgIndexBuildRequest(
            runtime_id="tiny-datg", activity_timeout_seconds=3
        ),
        RunKind.GENERATE_DATG: _datg_generation(),
        RunKind.TRAIN_PHON_RL: _training(),
    }

    for kind, request in requests.items():
        policy.validate(kind, request.model_dump(mode="json"))

    with pytest.raises(InvalidRequestError) as denied:
        ConfiguredRunAdmission.from_settings(Settings(environment="test")).validate(
            RunKind.GENERATE_LLM,
            _hosted().model_dump(mode="json"),
        )
    assert denied.value.operation == "model_runtime.hosted.allowlist"
    with pytest.raises(InvalidRequestError) as secure_default_denial:
        DenyAdvancedRunAdmission().validate(RunKind.GENERATE_LOCAL, {})
    assert secure_default_denial.value.operation == "run.advanced.allowlist"
    DenyAdvancedRunAdmission().validate(RunKind.EVALUATE, {})
    for admission in (
        DenyAdvancedRunAdmission(),
        ConfiguredRunAdmission.from_settings(_settings()),
    ):
        with pytest.raises(InvalidRequestError) as unsupported:
            admission.validate(RunKind.EXPORT, {})
        assert unsupported.value.operation == "run.kind.unsupported"

    with pytest.raises(InvalidRequestError) as rl_denied:
        ConfiguredRunAdmission.from_settings(Settings(environment="test")).validate(
            RunKind.TRAIN_PHON_RL,
            _training().model_dump(mode="json"),
        )
    assert rl_denied.value.operation == "phon_rl.runtime.allowlist"


@pytest.mark.asyncio
async def test_mounted_advanced_routes_are_validation_only_and_catalog_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_calls: list[object] = []

    def forbidden_network(*args: object, **kwargs: object) -> None:
        network_calls.append((args, kwargs))
        raise AssertionError("control-plane validation must not open a network socket")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    application = create_app(_settings())
    paths = set(application.openapi()["paths"])
    assert {
        "/api/v1/advanced/capabilities",
        "/api/v1/model-runtime/hosted/validate",
        "/api/v1/model-runtime/hosted/estimate",
        "/api/v1/model-runtime/local/validate",
        "/api/v1/model-runtime/analysis/validate",
        "/api/v1/model-runtime/analysis/estimate",
        "/api/v1/datg/index/validate",
        "/api/v1/datg/generation/validate",
        "/api/v1/projects/{project_id}/datg/index/preview/logits",
        "/api/v1/phon-rl/training/validate",
        "/api/v1/phon-rl/training/estimate",
    } <= paths
    assert {
        "/api/v1/model-runtime/hosted/execute",
        "/api/v1/model-runtime/local/execute",
        "/api/v1/model-runtime/analysis/execute",
        "/api/v1/datg/index/build",
        "/api/v1/datg/generation/execute",
        "/api/v1/phon-rl/training/execute",
    }.isdisjoint(paths)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        catalog = await client.get("/api/v1/advanced/capabilities")
        hosted = await client.post(
            "/api/v1/model-runtime/hosted/validate",
            json=_hosted().model_dump(mode="json"),
        )
        estimate = await client.post(
            "/api/v1/model-runtime/hosted/estimate",
            json=_hosted().model_dump(mode="json"),
        )
        local = await client.post(
            "/api/v1/model-runtime/local/validate",
            json=_local().model_dump(mode="json"),
        )
        analysis = await client.post(
            "/api/v1/model-runtime/analysis/validate",
            json=_analysis().model_dump(mode="json"),
        )
        analysis_estimate = await client.post(
            "/api/v1/model-runtime/analysis/estimate",
            json=_analysis().model_dump(mode="json"),
        )
        datg = await client.post(
            "/api/v1/datg/generation/validate",
            json=_datg_generation().model_dump(mode="json"),
        )
        training = await client.post(
            "/api/v1/phon-rl/training/estimate",
            json=_training().model_dump(mode="json"),
        )
        absent_execution = await client.post(
            "/api/v1/phon-rl/training/execute",
            json=_training().model_dump(mode="json"),
        )

    for response in (
        catalog,
        hosted,
        estimate,
        local,
        analysis,
        analysis_estimate,
        datg,
        training,
    ):
        assert response.status_code == 200, response.text
    body = catalog.json()
    assert body["schema_id"] == "corpuskit.advanced-capabilities.v2"
    assert body["advanced_operation_routes_validation_only"] is True
    assert body["durable_run_submission_route"] == "/api/v1/runs"
    assert body["local_models"][0]["allow_phon_rl_adapters"] is True
    assert body["hosted_models"][0]["request_delay_seconds"] == 0.125
    assert "execution_routes_exposed" not in body
    assert body["datg_inspection"] == "unavailable"
    assert "credential" not in catalog.text.casefold()
    assert "secret://" not in catalog.text
    assert DIGEST not in catalog.text
    assert hosted.json()["network_during_validation"] is False
    assert hosted.json()["request_delay_seconds"] == 0.125
    assert estimate.json()["network_during_estimate"] is False
    assert estimate.json()["request_delay_seconds"] == 0.125
    assert analysis_estimate.json()["network_during_estimate"] is False
    assert absent_execution.status_code == 404
    assert network_calls == []


@pytest.mark.asyncio
async def test_unconfigured_policy_and_cache_fail_with_typed_redacted_errors() -> None:
    class UnavailableInspection:
        async def list(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            offset: int,
            limit: int,
        ) -> tuple[DatgIndexPublication, ...]:
            del actor, project_id, offset, limit
            raise EngineUnavailableError("datg.index.inspection_unavailable")

        async def target(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgTargetInspectionRequest,
        ) -> DatgInspectionResult:
            del actor, project_id, request
            raise EngineUnavailableError("datg.index.inspection_unavailable")

        async def covered(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgCoveredInspectionRequest,
        ) -> DatgInspectionResult:
            del actor, project_id, request
            raise EngineUnavailableError("datg.index.inspection_unavailable")

        async def frequency(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgFrequencyInspectionRequest,
        ) -> DatgInspectionResult:
            del actor, project_id, request
            raise EngineUnavailableError("datg.index.inspection_unavailable")

        async def preview_logits(
            self,
            actor: DatgCatalogActor,
            *,
            project_id: UUID,
            request: DatgLogitDeltaPreviewRequest,
        ) -> DatgLogitDeltaPreviewResult:
            del actor, project_id, request
            raise EngineUnavailableError("datg.index.inspection_unavailable")

    application = create_app(
        Settings(environment="test"),
        datg_inspection_service_factory=lambda _: UnavailableInspection(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        denied = await client.post(
            "/api/v1/model-runtime/hosted/validate",
            json=_hosted().model_dump(mode="json"),
        )
        cache = await client.post(
            "/api/v1/projects/00000000-0000-4000-8000-000000000003/datg/index/inspect/targets",
            json={"cache_key_sha256": "c" * 64, "target_units": ["p"]},
        )

    assert denied.status_code == 422
    assert denied.json()["operation"] == "model_runtime.hosted.allowlist"
    assert cache.status_code == 503
    assert cache.json()["operation"] == "datg.index.inspection_unavailable"
    assert "secret" not in cache.text.casefold()


def test_read_only_datg_cache_enforces_content_addressed_boundary(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    artifact = _artifact()
    key = artifact.identity.cache_key_sha256
    (root / f"{key}.json").write_text(artifact.model_dump_json(), encoding="utf-8")
    cache = ReadOnlyFilesystemDatgIndexCache(root.resolve())

    assert cache.get(key) == artifact
    assert cache.get("d" * 64) is None
    with pytest.raises(EngineUnavailableError) as malformed_key:
        cache.get("../private")
    assert malformed_key.value.operation == "datg.index.cache_key"

    wrong_key = "e" * 64
    (root / f"{wrong_key}.json").write_text(artifact.model_dump_json(), encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as identity:
        cache.get(wrong_key)
    assert identity.value.operation == "datg.index.cache_identity"

    malformed = "f" * 64
    (root / f"{malformed}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as invalid:
        cache.get(malformed)
    assert invalid.value.operation == "datg.index.cache"

    directory_key = "1" * 64
    (root / f"{directory_key}.json").mkdir()
    with pytest.raises(EngineUnavailableError) as boundary:
        cache.get(directory_key)
    assert boundary.value.operation == "datg.index.cache_boundary"


def test_catalog_reports_datg_inspection_only_for_present_read_only_root(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "cache").resolve()
    missing = advanced_capabilities(
        _settings(
            worker_datg_index_cache_root=root,
            worker_datg_cache_mount_read_only=True,
        )
    )
    assert missing.datg_inspection == "unavailable"

    root.mkdir()
    assert read_only_datg_cache_available(root, declared_read_only=True) is True
    configured = advanced_capabilities(
        _settings(
            worker_datg_index_cache_root=root,
            worker_datg_cache_mount_read_only=True,
        )
    )
    assert configured.datg_inspection == "configured_read_only"
    assert read_only_datg_cache_available(root, declared_read_only=False) is False


def test_read_only_datg_cache_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    artifact = _artifact()
    outside = tmp_path / "outside.json"
    outside.write_text(artifact.model_dump_json(), encoding="utf-8")
    key = artifact.identity.cache_key_sha256
    try:
        (root / f"{key}.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available on this test host")

    with pytest.raises(EngineUnavailableError) as boundary:
        ReadOnlyFilesystemDatgIndexCache(root.resolve()).get(key)
    assert boundary.value.operation == "datg.index.cache_boundary"


def test_read_only_datg_cache_rejects_untrusted_roots_and_oversized_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(EngineUnavailableError) as relative:
        ReadOnlyFilesystemDatgIndexCache(Path("relative")).get("c" * 64)
    assert "relative" not in str(relative.value)
    with pytest.raises(EngineUnavailableError) as missing:
        ReadOnlyFilesystemDatgIndexCache(tmp_path / "missing").get("c" * 64)
    assert str(tmp_path) not in str(missing.value)
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("no", encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as boundary:
        ReadOnlyFilesystemDatgIndexCache(root_file.resolve()).get("c" * 64)
    assert boundary.value.operation == "datg.index.cache_boundary"

    root = tmp_path / "cache"
    root.mkdir()
    artifact = _artifact()
    key = artifact.identity.cache_key_sha256
    (root / f"{key}.json").write_text(artifact.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("corpuskit.persistence.datg_cache.MAX_DATG_INDEX_BYTES", 1)
    with pytest.raises(EngineUnavailableError) as oversized:
        ReadOnlyFilesystemDatgIndexCache(root.resolve()).get(key)
    assert oversized.value.operation == "datg.index.cache_size"

    with pytest.raises(EngineUnavailableError):
        UnavailableDatgIndexCache().get("c" * 64)
