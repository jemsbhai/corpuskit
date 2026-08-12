"""Opaque workflow-boundary, dispatcher, registry, and policy tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from corpuskit.config import Settings
from corpuskit.domain.jobs import RunKind
from corpuskit.services.jobs import DispatchMessage
from corpuskit.worker.routing import durable_task_queue_map
from corpuskit.workflows.contracts import RunWorkflowReference, workflow_id
from corpuskit.workflows.dispatcher import TemporalDispatcher
from corpuskit.workflows.handlers import (
    HandlerRegistry,
    PhonemizeRunSpec,
    RunExecutionError,
    SelectRunSpec,
    build_core_handler_registry,
)
from corpuskit.workflows.policies import CANCELLATION_SIGNAL, WORKFLOW_NAME


class RecordingHandle:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.signals: list[str] = []

    async def signal(self, signal: str) -> None:
        self.signals.append(signal)
        if self.fail:
            raise RuntimeError("closed")


class RecordingClient:
    def __init__(self, handle: RecordingHandle | None = None) -> None:
        self.handle = handle or RecordingHandle()
        self.starts: list[tuple[str, RunWorkflowReference, dict[str, Any]]] = []
        self.workflow_ids: list[str] = []

    async def start_workflow(
        self,
        workflow_name: str,
        reference: RunWorkflowReference,
        **options: Any,
    ) -> RecordingHandle:
        self.starts.append((workflow_name, reference, options))
        return self.handle

    def get_workflow_handle(self, temporal_id: str) -> RecordingHandle:
        self.workflow_ids.append(temporal_id)
        return self.handle


class TerminalProbe:
    def __init__(self, terminal: bool) -> None:
        self.terminal = terminal
        self.references: list[RunWorkflowReference] = []

    async def is_terminal(self, reference: RunWorkflowReference) -> bool:
        self.references.append(reference)
        return self.terminal


def _message(
    event_type: str = "run.dispatch",
    *,
    kind: RunKind = RunKind.PHONEMIZE,
) -> DispatchMessage:
    run_id = uuid4()
    return DispatchMessage(
        id=uuid4(),
        organization_id=uuid4(),
        run_id=run_id,
        event_type=event_type,
        payload={
            "kind": kind.value,
            "run_id": str(run_id),
            "spec_sha256": "a" * 64,
        },
        attempt=1,
    )


def test_workflow_reference_is_canonical_and_contains_no_spec_or_text() -> None:
    reference = RunWorkflowReference(
        organization_id=str(uuid4()),
        run_id=str(uuid4()),
        spec_sha256="a" * 64,
    )

    assert reference.validate() is reference
    assert set(asdict(reference)) == {"organization_id", "run_id", "spec_sha256"}
    assert workflow_id(reference).startswith("corpuskit-run-")
    with pytest.raises(ValueError, match="UUID"):
        RunWorkflowReference("NOT-A-UUID", reference.run_id, reference.spec_sha256).validate()
    with pytest.raises(ValueError, match="SHA-256"):
        RunWorkflowReference(reference.organization_id, reference.run_id, "A" * 64).validate()


@pytest.mark.asyncio
async def test_dispatch_start_is_deterministic_and_deduplicated_by_run_and_request_ids() -> None:
    client = RecordingClient()
    dispatcher = TemporalDispatcher(
        client, task_queue="batch-cpu", terminal_probe=TerminalProbe(False)
    )
    message = _message()

    await dispatcher.publish(message)
    await dispatcher.publish(message)

    assert len(client.starts) == 2
    name, reference, options = client.starts[0]
    assert name == WORKFLOW_NAME
    assert options["id"] == workflow_id(reference)
    assert options["request_id"] == str(message.id)
    assert options["task_queue"] == "batch-cpu"
    assert client.starts[1][2]["id"] == options["id"]
    assert set(asdict(reference)) == {"organization_id", "run_id", "spec_sha256"}


@pytest.mark.asyncio
async def test_dispatch_uses_exact_server_owned_profile_without_fallback() -> None:
    client = RecordingClient()
    dispatcher = TemporalDispatcher(
        client,
        task_queues=durable_task_queue_map(),
        terminal_probe=TerminalProbe(False),
    )
    for kind, expected in (
        (RunKind.GENERATE_REPOSITORY, "external-provider"),
        (RunKind.GENERATE_LLM, "external-provider"),
        (RunKind.GENERATE_LOCAL, "gpu-inference"),
        (RunKind.BUILD_DATG_INDEX, "batch-cpu"),
        (RunKind.GENERATE_DATG, "gpu-inference"),
        (RunKind.TRAIN_PHON_RL, "gpu-training"),
    ):
        await dispatcher.publish(_message(kind=kind))
        assert client.starts[-1][2]["task_queue"] == expected

    missing = TemporalDispatcher(
        client,
        task_queues={RunKind.PHONEMIZE: "batch-cpu"},
        terminal_probe=TerminalProbe(False),
    )
    with pytest.raises(ValueError, match="no configured worker profile"):
        await missing.publish(_message(kind=RunKind.GENERATE_LLM))
    assert client.starts[-1][2]["task_queue"] == "gpu-training"


@pytest.mark.asyncio
async def test_dispatch_cancellation_signals_and_closed_terminal_is_acknowledged() -> None:
    handle = RecordingHandle()
    client = RecordingClient(handle)
    probe = TerminalProbe(False)
    dispatcher = TemporalDispatcher(client, task_queue="batch-cpu", terminal_probe=probe)
    message = _message("run.cancel")

    await dispatcher.publish(message)

    assert handle.signals == [CANCELLATION_SIGNAL]
    assert client.workflow_ids[0].startswith("corpuskit-run-")
    closed = RecordingClient(RecordingHandle(fail=True))
    terminal_probe = TerminalProbe(True)
    await TemporalDispatcher(closed, task_queue="batch-cpu", terminal_probe=terminal_probe).publish(
        message
    )
    assert len(terminal_probe.references) == 1


@pytest.mark.asyncio
async def test_dispatch_rejects_unknown_events_and_payload_expansion() -> None:
    dispatcher = TemporalDispatcher(
        RecordingClient(), task_queue="batch-cpu", terminal_probe=TerminalProbe(False)
    )
    unknown = _message("run.secret")
    expanded = _message()
    expanded.payload["text"] = "must never enter Temporal history"  # type: ignore[index]

    with pytest.raises(ValueError, match="unsupported"):
        await dispatcher.publish(unknown)
    with pytest.raises(ValueError, match="opaque"):
        await dispatcher.publish(expanded)
    with pytest.raises(ValueError, match="task_queue"):
        TemporalDispatcher(RecordingClient(), task_queue="", terminal_probe=TerminalProbe(False))


@dataclass(frozen=True, slots=True)
class StaticHandler:
    kind: RunKind
    result: dict[str, Any]

    def execute(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        assert spec == {"ok": True}
        return self.result


def test_handler_registry_is_explicit_unique_and_json_safe() -> None:
    handler = StaticHandler(RunKind.PHONEMIZE, {"count": 1})
    registry = HandlerRegistry((handler,))

    assert registry.kinds == {RunKind.PHONEMIZE}
    assert registry.execute(RunKind.PHONEMIZE, {"ok": True}) == {"count": 1}
    with pytest.raises(ValueError, match="duplicate"):
        HandlerRegistry((handler, handler))
    with pytest.raises(RunExecutionError) as unsupported:
        registry.execute(RunKind.EXPORT, {"ok": True})
    assert unsupported.value.code == "unsupported_run_kind"
    unsafe = HandlerRegistry((StaticHandler(RunKind.PHONEMIZE, {"value": float("nan")}),))
    with pytest.raises(RunExecutionError) as invalid:
        unsafe.execute(RunKind.PHONEMIZE, {"ok": True})
    assert invalid.value.code == "invalid_run_spec"


def test_core_specs_reject_ambiguous_phonemize_and_unseeded_random_selection() -> None:
    assert PhonemizeRunSpec(text="hello").text == "hello"
    assert PhonemizeRunSpec(texts=("one", "two")).texts == ("one", "two")
    with pytest.raises(ValueError, match="exactly one"):
        PhonemizeRunSpec()
    with pytest.raises(ValueError, match="exactly one"):
        PhonemizeRunSpec(text="hello", texts=("world",))
    with pytest.raises(ValueError, match="seed"):
        SelectRunSpec(
            candidates=("hello",),
            options={"algorithm": "stochastic"},  # type: ignore[arg-type]
        )


def test_core_registry_fails_closed_on_privileged_worker_profile() -> None:
    settings = Settings(
        environment="test",
        worker_profile="external-provider",
        temporal_task_queue="external-provider",
        _env_file=None,
    )

    with pytest.raises(RuntimeError, match="batch-cpu"):
        build_core_handler_registry(settings)


def test_temporal_task_queue_cannot_diverge_from_server_worker_profile() -> None:
    with pytest.raises(ValueError, match="task queue"):
        Settings(
            worker_profile="batch-cpu",
            temporal_task_queue="browser-selected-queue",
            _env_file=None,
        )


def test_reference_requires_canonical_uuid_text() -> None:
    organization_id = uuid4()
    reference = RunWorkflowReference(
        organization_id=f"{{{organization_id}}}",
        run_id=str(UUID(int=1)),
        spec_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="canonical"):
        reference.validate()
