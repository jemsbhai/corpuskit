"""Authoritative run-manifest construction and durable replay coordination."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit import __version__ as corpuskit_version
from corpuskit.config import Settings
from corpuskit.domain.analysis import (
    CoverageTrajectoryRequest,
    DistributionAnalysisRequest,
    ErrorRatesAnalysisRequest,
)
from corpuskit.domain.artifacts import (
    ArtifactKind,
    ArtifactState,
    ContentDigest,
    ReplayComparison,
    RunManifest,
    StopReason,
    artifact_storage_key,
    compare_replay,
)
from corpuskit.domain.datg import DatgGuidedGenerationRequest, DatgIndexBuildRequest
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from corpuskit.domain.generation import (
    HuggingFaceRepository,
    RawTextRepository,
    RepositoryGenerationRequest,
)
from corpuskit.domain.jobs import RunKind, RunState, normalize_result_summary, normalize_run_spec
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
)
from corpuskit.domain.phon_rl import PhonRlTrainingRequest
from corpuskit.domain.platform import AuditAction, AuditResourceType
from corpuskit.domain.reproducibility import (
    ReplayLifecycle,
    ReplayStatus,
    TrustedExecutionFacts,
)
from corpuskit.domain.workspaces import ProjectLifecycle
from corpuskit.persistence.artifact_store import (
    ObjectIntegrityError,
    ObjectStore,
    ObjectStoreError,
)
from corpuskit.persistence.database import Database
from corpuskit.persistence.models import (
    Artifact,
    Corpus,
    CorpusVersion,
    Membership,
    OutboxMessage,
    OutboxState,
    Project,
    Role,
    Run,
    RunEvent,
    RunExecutionFact,
    RunReplay,
    Sentence,
    User,
)
from corpuskit.persistence.tenant_context import ServiceIdentity, TenantContext
from corpuskit.services.platform import AuditIdentity, AuditWriter, QuotaManager
from corpuskit.services.project_lifecycle import lock_project_lifecycle
from corpuskit.services.run_admission import DenyAdvancedRunAdmission, RunAdmissionPolicy
from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.handlers import EvaluateRunSpec, PhonemizeRunSpec, SelectRunSpec

_WRITER_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.EDITOR})
_OUTPUT_KINDS = frozenset(
    {
        ArtifactKind.RUN_RESULT,
        ArtifactKind.EVALUATION_REPORT,
        ArtifactKind.EXPORT,
        ArtifactKind.CHECKPOINT,
        ArtifactKind.MODEL_ADAPTER,
    }
)
_MODEL_KINDS = frozenset(
    {
        RunKind.PERPLEXITY,
        RunKind.GENERATE_LLM,
        RunKind.GENERATE_LOCAL,
        RunKind.GENERATE_DATG,
        RunKind.TRAIN_PHON_RL,
    }
)


class ReproducibilityError(RuntimeError):
    """A stable parent-only failure code without paths, content, or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReproducibilityActor:
    subject: str
    organization_id: UUID
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ManifestCreation:
    manifest: RunManifest
    artifact_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class ReplayCreation:
    replay: ReplayStatus
    created: bool


@dataclass(frozen=True, slots=True)
class _ArtifactRecord:
    id: UUID
    organization_id: UUID
    project_id: UUID
    run_id: UUID | None
    kind: str
    sha256: str
    size_bytes: int
    storage_key: str
    media_type: str
    filename: str
    state: ArtifactState


@dataclass(frozen=True, slots=True)
class _ManifestBuild:
    organization_id: UUID
    project_id: UUID
    run_id: UUID
    created_by: UUID
    spec_sha256: str
    facts_sha256: str
    manifest: RunManifest | None
    verified_artifacts: tuple[_ArtifactRecord, ...]
    replay_source: _ArtifactRecord | None


