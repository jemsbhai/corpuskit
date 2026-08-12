"""Transactional outbox to Temporal dispatcher process."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from uuid import uuid4

from corpuskit.config import RuntimeRole, Settings, get_settings
from corpuskit.persistence.database import Database
from corpuskit.services.jobs import TransactionalOutbox
from corpuskit.telemetry import configure_structured_logging
from corpuskit.worker.routing import durable_task_queue_map
from corpuskit.worker.runtime import connect_temporal, temporal_client_protocol
from corpuskit.workflows.dispatcher import TemporalDispatcher
from corpuskit.workflows.store import DurableRunStore


async def run_dispatcher(
    *,
    stop: asyncio.Event | None = None,
    settings: Settings | None = None,
) -> None:
    """Drain committed outbox rows until cancellation or an explicit stop event."""

    resolved = settings or get_settings()
    configure_structured_logging(resolved.log_level)
    if resolved.runtime_role is not RuntimeRole.DISPATCHER:
        raise RuntimeError("corpuskit-dispatcher requires CORPUSKIT_RUNTIME_ROLE=dispatcher")
    if resolved.job_backend != "temporal":
        raise RuntimeError("corpuskit-dispatcher requires CORPUSKIT_JOB_BACKEND=temporal")
    database = Database(resolved.database_url)
    stop_event = stop or asyncio.Event()
    try:
        client = await connect_temporal(resolved)
        publisher = TemporalDispatcher(
            temporal_client_protocol(client),
            task_queues=durable_task_queue_map(),
            terminal_probe=DurableRunStore(database),
        )
        outbox = TransactionalOutbox(database)
        worker_id = resolved.dispatcher_id or _ephemeral_dispatcher_id()
        while not stop_event.is_set():
            result = await outbox.dispatch_batch(
                publisher,
                worker_id=worker_id,
                limit=resolved.dispatcher_batch_size,
            )
            if result.claimed >= resolved.dispatcher_batch_size:
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=resolved.dispatcher_poll_seconds,
                )
    finally:
        await database.dispose()


def _ephemeral_dispatcher_id() -> str:
    host = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in socket.gethostname()
    )[:40]
    return f"{host}-{os.getpid()}-{uuid4().hex[:8]}"[:80]


def main() -> None:
    """Run the dispatcher process."""

    asyncio.run(run_dispatcher())


__all__ = ["main", "run_dispatcher"]
