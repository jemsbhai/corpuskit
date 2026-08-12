"""Adversarial tests for ephemeral parent-authorized worker inputs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.jobs import RunKind, RunState, canonical_spec_sha256
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
    PhonRlCheckpointCompatibility,
    PhonRlDynamicPromptSource,
    PhonRlPromptArtifact,
    PhonRlStaticPromptSource,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
)
from corpuskit.persistence.artifact_store import (
    ObjectDescriptor,
    ObjectStore,
    ObjectStoreError,
    ObjectStream,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact
from corpuskit.workflows.handlers import RunExecutionError
from corpuskit.workflows.store import ExecutionRecord
from corpuskit.workflows.trusted_inputs import (
    MaterializedCheckpointFile,
    MaterializedPeftManifest,
    TrustedPeftAdapterInput,
    TrustedPromptInput,
    TrustedRunInputMaterializer,
    TrustedRunInputs,
    claim_trusted_input,
    default_trusted_input_root,
    parse_trusted_run_inputs,
    read_materialized_peft_manifest,
    read_materialized_prompts,
)

_PIN = ImmutableModelPin(model="acme/tiny", revision="a" * 40)
_DIGEST = "b" * 64


def _prompt_input() -> TrustedPromptInput:
    return TrustedPromptInput(
        artifact_id=uuid4(),
        content_sha256="c" * 64,
        prompt_count=1,
    )


def _trusted_prompt(*, token: str = "d" * 64) -> TrustedRunInputs:
    return TrustedRunInputs(
        token=token,
        run_binding_sha256="e" * 64,
        spec_sha256="f" * 64,
        run_kind=RunKind.TRAIN_PHON_RL,
        prompt=_prompt_input(),
    )


def _compatibility() -> PhonRlCheckpointCompatibility:
    return PhonRlCheckpointCompatibility(
        base_model_id=_PIN.model,
        base_model_revision=_PIN.revision,
        base_model_snapshot_sha256=_DIGEST,
        tokenizer_id=_PIN.model,
        tokenizer_revision=_PIN.revision,
        tokenizer_snapshot_sha256=_DIGEST,
        corpusgen_version="0.1.7",
        torch_version="2.0",
        transformers_version="4.0",
        peft_version="0.1",
        peft_adapter=True,
    )


def _materializer(tmp_path: Path, *, chunk_bytes: int = 16 * 1024) -> TrustedRunInputMaterializer:
    return TrustedRunInputMaterializer(
        cast(Database, object()),
        cast(ObjectStore, object()),
        root=(tmp_path / "trusted").resolve(),
        local_policies=(
            LocalModelPolicy(
                pin=_PIN,
                artifact_sha256=_DIGEST,
                allowed_devices=(ModelDevice.CPU,),
                allowed_quantizations=(ModelQuantization.NONE,),
            ),
        ),
        chunk_bytes=chunk_bytes,
    )


def _record(kind: RunKind, spec: dict[str, Any]) -> ExecutionRecord:
    return ExecutionRecord(
        organization_id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        created_by=uuid4(),
        kind=kind,
        state=RunState.RUNNING,
        spec=spec,
        spec_sha256=canonical_spec_sha256(spec),
    )


def test_trusted_input_wire_parser_is_strict_and_json_native() -> None:
    trusted = _trusted_prompt()
    wire = trusted.model_dump(mode="json")

    assert parse_trusted_run_inputs(wire) == trusted

    wrong_kind = {**wire, "run_kind": RunKind.EVALUATE.value}
    with pytest.raises(ValidationError):
        parse_trusted_run_inputs(wrong_kind)

    not_json_native = {**wire, "token": {"not-json"}}
    with pytest.raises(ValueError, match="JSON-native envelope"):
        parse_trusted_run_inputs(not_json_native)

    with pytest.raises(ValueError, match="JSON-native envelope"):
        parse_trusted_run_inputs(trusted.model_dump(mode="python"))

    for unexpected in (
        {"unknown": "field"},
        {"api_key": "credential-like-value"},
        {"database_url": "postgresql://credential-like-value"},
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            parse_trusted_run_inputs({**wire, **unexpected})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "token": "d" * 64,
                "run_binding_sha256": "e" * 64,
                "spec_sha256": "f" * 64,
                "run_kind": RunKind.TRAIN_PHON_RL,
            },
            "requires one prompt artifact",
        ),
        (
            {
                "token": "d" * 64,
                "run_binding_sha256": "e" * 64,
                "spec_sha256": "f" * 64,
                "run_kind": RunKind.GENERATE_LOCAL,
                "prompt": _prompt_input(),
            },
            "requires one PEFT adapter",
        ),
        (
            {
                "token": "d" * 64,
                "run_binding_sha256": "e" * 64,
                "spec_sha256": "f" * 64,
                "run_kind": RunKind.PHONEMIZE,
            },
            "no trusted-input contract",
        ),
    ],
)
def test_trusted_envelope_is_exact_and_kind_bound(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        TrustedRunInputs.model_validate(payload)
    with pytest.raises(ValidationError, match="Extra inputs"):
        TrustedRunInputs.model_validate({**_trusted_prompt().model_dump(), "path": "secret"})


def test_materialized_manifest_rejects_ambiguous_or_unsafe_layouts() -> None:
    config = MaterializedCheckpointFile(path="adapter_config.json", size_bytes=2, sha256="1" * 64)
    weights = MaterializedCheckpointFile(
        path="adapter_model.safetensors", size_bytes=2, sha256="2" * 64
    )
    with pytest.raises(ValidationError, match="sorted and unique"):
        MaterializedPeftManifest(
            checkpoint_sha256="3" * 64,
            compatibility=_compatibility(),
            files=(weights, config),
        )
    with pytest.raises(ValidationError, match="require config and safetensors"):
        MaterializedPeftManifest(
            checkpoint_sha256="3" * 64,
            compatibility=_compatibility(),
            files=(
                config,
                MaterializedCheckpointFile(path="other.json", size_bytes=2, sha256="2" * 64),
            ),
        )
    with pytest.raises(ValidationError, match="file type is not permitted"):
        MaterializedPeftManifest(
            checkpoint_sha256="3" * 64,
            compatibility=_compatibility(),
            files=(
                config,
                weights,
                MaterializedCheckpointFile(path="notes.txt", size_bytes=2, sha256="4" * 64),
            ),
        )


def test_materializer_rejects_unbounded_roots_and_chunks_and_scrubs_old_orphans(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="bounded absolute"):
        TrustedRunInputMaterializer(
            cast(Database, object()),
            cast(ObjectStore, object()),
            root=Path("relative"),
            local_policies=(),
            chunk_bytes=16 * 1024,
        )
    with pytest.raises(ValueError, match="chunk size"):
        _materializer(tmp_path, chunk_bytes=1)

    root = (tmp_path / "trusted").resolve()
    root.mkdir()
    old = root / ("1" * 64)
    old.mkdir()
    recent = root / ("2" * 64)
    recent.mkdir()
    unrelated = root / "keep-me"
    unrelated.mkdir()
    old_time = time.time() - 49 * 60 * 60
    os.utime(old, (old_time, old_time))
    _materializer(tmp_path)
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()
    with pytest.raises(ValueError, match="not safe"):
        default_trusted_input_root("../unsafe")
    assert default_trusted_input_root("gpu-training").is_absolute()


@pytest.mark.asyncio
async def test_runs_without_artifact_selectors_do_not_create_trusted_state(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path)
    training = PhonRlTrainingRequest(
        runtime_id="tiny-rl",
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(seed=1, num_steps=1, batch_size=1),
    )
    async with materializer.materialize(
        _record(RunKind.TRAIN_PHON_RL, training.model_dump(mode="json"))
    ) as trusted:
        assert trusted is None
    local = LocalGenerationRequest(
        selection=LocalModelSelection(pin=_PIN),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(max_sentences=1, max_iterations=1),
    )
    async with materializer.materialize(
        _record(RunKind.GENERATE_LOCAL, local.model_dump(mode="json"))
    ) as trusted:
        assert trusted is None
    async with materializer.materialize(_record(RunKind.PHONEMIZE, {})) as trusted:
        assert trusted is None
    with pytest.raises(RunExecutionError) as invalid:
        async with materializer.materialize(_record(RunKind.GENERATE_LOCAL, {})):
            pass
    assert invalid.value.code == "invalid_run_spec"


@pytest.mark.asyncio
async def test_prompt_materialization_rejects_contract_drift_and_cleans_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materializer = _materializer(tmp_path)

    async def authorized(*_: object, **__: object) -> Artifact:
        return cast(Artifact, object())

    prompt_artifact = PhonRlPromptArtifact(prompts=("one prompt",))

    async def invalid_read(*_: object, **__: object) -> bytes:
        return b"{}"

    cast(Any, materializer)._authorized_artifact = authorized
    cast(Any, materializer)._read = invalid_read
    invalid_source = PhonRlStaticPromptSource(
        artifact_id=uuid4(),
        content_sha256=hashlib.sha256(b"{}").hexdigest(),
        prompt_count=1,
    )
    invalid_request = PhonRlTrainingRequest(
        runtime_id="tiny-rl",
        target_phonemes=("p",),
        prompt_source=invalid_source,
        parameters=PhonRlTrainingParameters(seed=1, num_steps=1, batch_size=1),
    )
    with pytest.raises(RunExecutionError) as invalid_contract:
        async with materializer.materialize(
            _record(RunKind.TRAIN_PHON_RL, invalid_request.model_dump(mode="json"))
        ):
            pass
    assert invalid_contract.value.code == "trusted_prompt_contract"

    pretty = json.dumps(prompt_artifact.model_dump(mode="json"), indent=2).encode()

    async def noncanonical_read(*_: object, **__: object) -> bytes:
        return pretty

    cast(Any, materializer)._read = noncanonical_read
    drift_source = PhonRlStaticPromptSource(
        artifact_id=uuid4(),
        content_sha256=hashlib.sha256(pretty).hexdigest(),
        prompt_count=1,
    )
    drift_request = invalid_request.model_copy(update={"prompt_source": drift_source})
    with pytest.raises(RunExecutionError) as drift:
        async with materializer.materialize(
            _record(RunKind.TRAIN_PHON_RL, drift_request.model_dump(mode="json"))
        ):
            pass
    assert drift.value.code == "trusted_prompt_integrity"

    async def valid_read(*_: object, **__: object) -> bytes:
        return prompt_artifact.canonical_bytes()

    cast(Any, materializer)._read = valid_read
    valid_source = PhonRlStaticPromptSource(
        artifact_id=uuid4(),
        content_sha256=prompt_artifact.sha256,
        prompt_count=1,
    )
    valid_request = invalid_request.model_copy(update={"prompt_source": valid_source})

    def fail_marker(*_: object, **__: object) -> None:
        raise OSError("simulated crash during marker creation")

    monkeypatch.setattr("corpuskit.workflows.trusted_inputs._write_ready_marker", fail_marker)
    with pytest.raises(OSError, match="simulated crash"):
        async with materializer.materialize(
            _record(RunKind.TRAIN_PHON_RL, valid_request.model_dump(mode="json"))
        ):
            pass
    assert tuple(materializer.root.iterdir()) == ()


@pytest.mark.asyncio
async def test_adapter_materialization_is_default_deny(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path)
    request = LocalGenerationRequest(
        selection=LocalModelSelection(pin=_PIN),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(max_sentences=1, max_iterations=1),
        phon_rl_adapter=PhonRlAdapterSelection(
            artifact_id=uuid4(),
            artifact_sha256="1" * 64,
            checkpoint_sha256="2" * 64,
        ),
    )
    with pytest.raises(RunExecutionError) as denied:
        async with materializer.materialize(
            _record(RunKind.GENERATE_LOCAL, request.model_dump(mode="json"))
        ):
            pass
    assert denied.value.code == "trusted_adapter_policy"


def test_claim_is_marker_exact_single_use_and_kind_bound(tmp_path: Path) -> None:
    root = (tmp_path / "claims").resolve()
    root.mkdir()
    trusted = _trusted_prompt()
    directory = root / trusted.token
    directory.mkdir()
    marker = directory / ".corpuskit-ready-v1.json"
    canonical = json.dumps(
        trusted.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    marker.write_bytes(b"tampered")
    with pytest.raises(RunExecutionError, match="trusted_input_claim"):
        claim_trusted_input(trusted, root=root, expected_kind=RunKind.TRAIN_PHON_RL)
    marker.write_bytes(canonical)
    with pytest.raises(RunExecutionError, match="trusted_input_claim"):
        claim_trusted_input(trusted, root=root, expected_kind=RunKind.GENERATE_LOCAL)
    assert claim_trusted_input(trusted, root=root, expected_kind=RunKind.TRAIN_PHON_RL) == directory
    with pytest.raises(RunExecutionError, match="trusted_input_claim"):
        claim_trusted_input(trusted, root=root, expected_kind=RunKind.TRAIN_PHON_RL)


def test_materialized_prompt_reader_reverifies_bytes_count_and_canonical_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path.resolve()
    artifact = PhonRlPromptArtifact(prompts=("one prompt",))
    content = artifact.canonical_bytes()
    path = directory / "prompts.json"
    path.write_bytes(content)
    trusted = TrustedPromptInput(
        artifact_id=uuid4(),
        content_sha256=artifact.sha256,
        prompt_count=1,
    )
    assert read_materialized_prompts(directory, trusted) == ("one prompt",)
    with pytest.raises(RunExecutionError, match="trusted_prompt_integrity"):
        read_materialized_prompts(directory, trusted.model_copy(update={"prompt_count": 2}))
    pretty = json.dumps(artifact.model_dump(mode="json"), indent=2).encode()
    path.write_bytes(pretty)
    with pytest.raises(RunExecutionError, match="trusted_prompt_integrity"):
        read_materialized_prompts(
            directory,
            trusted.model_copy(update={"content_sha256": hashlib.sha256(pretty).hexdigest()}),
        )
    path.write_bytes(content)
    with pytest.raises(RunExecutionError, match="trusted_prompt_integrity"):
        read_materialized_prompts(
            directory,
            trusted.model_copy(update={"content_sha256": "0" * 64}),
        )
    monkeypatch.setattr("corpuskit.workflows.trusted_inputs.MAX_RL_PROMPT_ARTIFACT_BYTES", 1)
    with pytest.raises(RunExecutionError, match="trusted_prompt_integrity"):
        read_materialized_prompts(directory, trusted)


def test_materialized_adapter_reader_reverifies_manifest_and_each_file(tmp_path: Path) -> None:
    directory = tmp_path.resolve()
    adapter_root = directory / "adapter"
    adapter_root.mkdir()
    config = b"{}"
    weights = b"safe"
    (adapter_root / "adapter_config.json").write_bytes(config)
    (adapter_root / "adapter_model.safetensors").write_bytes(weights)
    files = (
        MaterializedCheckpointFile(
            path="adapter_config.json",
            size_bytes=len(config),
            sha256=hashlib.sha256(config).hexdigest(),
        ),
        MaterializedCheckpointFile(
            path="adapter_model.safetensors",
            size_bytes=len(weights),
            sha256=hashlib.sha256(weights).hexdigest(),
        ),
    )
    manifest = MaterializedPeftManifest(
        checkpoint_sha256="3" * 64,
        compatibility=_compatibility(),
        files=files,
    )
    content = manifest.canonical_bytes()
    manifest_path = directory / "adapter-manifest.json"
    manifest_path.write_bytes(content)
    trusted = TrustedPeftAdapterInput(
        artifact_id=uuid4(),
        artifact_sha256="4" * 64,
        checkpoint_sha256=manifest.checkpoint_sha256,
        materialized_manifest_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert read_materialized_peft_manifest(directory, trusted) == (adapter_root, manifest)
    with pytest.raises(RunExecutionError, match="trusted_adapter_integrity"):
        read_materialized_peft_manifest(
            directory,
            trusted.model_copy(update={"materialized_manifest_sha256": "5" * 64}),
        )
    with pytest.raises(RunExecutionError, match="trusted_adapter_integrity"):
        read_materialized_peft_manifest(
            directory,
            trusted.model_copy(update={"checkpoint_sha256": "6" * 64}),
        )
    (adapter_root / "adapter_model.safetensors").write_bytes(b"evil")
    with pytest.raises(RunExecutionError, match="trusted_adapter_integrity"):
        read_materialized_peft_manifest(directory, trusted)
    (adapter_root / "adapter_model.safetensors").write_bytes(b"x")
    with pytest.raises(RunExecutionError, match="trusted_adapter_integrity"):
        read_materialized_peft_manifest(directory, trusted)


class _StreamStore:
    def __init__(
        self,
        descriptor: ObjectDescriptor,
        chunks: tuple[bytes, ...],
        *,
        fail: bool = False,
    ) -> None:
        self.descriptor = descriptor
        self.chunks = chunks
        self.fail = fail

    async def open(self, key: str, *, chunk_bytes: int) -> ObjectStream:
        del key, chunk_bytes
        if self.fail:
            raise ObjectStoreError("private store detail")

        async def stream() -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                yield chunk

        return ObjectStream(self.descriptor, stream())


@pytest.mark.asyncio
async def test_object_reader_rejects_descriptor_stream_and_store_failures(tmp_path: Path) -> None:
    content = b"canonical"
    digest = hashlib.sha256(content).hexdigest()
    artifact = cast(
        Artifact,
        SimpleNamespace(
            storage_key="objects/value",
            sha256=digest,
            size_bytes=len(content),
            media_type="application/json",
        ),
    )
    descriptor = ObjectDescriptor(
        key="objects/value",
        size_bytes=len(content),
        sha256=digest,
        media_type="application/json",
        modified_at=datetime.now(UTC),
    )
    materializer = _materializer(tmp_path)
    materializer._store = cast(ObjectStore, _StreamStore(descriptor, (content,)))
    assert await materializer._read(artifact, maximum_bytes=len(content)) == content

    common = {
        "key": descriptor.key,
        "size_bytes": descriptor.size_bytes,
        "sha256": descriptor.sha256,
        "media_type": descriptor.media_type,
        "modified_at": descriptor.modified_at,
    }
    for changed in (
        SimpleNamespace(**{**common, "sha256": "0" * 64}),
        SimpleNamespace(**{**common, "size_bytes": len(content) + 1}),
        SimpleNamespace(**{**common, "media_type": "text/plain"}),
    ):
        materializer._store = cast(ObjectStore, _StreamStore(cast(Any, changed), (content,)))
        with pytest.raises(RunExecutionError, match="trusted_input_integrity"):
            await materializer._read(artifact, maximum_bytes=len(content))

    materializer._store = cast(ObjectStore, _StreamStore(descriptor, (content, b"overflow")))
    with pytest.raises(RunExecutionError, match="trusted_input_integrity"):
        await materializer._read(artifact, maximum_bytes=len(content))
    materializer._store = cast(ObjectStore, _StreamStore(descriptor, (b"tampered!",)))
    with pytest.raises(RunExecutionError, match="trusted_input_integrity"):
        await materializer._read(artifact, maximum_bytes=len(content))
    materializer._store = cast(ObjectStore, _StreamStore(descriptor, (), fail=True))
    with pytest.raises(RunExecutionError) as unavailable:
        await materializer._read(artifact, maximum_bytes=len(content))
    assert unavailable.value.code == "trusted_input_store_unavailable"


def test_new_directory_is_collision_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materializer = _materializer(tmp_path)
    token = "9" * 64
    (materializer.root / token).mkdir()
    monkeypatch.setattr("corpuskit.workflows.trusted_inputs.secrets.token_hex", lambda _: token)
    with pytest.raises(RunExecutionError) as exhausted:
        materializer._new_directory()
    assert exhausted.value.code == "trusted_input_materialization"
