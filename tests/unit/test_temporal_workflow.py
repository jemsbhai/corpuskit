"""Deterministic workflow branch tests without persistence or CorpusGen imports."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import NoReturn
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.exceptions import ActivityError, RetryState

from corpuskit.workflows.contracts import RunWorkflowReference
from corpuskit.workflows.workflow import CorpusRunWorkflow


def _reference() -> RunWorkflowReference:
    return RunWorkflowReference(str(uuid4()), str(uuid4()), "a" * 64)


class ImmediateHandle:
    def __init__(self, callback=None) -> None:  # type: ignore[no-untyped-def]
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self) -> Generator[None, None, None]:
        if self.callback is not None:
            self.callback()
        if False:
            yield
        return None


class ErrorHandle(ImmediateHandle):
    def __init__(self, error: BaseException, callback=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(callback)
        self.error = error

    def __await__(self) -> Generator[None, None, NoReturn]:
        if self.callback is not None:
            self.callback()
        if False:
            yield
        raise self.error


def _activity_error() -> ActivityError:
    return ActivityError(
        "safe",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="test",
        activity_id="test",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )


@pytest.mark.asyncio
async def test_signal_before_start_finalizes_cancellation_with_opaque_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = CorpusRunWorkflow()
    reference = _reference()
    finalized: list[RunWorkflowReference] = []

    async def execute_activity(_name: str, value: RunWorkflowReference, **_options: object) -> None:
        finalized.append(value)

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    instance.request_cancellation()

    assert await instance.run(reference) == reference.run_id
    assert finalized == [reference]


@pytest.mark.asyncio
async def test_signal_between_prepare_and_execute_skips_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = CorpusRunWorkflow()
    reference = _reference()
    starts: list[ImmediateHandle] = []

    def start_activity(*_args: object, **_kwargs: object) -> ImmediateHandle:
        handle = ImmediateHandle(instance.request_cancellation)
        starts.append(handle)
        return handle

    async def execute_activity(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow, "start_activity", start_activity)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    assert await instance.run(reference) == reference.run_id
    assert len(starts) == 1
    assert starts[0].cancelled is True


@pytest.mark.asyncio
async def test_activity_error_after_signal_is_acknowledged_as_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = CorpusRunWorkflow()
    reference = _reference()
    finalized = 0

    def start_activity(*_args: object, **_kwargs: object) -> ErrorHandle:
        return ErrorHandle(_activity_error(), instance.request_cancellation)

    async def execute_activity(*_args: object, **_kwargs: object) -> None:
        nonlocal finalized
        finalized += 1

    monkeypatch.setattr(workflow, "start_activity", start_activity)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    assert await instance.run(reference) == reference.run_id
    assert finalized == 1


@pytest.mark.asyncio
async def test_workflow_cancellation_reaches_database_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = CorpusRunWorkflow()
    reference = _reference()
    finalized = 0

    def start_activity(*_args: object, **_kwargs: object) -> ErrorHandle:
        return ErrorHandle(asyncio.CancelledError())

    async def execute_activity(*_args: object, **_kwargs: object) -> None:
        nonlocal finalized
        finalized += 1

    monkeypatch.setattr(workflow, "start_activity", start_activity)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    assert await instance.run(reference) == reference.run_id
    assert finalized == 1


def test_signal_cancels_an_active_activity_handle() -> None:
    instance = CorpusRunWorkflow()
    handle = ImmediateHandle()
    instance._activity = handle

    instance.request_cancellation()

    assert handle.cancelled is True