class RunManifestService:
    """Persist worker facts, construct canonical manifests, and coordinate replays."""

    def __init__(
        self,
        database: Database,
        store: ObjectStore,
        settings: Settings,
        *,
        worker_database: Database | None = None,
        adoption_database: Database | None = None,
        admission_policy: RunAdmissionPolicy | None = None,
        expected_corpuskit_version: str | None = None,
        expected_corpusgen_version: str | None = None,
    ) -> None:
        self.database = database
        self._worker_database = worker_database or database
        self._adoption_database = adoption_database or database
        self._admission_policy = admission_policy or DenyAdvancedRunAdmission()
        self._store = store
        self._max_bytes = settings.artifact_max_bytes
        self._chunk_bytes = settings.artifact_download_chunk_bytes
        self._retention = timedelta(days=settings.artifact_retention_days)
        self._corpuskit_version = expected_corpuskit_version or corpuskit_version
        self._corpusgen_version = expected_corpusgen_version or _installed_version("corpusgen")

    async def record_execution(
        self,
        reference: RunWorkflowReference,
        facts: TrustedExecutionFacts,
    ) -> bool:
        """Record immutable parent-authored facts immediately before child computation."""

        organization_id, run_id = _reference_ids(reference)
        self._validate_runtime_facts(facts)
        context = TenantContext.service(ServiceIdentity.WORKER, organization_id)
        async with self._worker_database.session(context) as session:
            run = await _run(session, organization_id, run_id, for_update=True)
            _verify_reference(run, reference)
            if run.state is not RunState.RUNNING:
                raise ReproducibilityError("execution_facts_invalid_state")
            self._validate_run_facts(run, facts)
            inputs, input_artifacts = await self._authoritative_inputs(session, run, facts)

        for artifact in input_artifacts:
            await self._read_verified(artifact)

        async with self._worker_database.session(context) as session:
            run = await _run(session, organization_id, run_id, for_update=True)
            _verify_reference(run, reference)
            if run.state is not RunState.RUNNING:
                raise ReproducibilityError("execution_facts_invalid_state")
            self._validate_run_facts(run, facts)
            await self._validate_input_artifacts(session, run, inputs)
            values = {
                "run_id": run.id,
                "organization_id": run.organization_id,
                "project_id": run.project_id,
                "facts": facts.model_dump(mode="json"),
                "facts_sha256": facts.sha256,
                "input_digests": inputs,
            }
            statement = _insert_for(session, RunExecutionFact).values(**values)
            inserted = await session.scalar(
                statement.on_conflict_do_nothing(
                    index_elements=[RunExecutionFact.run_id]
                ).returning(RunExecutionFact.run_id)
            )
            if inserted is not None:
                return True
            existing = await session.scalar(
                select(RunExecutionFact).where(
                    RunExecutionFact.run_id == run.id,
                    RunExecutionFact.organization_id == organization_id,
                )
            )
            if existing is None or not _same_facts(existing, facts, inputs):
                raise ReproducibilityError("execution_facts_conflict")
            return False

    async def finalize(self, reference: RunWorkflowReference) -> ManifestCreation:
        """Construct and persist a manifest from durable facts only."""

        organization_id, run_id = _reference_ids(reference)
        context = TenantContext.service(ServiceIdentity.ADOPTION, organization_id)
        async with self._adoption_database.session(context) as session:
            build, existing = await self._manifest_build(session, reference)
        if existing is not None:
            manifest = await self._load_manifest(existing, run_id=run_id)
            await self._complete_replay_if_needed(reference, existing, manifest)
            return ManifestCreation(manifest, existing.id, created=False)

        if build.manifest is None:  # pragma: no cover - internal invariant
            raise ReproducibilityError("manifest_build_failed")

        for artifact in build.verified_artifacts:
            await self._read_verified(artifact)
        expected_manifest: RunManifest | None = None
        if build.replay_source is not None:
            expected_manifest = await self._load_manifest(
                build.replay_source,
                run_id=build.replay_source.run_id,
            )

        content = build.manifest.canonical_bytes()
        sha256 = build.manifest.sha256
        key = artifact_storage_key(
            organization_id=build.organization_id,
            project_id=build.project_id,
            run_id=build.run_id,
            kind=ArtifactKind.RUN_MANIFEST,
            sha256=sha256,
        )
        try:
            await self._store.put(
                key=key,
                content=content,
                sha256=sha256,
                media_type="application/json",
            )
        except (ObjectStoreError, ValueError) as exc:
            raise DependencyUnavailableError("manifest.create") from exc

        async with self._adoption_database.session(context) as session:
            run = await _run(session, organization_id, run_id, for_update=True)
            _verify_reference(run, reference)
            if run.state is not RunState.SUCCEEDED:
                raise ReproducibilityError("manifest_invalid_state")
            recorded = await session.scalar(
                select(RunExecutionFact)
                .where(
                    RunExecutionFact.run_id == run.id,
                    RunExecutionFact.organization_id == organization_id,
                )
                .with_for_update()
            )
            if recorded is None or recorded.facts_sha256 != build.facts_sha256:
                raise ReproducibilityError("execution_facts_integrity_violation")
            if recorded.manifest_artifact_id is not None:
                artifact = await _active_artifact(
                    session,
                    organization_id,
                    recorded.manifest_artifact_id,
                    ArtifactKind.RUN_MANIFEST,
                )
                return ManifestCreation(build.manifest, artifact.id, created=False)
            await self._validate_input_artifacts(session, run, list(recorded.input_digests))
            await self._validate_outputs(session, run, build.verified_artifacts)

            artifact_id = uuid4()
            values = {
                "id": artifact_id,
                "organization_id": run.organization_id,
                "project_id": run.project_id,
                "run_id": run.id,
                "created_by": run.created_by,
                "scope_key": str(run.id),
                "kind": ArtifactKind.RUN_MANIFEST.value,
                "sha256": sha256,
                "size_bytes": len(content),
                "storage_key": key,
                "media_type": "application/json",
                "filename": f"run-manifest-{run.id}.json",
                "state": ArtifactState.ACTIVE,
                "retention_until": datetime.now(UTC) + self._retention,
            }
            statement = _insert_for(session, Artifact).values(**values)
            inserted = await session.scalar(
                statement.on_conflict_do_nothing(
                    index_elements=[
                        Artifact.organization_id,
                        Artifact.project_id,
                        Artifact.scope_key,
                        Artifact.kind,
                        Artifact.sha256,
                    ]
                ).returning(Artifact.id)
            )
            created = inserted is not None
            if inserted is None:
                artifact = await session.scalar(
                    select(Artifact).where(
                        Artifact.organization_id == run.organization_id,
                        Artifact.project_id == run.project_id,
                        Artifact.run_id == run.id,
                        Artifact.kind == ArtifactKind.RUN_MANIFEST.value,
                        Artifact.sha256 == sha256,
                    )
                )
                if artifact is None or not _same_manifest_artifact(
                    artifact,
                    key=key,
                    size_bytes=len(content),
                ):
                    raise ReproducibilityError("manifest_metadata_conflict")
                artifact_id = artifact.id
            else:
                await QuotaManager.consume_artifact(
                    session,
                    organization_id=run.organization_id,
                    kind=ArtifactKind.RUN_MANIFEST,
                    size_bytes=len(content),
                )
                await AuditWriter.append(
                    session,
                    organization_id=run.organization_id,
                    actor=AuditIdentity.service(ServiceIdentity.ADOPTION),
                    action=AuditAction.RUN_MANIFEST_CREATED,
                    resource_type=AuditResourceType.ARTIFACT,
                    resource_id=artifact_id,
                    metadata={"sha256": sha256, "size_bytes": len(content)},
                )
            recorded.manifest_artifact_id = artifact_id
            recorded.manifest_sha256 = sha256
            recorded.finalized_at = datetime.now(UTC)
            await self._commit_replay_comparison(
                session,
                run=run,
                manifest_artifact_id=artifact_id,
                observed=build.manifest,
                expected=expected_manifest,
            )
            await session.flush()
            return ManifestCreation(build.manifest, artifact_id, created=created)

    async def submit_replay(
        self,
        actor: ReproducibilityActor,
        *,
        project_id: UUID,
        source_run_id: UUID,
        idempotency_key: str,
    ) -> ReplayCreation:
        """Submit the exact source recipe with quota, outbox, lineage, and audit atomically."""

        key = _idempotency_key(idempotency_key)
        context = _user_context(actor)
        async with self.database.session(context) as session:
            _, role = await _actor(session, actor, "replay.submit")
            if role not in _WRITER_ROLES:
                raise ResourceNotFoundError("replay.submit")
            source, facts, source_artifact = await _source_manifest_rows(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                source_run_id=source_run_id,
            )
        expected = await self._load_manifest(source_artifact, run_id=source.id)

        async with self.database.session(context) as session:
            user_id, role = await _actor(session, actor, "replay.submit")
            if role not in _WRITER_ROLES:
                raise ResourceNotFoundError("replay.submit")
            source, facts, source_artifact = await _source_manifest_rows(
                session,
                organization_id=actor.organization_id,
                project_id=project_id,
                source_run_id=source_run_id,
                for_update=True,
            )
            if (
                source_artifact.sha256 != expected.sha256
                or facts.manifest_sha256 != expected.sha256
            ):
                raise ReproducibilityError("source_manifest_integrity_violation")
            existing = await session.scalar(
                select(Run).where(
                    Run.organization_id == actor.organization_id,
                    Run.idempotency_key == key,
                )
            )
            if existing is not None:
                replay = await self._same_replay(
                    session,
                    existing,
                    source=source,
                    source_artifact=source_artifact,
                    expected=expected,
                )
                return ReplayCreation(_replay_status(replay, existing), created=False)

            try:
                self._admission_policy.validate(source.kind, dict(source.spec))
            except (TypeError, ValueError) as exc:
                raise InvalidRequestError("replay.submit") from exc

            replay_run_id = uuid4()
            values = {
                "id": replay_run_id,
                "organization_id": actor.organization_id,
                "project_id": source.project_id,
                "corpus_version_id": source.corpus_version_id,
                "parent_run_id": source.id,
                "created_by": user_id,
                "kind": source.kind,
                "state": RunState.QUEUED,
                "idempotency_key": key,
                "attempt": source.attempt + 1,
                "event_sequence": 1,
                "spec": dict(source.spec),
                "spec_sha256": source.spec_sha256,
            }
            statement = _insert_for(session, Run).values(**values)
            inserted = await session.scalar(
                statement.on_conflict_do_nothing(
                    index_elements=[Run.organization_id, Run.idempotency_key]
                ).returning(Run.id)
            )
            if inserted is None:
                existing = await session.scalar(
                    select(Run).where(
                        Run.organization_id == actor.organization_id,
                        Run.idempotency_key == key,
                    )
                )
                if existing is None:
                    raise ResourceConflictError("replay.submit")
                replay = await self._same_replay(
                    session,
                    existing,
                    source=source,
                    source_artifact=source_artifact,
                    expected=expected,
                )
                return ReplayCreation(_replay_status(replay, existing), created=False)

            run = await _run(session, actor.organization_id, replay_run_id)
            await QuotaManager.reserve_run(
                session,
                organization_id=actor.organization_id,
                run=run,
            )
            replay = RunReplay(
                replay_run_id=run.id,
                organization_id=run.organization_id,
                project_id=run.project_id,
                source_run_id=source.id,
                source_manifest_artifact_id=source_artifact.id,
                expected_manifest_sha256=expected.sha256,
                classification=expected.determinism,
                created_by=user_id,
            )
            session.add(replay)
            session.add(
                RunEvent(
                    organization_id=run.organization_id,
                    run_id=run.id,
                    sequence=1,
                    event_type="run.replay_submitted",
                    payload={
                        "source_manifest_id": str(source_artifact.id),
                        "source_run_id": str(source.id),
                        "state": RunState.QUEUED.value,
                    },
                )
            )
            session.add(_outbox(run))
            await AuditWriter.append(
                session,
                organization_id=run.organization_id,
                actor=AuditIdentity.user(user_id),
                action=AuditAction.RUN_REPLAY_SUBMITTED,
                resource_type=AuditResourceType.REPLAY,
                resource_id=run.id,
                request_id=actor.request_id,
                metadata={
                    "classification": expected.determinism.value,
                    "kind": run.kind.value,
                    "source_manifest_id": str(source_artifact.id),
                    "source_run_id": str(source.id),
                },
            )
            await session.flush()
            return ReplayCreation(_replay_status(replay, run), created=True)

    async def get_replay(
        self,
        actor: ReproducibilityActor,
        replay_run_id: UUID,
    ) -> ReplayStatus:
        async with self.database.session(_user_context(actor)) as session:
            await _actor(session, actor, "replay.get")
            row = (
                await session.execute(
                    select(RunReplay, Run)
                    .join(Run, Run.id == RunReplay.replay_run_id)
                    .join(Project, Project.id == Run.project_id)
                    .where(
                        RunReplay.replay_run_id == replay_run_id,
                        RunReplay.organization_id == actor.organization_id,
                        Run.organization_id == actor.organization_id,
                        Project.organization_id == actor.organization_id,
                        Project.lifecycle_state == ProjectLifecycle.ACTIVE,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ResourceNotFoundError("replay.get")
            replay, run = row._tuple()
            return _replay_status(replay, run)

    async def _manifest_build(
        self,
        session: AsyncSession,
        reference: RunWorkflowReference,
    ) -> tuple[_ManifestBuild, _ArtifactRecord | None]:
        organization_id, run_id = _reference_ids(reference)
        run = await _run(session, organization_id, run_id)
        _verify_reference(run, reference)
        if run.state is not RunState.SUCCEEDED:
            raise ReproducibilityError("manifest_invalid_state")
        recorded = await session.scalar(
            select(RunExecutionFact).where(
                RunExecutionFact.run_id == run.id,
                RunExecutionFact.organization_id == organization_id,
            )
        )
        if recorded is None:
            raise ReproducibilityError("execution_facts_missing")
        try:
            facts = TrustedExecutionFacts.model_validate_json(
                _canonical_json(recorded.facts), strict=True
            )
        except ValidationError as exc:
            raise ReproducibilityError("execution_facts_integrity_violation") from exc
        if facts.sha256 != recorded.facts_sha256:
            raise ReproducibilityError("execution_facts_integrity_violation")
        self._validate_runtime_facts(facts)
        self._validate_run_facts(run, facts)
        if recorded.manifest_artifact_id is not None:
            artifact = await _active_artifact(
                session,
                organization_id,
                recorded.manifest_artifact_id,
                ArtifactKind.RUN_MANIFEST,
            )
            if artifact.sha256 != recorded.manifest_sha256:
                raise ReproducibilityError("manifest_metadata_conflict")
            empty = _ManifestBuild(
                run.organization_id,
                run.project_id,
                run.id,
                run.created_by,
                run.spec_sha256,
                recorded.facts_sha256,
                None,
                (),
                None,
            )
            return empty, artifact

        inputs = _content_digests(recorded.input_digests)
        await self._validate_input_artifacts(session, run, list(recorded.input_digests))
        outputs, output_artifacts = await self._authoritative_outputs(session, run)
        started_at, finished_at = await _execution_times(session, run)
        replay = await session.scalar(
            select(RunReplay).where(
                RunReplay.replay_run_id == run.id,
                RunReplay.organization_id == run.organization_id,
            )
        )
        replay_source: _ArtifactRecord | None = None
        if replay is not None:
            replay_source = await _active_artifact(
                session,
                run.organization_id,
                replay.source_manifest_artifact_id,
                ArtifactKind.RUN_MANIFEST,
            )
            if (
                replay_source.sha256 != replay.expected_manifest_sha256
                or replay_source.run_id != replay.source_run_id
            ):
                raise ReproducibilityError("source_manifest_integrity_violation")
        language, target_source, unit, seed = await _manifest_parameters(session, run)
        try:
            manifest = RunManifest(
                project_id=run.project_id,
                run_id=run.id,
                operation=run.kind,
                corpuskit_version=facts.corpuskit_version,
                corpusgen_version=facts.corpusgen_version,
                espeak_version=facts.espeak_version,
                phoible=facts.phoible,
                model=facts.model,
                dataset=facts.dataset,
                worker_image_digest=facts.worker_image_digest,
                runtime_profile=facts.worker_profile,
                language=language,
                target_source=target_source,
                unit=unit,
                parameters=dict(run.spec),
                seed=seed,
                input_digests=inputs,
                output_digests=outputs,
                started_at=started_at,
                finished_at=finished_at,
                stop_reason=_stop_reason(run.result_summary),
                determinism=facts.determinism,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise ReproducibilityError("manifest_facts_invalid") from exc
        return (
            _ManifestBuild(
                run.organization_id,
                run.project_id,
                run.id,
                run.created_by,
                run.spec_sha256,
                recorded.facts_sha256,
                manifest,
                output_artifacts,
                replay_source,
            ),
            None,
        )

    async def _authoritative_inputs(
        self,
        session: AsyncSession,
        run: Run,
        facts: TrustedExecutionFacts,
    ) -> tuple[list[dict[str, Any]], tuple[_ArtifactRecord, ...]]:
        normalized, spec_sha256 = normalize_run_spec(dict(run.spec))
        encoded_spec = _canonical_json(normalized)
        if spec_sha256 != run.spec_sha256:
            raise ReproducibilityError("spec_integrity_violation")
        inputs: list[dict[str, Any]] = [
            {"name": "run-spec", "sha256": spec_sha256, "size_bytes": len(encoded_spec)}
        ]
        if run.corpus_version_id is not None:
            inputs.append(await _corpus_digest(session, run))
        inputs.append(facts.worker_policy.model_dump(mode="json"))

        artifact_records: list[_ArtifactRecord] = []
        if facts.input_artifact_ids:
            rows = tuple(
                await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.id.in_(facts.input_artifact_ids),
                        Artifact.organization_id == run.organization_id,
                        Artifact.project_id == run.project_id,
                        Artifact.state == ArtifactState.ACTIVE,
                    )
                    .order_by(Artifact.kind, Artifact.id)
                )
            )
            if {row.id for row in rows} != set(facts.input_artifact_ids):
                raise ReproducibilityError("input_artifact_not_found")
            for index, row in enumerate(rows, start=1):
                record = _artifact_record(row)
                artifact_records.append(record)
                inputs.append(
                    {
                        "name": f"input-{row.kind}-{index:03d}",
                        "sha256": row.sha256,
                        "size_bytes": row.size_bytes,
                        "artifact_id": str(row.id),
                    }
                )
        inputs.extend(item.model_dump(mode="json") for item in facts.input_attestations)
        _content_digests(inputs)
        return inputs, tuple(artifact_records)

    async def _authoritative_outputs(
        self,
        session: AsyncSession,
        run: Run,
    ) -> tuple[tuple[ContentDigest, ...], tuple[_ArtifactRecord, ...]]:
        rows = tuple(
            await session.scalars(
                select(Artifact)
                .where(
                    Artifact.organization_id == run.organization_id,
                    Artifact.project_id == run.project_id,
                    Artifact.run_id == run.id,
                    Artifact.state == ArtifactState.ACTIVE,
                    Artifact.kind.in_(kind.value for kind in _OUTPUT_KINDS),
                )
                .order_by(Artifact.kind, Artifact.id)
            )
        )
        records = tuple(_artifact_record(row) for row in rows)
        if rows:
            return (
                tuple(
                    ContentDigest(
                        name=f"output-{row.kind}-{index:03d}",
                        sha256=row.sha256,
                        size_bytes=row.size_bytes,
                    )
                    for index, row in enumerate(rows, start=1)
                ),
                records,
            )
        if run.result_summary is None:
            raise ReproducibilityError("manifest_outputs_missing")
        try:
            summary = normalize_result_summary(dict(run.result_summary))
            encoded = _canonical_json(summary)
        except (TypeError, ValueError) as exc:
            raise ReproducibilityError("result_summary_integrity_violation") from exc
        return (
            (
                ContentDigest(
                    name="result-summary",
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    size_bytes=len(encoded),
                ),
            ),
            (),
        )

    async def _validate_input_artifacts(
        self,
        session: AsyncSession,
        run: Run,
        inputs: list[dict[str, Any]],
    ) -> None:
        for value in inputs:
            artifact_value = value.get("artifact_id")
            if artifact_value is None:
                continue
            try:
                artifact_id = UUID(str(artifact_value))
            except ValueError:
                raise ReproducibilityError("execution_facts_integrity_violation") from None
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.organization_id == run.organization_id,
                    Artifact.project_id == run.project_id,
                    Artifact.state == ArtifactState.ACTIVE,
                )
            )
            if artifact is None or (
                artifact.sha256 != value.get("sha256")
                or artifact.size_bytes != value.get("size_bytes")
            ):
                raise ReproducibilityError("input_artifact_integrity_violation")

    async def _validate_outputs(
        self,
        session: AsyncSession,
        run: Run,
        expected: tuple[_ArtifactRecord, ...],
    ) -> None:
        _, current = await self._authoritative_outputs(session, run)
        if current != expected:
            raise ReproducibilityError("run_outputs_changed")

    async def _read_verified(self, artifact: _ArtifactRecord) -> bytes:
        try:
            opened = await self._store.open(artifact.storage_key, chunk_bytes=self._chunk_bytes)
            descriptor = opened.descriptor
            if (
                descriptor.key != artifact.storage_key
                or descriptor.size_bytes != artifact.size_bytes
                or descriptor.sha256 != artifact.sha256
                or descriptor.media_type != artifact.media_type
            ):
                raise ObjectIntegrityError("artifact metadata mismatch")
            digest = hashlib.sha256()
            size = 0
            chunks: list[bytes] = []
            async for chunk in opened.chunks:
                size += len(chunk)
                if size > artifact.size_bytes or size > self._max_bytes:
                    raise ObjectIntegrityError("artifact exceeds declared size")
                digest.update(chunk)
                chunks.append(chunk)
            if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                raise ObjectIntegrityError("artifact content mismatch")
            return b"".join(chunks)
        except ObjectStoreError as exc:
            raise DependencyUnavailableError("manifest.storage") from exc

    async def _load_manifest(
        self,
        artifact: _ArtifactRecord,
        *,
        run_id: UUID | None,
    ) -> RunManifest:
        if artifact.kind != ArtifactKind.RUN_MANIFEST.value:
            raise ReproducibilityError("manifest_metadata_conflict")
        content = await self._read_verified(artifact)
        try:
            manifest = RunManifest.model_validate_json(content, strict=True)
        except ValidationError as exc:
            raise ReproducibilityError("manifest_integrity_violation") from exc
        if (
            manifest.canonical_bytes() != content
            or manifest.sha256 != artifact.sha256
            or manifest.project_id != artifact.project_id
            or artifact.run_id is None
            or manifest.run_id != artifact.run_id
            or (run_id is not None and artifact.run_id != run_id)
        ):
            raise ReproducibilityError("manifest_integrity_violation")
        return manifest

    async def _complete_replay_if_needed(
        self,
        reference: RunWorkflowReference,
        observed_artifact: _ArtifactRecord,
        observed: RunManifest,
    ) -> None:
        organization_id, run_id = _reference_ids(reference)
        context = TenantContext.service(ServiceIdentity.ADOPTION, organization_id)
        async with self._adoption_database.session(context) as session:
            run = await _run(session, organization_id, run_id, for_update=True)
            replay = await session.scalar(
                select(RunReplay)
                .where(
                    RunReplay.replay_run_id == run.id,
                    RunReplay.organization_id == organization_id,
                )
                .with_for_update()
            )
            if replay is None or replay.comparison is not None:
                return
            source = await _active_artifact(
                session,
                organization_id,
                replay.source_manifest_artifact_id,
                ArtifactKind.RUN_MANIFEST,
            )
        expected = await self._load_manifest(source, run_id=replay.source_run_id)
        async with self._adoption_database.session(context) as session:
            run = await _run(session, organization_id, run_id, for_update=True)
            await self._commit_replay_comparison(
                session,
                run=run,
                manifest_artifact_id=observed_artifact.id,
                observed=observed,
                expected=expected,
            )

    async def _commit_replay_comparison(
        self,
        session: AsyncSession,
        *,
        run: Run,
        manifest_artifact_id: UUID,
        observed: RunManifest,
        expected: RunManifest | None,
    ) -> None:
        replay = await session.scalar(
            select(RunReplay)
            .where(
                RunReplay.replay_run_id == run.id,
                RunReplay.organization_id == run.organization_id,
            )
            .with_for_update()
        )
        if replay is None:
            return
        if replay.comparison is not None:
            if replay.observed_manifest_artifact_id != manifest_artifact_id:
                raise ReproducibilityError("replay_comparison_conflict")
            return
        if expected is None or expected.sha256 != replay.expected_manifest_sha256:
            raise ReproducibilityError("source_manifest_integrity_violation")
        comparison = compare_replay(expected, observed)
        replay.observed_manifest_artifact_id = manifest_artifact_id
        replay.verdict = comparison.verdict
        replay.comparison = comparison.model_dump(mode="json")
        replay.completed_at = datetime.now(UTC)
        await AuditWriter.append(
            session,
            organization_id=run.organization_id,
            actor=AuditIdentity.service(ServiceIdentity.ADOPTION),
            action=AuditAction.RUN_REPLAY_COMPARED,
            resource_type=AuditResourceType.REPLAY,
            resource_id=run.id,
            metadata={
                "classification": comparison.classification.value,
                "outputs_match": comparison.outputs_match,
                "verdict": comparison.verdict.value,
            },
        )

    async def _same_replay(
        self,
        session: AsyncSession,
        run: Run,
        *,
        source: Run,
        source_artifact: _ArtifactRecord,
        expected: RunManifest,
    ) -> RunReplay:
        replay = await session.scalar(
            select(RunReplay).where(
                RunReplay.replay_run_id == run.id,
                RunReplay.organization_id == run.organization_id,
            )
        )
        if (
            replay is None
            or run.project_id != source.project_id
            or run.corpus_version_id != source.corpus_version_id
            or run.parent_run_id != source.id
            or run.kind is not source.kind
            or run.spec_sha256 != source.spec_sha256
            or replay.source_manifest_artifact_id != source_artifact.id
            or replay.expected_manifest_sha256 != expected.sha256
            or replay.classification is not expected.determinism
        ):
            raise ResourceConflictError("replay.idempotency")
        return replay

    def _validate_runtime_facts(self, facts: TrustedExecutionFacts) -> None:
        if (
            facts.corpuskit_version != self._corpuskit_version
            or facts.corpusgen_version != self._corpusgen_version
        ):
            raise ReproducibilityError("runtime_version_mismatch")

    @staticmethod
    def _validate_run_facts(run: Run, facts: TrustedExecutionFacts) -> None:
        if run.kind in _MODEL_KINDS and facts.model is None:
            raise ReproducibilityError("model_provenance_missing")


