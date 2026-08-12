"""Construction helpers shared by worker and dispatcher entry points."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import cast

from temporalio.client import Client
from temporalio.worker import Worker

from corpuskit.config import Settings
from corpuskit.persistence.artifact_store import build_object_store
from corpuskit.persistence.database import Database
from corpuskit.persistence.datg_cache import FilesystemDatgIndexPublisher
from corpuskit.services.artifact_adoption import ArtifactAdoptionService
from corpuskit.services.reproducibility import RunManifestService
from corpuskit.worker.composition import (
    WorkerExecutionFactsFactory,
    build_profile_handler_registry,
)
from corpuskit.workflows.activities import CoreRunActivities
from corpuskit.workflows.dispatcher import TemporalClientLike
from corpuskit.workflows.handlers import DurableRunHandler
from corpuskit.workflows.store import DurableRunStore
from corpuskit.workflows.trusted_inputs import (
    TrustedRunInputMaterializer,
    default_trusted_input_root,
)
from corpuskit.workflows.workflow import CorpusRunWorkflow


async def connect_temporal(settings: Settings) -> Client:
    """Connect within a bounded startup deadline without exposing credentials."""

    api_key = (
        settings.temporal_api_key.get_secret_value()
        if settings.temporal_api_key is not None
        else None
    )
    async with asyncio.timeout(settings.temporal_connect_timeout_seconds):
        return await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            api_key=api_key,
            tls=settings.temporal_tls,
        )


def build_worker(
    client: Client,
    database: Database,
    settings: Settings,
    *,
    adoption_database: Database | None = None,
    additional_handlers: Sequence[DurableRunHandler] = (),
) -> Worker:
    """Register the versioned workflow and an explicit server-side handler allowlist."""

    resolved_adoption_database = _adoption_database(database, adoption_database, settings)
    registry = build_profile_handler_registry(settings).extended(additional_handlers)
    run_store = _store(database)
    adoption_store = _store(resolved_adoption_database)
    object_store = build_object_store(settings)
    datg_index_publisher = _datg_index_publisher(settings)
    manifest_service = (
        RunManifestService(
            database,
            object_store,
            settings,
            worker_database=database,
            adoption_database=resolved_adoption_database,
        )
        if settings.worker_image_digest is not None
        else None
    )
    facts_factory = (
        WorkerExecutionFactsFactory.from_settings(settings)
        if manifest_service is not None or datg_index_publisher is not None
        else None
    )
    execution_facts = facts_factory if manifest_service is not None else None
    datg_runtime_versions: tuple[str, str] | None = None
    if datg_index_publisher is not None:
        if facts_factory is None or facts_factory.espeak_version is None:
            raise RuntimeError("DATG index publication requires parent runtime provenance")
        datg_runtime_versions = (
            facts_factory.corpusgen_version,
            facts_factory.espeak_version,
        )
    activities = CoreRunActivities(
        store=run_store,
        handlers=registry,
        heartbeat_seconds=settings.temporal_activity_heartbeat_seconds,
        artifact_adopter=ArtifactAdoptionService(
            run_store,
            object_store,
            settings,
            adoption_runs=adoption_store,
            datg_index_publisher=datg_index_publisher,
            datg_runtime_versions=datg_runtime_versions,
        ),
        activity_deadline_cap_seconds=settings.worker_activity_deadline_cap_seconds,
        process_hard_timeout_seconds=settings.worker_activity_deadline_cap_seconds,
        execution_facts=execution_facts,
        manifest_recorder=manifest_service,
        input_materializer=TrustedRunInputMaterializer(
            database,
            object_store,
            root=default_trusted_input_root(settings.worker_profile),
            local_policies=settings.worker_local_model_policies,
            chunk_bytes=settings.artifact_download_chunk_bytes,
        ),
    )
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[CorpusRunWorkflow],
        activities=[
            activities.prepare_run,
            activities.execute_run,
            activities.finalize_failure,
            activities.finalize_cancellation,
        ],
        identity=f"corpuskit-{settings.worker_profile}",
        max_concurrent_activities=settings.temporal_max_concurrent_activities,
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
    )


def temporal_client_protocol(client: Client) -> TemporalClientLike:
    """Narrow the SDK client to the publisher contract after construction."""

    return cast(TemporalClientLike, client)


def _store(database: Database) -> DurableRunStore:
    return DurableRunStore(database)


def _adoption_database(
    worker_database: Database,
    adoption_database: Database | None,
    settings: Settings,
) -> Database:
    if settings.worker_image_digest is not None and adoption_database is None:
        raise RuntimeError("durable result publication requires a distinct adoption database")
    resolved = adoption_database or worker_database
    if settings.environment in {"staging", "production"}:
        if settings.adoption_database_url is None or adoption_database is None:
            raise RuntimeError("deployed workers require a configured adoption database")
        if (
            resolved.engine.url.username,
            resolved.engine.url.password,
        ) == (
            worker_database.engine.url.username,
            worker_database.engine.url.password,
        ):
            raise RuntimeError("deployed worker and adoption database credentials must be distinct")
    return resolved


def _datg_index_publisher(settings: Settings) -> FilesystemDatgIndexPublisher | None:
    if settings.worker_profile != "batch-cpu" or not settings.worker_datg_runtime_policies:
        if settings.worker_datg_index_publish_root is not None:
            raise RuntimeError("only the DATG batch worker may publish index cache entries")
        return None
    root = settings.worker_datg_index_publish_root
    if root is None or not root.is_absolute():
        raise RuntimeError("DATG index builds require an absolute parent publication root")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeError("DATG index publication root must be provisioned") from None
    if not resolved.is_dir():
        raise RuntimeError("DATG index publication root must be a directory")
    return FilesystemDatgIndexPublisher(root)


__all__ = ["build_worker", "connect_temporal", "temporal_client_protocol"]
