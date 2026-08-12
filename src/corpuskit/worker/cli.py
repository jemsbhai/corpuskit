"""Temporal worker entry point."""

from __future__ import annotations

import asyncio

from corpuskit.config import RuntimeRole, get_settings
from corpuskit.persistence.database import Database
from corpuskit.telemetry import configure_structured_logging
from corpuskit.worker.runtime import build_worker, connect_temporal


async def run_worker() -> None:
    """Run one exact profile worker until Temporal initiates graceful shutdown."""

    settings = get_settings()
    configure_structured_logging(settings.log_level)
    if settings.runtime_role is not RuntimeRole.WORKER:
        raise RuntimeError("corpuskit-worker requires CORPUSKIT_RUNTIME_ROLE=worker")
    if settings.job_backend != "temporal":
        raise RuntimeError("corpuskit-worker requires CORPUSKIT_JOB_BACKEND=temporal")
    database = Database(settings.database_url)
    adoption_database = (
        Database(settings.adoption_database_url.get_secret_value())
        if settings.adoption_database_url is not None
        else database
    )
    try:
        client = await connect_temporal(settings)
        worker = build_worker(
            client,
            database,
            settings,
            adoption_database=adoption_database,
        )
        await worker.run()
    finally:
        if adoption_database is not database:
            await adoption_database.dispose()
        await database.dispose()


def main() -> None:
    """Run the durable worker process."""

    asyncio.run(run_worker())
