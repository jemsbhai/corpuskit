from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import ValidationError

from corpuskit.domain.datg import (
    DatgCacheIdentity,
    DatgGeneratedCandidate,
    DatgGuidanceManifest,
    DatgGuidanceOptions,
    DatgGuidedGenerationRequest,
    DatgGuidedGenerationResult,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexBuildResult,
    DatgIndexedToken,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgUnit,
    DatgUnitTokenSet,
    DatgWorkerProfile,
)
from corpuskit.domain.errors import EngineContractError
from corpuskit.domain.jobs import RunKind, normalize_run_spec
from corpuskit.services.datg import DatgCoordinator, DatgRuntimePolicy
from corpuskit.worker.datg_handler import (
    BuildDatgIndexDurableHandler,
    GenerateDatgDurableHandler,
    build_datg_handler_registry,
    build_datg_handlers,
    datg_activity_timeout_seconds,
)
from corpuskit.workflows.deadlines import activity_deadline_seconds


def policy_entry() -> DatgRuntimePolicyEntry:
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


def artifact() -> DatgIndexArtifact:
    entry = policy_entry()
    identity = DatgCacheIdentity.create(
        tokenizer=entry.tokenizer,
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


def generation_request(value: DatgIndexArtifact) -> DatgGuidedGenerationRequest:
    return DatgGuidedGenerationRequest(
        runtime_id="tiny-datg",
        index_cache_key_sha256=value.identity.cache_key_sha256,
        target_phonemes=("p",),
        target_units=("p",),
        candidates=1,
        max_new_tokens=8,
        activity_timeout_seconds=11,
    )


class RecordingCoordinator:
    def __init__(self, profile: DatgWorkerProfile) -> None:
        self.policy = DatgRuntimePolicy((policy_entry(),), worker_profile=profile)
        self.value = artifact()

    def build_index(self, request: DatgIndexBuildRequest) -> DatgIndexBuildResult:
        assert request.runtime_id == "tiny-datg"
        return DatgIndexBuildResult(artifact=self.value, elapsed_seconds=0.1)

    def generate(self, request: DatgGuidedGenerationRequest) -> DatgGuidedGenerationResult:
        assert request.index_cache_key_sha256 == self.value.identity.cache_key_sha256
        entry = policy_entry()
        return DatgGuidedGenerationResult(
            manifest=DatgGuidanceManifest(
                runtime_id=entry.runtime_id,
                model_id=entry.model.repository_id,
                model_revision=entry.model.revision,
                model_snapshot_sha256=entry.model.snapshot_sha256,
                tokenizer_id=entry.tokenizer.repository_id,
                tokenizer_revision=entry.tokenizer.revision,
                tokenizer_snapshot_sha256=entry.tokenizer.snapshot_sha256,
                index_cache_key_sha256=self.value.identity.cache_key_sha256,
                index_content_sha256=self.value.content_sha256,
                language="en-us",
                unit=DatgUnit.PHONEME,
                guidance=DatgGuidanceOptions(),
                quantization=DatgQuantization.NONE,
                seed=0,
                sampling_enabled=False,
                corpusgen_version="0.1.7",
                espeak_version="1.52.0",
            ),
            candidates=(
                DatgGeneratedCandidate(
                    source_id=f"datg:{'a' * 48}",
                    text="Peas bloom.",
                    phonemes=("p",),
                ),
            ),
            attribute_token_ids=(0,),
            anti_attribute_token_ids=(),
            elapsed_seconds=0.2,
        )


class RecordingStager:
    def __init__(self, *, corrupt: bool = False) -> None:
        self.corrupt = corrupt
        self.calls: list[tuple[RunKind, bytes, str]] = []

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        self.calls.append((kind, payload, content_sha256))
        digest = "0" * 64 if self.corrupt else content_sha256
        return f"staged-artifact://sha256/{digest}"


def as_coordinator(value: RecordingCoordinator) -> DatgCoordinator:
    return cast(DatgCoordinator, value)


def test_build_and_generation_handlers_return_only_bounded_staged_summaries() -> None:
    stager = RecordingStager()
    cpu = RecordingCoordinator(DatgWorkerProfile.LOCAL_CPU)
    build = BuildDatgIndexDurableHandler(as_coordinator(cpu), stager)
    build_spec = DatgIndexBuildRequest(
        runtime_id="tiny-datg", activity_timeout_seconds=7
    ).model_dump(mode="json")
    build_summary = build.execute(build_spec)
    assert build.kind is RunKind.BUILD_DATG_INDEX
    assert build_summary == {
        "contract": "corpuskit.staged-artifact-result.v1",
        "staged_artifact_ref": f"staged-artifact://sha256/{stager.calls[0][2]}",
        "schema_id": "corpuskit.datg-index-build-result.v1",
        "artifact_type": "run-result",
        "media_type": "application/json",
        "size_bytes": len(stager.calls[0][1]),
    }

    gpu = RecordingCoordinator(DatgWorkerProfile.LOCAL_GPU)
    generation = GenerateDatgDurableHandler(as_coordinator(gpu), stager)
    generation_summary = generation.execute(generation_request(gpu.value).model_dump(mode="json"))
    assert generation.kind is RunKind.GENERATE_DATG
    assert generation_summary == {
        "contract": "corpuskit.staged-artifact-result.v1",
        "staged_artifact_ref": f"staged-artifact://sha256/{stager.calls[1][2]}",
        "schema_id": "corpuskit.datg-guided-generation-result.v1",
        "artifact_type": "run-result",
        "media_type": "application/json",
        "size_bytes": len(stager.calls[1][1]),
    }
    assert json.dumps(build_summary | generation_summary).__sizeof__() < 64 * 1024
    assert not {
        "artifact_id",
        "artifact_ref",
        "organization_id",
        "project_id",
        "run_id",
    }.intersection(build_summary | generation_summary)
    for kind, payload, digest in stager.calls:
        assert kind in {RunKind.BUILD_DATG_INDEX, RunKind.GENERATE_DATG}
        assert hashlib.sha256(payload).hexdigest() == digest
    assert DatgIndexBuildResult.model_validate_json(stager.calls[0][1]).artifact == cpu.value
    assert DatgGuidedGenerationResult.model_validate_json(stager.calls[1][1]).candidates[0].text


def test_staging_reference_is_content_bound_and_specs_fail_closed() -> None:
    coordinator = RecordingCoordinator(DatgWorkerProfile.LOCAL_CPU)
    handler = BuildDatgIndexDurableHandler(
        as_coordinator(coordinator), RecordingStager(corrupt=True)
    )
    with pytest.raises(EngineContractError) as error:
        handler.execute(DatgIndexBuildRequest(runtime_id="tiny-datg").model_dump(mode="json"))
    assert error.value.operation == "datg.staging.reference"
    with pytest.raises(ValidationError):
        handler.execute({"runtime_id": "tiny-datg", "unexpected": True})


def test_profile_specific_handler_builders_never_add_a_nested_process() -> None:
    cpu = RecordingCoordinator(DatgWorkerProfile.LOCAL_CPU)
    gpu = RecordingCoordinator(DatgWorkerProfile.LOCAL_GPU)
    stager = RecordingStager()
    cpu_handlers = build_datg_handlers("batch-cpu", as_coordinator(cpu), stager)
    gpu_handlers = build_datg_handlers("gpu-inference", as_coordinator(gpu), stager)
    assert [handler.kind for handler in cpu_handlers] == [RunKind.BUILD_DATG_INDEX]
    assert [handler.kind for handler in gpu_handlers] == [
        RunKind.GENERATE_DATG,
    ]
    registry = build_datg_handler_registry("batch-cpu", as_coordinator(cpu), stager)
    assert registry.kinds == frozenset({RunKind.BUILD_DATG_INDEX})
    with pytest.raises(RuntimeError, match="batch CPU"):
        build_datg_handlers("batch-cpu", as_coordinator(gpu), stager)
    with pytest.raises(RuntimeError, match="GPU"):
        build_datg_handlers("gpu-inference", as_coordinator(cpu), stager)
    with pytest.raises(RuntimeError, match="does not permit"):
        build_datg_handlers("external-provider", as_coordinator(cpu), stager)

    import corpuskit.worker.datg_handler as module

    source = inspect.getsource(module)
    assert "multiprocessing" not in source
    assert "ProcessExecutionRunner(" not in source


def test_run_kinds_normalization_and_parent_deadline_metadata_are_compatible() -> None:
    assert RunKind("build-datg-index") is RunKind.BUILD_DATG_INDEX
    assert RunKind("generate-datg") is RunKind.GENERATE_DATG
    build = DatgIndexBuildRequest(runtime_id="tiny-datg", activity_timeout_seconds=17).model_dump(
        mode="json"
    )
    generation = generation_request(artifact()).model_dump(mode="json")
    for kind, spec, expected in (
        (RunKind.BUILD_DATG_INDEX, build, 17.0),
        (RunKind.GENERATE_DATG, generation, 11.0),
    ):
        normalized, digest = normalize_run_spec(spec)
        assert normalized == spec
        assert len(digest) == 64
        assert datg_activity_timeout_seconds(kind, cast(Mapping[str, Any], spec)) == expected
        assert activity_deadline_seconds(kind, cast(Mapping[str, Any], spec)) == expected
    with pytest.raises(RuntimeError, match="no DATG"):
        datg_activity_timeout_seconds(RunKind.EVALUATE, {})


def test_only_adapter_module_imports_corpusgen_and_app_mounts_datg_lab() -> None:
    from corpuskit.api import app, datg_lab
    from corpuskit.domain import datg as domain
    from corpuskit.services import datg as services
    from corpuskit.worker import datg_handler

    for module in (domain, services, datg_handler, datg_lab):
        assert "import corpusgen" not in inspect.getsource(module)
        assert "from corpusgen" not in inspect.getsource(module)
    app_source = inspect.getsource(app)
    assert "datg_lab_router(" in app_source
    assert '"/datg/index/build"' not in inspect.getsource(datg_lab)
    assert '"/datg/generation/execute"' not in inspect.getsource(datg_lab)
    assert "build_datg_handlers" not in app_source
