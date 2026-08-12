"""Whole-activity deadline, message integrity and pure model job-handler tests."""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from typing import cast

import pytest

from corpuskit.adapters.corpusgen.model_runtime import CorpusgenModelRuntimeAdapter
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError, InvalidRequestError
from corpuskit.domain.generation import (
    AcceptedCandidate,
    GenerationStoppingCriteria,
    GenerationStopReason,
    GenerationTarget,
)
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    AnalysisText,
    CorpusPerplexity,
    FluencyScore,
    HostedExecutionManifest,
    HostedGenerationRequest,
    HostedGenerationResult,
    HostedModelPolicy,
    HostedModelSelection,
    HostedRunBudget,
    HostedUsage,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LanguageModelAnalysisResult,
    LocalGenerationRequest,
    LocalGenerationResult,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelExecutionManifest,
    ModelQuantization,
    PerplexitySentenceStatus,
    ProviderRetryPolicy,
    ReproducibilityClass,
    SecretReference,
    SentencePerplexity,
    WorkerModelProfile,
)
from corpuskit.services.model_runtime import ModelRuntimeCoordinator, ModelRuntimePolicy
from corpuskit.worker.model_handlers import (
    HostedGenerationJobHandler,
    LanguageModelAnalysisJobHandler,
    LocalGenerationJobHandler,
    ProcessModelActivityDeadlineExecutor,
    _dispatch,
    _model_activity_process,
)
from corpuskit.worker.model_registry import (
    MAX_MODEL_RESULT_ARTIFACT_BYTES,
    _stage,
    build_model_handler_registry,
    model_activity_timeout_seconds,
)
from corpuskit.workflows.deadlines import activity_deadline_seconds
from corpuskit.workflows.handlers import RunExecutionError

SECRET = SecretReference(reference="secret://env/MODEL_HANDLER_KEY")
PIN = ImmutableModelPin(model="acme/tiny-causal", revision="a" * 40)
ARTIFACT_DIGEST = "b" * 64


def hosted_policy() -> HostedModelPolicy:
    return HostedModelPolicy(
        provider="openai",
        model="openai/demo-model",
        connection_id="demo-provider",
        credential_ref=SECRET,
        input_cost_per_million_usd=Decimal("1"),
        output_cost_per_million_usd=Decimal("2"),
        max_output_tokens_per_request=64,
    )


def local_policy() -> LocalModelPolicy:
    return LocalModelPolicy(
        pin=PIN,
        artifact_sha256=ARTIFACT_DIGEST,
        allowed_devices=(ModelDevice.CPU,),
        allowed_quantizations=(ModelQuantization.NONE,),
    )


def hosted_request(*, timeout: float = 2.0) -> HostedGenerationRequest:
    return HostedGenerationRequest(
        selection=HostedModelSelection(
            provider="openai",
            model="openai/demo-model",
            connection_id="demo-provider",
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1,
        ),
        max_tokens_per_request=16,
        retry=ProviderRetryPolicy(max_retries=0, request_timeout_seconds=1),
        budget=HostedRunBudget(
            max_requests=1,
            max_input_tokens=2_000,
            max_output_tokens=16,
            max_cost_usd=Decimal("1"),
        ),
        activity_timeout_seconds=timeout,
        external_processing_confirmed=True,
    )


