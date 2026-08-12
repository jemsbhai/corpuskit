"""Bounds, state-machine, allowlist, and pure-handler tests."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.domain.errors import (
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    AcceptedCandidate,
    GenerationExecutionMode,
    GenerationPhase,
    GenerationProgress,
    GenerationSourceKind,
    GenerationStoppingCriteria,
    GenerationStopReason,
    GenerationTarget,
    HuggingFaceRepository,
    HuggingFaceRepositorySpec,
    PhonotacticArtifact,
    PhonotacticArtifactType,
    PrephonemizedRepository,
    RawTextCandidate,
    RawTextRepository,
    RepositoryCandidate,
    RepositoryGenerationRequest,
    RepositoryGenerationResult,
)
from corpuskit.services.generation_scoring import (
    GenerationCoordinator,
    GenerationPreviewService,
)
from corpuskit.worker.generation_handler import (
    ProcessActivityDeadlineExecutor,
    RepositoryGenerationDurableHandler,
    RepositoryGenerationJobHandler,
    _activity_process,
)
from corpuskit.workflows.progress import DurableRunProgress, RunProgressPhase


def _candidate(source_id: str = "source-1") -> RepositoryCandidate:
    return RepositoryCandidate(source_id=source_id, text="A sentence.", phonemes=("a",))


def _request(source: Any | None = None, **changes: Any) -> RepositoryGenerationRequest:
    values: dict[str, Any] = {
        "source": source or PrephonemizedRepository(entries=(_candidate(),)),
        "target": GenerationTarget(phonemes=("a",)),
        "stopping": GenerationStoppingCriteria(
            max_sentences=2,
            max_iterations=3,
            timeout_seconds=1.0,
        ),
        "activity_timeout_seconds": 2.0,
    }
    values.update(changes)
    return RepositoryGenerationRequest(**values)


class RecordingEngine:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def run_repository(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        on_accepted: Callable[[AcceptedCandidate, float], None] | None = None,
    ) -> RepositoryGenerationResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("private backend detail")
        accepted = AcceptedCandidate(
            source_id="source-1",
            text="A sentence.",
            phonemes=("a",),
            iteration=1,
            coverage_gain=1,
        )
        if on_accepted is not None:
            on_accepted(accepted, 1.0)
        return RepositoryGenerationResult(
            execution_mode=execution_mode,
            source_kind=request.source.kind,
            unit=request.target.unit,
            accepted=(accepted,),
            coverage=1.0,
            covered_units=("a",),
            missing_units=(),
            iterations=1,
            elapsed_seconds=0.01,
            stop_reason=GenerationStopReason.TARGET_COVERAGE,
        )


class InlineDeadlineExecutor:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def run(
        self,
        coordinator: GenerationCoordinator,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        timeout_seconds: float,
        emit: Callable[[GenerationProgress], None] | None,
    ) -> RepositoryGenerationResult:
        self.timeout = timeout_seconds
        return coordinator.execute(request, execution_mode=execution_mode, emit=emit)


class TimeoutExecutor(InlineDeadlineExecutor):
    def run(self, *_: Any, **__: Any) -> RepositoryGenerationResult:
        raise EngineUnavailableError("generation.activity.timeout")


class SleepingEngine(RecordingEngine):
    def run_repository(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        on_accepted: Callable[[AcceptedCandidate, float], None] | None = None,
    ) -> RepositoryGenerationResult:
        time.sleep(10)
        return super().run_repository(
            request,
            execution_mode=execution_mode,
            on_accepted=on_accepted,
        )


class SilentEngine(RecordingEngine):
    def run_repository(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        on_accepted: Callable[[AcceptedCandidate, float], None] | None = None,
    ) -> RepositoryGenerationResult:
        del on_accepted
        return super().run_repository(
            request,
            execution_mode=execution_mode,
            on_accepted=None,
        )


class FakeConnection:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.closed = False

    def send(self, message: object) -> None:
        self.messages.append(message)

    def close(self) -> None:
        self.closed = True


class BrokenCoordinator:
    def execute(self, *_: object, **__: object) -> RepositoryGenerationResult:
        raise ValueError("secret")


class OutOfOrderExecutor(InlineDeadlineExecutor):
    def run(
        self,
        coordinator: GenerationCoordinator,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        timeout_seconds: float,
        emit: Callable[[GenerationProgress], None] | None,
    ) -> RepositoryGenerationResult:
        del coordinator, request, execution_mode, timeout_seconds
        assert emit is not None
        event = GenerationProgress(sequence=1, phase=GenerationPhase.GENERATING)
        emit(event)
        emit(event)
        raise AssertionError("unreachable")


class ExplodingExecutor(InlineDeadlineExecutor):
    def run(self, *_: object, **__: object) -> RepositoryGenerationResult:
        raise ValueError("secret")


class RecordingStager:
    def __init__(self, *, fail: bool = False, wrong_digest: bool = False) -> None:
        self.fail = fail
        self.wrong_digest = wrong_digest
        self.calls: list[tuple[object, bytes, str]] = []

    def stage_model_result(
        self,
        *,
        kind: object,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        self.calls.append((kind, payload, content_sha256))
        if self.fail:
            raise OSError("private storage detail")
        digest = "0" * 64 if self.wrong_digest else content_sha256
        return f"staged-artifact://sha256/{digest}"


def test_stopping_criteria_requires_a_finite_safety_stop() -> None:
    with pytest.raises(ValidationError):
        GenerationStoppingCriteria(
            max_sentences=None,
            max_iterations=None,
            timeout_seconds=None,
        )
    for payload in (
        {"target_coverage": float("nan")},
        {"timeout_seconds": float("inf")},
        {"max_iterations": 101},
        {"max_sentences": 251},
    ):
        with pytest.raises(ValidationError):
            GenerationStoppingCriteria(**payload)


def test_repository_and_target_contracts_reject_ambiguity_and_explosion() -> None:
    with pytest.raises(ValidationError):
        PrephonemizedRepository(entries=(_candidate(), _candidate()))
    with pytest.raises(ValidationError):
        RawTextCandidate(source_id="unsafe id", text="text")
    with pytest.raises(ValidationError):
        RawTextCandidate(source_id="safe", text="   ")
    with pytest.raises(ValidationError):
        GenerationTarget(
            phonemes=tuple(f"p{index}" for index in range(17)),
            unit="triphone",
        )
    with pytest.raises(ValidationError):
        GenerationTarget(phonemes=("a", "a"))


def test_huggingface_manifest_requires_explicit_immutable_safe_pin() -> None:
    valid = HuggingFaceRepositorySpec(
        dataset="owner/corpus",
        config="clean",
        split="train",
        text_column="sentence",
        revision="a" * 40,
    )
    assert valid.trust_remote_code is False
    for changes in (
        {"dataset": "unqualified"},
        {"config": "bad config"},
        {"split": "../train"},
        {"text_column": ""},
        {"revision": "main"},
        {"revision": "A" * 40},
        {"trust_remote_code": True},
        {"max_samples": 1_001},
    ):
        with pytest.raises(ValidationError):
            HuggingFaceRepositorySpec.model_validate({**valid.model_dump(), **changes})


def test_artifact_integrity_and_json_safety_are_enforced() -> None:
    artifact = PhonotacticArtifact.build(
        PhonotacticArtifactType.NGRAM_SCORER,
        {"n": 2, "phonemes": ["a", "b"]},
    )
    assert len(artifact.content_sha256) == 64
    with pytest.raises(ValidationError):
        artifact.model_copy(update={"payload": {"n": 3}}).model_validate(
            artifact.model_copy(update={"payload": {"n": 3}}).model_dump()
        )
    with pytest.raises(ValueError, match="JSON-safe"):
        PhonotacticArtifact.build(
            PhonotacticArtifactType.NGRAM_SCORER,
            {"score": float("nan")},
        )


def test_coordinator_emits_ordered_truthful_progress() -> None:
    events: list[GenerationProgress] = []
    result = GenerationCoordinator(RecordingEngine()).execute(
        _request(),
        execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
        emit=events.append,
    )

    assert result.execution_mode is GenerationExecutionMode.SYNCHRONOUS_PREVIEW
    assert [event.sequence for event in events] == list(range(5))
    assert [event.phase for event in events] == [
        GenerationPhase.VALIDATING,
        GenerationPhase.PREPARING_REPOSITORY,
        GenerationPhase.GENERATING,
        GenerationPhase.CANDIDATE_ACCEPTED,
        GenerationPhase.FINISHED,
    ]
    assert events[-1].stop_reason is GenerationStopReason.TARGET_COVERAGE


def test_remote_source_is_rejected_in_preview_without_engine_or_network() -> None:
    engine = RecordingEngine()
    source = HuggingFaceRepository(
        spec=HuggingFaceRepositorySpec(
            dataset="owner/corpus",
            config="clean",
            split="train",
            text_column="text",
            revision="b" * 40,
        )
    )
    service = GenerationPreviewService(GenerationCoordinator(engine))

    with pytest.raises(InvalidRequestError) as caught:
        service.preview(_request(source))

    assert caught.value.operation == "generation.preview.remote_source"
    assert engine.calls == 0


def test_huggingface_worker_allowlist_is_exact_and_default_deny() -> None:
    spec = HuggingFaceRepositorySpec(
        dataset="owner/corpus",
        config="clean",
        split="train",
        text_column="text",
        revision="c" * 40,
    )
    request = _request(HuggingFaceRepository(spec=spec))
    engine = RecordingEngine()
    with pytest.raises(InvalidRequestError):
        GenerationCoordinator(engine).validate(
            request,
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
    for incorrect_pin in (
        (spec.dataset, "other", spec.revision),
        (spec.dataset, spec.config, "d" * 40),
        ("other/corpus", spec.config, spec.revision),
    ):
        with pytest.raises(InvalidRequestError):
            GenerationCoordinator(
                engine,
                allowed_huggingface_revisions=frozenset({incorrect_pin}),
            ).validate(request, execution_mode=GenerationExecutionMode.WORKER_ACTIVITY)

    GenerationCoordinator(
        engine,
        allowed_huggingface_revisions=frozenset({(spec.dataset, spec.config, spec.revision)}),
    ).validate(request, execution_mode=GenerationExecutionMode.WORKER_ACTIVITY)

    invalid_language = request.model_copy(
        update={"source": HuggingFaceRepository(spec=spec.model_copy(update={"language": "bad_"}))}
    )
    with pytest.raises(InvalidRequestError) as language_error:
        GenerationCoordinator(
            engine,
            allowed_huggingface_revisions=frozenset({(spec.dataset, spec.config, spec.revision)}),
        ).validate(
            invalid_language,
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
    assert language_error.value.operation == "generation.repository.language"


def test_huggingface_worker_policy_binds_every_selector_and_allows_only_lower_cap() -> None:
    policy = HuggingFaceRepositorySpec(
        dataset="owner/corpus",
        config="clean",
        split="train",
        text_column="text",
        revision="c" * 40,
        language="en-us",
        max_samples=500,
    )
    request_spec = policy.model_copy(update={"max_samples": 250})
    coordinator = GenerationCoordinator(
        RecordingEngine(),
        allowed_huggingface_sources=(policy,),
    )
    coordinator.validate(
        _request(HuggingFaceRepository(spec=request_spec)),
        execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
    )

    for changes in (
        {"dataset": "other/corpus"},
        {"config": "other"},
        {"split": "validation"},
        {"text_column": "sentence"},
        {"revision": "d" * 40},
        {"language": "fr-fr"},
        {"max_samples": 501},
    ):
        denied = request_spec.model_copy(update=changes)
        with pytest.raises(InvalidRequestError) as caught:
            coordinator.validate(
                _request(HuggingFaceRepository(spec=denied)),
                execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
            )
        assert caught.value.operation == "generation.huggingface.allowlist"


def test_engine_failure_is_sanitized_and_never_returned_as_empty_success() -> None:
    events: list[GenerationProgress] = []
    with pytest.raises(EngineUnavailableError) as caught:
        GenerationCoordinator(RecordingEngine(fail=True)).execute(
            _request(),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
            emit=events.append,
        )

    assert "private backend detail" not in str(caught.value)
    assert events[-1].phase is GenerationPhase.FAILED


def test_progress_count_mismatch_is_a_hard_failure() -> None:
    with pytest.raises(EngineUnavailableError) as caught:
        GenerationCoordinator(SilentEngine()).execute(
            _request(),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
    assert caught.value.operation == "generation.progress"


def test_worker_handler_forwards_whole_activity_deadline_and_progress() -> None:
    executor = InlineDeadlineExecutor()
    events: list[GenerationProgress] = []
    handler = RepositoryGenerationJobHandler(
        GenerationCoordinator(RecordingEngine()),
        executor,
    )

    result = handler(_request(activity_timeout_seconds=1.25), events.append)

    assert result.execution_mode is GenerationExecutionMode.WORKER_ACTIVITY
    assert executor.timeout == 1.25
    assert events[-1].phase is GenerationPhase.FINISHED


def test_worker_timeout_emits_failure_and_does_not_fabricate_result() -> None:
    events: list[GenerationProgress] = []
    handler = RepositoryGenerationJobHandler(
        GenerationCoordinator(RecordingEngine()),
        TimeoutExecutor(),
    )

    with pytest.raises(EngineUnavailableError):
        handler(_request(), events.append)

    assert events == [GenerationProgress(sequence=0, phase=GenerationPhase.FAILED)]


@pytest.mark.integration
def test_process_executor_enforces_a_hard_whole_activity_deadline() -> None:
    handler = RepositoryGenerationJobHandler(
        GenerationCoordinator(SleepingEngine()),
        ProcessActivityDeadlineExecutor(),
    )
    started = time.monotonic()

    with pytest.raises(EngineUnavailableError) as caught:
        handler(_request(activity_timeout_seconds=0.25))

    assert caught.value.operation == "generation.activity.timeout"
    assert time.monotonic() - started < 3.0


@pytest.mark.integration
def test_process_executor_returns_structured_success() -> None:
    result = RepositoryGenerationJobHandler(
        GenerationCoordinator(RecordingEngine()),
        ProcessActivityDeadlineExecutor(),
    )(_request(activity_timeout_seconds=5.0))

    assert result.coverage == 1.0
    assert result.execution_mode is GenerationExecutionMode.WORKER_ACTIVITY


def test_activity_child_sends_only_structured_messages() -> None:
    success = FakeConnection()
    _activity_process(
        success,  # type: ignore[arg-type]
        GenerationCoordinator(RecordingEngine()),
        _request(),
        GenerationExecutionMode.WORKER_ACTIVITY,
    )
    assert success.closed is True
    assert success.messages[-1][0] == "result"  # type: ignore[index]

    failure = FakeConnection()
    _activity_process(
        failure,  # type: ignore[arg-type]
        GenerationCoordinator(RecordingEngine(fail=True)),
        _request(),
        GenerationExecutionMode.WORKER_ACTIVITY,
    )
    assert failure.messages[-1][0] == "application_error"  # type: ignore[index]

    broken = FakeConnection()
    _activity_process(
        broken,  # type: ignore[arg-type]
        BrokenCoordinator(),  # type: ignore[arg-type]
        _request(),
        GenerationExecutionMode.WORKER_ACTIVITY,
    )
    assert broken.messages == [("engine_error", {"operation": "generation.activity"})]


def test_activity_message_decoder_accepts_only_versioned_safe_contracts() -> None:
    executor = ProcessActivityDeadlineExecutor()
    events: list[GenerationProgress] = []
    progress = GenerationProgress(sequence=0, phase=GenerationPhase.GENERATING)
    assert (
        executor._handle_message(("progress", progress.model_dump(mode="json")), events.append)
        is None
    )
    assert events == [progress]
    assert executor._handle_message(("progress", progress.model_dump(mode="json")), None) is None

    expected = RecordingEngine().run_repository(
        _request(),
        execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
    )
    assert executor._handle_message(("result", expected.model_dump(mode="json")), None) == expected

    for message in (
        "invalid",
        ("unknown", {}),
        ("progress", {}),
        ("application_error", "invalid"),
        ("application_error", {"code": "invalid", "operation": "op"}),
        (
            "application_error",
            {"code": "invalid_request", "operation": 7},
        ),
    ):
        with pytest.raises(EngineContractError):
            executor._handle_message(message, None)
    with pytest.raises(InvalidRequestError):
        executor._handle_message(
            (
                "application_error",
                {"code": "invalid_request", "operation": "safe.operation"},
            ),
            None,
        )
    with pytest.raises(EngineUnavailableError):
        executor._handle_message(("engine_error", {}), None)


def test_handler_rejects_out_of_order_progress_and_sanitizes_executor_errors() -> None:
    events: list[GenerationProgress] = []
    with pytest.raises(EngineContractError):
        RepositoryGenerationJobHandler(
            GenerationCoordinator(RecordingEngine()), OutOfOrderExecutor()
        )(_request(), events.append)
    assert [event.phase for event in events] == [
        GenerationPhase.GENERATING,
        GenerationPhase.FAILED,
    ]

    events.clear()
    with pytest.raises(EngineUnavailableError):
        RepositoryGenerationJobHandler(
            GenerationCoordinator(RecordingEngine()), ExplodingExecutor()
        )(_request(), events.append)
    assert events[-1].phase is GenerationPhase.FAILED


def test_durable_repository_handler_stages_canonical_result_and_fails_closed() -> None:
    stager = RecordingStager()
    handler = RepositoryGenerationDurableHandler(
        GenerationCoordinator(RecordingEngine()),
        stager,
    )

    summary = handler.execute(_request().model_dump(mode="json"))

    assert summary == {
        "contract": "corpuskit.staged-artifact-result.v1",
        "staged_artifact_ref": f"staged-artifact://sha256/{stager.calls[0][2]}",
        "schema_id": "corpuskit.repository-generation-result.v1",
        "artifact_type": "run-result",
        "media_type": "application/json",
        "size_bytes": len(stager.calls[0][1]),
    }
    assert b'"execution_mode":"worker_activity"' in stager.calls[0][1]

    for broken in (
        RecordingStager(fail=True),
        RecordingStager(wrong_digest=True),
    ):
        with pytest.raises((EngineUnavailableError, EngineContractError)):
            RepositoryGenerationDurableHandler(
                GenerationCoordinator(RecordingEngine()),
                broken,
            ).execute(_request().model_dump(mode="json"))


def test_durable_repository_handler_emits_sanitized_state_machine_progress() -> None:
    events: list[DurableRunProgress] = []
    RepositoryGenerationDurableHandler(
        GenerationCoordinator(RecordingEngine()),
        RecordingStager(),
    ).execute_with_progress(_request().model_dump(mode="json"), events.append)

    assert [event.sequence for event in events] == list(range(6))
    assert [event.phase for event in events] == [
        RunProgressPhase.VALIDATING,
        RunProgressPhase.PREPARING_REPOSITORY,
        RunProgressPhase.GENERATING,
        RunProgressPhase.CANDIDATE_ACCEPTED,
        RunProgressPhase.STAGING_RESULT,
        RunProgressPhase.FINISHED,
    ]
    assert events[3].coverage == 1.0
    assert events[3].accepted_count == 1
    assert events[-1].completed == 1
    assert "source-1" not in repr(events)
    assert "A sentence" not in repr(events)


def test_local_repository_count_and_byte_caps_are_mode_specific() -> None:
    engine = RecordingEngine()
    too_many = RawTextRepository(
        entries=tuple(RawTextCandidate(source_id=f"row-{index}", text="a") for index in range(251))
    )
    with pytest.raises(InvalidRequestError) as count_error:
        GenerationCoordinator(engine).validate(
            _request(too_many),
            execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
        )
    assert count_error.value.operation == "generation.repository.size"

    too_large = RawTextRepository(
        entries=tuple(
            RawTextCandidate(source_id=f"large-{index}", text="a" * 4_000) for index in range(300)
        )
    )
    with pytest.raises(InvalidRequestError) as byte_error:
        GenerationCoordinator(engine).validate(
            _request(too_large),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
    assert byte_error.value.operation == "generation.repository.payload"


def test_local_repository_policy_rejects_invalid_language_before_engine() -> None:
    engine = RecordingEngine()
    source = RawTextRepository(
        entries=(RawTextCandidate(source_id="one", text="A sentence."),),
        language="bad_language",
    )
    coordinator = GenerationCoordinator(engine)

    with pytest.raises(InvalidRequestError):
        coordinator.execute(
            _request(source),
            execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
        )

    assert engine.calls == 0
    assert source.kind is GenerationSourceKind.RAW_TEXT