async def _source_manifest_rows(
    session: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID,
    source_run_id: UUID,
    for_update: bool = False,
) -> tuple[Run, RunExecutionFact, _ArtifactRecord]:
    if for_update:
        await lock_project_lifecycle(session, project_id)
    statement = (
        select(Run)
        .join(Project, Project.id == Run.project_id)
        .where(
            Run.id == source_run_id,
            Run.organization_id == organization_id,
            Run.project_id == project_id,
            Project.organization_id == organization_id,
            Project.lifecycle_state == ProjectLifecycle.ACTIVE,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=Run)
    run = await session.scalar(statement)
    if run is None or run.state is not RunState.SUCCEEDED:
        raise ResourceNotFoundError("replay.submit")
    facts = await session.scalar(
        select(RunExecutionFact).where(
            RunExecutionFact.run_id == run.id,
            RunExecutionFact.organization_id == organization_id,
            RunExecutionFact.project_id == project_id,
        )
    )
    if facts is None or facts.manifest_artifact_id is None or facts.manifest_sha256 is None:
        raise ResourceNotFoundError("replay.submit")
    artifact = await _active_artifact(
        session,
        organization_id,
        facts.manifest_artifact_id,
        ArtifactKind.RUN_MANIFEST,
    )
    if artifact.project_id != project_id or artifact.run_id != run.id:
        raise ReproducibilityError("source_manifest_integrity_violation")
    return run, facts, artifact


async def _active_artifact(
    session: AsyncSession,
    organization_id: UUID,
    artifact_id: UUID,
    kind: ArtifactKind,
) -> _ArtifactRecord:
    artifact = await session.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.organization_id == organization_id,
            Artifact.kind == kind.value,
            Artifact.state == ArtifactState.ACTIVE,
        )
    )
    if artifact is None:
        raise ReproducibilityError("artifact_not_found")
    return _artifact_record(artifact)