def local_request(*, timeout: float = 2.0) -> LocalGenerationRequest:
    return LocalGenerationRequest(
        selection=LocalModelSelection(pin=PIN),
        target=GenerationTarget(phonemes=("p",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1,
        ),
        activity_timeout_seconds=timeout,
    )


def analysis_request(*, timeout: float = 2.0) -> LanguageModelAnalysisRequest:
    return LanguageModelAnalysisRequest(
        selection=LocalModelSelection(pin=PIN),
        texts=(AnalysisText(source_id="one", text="A complete sentence."),),
        activity_timeout_seconds=timeout,
    )


def hosted_result() -> HostedGenerationResult:
    request = hosted_request()
    return HostedGenerationResult(
        manifest=HostedExecutionManifest(
            provider="openai",
            model="openai/demo-model",
            temperature=request.temperature,
            max_tokens_per_request=request.max_tokens_per_request,
            prompt_template_sha256="c" * 64,
            custom_prompt_template=False,
            retry=request.retry,
            budget=request.budget,
            whole_activity_timeout_seconds=request.activity_timeout_seconds,
        ),
        accepted=(
            AcceptedCandidate(
                source_id="generated:one",
                text="Pat.",
                phonemes=("p",),
                iteration=1,
                coverage_gain=1,
            ),
        ),
        coverage=1,
        covered_units=("p",),
        missing_units=(),
        iterations=1,
        elapsed_seconds=0.01,
        stop_reason=GenerationStopReason.TARGET_COVERAGE,
        usage=HostedUsage(
            requests=1,
            retries=0,
            input_tokens=2,
            output_tokens=2,
            reserved_input_tokens=10,
            reserved_output_tokens=16,
            actual_cost_usd=Decimal("0.000006"),
            reserved_cost_usd=Decimal("0.000042"),
        ),
    )


def local_manifest() -> ModelExecutionManifest:
    return ModelExecutionManifest(
        model=PIN.model,
        revision=PIN.revision,
        artifact_sha256=ARTIFACT_DIGEST,
        device=ModelDevice.CPU,
        quantization=ModelQuantization.NONE,
        sampling_enabled=False,
        seed=0,
    )


def local_result() -> LocalGenerationResult:
    return LocalGenerationResult(
        model=local_manifest(),
        accepted=(
            AcceptedCandidate(
                source_id="generated:local",
                text="Pat.",
                phonemes=("p",),
                iteration=1,
                coverage_gain=1,
            ),
        ),
        coverage=1,
        covered_units=("p",),
        missing_units=(),
        iterations=1,
        elapsed_seconds=0.01,
        stop_reason=GenerationStopReason.TARGET_COVERAGE,
        reproducibility=ReproducibilityClass.BEST_EFFORT,
    )


def analysis_result() -> LanguageModelAnalysisResult:
    return LanguageModelAnalysisResult(
        model=local_manifest(),
        fluency=(FluencyScore(source_id="one", score=0.75),),
        perplexity=CorpusPerplexity(
            per_sentence=(2.0,),
            corpus_perplexity=2,
            mean_perplexity=2,
            median_perplexity=2,
            std_perplexity=0,
            min_perplexity=2,
            max_perplexity=2,
            num_sentences=1,
            num_tokens=4,
            total_nll=2.7,
        ),
        sentence_perplexities=(
            SentencePerplexity(
                source_id="one",
                status=PerplexitySentenceStatus.SCORED,
                perplexity=2.0,
            ),
        ),
        input_sentence_count=1,
        scored_sentence_count=1,
    )


class RecordingEngine:
    def run_hosted(self, request: object, policy: object) -> HostedGenerationResult:
        del request, policy
        return hosted_result()

    def run_local(
        self,
        request: object,
        policy: object,
        profile: object,
    ) -> LocalGenerationResult:
        del request, policy, profile
        return local_result()

    def analyze_language_model(
        self,
        request: object,
        policy: object,
        profile: object,
    ) -> LanguageModelAnalysisResult:
        del request, policy, profile
        return analysis_result()


class SleepingEngine(RecordingEngine):
    def run_hosted(self, request: object, policy: object) -> HostedGenerationResult:
        del request, policy
        time.sleep(5)
        return hosted_result()


class BrokenEngine(RecordingEngine):
    def run_hosted(self, request: object, policy: object) -> HostedGenerationResult:
        del request, policy
        raise RuntimeError("raw private engine failure")


def coordinator(engine: object | None = None) -> ModelRuntimeCoordinator:
    return ModelRuntimeCoordinator(
        ModelRuntimePolicy(
            hosted_models=(hosted_policy(),),
            local_models=(local_policy(),),
            worker_profile=WorkerModelProfile.LOCAL_CPU,
        ),
        cast(RecordingEngine, engine or RecordingEngine()),
    )


class InlineExecutor:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.operation: str | None = None

    def run(
        self,
        runtime: ModelRuntimeCoordinator,
        operation: object,
        request: object,
        *,
        timeout_seconds: float,
    ) -> object:
        self.timeout = timeout_seconds
        self.operation = cast(str, operation)
        return _dispatch(runtime, cast(object, operation), cast(object, request))  # type: ignore[arg-type]


class ExplodingExecutor(InlineExecutor):
    def run(self, *_: object, **__: object) -> object:
        raise RuntimeError("private executor failure")


class FakeConnection:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.closed = False

    def send(self, message: object) -> None:
        self.messages.append(message)

    def close(self) -> None:
        self.closed = True


class MemoryArtifactStager:
    """Test-only byte sink; it deliberately has no tenant/run metadata API."""

    def __init__(self, *, invalid_reference: bool = False) -> None:
        self.invalid_reference = invalid_reference
        self.payloads: list[tuple[RunKind, bytes, str]] = []

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        self.payloads.append((kind, payload, content_sha256))
        if self.invalid_reference:
            return f"staged-artifact://sha256/{'0' * 64}"
        return f"staged-artifact://sha256/{content_sha256}"


def test_three_handlers_forward_exact_whole_activity_deadlines() -> None:
    hosted_executor = InlineExecutor()
    local_executor = InlineExecutor()
    analysis_executor = InlineExecutor()
    runtime = coordinator()

    hosted = HostedGenerationJobHandler(runtime, hosted_executor)(hosted_request(timeout=1.25))
    local = LocalGenerationJobHandler(runtime, local_executor)(local_request(timeout=1.5))
    analysis = LanguageModelAnalysisJobHandler(runtime, analysis_executor)(
        analysis_request(timeout=1.75)
    )

    assert hosted.coverage == local.coverage == 1
    assert analysis.perplexity.corpus_perplexity == 2
    assert hosted_executor.timeout == 1.25
    assert local_executor.timeout == 1.5
    assert analysis_executor.timeout == 1.75
    assert [hosted_executor.operation, local_executor.operation, analysis_executor.operation] == [
        "hosted_generation",
        "local_generation",
        "language_model_analysis",
    ]


def test_handler_validates_before_executor_and_sanitizes_unknown_failures() -> None:
    denied = ModelRuntimeCoordinator(ModelRuntimePolicy(), RecordingEngine())
    with pytest.raises(InvalidRequestError):
        HostedGenerationJobHandler(denied, ExplodingExecutor())(hosted_request())

    with pytest.raises(EngineUnavailableError) as caught:
        HostedGenerationJobHandler(coordinator(), ExplodingExecutor())(hosted_request())
    assert "private executor" not in str(caught.value)


@pytest.mark.integration
def test_process_executor_enforces_deadline_over_entire_hosted_activity() -> None:
    started = time.monotonic()
    with pytest.raises(EngineUnavailableError) as caught:
        HostedGenerationJobHandler(
            coordinator(SleepingEngine()),
            ProcessModelActivityDeadlineExecutor(),
        )(hosted_request(timeout=0.25))
    assert caught.value.operation == "model_runtime.activity.timeout"
    assert time.monotonic() - started < 3


@pytest.mark.integration
def test_process_executor_returns_only_validated_result_contracts() -> None:
    result = HostedGenerationJobHandler(
        coordinator(),
        ProcessModelActivityDeadlineExecutor(),
    )(hosted_request(timeout=5))
    assert result.schema_id == "corpuskit.hosted-generation-result.v1"


@pytest.mark.integration
def test_process_executor_can_spawn_the_default_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_HANDLER_KEY", raising=False)
    runtime = ModelRuntimeCoordinator(
        ModelRuntimePolicy(hosted_models=(hosted_policy(),)),
        CorpusgenModelRuntimeAdapter(),
    )
    with pytest.raises(EngineUnavailableError) as caught:
        HostedGenerationJobHandler(
            runtime,
            ProcessModelActivityDeadlineExecutor(),
        )(hosted_request(timeout=5))
    assert caught.value.operation == "model_runtime.secret.resolve"


def test_child_process_target_sends_structured_success_and_sanitized_failure() -> None:
    success = FakeConnection()
    _model_activity_process(
        success,  # type: ignore[arg-type]
        coordinator(),
        "hosted_generation",
        hosted_request(),
    )
    assert success.closed is True
    assert cast(tuple[object, object], success.messages[0])[0] == "result"

    failure = FakeConnection()
    _model_activity_process(
        failure,  # type: ignore[arg-type]
        coordinator(BrokenEngine()),
        "hosted_generation",
        hosted_request(),
    )
    kind, payload = cast(tuple[str, object], failure.messages[0])
    assert kind == "application_error"
    assert "private" not in str(payload)

    broken = FakeConnection()
    _model_activity_process(
        broken,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        "hosted_generation",
        hosted_request(),
    )
    assert broken.messages == [("engine_error", {"operation": "model_runtime.activity"})]


def test_message_decoder_rejects_malformed_or_mismatched_success() -> None:
    executor = ProcessModelActivityDeadlineExecutor()
    assert (
        executor._handle(
            ("result", hosted_result().model_dump(mode="json")),
            "hosted_generation",
        )
        == hosted_result()
    )
    assert (
        executor._handle(("result", local_result().model_dump(mode="json")), "local_generation")
        == local_result()
    )
    assert (
        executor._handle(
            ("result", analysis_result().model_dump(mode="json")),
            "language_model_analysis",
        )
        == analysis_result()
    )

    for message in (
        "bad",
        ("unknown", {}),
        ("result", {}),
        ("application_error", "bad"),
        ("application_error", {"code": "bad", "operation": "safe"}),
        ("application_error", {"code": "invalid_request", "operation": 7}),
    ):
        with pytest.raises(EngineContractError):
            executor._handle(message, "hosted_generation")
    with pytest.raises(InvalidRequestError):
        executor._handle(
            (
                "application_error",
                {"code": "invalid_request", "operation": "safe.operation"},
            ),
            "hosted_generation",
        )
    with pytest.raises(EngineUnavailableError):
        executor._handle(("engine_error", {}), "hosted_generation")


def test_dispatch_rejects_operation_request_type_mismatches() -> None:
    with pytest.raises(EngineContractError) as caught:
        _dispatch(coordinator(), "hosted_generation", local_request())
    assert caught.value.operation == "model_runtime.activity.operation"


def test_profile_specific_durable_registries_execute_inside_one_process_boundary() -> None:
    artifacts = MemoryArtifactStager()
    runtime = coordinator()
    hosted_registry = build_model_handler_registry(
        "external-provider",
        runtime,
        artifacts,
    )
    cpu_registry = build_model_handler_registry("batch-cpu", runtime, artifacts)

    hosted_summary = hosted_registry.execute(
        RunKind.GENERATE_LLM,
        hosted_request().model_dump(mode="json"),
    )
    local_summary = cpu_registry.execute(
        RunKind.GENERATE_LOCAL,
        local_request().model_dump(mode="json"),
    )
    analysis_summary = cpu_registry.execute(
        RunKind.PERPLEXITY,
        analysis_request().model_dump(mode="json"),
    )

    assert hosted_registry.kinds == frozenset({RunKind.GENERATE_LLM})
    assert cpu_registry.kinds == frozenset({RunKind.GENERATE_LOCAL, RunKind.PERPLEXITY})
    assert hosted_summary["staged_artifact_ref"].startswith("staged-artifact://sha256/")
    assert local_summary["staged_artifact_ref"].startswith("staged-artifact://sha256/")
    for summary in (hosted_summary, local_summary, analysis_summary):
        assert set(summary) == {
            "artifact_type",
            "contract",
            "media_type",
            "schema_id",
            "size_bytes",
            "staged_artifact_ref",
        }
        assert "artifact_ref" not in summary
        assert not {"organization_id", "project_id", "run_id"} & summary.keys()
    assert all(
        digest == hashlib.sha256(payload).hexdigest() for _, payload, digest in artifacts.payloads
    )
    assert "Pat." not in repr((hosted_summary, local_summary, analysis_summary))
    assert (
        SECRET.reference not in b"".join(payload for _, payload, _ in artifacts.payloads).decode()
    )


def test_gpu_registry_requires_exact_profile_and_unsupported_profiles_fail_closed() -> None:
    artifacts = MemoryArtifactStager()
    gpu_runtime = ModelRuntimeCoordinator(
        ModelRuntimePolicy(
            local_models=(local_policy(),),
            worker_profile=WorkerModelProfile.LOCAL_GPU,
        ),
        RecordingEngine(),
    )
    assert build_model_handler_registry(
        "gpu-inference",
        gpu_runtime,
        artifacts,
    ).kinds == frozenset({RunKind.GENERATE_LOCAL, RunKind.PERPLEXITY})

    with pytest.raises(RuntimeError, match="does not match"):
        build_model_handler_registry("gpu-inference", coordinator(), artifacts)
    with pytest.raises(RuntimeError, match="does not permit"):
        build_model_handler_registry("api", coordinator(), artifacts)


def test_parent_deadline_metadata_parser_is_pure_bounded_and_kind_specific() -> None:
    assert (
        model_activity_timeout_seconds(
            RunKind.GENERATE_LLM,
            hosted_request(timeout=1.25).model_dump(mode="json"),
        )
        == 1.25
    )
    assert (
        model_activity_timeout_seconds(
            RunKind.GENERATE_LOCAL,
            local_request(timeout=1.5).model_dump(mode="json"),
        )
        == 1.5
    )
    assert (
        model_activity_timeout_seconds(
            RunKind.PERPLEXITY,
            analysis_request(timeout=1.75).model_dump(mode="json"),
        )
        == 1.75
    )
    with pytest.raises(RuntimeError, match="no model activity deadline"):
        model_activity_timeout_seconds(RunKind.EVALUATE, {})


def test_authoritative_parent_deadline_is_dto_validated_and_server_capped() -> None:
    spec = hosted_request(timeout=2.5).model_dump(mode="json")

    assert activity_deadline_seconds(RunKind.GENERATE_LLM, spec) == 2.5
    assert (
        activity_deadline_seconds(
            RunKind.GENERATE_LLM,
            spec,
            server_cap_seconds=1.25,
        )
        == 1.25
    )
    assert (
        activity_deadline_seconds(
            RunKind.PHONEMIZE,
            {"activity_timeout_seconds": 0.0001},
            server_cap_seconds=3,
        )
        == 3
    )
    assert (
        activity_deadline_seconds(
            RunKind.PERPLEXITY,
            analysis_request(timeout=2.75).model_dump(mode="json"),
        )
        == 2.75
    )
    for invalid_cap in (0, float("inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            activity_deadline_seconds(
                RunKind.PHONEMIZE,
                {},
                server_cap_seconds=invalid_cap,
            )
    with pytest.raises(ValueError, match="deadline contract"):
        activity_deadline_seconds(
            RunKind.GENERATE_LLM,
            dict(spec, activity_timeout_seconds=301),
        )


def test_durable_artifact_contract_rejects_non_content_addressed_or_oversized_results() -> None:
    invalid = MemoryArtifactStager(invalid_reference=True)
    registry = build_model_handler_registry("external-provider", coordinator(), invalid)
    with pytest.raises(RunExecutionError) as invalid_reference:
        registry.execute(
            RunKind.GENERATE_LLM,
            hosted_request().model_dump(mode="json"),
        )
    assert invalid_reference.value.code == "engine_contract_violation"
    assert invalid_reference.value.retryable is False

    with pytest.raises(EngineContractError) as oversized:
        _stage(
            MemoryArtifactStager(),
            RunKind.GENERATE_LOCAL,
            b"x" * (MAX_MODEL_RESULT_ARTIFACT_BYTES + 1),
            schema_id="corpuskit.local-generation-result.v1",
        )
    assert oversized.value.operation == "model_runtime.artifact.size"
