"""Ephemeral parent-authorized inputs for one killable worker child.

Public run specs contain only immutable artifact selectors. The parent reloads tenant and
project authority from PostgreSQL, verifies object bytes, and materializes a random one-use
directory. Only a bounded opaque claim crosses process IPC; paths and content never enter
durable run state, events, manifests, logs, or result summaries.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import select

from corpuskit.domain.artifacts import ArtifactKind, ArtifactState
from corpuskit.domain.corpus import FrozenDomainModel
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.model_runtime import LocalGenerationRequest, LocalModelPolicy
from corpuskit.domain.phon_rl import (
    MAX_RL_PROMPT_ARTIFACT_BYTES,
    MAX_RL_RESULT_BYTES,
    PhonRlCheckpointCompatibility,
    PhonRlPromptArtifact,
    PhonRlStaticPromptSource,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
)
from corpuskit.persistence.artifact_store import ObjectStore, ObjectStoreError
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import Artifact, Run
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.workflows.handlers import RunExecutionError
from corpuskit.workflows.store import ExecutionRecord

_TOKEN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_READY_MARKER = ".corpuskit-ready-v1.json"
_CLAIMED_MARKER = ".corpuskit-claimed-v1.json"
_PROMPT_FILE = "prompts.json"
_ADAPTER_DIRECTORY = "adapter"
_ADAPTER_MANIFEST = "adapter-manifest.json"
_ORPHAN_SECONDS = 48 * 60 * 60
_MAX_TRUSTED_ROOT_BYTES = 1_024


class _TrustedModel(FrozenDomainModel):
    """Strict internal model without introducing a second domain base class."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrustedPromptInput(_TrustedModel):
    artifact_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_count: int = Field(ge=1, le=10_000)


class TrustedPeftAdapterInput(_TrustedModel):
    artifact_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialized_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TrustedRunInputs(_TrustedModel):
    schema_id: Literal["corpuskit.trusted-run-inputs.v1"] = "corpuskit.trusted-run-inputs.v1"
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_kind: RunKind
    prompt: TrustedPromptInput | None = None
    peft_adapter: TrustedPeftAdapterInput | None = None

    @model_validator(mode="after")
    def exact_payload_for_kind(self) -> Self:
        if self.run_kind is RunKind.TRAIN_PHON_RL:
            if self.prompt is None or self.peft_adapter is not None:
                raise ValueError("Trusted Phon-RL training input requires one prompt artifact.")
        elif self.run_kind is RunKind.GENERATE_LOCAL:
            if self.peft_adapter is None or self.prompt is not None:
                raise ValueError("Trusted local generation input requires one PEFT adapter.")
        else:
            raise ValueError("This run kind has no trusted-input contract.")
        return self


def parse_trusted_run_inputs(value: Mapping[str, Any]) -> TrustedRunInputs:
    """Validate the JSON-native IPC envelope without relaxing strict model validation."""

    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError("Trusted run inputs are not a JSON-native envelope.") from None
    return TrustedRunInputs.model_validate_json(encoded, strict=True)