async def _run(
    session: AsyncSession,
    organization_id: UUID,
    run_id: UUID,
    *,
    for_update: bool = False,
) -> Run:
    if for_update:
        project_id = await session.scalar(
            select(Run.project_id).where(
                Run.id == run_id,
                Run.organization_id == organization_id,
            )
        )
        if project_id is None:
            raise ReproducibilityError("run_not_found")
        await lock_project_lifecycle(session, project_id)
    statement = (
        select(Run)
        .join(Project, Project.id == Run.project_id)
        .where(
            Run.id == run_id,
            Run.organization_id == organization_id,
            Project.organization_id == organization_id,
            Project.lifecycle_state == ProjectLifecycle.ACTIVE,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=Run)
    run = await session.scalar(statement)
    if run is None:
        raise ReproducibilityError("run_not_found")
    return run


async def _actor(
    session: AsyncSession,
    actor: ReproducibilityActor,
    operation: str,
) -> tuple[UUID, Role]:
    row = (
        await session.execute(
            select(User.id, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(
                User.oidc_subject == actor.subject,
                Membership.organization_id == actor.organization_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ResourceNotFoundError(operation)
    return row._tuple()


async def _corpus_digest(session: AsyncSession, run: Run) -> dict[str, Any]:
    version = await session.scalar(
        select(CorpusVersion)
        .join(Corpus, Corpus.id == CorpusVersion.corpus_id)
        .where(
            CorpusVersion.id == run.corpus_version_id,
            CorpusVersion.organization_id == run.organization_id,
            Corpus.organization_id == run.organization_id,
            Corpus.project_id == run.project_id,
        )
    )
    if version is None:
        raise ReproducibilityError("corpus_version_not_found")
    sentences = tuple(
        await session.scalars(
            select(Sentence)
            .where(
                Sentence.organization_id == run.organization_id,
                Sentence.corpus_version_id == version.id,
            )
            .order_by(Sentence.ordinal, Sentence.id)
        )
    )
    if len(sentences) != version.sentence_count or [item.ordinal for item in sentences] != list(
        range(version.sentence_count)
    ):
        raise ReproducibilityError("corpus_version_integrity_violation")
    content = _canonical_json(
        {
            "language": version.language,
            "sentences": [sentence.normalized_text for sentence in sentences],
        }
    )
    sha256 = hashlib.sha256(content).hexdigest()
    if sha256 != version.content_sha256:
        raise ReproducibilityError("corpus_version_integrity_violation")
    return {"name": "corpus-version", "sha256": sha256, "size_bytes": len(content)}


async def _execution_times(session: AsyncSession, run: Run) -> tuple[datetime, datetime]:
    events = tuple(
        await session.scalars(
            select(RunEvent)
            .where(
                RunEvent.organization_id == run.organization_id,
                RunEvent.run_id == run.id,
                RunEvent.event_type.in_(("run.started", "run.succeeded")),
            )
            .order_by(RunEvent.sequence)
        )
    )
    started = next(
        (event.occurred_at for event in events if event.event_type == "run.started"), None
    )
    finished = next(
        (event.occurred_at for event in reversed(events) if event.event_type == "run.succeeded"),
        None,
    )
    if started is None or finished is None:
        raise ReproducibilityError("execution_timestamps_missing")
    started = _utc(started)
    finished = _utc(finished)
    if finished < started:
        raise ReproducibilityError("execution_timestamps_invalid")
    return started, finished


async def _manifest_parameters(
    session: AsyncSession,
    run: Run,
) -> tuple[str, str, str, int | None]:
    language, target_source, unit, seed = _validated_manifest_parameters(
        run.kind,
        dict(run.spec),
    )
    if language == "und" and run.corpus_version_id is not None:
        language_value = await session.scalar(
            select(CorpusVersion.language).where(
                CorpusVersion.id == run.corpus_version_id,
                CorpusVersion.organization_id == run.organization_id,
            )
        )
        if isinstance(language_value, str) and language_value:
            language = language_value
    return language, target_source, unit, seed


def _validated_manifest_parameters(
    kind: RunKind,
    spec: dict[str, Any],
) -> tuple[str, str, str, int | None]:
    """Extract replay metadata only after validating the exact durable DTO."""

    try:
        if kind is RunKind.PHONEMIZE:
            phonemize = PhonemizeRunSpec.model_validate(spec)
            return phonemize.language, "none", "phoneme", None
        if kind is RunKind.EVALUATE:
            evaluation = EvaluateRunSpec.model_validate(spec)
            return (
                evaluation.language,
                evaluation.target.mode.value,
                evaluation.unit.value,
                None,
            )
        if kind is RunKind.DISTRIBUTION:
            DistributionAnalysisRequest.model_validate(spec)
            return "und", "explicit", "phoneme", None
        if kind is RunKind.TRAJECTORY:
            trajectory = CoverageTrajectoryRequest.model_validate(spec)
            return "und", "explicit", trajectory.unit.value, None
        if kind is RunKind.ERROR_RATES:
            ErrorRatesAnalysisRequest.model_validate(spec)
            return "und", "none", "none", None
        if kind is RunKind.SELECT:
            selection = SelectRunSpec.model_validate(spec)
            return (
                selection.language,
                selection.target.mode.value,
                selection.unit.value,
                selection.options.seed,
            )
        if kind is RunKind.GENERATE_REPOSITORY:
            repository = RepositoryGenerationRequest.model_validate(spec)
            language = (
                repository.source.spec.language
                if isinstance(repository.source, HuggingFaceRepository)
                else repository.source.language
                if isinstance(repository.source, RawTextRepository)
                else "und"
            )
            return language, "explicit", repository.target.unit.value, None
        if kind is RunKind.GENERATE_LLM:
            hosted = HostedGenerationRequest.model_validate(spec)
            return hosted.language, "explicit", hosted.target.unit.value, None
        if kind is RunKind.GENERATE_LOCAL:
            local = LocalGenerationRequest.model_validate(spec)
            return local.language, "explicit", local.target.unit.value, local.seed
        if kind is RunKind.PERPLEXITY:
            LanguageModelAnalysisRequest.model_validate(spec)
            return "und", "none", "none", None
        if kind is RunKind.BUILD_DATG_INDEX:
            index_build = DatgIndexBuildRequest.model_validate(spec)
            return index_build.language, "none", index_build.unit.value, None
        if kind is RunKind.GENERATE_DATG:
            datg_generation = DatgGuidedGenerationRequest.model_validate(spec)
            return (
                datg_generation.language,
                "explicit",
                datg_generation.unit.value,
                datg_generation.seed,
            )
        if kind is RunKind.TRAIN_PHON_RL:
            training = PhonRlTrainingRequest.model_validate(spec)
            return training.language, "explicit", training.unit.value, training.parameters.seed
        raise ReproducibilityError("manifest_run_kind_unsupported")
    except ValidationError as exc:
        raise ReproducibilityError("manifest_spec_invalid") from exc


def _stop_reason(summary: dict[str, Any] | None) -> StopReason:
    if summary is not None:
        value = summary.get("stop_reason")
        if isinstance(value, str):
            try:
                return StopReason(value)
            except ValueError:
                pass
    return StopReason.COMPLETED


def _content_digests(values: list[dict[str, Any]]) -> tuple[ContentDigest, ...]:
    try:
        digests = tuple(
            ContentDigest.model_validate(
                {key: value[key] for key in ("name", "sha256", "size_bytes")},
                strict=True,
            )
            for value in values
        )
    except (KeyError, ValidationError) as exc:
        raise ReproducibilityError("execution_facts_integrity_violation") from exc
    if len({item.name for item in digests}) != len(digests):
        raise ReproducibilityError("execution_facts_integrity_violation")
    return digests


def _same_facts(
    existing: RunExecutionFact,
    facts: TrustedExecutionFacts,
    inputs: list[dict[str, Any]],
) -> bool:
    return (
        existing.facts_sha256 == facts.sha256
        and existing.facts == facts.model_dump(mode="json")
        and existing.input_digests == inputs
    )


def _verify_reference(run: Run, reference: RunWorkflowReference) -> None:
    if run.spec_sha256 != reference.spec_sha256:
        raise ReproducibilityError("spec_integrity_violation")
    try:
        _, actual = normalize_run_spec(dict(run.spec))
    except (TypeError, ValueError):
        raise ReproducibilityError("spec_integrity_violation") from None
    if actual != run.spec_sha256:
        raise ReproducibilityError("spec_integrity_violation")


def _reference_ids(reference: RunWorkflowReference) -> tuple[UUID, UUID]:
    try:
        reference.validate()
        return UUID(reference.organization_id), UUID(reference.run_id)
    except ValueError:
        raise ReproducibilityError("invalid_workflow_reference") from None


def _artifact_record(artifact: Artifact) -> _ArtifactRecord:
    return _ArtifactRecord(
        id=artifact.id,
        organization_id=artifact.organization_id,
        project_id=artifact.project_id,
        run_id=artifact.run_id,
        kind=artifact.kind,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        storage_key=artifact.storage_key,
        media_type=artifact.media_type,
        filename=artifact.filename,
        state=artifact.state,
    )


def _same_manifest_artifact(artifact: Artifact, *, key: str, size_bytes: int) -> bool:
    return (
        artifact.storage_key == key
        and artifact.size_bytes == size_bytes
        and artifact.media_type == "application/json"
        and artifact.state is ArtifactState.ACTIVE
    )


def _replay_status(replay: RunReplay, run: Run) -> ReplayStatus:
    comparison: ReplayComparison | None = None
    if replay.comparison is not None:
        try:
            comparison = ReplayComparison.model_validate_json(
                _canonical_json(replay.comparison), strict=True
            )
        except ValidationError as exc:
            raise ReproducibilityError("replay_comparison_integrity_violation") from exc
    if comparison is not None:
        lifecycle = ReplayLifecycle.COMPARED
    elif run.state in {RunState.QUEUED, RunState.PROVISIONING, RunState.DRAFT}:
        lifecycle = ReplayLifecycle.QUEUED
    elif run.state in {RunState.RUNNING, RunState.CANCELLING}:
        lifecycle = ReplayLifecycle.RUNNING
    else:
        lifecycle = ReplayLifecycle.UNAVAILABLE
    return ReplayStatus(
        replay_run_id=replay.replay_run_id,
        source_run_id=replay.source_run_id,
        source_manifest_artifact_id=replay.source_manifest_artifact_id,
        expected_manifest_sha256=replay.expected_manifest_sha256,
        observed_manifest_artifact_id=replay.observed_manifest_artifact_id,
        classification=replay.classification,
        lifecycle=lifecycle,
        comparison=comparison,
    )


def _outbox(run: Run) -> OutboxMessage:
    return OutboxMessage(
        id=uuid4(),
        organization_id=run.organization_id,
        run_id=run.id,
        event_type="run.dispatch",
        payload={
            "run_id": str(run.id),
            "kind": run.kind.value,
            "spec_sha256": run.spec_sha256,
        },
        state=OutboxState.PENDING,
        attempts=0,
    )


def _idempotency_key(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise InvalidRequestError("replay.idempotency")
    return value


def _insert_for(session: AsyncSession, model: type[Any]) -> Any:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert(model)
    if dialect == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError("reproducibility persistence requires PostgreSQL or SQLite")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _installed_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:  # pragma: no cover - packaging fault
        raise RuntimeError(f"required distribution is unavailable: {distribution}") from exc


def _user_context(actor: ReproducibilityActor) -> TenantContext:
    return TenantContext.user(actor.organization_id, actor.subject)


__all__ = [
    "ManifestCreation",
    "ReplayCreation",
    "ReproducibilityActor",
    "ReproducibilityError",
    "RunManifestService",
]
