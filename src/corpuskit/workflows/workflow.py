"""Versioned deterministic workflow with no persistence or engine imports."""

from __future__ import annotations

import asyncio

from temporalio import workflow
from temporalio.exceptions import ActivityError

from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.policies import (
    CANCELLATION_SIGNAL,
    CONTROL_RETRY_POLICY,
    EXECUTE_ACTIVITY,
    EXECUTION_HEARTBEAT_TIMEOUT,
    EXECUTION_RETRY_POLICY,
    EXECUTION_SCHEDULE_TIMEOUT,
    EXECUTION_START_TIMEOUT,
    FINALIZE_CANCELLATION_ACTIVITY,
    FINALIZE_FAILURE_ACTIVITY,
    FINALIZE_TIMEOUT,
    PREPARE_ACTIVITY,
    PREPARE_TIMEOUT,
    WORKFLOW_NAME,
)


@workflow.defn(name=WORKFLOW_NAME)
class CorpusRunWorkflow:
    """Orchestrate one run without placing its kind, spec, or text in history."""

    def __init__(self) -> None:
        self._cancel_requested = False
        self._activity: workflow.ActivityHandle[None] | None = None

    @workflow.signal(name=CANCELLATION_SIGNAL)
    def request_cancellation(self) -> None:
        self._cancel_requested = True
        if self._activity is not None:
            self._activity.cancel()

    @workflow.run
    async def run(self, reference: RunWorkflowReference) -> str:
        try:
            if self._cancellation_requested():
                await self._finalize_cancellation(reference)
                return reference.run_id
            self._activity = workflow.start_activity(
                PREPARE_ACTIVITY,
                reference,
                start_to_close_timeout=PREPARE_TIMEOUT,
                retry_policy=CONTROL_RETRY_POLICY,
            )
            await self._activity
            self._activity = None
            if self._cancellation_requested():
                await self._finalize_cancellation(reference)
                return reference.run_id
            self._activity = workflow.start_activity(
                EXECUTE_ACTIVITY,
                reference,
                schedule_to_close_timeout=EXECUTION_SCHEDULE_TIMEOUT,
                start_to_close_timeout=EXECUTION_START_TIMEOUT,
                heartbeat_timeout=EXECUTION_HEARTBEAT_TIMEOUT,
                retry_policy=EXECUTION_RETRY_POLICY,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            await self._activity
            self._activity = None
            return reference.run_id
        except ActivityError:
            self._activity = None
            if self._cancellation_requested():
                await self._finalize_cancellation(reference)
                return reference.run_id
            await self._finalize_failure(reference)
            raise
        except asyncio.CancelledError:
            self._activity = None
            await self._finalize_cancellation(reference)
            return reference.run_id

    async def _finalize_failure(self, reference: RunWorkflowReference) -> None:
        await workflow.execute_activity(
            FINALIZE_FAILURE_ACTIVITY,
            reference,
            start_to_close_timeout=FINALIZE_TIMEOUT,
            retry_policy=CONTROL_RETRY_POLICY,
        )

    def _cancellation_requested(self) -> bool:
        return self._cancel_requested

    async def _finalize_cancellation(self, reference: RunWorkflowReference) -> None:
        await workflow.execute_activity(
            FINALIZE_CANCELLATION_ACTIVITY,
            reference,
            start_to_close_timeout=FINALIZE_TIMEOUT,
            retry_policy=CONTROL_RETRY_POLICY,
        )


__all__ = ["CorpusRunWorkflow"]
