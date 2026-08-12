"""Idempotent Temporal publisher for committed outbox intents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from corpuskit.domain.jobs import RunKind
from corpuskit.services.jobs import DispatchMessage
from corpuskit.workflows.contracts import RunWorkflowReference, workflow_id
from corpuskit.workflows.policies import (
    CANCELLATION_SIGNAL,
    WORKFLOW_EXECUTION_TIMEOUT,
    WORKFLOW_NAME,
    WORKFLOW_RUN_TIMEOUT,
)


class _WorkflowHandle(Protocol):
    async def signal(self, signal: str) -> None: ...


class TemporalClientLike(Protocol):
    async def start_workflow(
        self,
        workflow_name: str,
        reference: RunWorkflowReference,
        **options: Any,
    ) -> _WorkflowHandle: ...

    def get_workflow_handle(self, workflow_id: str) -> _WorkflowHandle: ...


class TerminalRunProbe(Protocol):
    async def is_terminal(self, reference: RunWorkflowReference) -> bool: ...


class TemporalDispatcher:
    """Publish outbox start/cancel intents with deterministic Temporal identities."""

    def __init__(
        self,
        client: TemporalClientLike,
        *,
        task_queue: str | None = None,
        task_queues: Mapping[RunKind, str] | None = None,
        terminal_probe: TerminalRunProbe,
    ) -> None:
        if (task_queue is None) == (task_queues is None):
            raise ValueError("configure exactly one task_queue routing mode")
        resolved = (
            dict.fromkeys(RunKind, task_queue)
            if task_queue is not None
            else dict(task_queues or {})
        )
        if any(
            not isinstance(queue, str) or not queue or len(queue) > 128
            for queue in resolved.values()
        ):
            raise ValueError("task_queue must contain 1 to 128 characters")
        self._client = client
        self._task_queues = resolved
        self._terminal_probe = terminal_probe

    async def publish(self, message: DispatchMessage) -> None:
        reference, kind = _reference(message)
        temporal_id = workflow_id(reference)
        if message.event_type == "run.dispatch":
            task_queue = self._task_queues.get(kind)
            if task_queue is None:
                raise ValueError("run kind has no configured worker profile")
            try:
                await self._client.start_workflow(
                    WORKFLOW_NAME,
                    reference,
                    id=temporal_id,
                    task_queue=task_queue,
                    execution_timeout=WORKFLOW_EXECUTION_TIMEOUT,
                    run_timeout=WORKFLOW_RUN_TIMEOUT,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    request_id=str(message.id),
                    static_summary="CorpusKit durable corpus run",
                )
            except WorkflowAlreadyStartedError:
                return
            return
        if message.event_type == "run.cancel":
            try:
                handle = self._client.get_workflow_handle(temporal_id)
                await handle.signal(CANCELLATION_SIGNAL)
            except Exception:
                if await self._terminal_probe.is_terminal(reference):
                    return
                raise
            return
        raise ValueError("unsupported outbox event type")


def _reference(message: DispatchMessage) -> tuple[RunWorkflowReference, RunKind]:
    payload: Mapping[str, Any] = message.payload
    if set(payload) != {"kind", "run_id", "spec_sha256"}:
        raise ValueError("outbox payload violates the opaque dispatch contract")
    if payload.get("run_id") != str(message.run_id):
        raise ValueError("outbox run identity does not match its payload")
    raw_kind = payload.get("kind")
    if not isinstance(raw_kind, str) or not raw_kind:
        raise ValueError("outbox run kind is invalid")
    try:
        kind = RunKind(raw_kind)
    except ValueError:
        raise ValueError("outbox run kind is invalid") from None
    digest = payload.get("spec_sha256")
    if not isinstance(digest, str):
        raise ValueError("outbox spec digest is invalid")
    return (
        RunWorkflowReference(
            organization_id=str(message.organization_id),
            run_id=str(message.run_id),
            spec_sha256=digest,
        ).validate(),
        kind,
    )


__all__ = ["TemporalClientLike", "TemporalDispatcher"]