class MaterializedCheckpointFile(_TrustedModel):
    path: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    size_bytes: int = Field(ge=1, le=MAX_RL_RESULT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MaterializedPeftManifest(_TrustedModel):
    schema_id: Literal["corpuskit.materialized-peft-adapter.v1"] = (
        "corpuskit.materialized-peft-adapter.v1"
    )
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compatibility: PhonRlCheckpointCompatibility
    files: tuple[MaterializedCheckpointFile, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def safe_adapter_layout(self) -> Self:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("Materialized PEFT paths must be sorted and unique.")
        if "adapter_config.json" not in paths or "adapter_model.safetensors" not in paths:
            raise ValueError("Materialized PEFT adapters require config and safetensors weights.")
        if any(
            ".." in path.split("/") or not path.endswith((".json", ".safetensors"))
            for path in paths
        ):
            raise ValueError("Materialized PEFT adapter file type is not permitted.")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class TrustedRunInputMaterializer:
    """Parent-side tenant authorization and ephemeral object materialization."""

    def __init__(
        self,
        database: Database,
        store: ObjectStore,
        *,
        root: Path,
        local_policies: tuple[LocalModelPolicy, ...],
        chunk_bytes: int,
    ) -> None:
        if not root.is_absolute() or len(str(root).encode("utf-8")) > _MAX_TRUSTED_ROOT_BYTES:
            raise ValueError("Trusted input root must be a bounded absolute path.")
        if not 16 * 1024 <= chunk_bytes <= 8 * 1024 * 1024:
            raise ValueError("Trusted input chunk size is outside the reviewed bound.")
        self._database = database
        self._store = store
        self.root = root
        self._local_policies = local_policies
        self._chunk_bytes = chunk_bytes
        self._initialize_root()

    @asynccontextmanager
    async def materialize(
        self,
        record: ExecutionRecord,
    ) -> AsyncIterator[TrustedRunInputs | None]:
        prepared: tuple[TrustedRunInputs, Path] | None = None
        try:
            if record.kind is RunKind.TRAIN_PHON_RL:
                training_request = PhonRlTrainingRequest.model_validate(record.spec)
                if isinstance(training_request.prompt_source, PhonRlStaticPromptSource):
                    prepared = await self._materialize_prompts(
                        record,
                        training_request.prompt_source,
                    )
            elif record.kind is RunKind.GENERATE_LOCAL:
                local_request = LocalGenerationRequest.model_validate(record.spec)
                if local_request.phon_rl_adapter is not None:
                    prepared = await self._materialize_adapter(record, local_request)
            yield prepared[0] if prepared is not None else None
        except RunExecutionError:
            raise
        except ValueError:
            raise RunExecutionError("invalid_run_spec", retryable=False) from None
        finally:
            if prepared is not None:
                _remove_materialization(prepared[1], self.root)

    async def _materialize_prompts(
        self,
        record: ExecutionRecord,
        source: PhonRlStaticPromptSource,
    ) -> tuple[TrustedRunInputs, Path]:
        artifact = await self._authorized_artifact(
            record,
            source.artifact_id,
            expected_kind=ArtifactKind.PROMPT_SET,
            expected_sha256=source.content_sha256,
            source_run_kind=None,
        )
        content = await self._read(artifact, maximum_bytes=MAX_RL_PROMPT_ARTIFACT_BYTES)
        try:
            prompt_artifact = PhonRlPromptArtifact.model_validate_json(content, strict=True)
        except ValueError:
            raise RunExecutionError("trusted_prompt_contract", retryable=False) from None
        if (
            prompt_artifact.canonical_bytes() != content
            or prompt_artifact.sha256 != source.content_sha256
            or len(prompt_artifact.prompts) != source.prompt_count
        ):
            raise RunExecutionError("trusted_prompt_integrity", retryable=False)
        token, directory = self._new_directory()
        try:
            _write_new(directory / _PROMPT_FILE, content, mode=0o400)
            inputs = TrustedRunInputs(
                token=token,
                run_binding_sha256=_run_binding(record),
                spec_sha256=record.spec_sha256,
                run_kind=record.kind,
                prompt=TrustedPromptInput(
                    artifact_id=source.artifact_id,
                    content_sha256=source.content_sha256,
                    prompt_count=source.prompt_count,
                ),
            )
            _write_ready_marker(directory, inputs)
            return inputs, directory
        except Exception:
            _remove_materialization(directory, self.root)
            raise

    async def _materialize_adapter(
        self,
        record: ExecutionRecord,
        request: LocalGenerationRequest,
    ) -> tuple[TrustedRunInputs, Path]:
        selection = request.phon_rl_adapter
        assert selection is not None
        policy = next(
            (
                item
                for item in self._local_policies
                if item.pin == request.selection.pin and item.allow_phon_rl_adapters
            ),
            None,
        )
        if policy is None:
            raise RunExecutionError("trusted_adapter_policy", retryable=False)
        artifact = await self._authorized_artifact(
            record,
            selection.artifact_id,
            expected_kind=ArtifactKind.RUN_RESULT,
            expected_sha256=selection.artifact_sha256,
            source_run_kind=RunKind.TRAIN_PHON_RL,
        )
        content = await self._read(artifact, maximum_bytes=MAX_RL_RESULT_BYTES)
        try:
            result = PhonRlTrainingResult.model_validate_json(content, strict=True)
        except ValueError:
            raise RunExecutionError("trusted_adapter_contract", retryable=False) from None
        checkpoint = result.checkpoint
        compatibility = checkpoint.compatibility
        if (
            checkpoint.content_sha256 != selection.checkpoint_sha256
            or not compatibility.peft_adapter
            or result.peft_inference_status != "application_loader_ready"
            or compatibility.base_model_id != policy.pin.model
            or compatibility.base_model_revision != policy.pin.revision
            or compatibility.base_model_snapshot_sha256 != policy.artifact_sha256
            or compatibility.tokenizer_id != policy.pin.model
            or compatibility.tokenizer_revision != policy.pin.revision
            or compatibility.tokenizer_snapshot_sha256 != policy.artifact_sha256
            or compatibility.corpusgen_version != _version("corpusgen")
            or compatibility.torch_version != _version("torch")
            or compatibility.transformers_version != _version("transformers")
            or compatibility.peft_version != _version("peft")
        ):
            raise RunExecutionError("trusted_adapter_compatibility", retryable=False)
        selected_files = tuple(
            item
            for item in checkpoint.files
            if item.path in {"adapter_config.json", "adapter_model.safetensors"}
        )
        if len(selected_files) != 2:
            raise RunExecutionError("trusted_adapter_layout", retryable=False)
        token, directory = self._new_directory()
        adapter_root = directory / _ADAPTER_DIRECTORY
        try:
            adapter_root.mkdir(mode=0o700)
            descriptors: list[MaterializedCheckpointFile] = []
            for item in sorted(selected_files, key=lambda value: value.path):
                decoded = base64.b64decode(item.content_base64, validate=True)
                _write_new(adapter_root / item.path, decoded, mode=0o400)
                descriptors.append(
                    MaterializedCheckpointFile(
                        path=item.path,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                    )
                )
            manifest = MaterializedPeftManifest(
                checkpoint_sha256=checkpoint.content_sha256,
                compatibility=compatibility,
                files=tuple(descriptors),
            )
            manifest_bytes = manifest.canonical_bytes()
            _write_new(directory / _ADAPTER_MANIFEST, manifest_bytes, mode=0o400)
            adapter_root.chmod(0o500)
            inputs = TrustedRunInputs(
                token=token,
                run_binding_sha256=_run_binding(record),
                spec_sha256=record.spec_sha256,
                run_kind=record.kind,
                peft_adapter=TrustedPeftAdapterInput(
                    artifact_id=selection.artifact_id,
                    artifact_sha256=selection.artifact_sha256,
                    checkpoint_sha256=selection.checkpoint_sha256,
                    materialized_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                ),
            )
            _write_ready_marker(directory, inputs)
            return inputs, directory
        except Exception:
            _remove_materialization(directory, self.root)
            raise

    async def _authorized_artifact(
        self,
        record: ExecutionRecord,
        artifact_id: UUID,
        *,
        expected_kind: ArtifactKind,
        expected_sha256: str,
        source_run_kind: RunKind | None,
    ) -> Artifact:
        context = TenantContext.service(ServiceIdentity.WORKER, record.organization_id)
        async with self._database.session(context) as session:
            statement = select(Artifact).where(
                Artifact.id == artifact_id,
                Artifact.organization_id == record.organization_id,
                Artifact.project_id == record.project_id,
                Artifact.kind == expected_kind,
                Artifact.state == ArtifactState.ACTIVE,
                Artifact.sha256 == expected_sha256,
            )
            artifact = await session.scalar(statement)
            if artifact is None:
                raise RunExecutionError("trusted_input_not_authorized", retryable=False)
            if source_run_kind is not None:
                if artifact.run_id is None:
                    raise RunExecutionError("trusted_input_not_authorized", retryable=False)
                source_run = await session.scalar(
                    select(Run).where(
                        Run.id == artifact.run_id,
                        Run.organization_id == record.organization_id,
                        Run.project_id == record.project_id,
                        Run.kind == source_run_kind,
                        Run.state == RunState.SUCCEEDED,
                    )
                )
                if source_run is None:
                    raise RunExecutionError("trusted_input_not_authorized", retryable=False)
            session.expunge(artifact)
            return artifact

    async def _read(self, artifact: Artifact, *, maximum_bytes: int) -> bytes:
        try:
            stream = await self._store.open(artifact.storage_key, chunk_bytes=self._chunk_bytes)
            if (
                stream.descriptor.sha256 != artifact.sha256
                or stream.descriptor.size_bytes != artifact.size_bytes
                or stream.descriptor.media_type != artifact.media_type
                or artifact.size_bytes > maximum_bytes
            ):
                raise RunExecutionError("trusted_input_integrity", retryable=False)
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            size = 0
            async for chunk in stream.chunks:
                size += len(chunk)
                if size > maximum_bytes:
                    raise RunExecutionError("trusted_input_integrity", retryable=False)
                digest.update(chunk)
                chunks.append(chunk)
            if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                raise RunExecutionError("trusted_input_integrity", retryable=False)
            return b"".join(chunks)
        except RunExecutionError:
            raise
        except ObjectStoreError:
            raise RunExecutionError("trusted_input_store_unavailable", retryable=True) from None

    def _new_directory(self) -> tuple[str, Path]:
        for _ in range(8):
            token = secrets.token_hex(32)
            directory = self.root / token
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                continue
            return token, directory
        raise RunExecutionError("trusted_input_materialization", retryable=True)

    def _initialize_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = self.root.resolve(strict=True)
        if not resolved.is_dir() or resolved != self.root.resolve():
            raise ValueError("Trusted input root is not a stable directory.")
        self.root.chmod(0o700)
        cutoff = time.time() - _ORPHAN_SECONDS
        for child in tuple(self.root.iterdir()):
            if (
                child.is_dir()
                and _TOKEN.fullmatch(child.name) is not None
                and child.stat().st_mtime < cutoff
            ):
                _remove_materialization(child, self.root)


def default_trusted_input_root(profile: str) -> Path:
    if re.fullmatch(r"[a-z][a-z0-9-]{1,63}", profile, re.ASCII) is None:
        raise ValueError("Worker profile is not safe for a trusted input root.")
    return Path(tempfile.gettempdir()).resolve() / "corpuskit-trusted-inputs-v1" / profile


def claim_trusted_input(
    inputs: TrustedRunInputs,
    *,
    root: Path,
    expected_kind: RunKind,
) -> Path:
    """Atomically consume one parent-created claim and return its confined directory."""

    if inputs.run_kind is not expected_kind or _TOKEN.fullmatch(inputs.token) is None:
        raise RunExecutionError("trusted_input_claim", retryable=False)
    try:
        trusted_root = root.resolve(strict=True)
        directory = (trusted_root / inputs.token).resolve(strict=True)
        if directory.parent != trusted_root or directory.name != inputs.token:
            raise RunExecutionError("trusted_input_claim", retryable=False)
        ready = directory / _READY_MARKER
        claimed = directory / _CLAIMED_MARKER
        raw = ready.read_bytes()
        if raw != _canonical(inputs.model_dump(mode="json")):
            raise RunExecutionError("trusted_input_claim", retryable=False)
        os.replace(ready, claimed)
        return directory
    except RunExecutionError:
        raise
    except (OSError, ValueError):
        raise RunExecutionError("trusted_input_claim", retryable=False) from None


def read_materialized_prompts(
    directory: Path,
    trusted: TrustedPromptInput,
) -> tuple[str, ...]:
    try:
        path = (directory / _PROMPT_FILE).resolve(strict=True)
        if (
            path.parent != directory.resolve(strict=True)
            or path.stat().st_size > MAX_RL_PROMPT_ARTIFACT_BYTES
        ):
            raise ValueError
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != trusted.content_sha256:
            raise ValueError
        artifact = PhonRlPromptArtifact.model_validate_json(content, strict=True)
        if artifact.canonical_bytes() != content or len(artifact.prompts) != trusted.prompt_count:
            raise ValueError
        return artifact.prompts
    except (OSError, ValueError):
        raise RunExecutionError("trusted_prompt_integrity", retryable=False) from None


def read_materialized_peft_manifest(
    directory: Path,
    trusted: TrustedPeftAdapterInput,
) -> tuple[Path, MaterializedPeftManifest]:
    try:
        root = directory.resolve(strict=True)
        manifest_path = (root / _ADAPTER_MANIFEST).resolve(strict=True)
        adapter_root = (root / _ADAPTER_DIRECTORY).resolve(strict=True)
        if manifest_path.parent != root or adapter_root.parent != root or not adapter_root.is_dir():
            raise ValueError
        content = manifest_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != trusted.materialized_manifest_sha256:
            raise ValueError
        manifest = MaterializedPeftManifest.model_validate_json(content, strict=True)
        if (
            manifest.canonical_bytes() != content
            or manifest.checkpoint_sha256 != trusted.checkpoint_sha256
        ):
            raise ValueError
        for item in manifest.files:
            path = (adapter_root / item.path).resolve(strict=True)
            if (
                path.parent != adapter_root
                or not path.is_file()
                or path.stat().st_size != item.size_bytes
            ):
                raise ValueError
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item.sha256:
                raise ValueError
        return adapter_root, manifest
    except (OSError, ValueError):
        raise RunExecutionError("trusted_adapter_integrity", retryable=False) from None


def _run_binding(record: ExecutionRecord) -> str:
    value = {
        "kind": record.kind.value,
        "organization_id": str(record.organization_id),
        "project_id": str(record.project_id),
        "run_id": str(record.run_id),
        "spec_sha256": record.spec_sha256,
    }
    return hashlib.sha256(_canonical(value)).hexdigest()


def _write_ready_marker(directory: Path, inputs: TrustedRunInputs) -> None:
    _write_new(directory / _READY_MARKER, _canonical(inputs.model_dump(mode="json")), mode=0o400)


def _write_new(path: Path, content: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    path.chmod(mode)


def _remove_materialization(directory: Path, root: Path) -> None:
    try:
        trusted_root = root.resolve(strict=True)
        if (
            directory.parent.resolve(strict=True) != trusted_root
            or _TOKEN.fullmatch(directory.name) is None
        ):
            return
        if directory.is_symlink():
            directory.unlink(missing_ok=True)
            return
        candidate = directory.resolve(strict=True)
        if candidate.parent != trusted_root:
            return
        for path in sorted(candidate.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            with suppress(OSError):
                path.chmod(0o700 if path.is_dir() else 0o600)
        candidate.chmod(0o700)
        shutil.rmtree(candidate)
    except (FileNotFoundError, OSError, RuntimeError):
        return


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        raise RunExecutionError("trusted_adapter_dependency", retryable=False) from None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "MaterializedPeftManifest",
    "TrustedPeftAdapterInput",
    "TrustedPromptInput",
    "TrustedRunInputMaterializer",
    "TrustedRunInputs",
    "claim_trusted_input",
    "default_trusted_input_root",
    "parse_trusted_run_inputs",
    "read_materialized_peft_manifest",
    "read_materialized_prompts",
]
