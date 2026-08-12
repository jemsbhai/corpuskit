"""End-to-end DATG build publication, tenancy, and reuse acceptance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from corpuskit.adapters.corpusgen.datg import CorpusgenDatgAdapter, SnapshotLocation
from corpuskit.adapters.corpusgen.model_runtime import compute_snapshot_digest
from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings
from corpuskit.domain.artifacts import StagedArtifactResult, staged_artifact_storage_key
from corpuskit.domain.datg import (
    DatgCacheIdentity,
    DatgGuidedGenerationRequest,
    DatgIndexArtifact,
    DatgIndexBuildRequest,
    DatgIndexBuildResult,
    DatgIndexedToken,
    DatgLogitDeltaPreviewRequest,
    DatgPhonemeSequence,
    DatgQuantization,
    DatgRuntimePolicyEntry,
    DatgSnapshotPin,
    DatgTargetInspectionRequest,
    DatgUnit,
    DatgUnitTokenSet,
)
from corpuskit.domain.errors import EngineUnavailableError, ResourceNotFoundError
from corpuskit.domain.jobs import RunKind, RunState
from corpuskit.domain.workspaces import ProjectInput
from corpuskit.persistence.artifact_store import InMemoryObjectStore
from corpuskit.persistence.database import Database
from corpuskit.persistence.datg_cache import (
    FilesystemDatgIndexPublisher,
    ReadOnlyFilesystemDatgIndexCache,
)
from corpuskit.persistence.models import DatgIndexPublicationRecord
from corpuskit.services.artifact_adoption import ArtifactAdoptionError, ArtifactAdoptionService
from corpuskit.services.datg_catalog import DatgCatalogActor, DatgIndexCatalogService
from corpuskit.services.jobs import DEMO_PROJECT_ID, JobActor, JobControlPlane, RunSubmission
from corpuskit.services.project_workspaces import ProjectWorkspaceService, WorkspaceActor
from corpuskit.services.run_admission import ConfiguredRunAdmission
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.store import DurableRunStore


def _policy() -> DatgRuntimePolicyEntry:
    pin = DatgSnapshotPin(
        repository_id="acme/tiny-datg",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    return DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=pin,
        tokenizer=pin,
        allowed_quantizations=(DatgQuantization.NONE,),
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'datg-publication.db').as_posix()}",
        artifact_max_bytes=36 * 1024 * 1024,
        worker_datg_runtime_policies=(_policy(),),
        _env_file=None,
    )


def _index(
    *,
    language: str = "en-us",
    vocabulary_size: int = 1,
    corpusgen_version: str = "0.1.7",
) -> DatgIndexArtifact:
    identity = DatgCacheIdentity.create(
        tokenizer=_policy().tokenizer,
        language=language,
        unit=DatgUnit.PHONEME,
        corpusgen_version=corpusgen_version,
        espeak_version="1.52.0",
    )
    return DatgIndexArtifact.create(
        identity=identity,
        vocabulary_size=vocabulary_size,
        unit_to_tokens=(DatgUnitTokenSet(unit="p", token_ids=(0,)),),
        token_units=(DatgIndexedToken(token_id=0, decoded_text="pea", units=("p",)),),
    )


def _generation(index: DatgIndexArtifact) -> dict[str, object]:
    return DatgGuidedGenerationRequest(
        runtime_id="tiny-datg",
        index_cache_key_sha256=index.identity.cache_key_sha256,
        language="en-us",
        unit=DatgUnit.PHONEME,
        target_phonemes=("p",),
        target_units=("p",),
        candidates=1,
        max_new_tokens=4,
        activity_timeout_seconds=10,
    ).model_dump(mode="json")


def _payload(index: DatgIndexArtifact) -> bytes:
    result = DatgIndexBuildResult(artifact=index, elapsed_seconds=0.01)
    return json.dumps(
        result.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


async def _stage(objects: InMemoryObjectStore, payload: bytes) -> dict[str, object]:
    digest = hashlib.sha256(payload).hexdigest()
    await objects.put(
        key=staged_artifact_storage_key(digest),
        content=payload,
        sha256=digest,
        media_type="application/json",
    )
    return StagedArtifactResult(
        staged_artifact_ref=f"staged-artifact://sha256/{digest}",
        schema_id="corpuskit.datg-index-build-result.v1",
        artifact_type="run-result",
        media_type="application/json",
        size_bytes=len(payload),
    ).model_dump(mode="json")


async def _build_run(
    jobs: JobControlPlane,
    actor: JobActor,
    *,
    project_id: UUID = DEMO_PROJECT_ID,
) -> RunWorkflowReference:
    submitted = await jobs.submit(
        actor,
        RunSubmission(
            project_id=project_id,
            kind=RunKind.BUILD_DATG_INDEX,
            spec=DatgIndexBuildRequest(
                runtime_id="tiny-datg",
                max_vocabulary_size=10,
                activity_timeout_seconds=10,
            ).model_dump(mode="json"),
        ),
        idempotency_key=f"datg-build-{uuid4()}",
    )
    reference = RunWorkflowReference(
        organization_id=str(actor.organization_id),
        run_id=str(submitted.run.id),
        spec_sha256=submitted.run.spec_sha256,
    )
    assert await DurableRunStore(jobs.database).begin_execution(reference) is True
    return reference


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_publication_closes_build_catalog_inspect_and_generation_loop(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    objects = InMemoryObjectStore()
    root = (tmp_path / "published-datg").resolve()
    root.mkdir()
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    jobs = JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings))
    await jobs.bootstrap_demo(actor, environment="test")
    reference = await _build_run(jobs, actor)
    index = _index()
    claim = await _stage(objects, _payload(index))
    runs = DurableRunStore(database)
    adopter = ArtifactAdoptionService(
        runs,
        objects,
        settings,
        datg_index_publisher=FilesystemDatgIndexPublisher(root),
        datg_runtime_versions=("0.1.7", "1.52.0"),
    )
    try:
        committed = await adopter.adopt(reference, claim)
        duplicate = await adopter.adopt(reference, claim)
        assert committed.state is RunState.SUCCEEDED
        assert committed.created is True
        assert duplicate.created is False

        cache = ReadOnlyFilesystemDatgIndexCache(root)
        assert cache.get(index.identity.cache_key_sha256) == index
        assert tuple(root.glob("*.json")) == (root / f"{index.identity.cache_key_sha256}.json",)
        async with database.session() as session:
            assert (
                await session.scalar(select(func.count()).select_from(DatgIndexPublicationRecord))
                == 1
            )

        preview_engine = CorpusgenDatgAdapter(
            corpusgen_version="0.1.7",
            espeak_version="1.52.0",
        )
        catalog = DatgIndexCatalogService(database, cache, preview_engine)
        catalog_actor = DatgCatalogActor(actor.subject, actor.organization_id)
        available = await catalog.list(catalog_actor, project_id=DEMO_PROJECT_ID)
        assert len(available) == 1
        assert available[0].cache_key_sha256 == index.identity.cache_key_sha256
        inspected = await catalog.target(
            catalog_actor,
            project_id=DEMO_PROJECT_ID,
            request=DatgTargetInspectionRequest(
                cache_key_sha256=index.identity.cache_key_sha256,
                target_units=("p",),
            ),
        )
        assert inspected.token_ids == (0,)
        preview = await catalog.preview_logits(
            catalog_actor,
            project_id=DEMO_PROJECT_ID,
            request=DatgLogitDeltaPreviewRequest(
                cache_key_sha256=index.identity.cache_key_sha256,
                target_phonemes=("p",),
                target_units=("p",),
                logits=((1.0,),),
            ),
        )
        assert preview.delta_logits == ((5.0,),)
        assert preview.modified_logits == ((6.0,),)
        assert preview.attribute_token_ids == (0,)
        assert preview.anti_attribute_token_ids == ()

        generation = await jobs.submit(
            actor,
            RunSubmission(
                project_id=DEMO_PROJECT_ID,
                kind=RunKind.GENERATE_DATG,
                spec=_generation(index),
            ),
            idempotency_key="authorized-datg-generation",
        )
        assert generation.run.state is RunState.QUEUED

        workspace = ProjectWorkspaceService(database, settings)
        other = await workspace.create_project(
            WorkspaceActor(actor.subject, actor.organization_id),
            ProjectInput(name="Other project"),
        )
        assert await catalog.list(catalog_actor, project_id=other.id) == ()
        with pytest.raises(ResourceNotFoundError):
            await catalog.target(
                catalog_actor,
                project_id=other.id,
                request=DatgTargetInspectionRequest(
                    cache_key_sha256=index.identity.cache_key_sha256,
                    target_units=("p",),
                ),
            )
        with pytest.raises(ResourceNotFoundError):
            await catalog.preview_logits(
                catalog_actor,
                project_id=other.id,
                request=DatgLogitDeltaPreviewRequest(
                    cache_key_sha256=index.identity.cache_key_sha256,
                    target_phonemes=("p",),
                    target_units=("p",),
                    logits=((1.0,),),
                ),
            )
        with pytest.raises(ResourceNotFoundError):
            await jobs.submit(
                actor,
                RunSubmission(
                    project_id=other.id,
                    kind=RunKind.GENERATE_DATG,
                    spec=_generation(index),
                ),
                idempotency_key="cross-project-datg-generation",
            )

        conflicting = _index(vocabulary_size=2)
        with pytest.raises(EngineUnavailableError) as conflict:
            FilesystemDatgIndexPublisher(root).publish(conflicting)
        assert conflict.value.operation == "datg.index.publication_conflict"
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_offline_transformers_index_publish_inspect_and_logit_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real HF tokenizer closes the local DATG build-to-preview acceptance path."""

    tokenizers = pytest.importorskip("tokenizers")
    transformers = pytest.importorskip("transformers")
    root = (tmp_path / "tiny-datg-cache").resolve()
    snapshot = root / "models--corpuskit--tiny-offline-datg" / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    vocabulary = {"<unk>": 0, "pea": 1, "bee": 2, "tea": 3}
    backend = tokenizers.Tokenizer(tokenizers.models.WordLevel(vocab=vocabulary, unk_token="<unk>"))
    backend.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tokenizer = transformers.PreTrainedTokenizerFast(
        tokenizer_object=backend,
        unk_token="<unk>",
    )
    tokenizer.save_pretrained(snapshot)
    (snapshot / "model.safetensors").write_bytes(b"safe offline DATG fixture")
    digest = compute_snapshot_digest(snapshot, approved_cache_root=root)
    exact_pin = DatgSnapshotPin(
        repository_id="corpuskit/tiny-offline-datg",
        revision="a" * 40,
        snapshot_sha256=digest,
    )
    runtime = DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=exact_pin,
        tokenizer=exact_pin,
        allowed_quantizations=(DatgQuantization.NONE,),
    )

    class ExactSnapshotResolver:
        def resolve(self, requested: DatgSnapshotPin) -> SnapshotLocation:
            assert requested == exact_pin
            return SnapshotLocation(snapshot=snapshot, approved_cache_root=root)

    def forbidden_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("offline DATG acceptance must not open a network socket")

    def forbidden_model_load(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("DATG index and logit preview must not load a language model")

    monkeypatch.setattr("socket.create_connection", forbidden_network)
    monkeypatch.setattr(
        transformers.AutoModelForCausalLM,
        "from_pretrained",
        forbidden_model_load,
    )
    engine = CorpusgenDatgAdapter(
        snapshot_resolver=ExactSnapshotResolver(),
        corpusgen_version="0.1.7",
        espeak_version="1.52.0",
    )
    built = engine.build_index(
        DatgIndexBuildRequest(
            runtime_id="tiny-datg",
            batch_size=2,
            max_vocabulary_size=len(vocabulary),
            activity_timeout_seconds=60,
        ),
        runtime,
    )
    index = built.artifact
    assert index.identity.tokenizer_snapshot_sha256 == digest
    assert index.vocabulary_size == len(vocabulary)
    assert index.indexed_token_count == 3
    assert {entry.unit for entry in index.unit_to_tokens} >= {"p", "b"}

    settings = Settings(
        environment="test",
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / 'real-datg-publication.db').as_posix()}"),
        artifact_max_bytes=36 * 1024 * 1024,
        worker_datg_runtime_policies=(runtime,),
        _env_file=None,
    )
    database = Database(settings.database_url)
    await database.create_schema()
    objects = InMemoryObjectStore()
    publication_root = (tmp_path / "real-published-datg").resolve()
    publication_root.mkdir()
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    jobs = JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings))
    await jobs.bootstrap_demo(actor, environment="test")
    reference = await _build_run(jobs, actor)
    claim = await _stage(objects, _payload(index))
    adopter = ArtifactAdoptionService(
        DurableRunStore(database),
        objects,
        settings,
        datg_index_publisher=FilesystemDatgIndexPublisher(publication_root),
        datg_runtime_versions=(
            index.identity.corpusgen_version,
            index.identity.espeak_version,
        ),
    )
    try:
        adopted = await adopter.adopt(reference, claim)
        assert adopted.state is RunState.SUCCEEDED
        cache = ReadOnlyFilesystemDatgIndexCache(publication_root)
        catalog = DatgIndexCatalogService(database, cache, engine)
        catalog_actor = DatgCatalogActor(actor.subject, actor.organization_id)
        inspected = await catalog.target(
            catalog_actor,
            project_id=DEMO_PROJECT_ID,
            request=DatgTargetInspectionRequest(
                cache_key_sha256=index.identity.cache_key_sha256,
                target_units=("p",),
            ),
        )
        assert inspected.token_ids

        logits = (tuple(float(token_id) for token_id in range(len(vocabulary))),)
        anti_token = next(record for record in index.token_units if "p" not in record.units)
        preview_targets = tuple(dict.fromkeys(("p", *anti_token.units)))
        preview = await catalog.preview_logits(
            catalog_actor,
            project_id=DEMO_PROJECT_ID,
            request=DatgLogitDeltaPreviewRequest(
                cache_key_sha256=index.identity.cache_key_sha256,
                target_phonemes=preview_targets,
                target_units=("p",),
                coverage_sequences=(DatgPhonemeSequence(phonemes=anti_token.units),),
                logits=logits,
            ),
        )
        assert preview.original_logits == logits
        assert preview.modified_logits == tuple(
            tuple(before + delta for before, delta in zip(row, deltas, strict=True))
            for row, deltas in zip(
                preview.original_logits,
                preview.delta_logits,
                strict=True,
            )
        )
        assert preview.attribute_token_ids == inspected.token_ids
        assert preview.attribute_token_ids
        assert preview.anti_attribute_token_ids
        assert all(
            preview.delta_logits[0][token_id] == 5.0 for token_id in preview.attribute_token_ids
        )
        assert all(
            preview.delta_logits[0][token_id] == -5.0
            for token_id in preview.anti_attribute_token_ids
        )
        assert preview.generation_executed is False
        assert preview.model_loaded is False
        assert preview.network_used is False
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "corpusgen_version"),
    [("fr-fr", "0.1.7"), ("en-us", "0.1.6")],
    ids=("language", "parent-runtime-version"),
)
async def test_parent_rejects_valid_but_run_mismatched_datg_index(
    tmp_path: Path,
    language: str,
    corpusgen_version: str,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.create_schema()
    objects = InMemoryObjectStore()
    root = (tmp_path / "published-datg").resolve()
    root.mkdir()
    actor = JobActor(DEMO_PRINCIPAL.subject, DEMO_PRINCIPAL.organization_id)
    jobs = JobControlPlane(database, ConfiguredRunAdmission.from_settings(settings))
    await jobs.bootstrap_demo(actor, environment="test")
    reference = await _build_run(jobs, actor)
    claim = await _stage(
        objects,
        _payload(_index(language=language, corpusgen_version=corpusgen_version)),
    )
    try:
        with pytest.raises(ArtifactAdoptionError) as mismatch:
            await ArtifactAdoptionService(
                DurableRunStore(database),
                objects,
                settings,
                datg_index_publisher=FilesystemDatgIndexPublisher(root),
                datg_runtime_versions=("0.1.7", "1.52.0"),
            ).adopt(reference, claim)
        assert (mismatch.value.code, mismatch.value.retryable) == (
            "datg_index_publication_policy",
            False,
        )
        assert not tuple(root.iterdir())
        assert (await jobs.get(actor, UUID(reference.run_id))).state is RunState.RUNNING
    finally:
        await database.dispose()
