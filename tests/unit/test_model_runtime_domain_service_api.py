"""Model-runtime DTO, fail-closed policy and validation-only HTTP acceptance."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from corpuskit.api.model_runtime import model_runtime_router
from corpuskit.domain.errors import ApplicationError, EngineUnavailableError, InvalidRequestError
from corpuskit.domain.generation import (
    CompositeScoringRequest,
    GenerationScoringOptions,
    GenerationStoppingCriteria,
    GenerationTarget,
    RepositoryCandidate,
    ScoreWeights,
)
from corpuskit.domain.jobs import normalize_run_spec
from corpuskit.domain.model_runtime import (
    AnalysisText,
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedModelSelection,
    HostedPromptTemplatePolicy,
    HostedRunBudget,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelQuantization,
    ProviderRetryPolicy,
    SecretReference,
    WorkerModelProfile,
)
from corpuskit.services.model_runtime import (
    ModelRuntimeCoordinator,
    ModelRuntimePolicy,
)

SECRET = SecretReference(reference="secret://env/CORPUSKIT_TEST_KEY")
PROMPT_SECRET = SecretReference(reference="secret://env/CORPUSKIT_TEST_PROMPT")
PROMPT = "Use {target_units} for {k} lines in {language}."
PIN = ImmutableModelPin(model="acme/tiny-causal", revision="a" * 40)
ARTIFACT_DIGEST = "b" * 64


def hosted_policy(
    *,
    with_prompt: bool = False,
    request_delay_seconds: float = 0.25,
) -> HostedModelPolicy:
    return HostedModelPolicy(
        provider="openai",
        model="openai/demo-model",
        connection_id="demo-provider",
        credential_ref=SECRET,
        input_cost_per_million_usd=Decimal("1.25"),
        output_cost_per_million_usd=Decimal("5.00"),
        max_output_tokens_per_request=512,
        request_delay_seconds=request_delay_seconds,
        prompt_templates=(
            HostedPromptTemplatePolicy(
                template_id="coverage-v1",
                template_ref=PROMPT_SECRET,
                sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
                size_bytes=len(PROMPT.encode()),
                max_rendered_bytes=1024,
            ),
        )
        if with_prompt
        else (),
    )


def hosted_request(**changes: object) -> HostedGenerationRequest:
    values: dict[str, object] = {
        "selection": HostedModelSelection(
            provider="openai",
            model="openai/demo-model",
            connection_id="demo-provider",
        ),
        "target": GenerationTarget(phonemes=("p", "b")),
        "stopping": GenerationStoppingCriteria(
            max_sentences=2,
            max_iterations=2,
            timeout_seconds=2.0,
        ),
        "candidates_per_iteration": 2,
        "max_tokens_per_request": 64,
        "retry": ProviderRetryPolicy(
            max_retries=1,
            request_timeout_seconds=1.0,
            base_delay_seconds=0.0,
            max_delay_seconds=0.0,
        ),
        "budget": HostedRunBudget(
            max_requests=4,
            max_input_tokens=4_000,
            max_output_tokens=256,
            max_cost_usd=Decimal("1.00"),
        ),
        "activity_timeout_seconds": 3.0,
        "external_processing_confirmed": True,
    }
    values.update(changes)
    return HostedGenerationRequest(**values)


def local_policy(
    *,
    devices: tuple[ModelDevice, ...] = (ModelDevice.CPU,),
    quantizations: tuple[ModelQuantization, ...] = (ModelQuantization.NONE,),
) -> LocalModelPolicy:
    return LocalModelPolicy(
        pin=PIN,
        artifact_sha256=ARTIFACT_DIGEST,
        allowed_devices=devices,
        allowed_quantizations=quantizations,
    )


def local_request(**changes: object) -> LocalGenerationRequest:
    values: dict[str, object] = {
        "selection": LocalModelSelection(pin=PIN),
        "target": GenerationTarget(phonemes=("p",)),
        "stopping": GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=1.0,
        ),
        "activity_timeout_seconds": 2.0,
    }
    values.update(changes)
    return LocalGenerationRequest(**values)


def analysis_request(**changes: object) -> LanguageModelAnalysisRequest:
    values: dict[str, object] = {
        "selection": LocalModelSelection(pin=PIN),
        "texts": (
            AnalysisText(source_id="one", text="A complete sentence."),
            AnalysisText(source_id="two", text="Another complete sentence."),
        ),
        "activity_timeout_seconds": 2.0,
    }
    values.update(changes)
    return LanguageModelAnalysisRequest(**values)


def composite_analysis_request(**changes: object) -> LanguageModelAnalysisRequest:
    composite = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p", "b")),
        candidates=(
            RepositoryCandidate(
                source_id="one",
                text="A complete sentence.",
                phonemes=("p",),
            ),
            RepositoryCandidate(
                source_id="two",
                text="Another complete sentence.",
                phonemes=("b",),
            ),
        ),
        options=GenerationScoringOptions(weights=ScoreWeights(coverage=0, fluency=1)),
    )
    return analysis_request(composite_scoring=composite, **changes)


def test_hosted_specs_use_persistable_secret_references_and_results_cannot_hold_secrets() -> None:
    request = hosted_request()
    serialized = request.model_dump(mode="json")
    normalized, digest = normalize_run_spec(serialized)

    assert normalized["selection"]["connection_id"] == "demo-provider"
    assert len(digest) == 64
    assert "api_key" not in serialized["selection"]
    assert "secret://" not in request.model_dump_json()
    with pytest.raises(ValidationError, match="Extra inputs"):
        hosted_request(prompt_template=PROMPT)
    with pytest.raises(ValueError, match="opaque secret_ref"):
        normalize_run_spec({"credential": "raw-value"})


@pytest.mark.parametrize(
    ("constructor", "kwargs", "fragment"),
    [
        (
            HostedModelSelection,
            {
                "provider": "OpenAI!",
                "model": "openai/model",
                "connection_id": "demo-provider",
            },
            "safe grammar",
        ),
        (
            HostedModelSelection,
            {
                "provider": "openai",
                "model": "anthropic/model",
                "connection_id": "demo-provider",
            },
            "exactly match",
        ),
        (
            ImmutableModelPin,
            {"model": "https://example/model", "revision": "a" * 40},
            "namespaced",
        ),
        (
            ImmutableModelPin,
            {"model": "acme/model", "revision": "main"},
            "40 characters",
        ),
        (
            LocalModelSelection,
            {"pin": PIN, "device": "cpu", "quantization": "4bit"},
            "CUDA",
        ),
        (
            AnalysisText,
            {"source_id": "bad id", "text": "sentence"},
            "safe and non-empty",
        ),
    ],
)
def test_identifier_and_device_contracts_fail_closed(
    constructor: type[object],
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    with pytest.raises(ValidationError, match=fragment):
        constructor(**kwargs)


def test_prompt_retry_source_and_policy_invariants() -> None:
    with pytest.raises(InvalidRequestError) as prompt_error:
        ModelRuntimePolicy(hosted_models=(hosted_policy(),)).validate_hosted(
            hosted_request(prompt_template_id="coverage-v1")
        )
    assert prompt_error.value.operation == "model_runtime.hosted.prompt_allowlist"
    with pytest.raises(ValidationError, match="Field required"):
        HostedGenerationRequest.model_validate(
            hosted_request().model_dump(exclude={"external_processing_confirmed"})
        )
    ModelRuntimePolicy(hosted_models=(hosted_policy(with_prompt=True),)).validate_hosted(
        hosted_request(prompt_template_id="coverage-v1")
    )
    with pytest.raises(ValidationError, match="maximum delay"):
        ProviderRetryPolicy(base_delay_seconds=2, max_delay_seconds=1)
    with pytest.raises(ValidationError, match="less than or equal to 30"):
        hosted_policy(request_delay_seconds=30.01)
    with pytest.raises(ValidationError, match="Extra inputs"):
        HostedGenerationRequest.model_validate(
            {**hosted_request().model_dump(mode="json"), "request_delay_seconds": 1}
        )
    with pytest.raises(ValidationError, match="unique"):
        LanguageModelAnalysisRequest(
            selection=LocalModelSelection(pin=PIN),
            texts=(
                AnalysisText(source_id="same", text="first"),
                AnalysisText(source_id="same", text="second"),
            ),
        )
    with pytest.raises(ValidationError, match="exactly match"):
        composite_analysis_request(
            texts=(AnalysisText(source_id="one", text="A complete sentence."),)
        )
    with pytest.raises(ValidationError, match="non-zero fluency"):
        analysis_request(
            composite_scoring=CompositeScoringRequest(
                target=GenerationTarget(phonemes=("p", "b")),
                candidates=(
                    RepositoryCandidate(
                        source_id="one",
                        text="A complete sentence.",
                        phonemes=("p",),
                    ),
                    RepositoryCandidate(
                        source_id="two",
                        text="Another complete sentence.",
                        phonemes=("b",),
                    ),
                ),
            )
        )
    with pytest.raises(ValidationError, match="iteration cap"):
        local_request(
            scoring=GenerationScoringOptions(weights=ScoreWeights(coverage=0, fluency=1)),
            candidates_per_iteration=8,
            stopping=GenerationStoppingCriteria(
                max_sentences=50,
                max_iterations=100,
                timeout_seconds=30,
            ),
        )
    with pytest.raises(ValidationError, match="durable local-model policy"):
        hosted_request(
            scoring=GenerationScoringOptions(weights=ScoreWeights(coverage=0, fluency=1))
        )
    with pytest.raises(ValidationError, match="match pattern"):
        HostedModelPolicy(
            provider="openai",
            model="openai/demo-model",
            connection_id="INVALID CONNECTION",
            credential_ref=SECRET,
            input_cost_per_million_usd=0,
            output_cost_per_million_usd=0,
            max_output_tokens_per_request=1,
        )
    with pytest.raises(ValidationError, match="unique"):
        LocalModelPolicy(
            pin=PIN,
            artifact_sha256=ARTIFACT_DIGEST,
            allowed_devices=(ModelDevice.CPU, ModelDevice.CPU),
            allowed_quantizations=(ModelQuantization.NONE,),
        )


def test_policy_validates_and_estimates_without_an_execution_engine() -> None:
    policy = ModelRuntimePolicy(
        hosted_models=(hosted_policy(),),
        local_models=(local_policy(),),
    )

    hosted = policy.validate_hosted(hosted_request())
    estimate = policy.estimate_hosted(hosted_request())
    local = policy.validate_local(local_request())
    analysis = policy.validate_analysis(analysis_request())
    analysis_estimate = policy.estimate_analysis(composite_analysis_request())

    assert hosted.network_during_validation is False
    assert hosted.worker_only is True
    assert hosted.request_delay_seconds == 0.25
    assert estimate.network_during_estimate is False
    assert estimate.request_delay_seconds == 0.25
    assert estimate.estimated_ceiling_usd <= estimate.authorized_ceiling_usd
    assert estimate.maximum_requests == 4
    assert local.required_profile is WorkerModelProfile.LOCAL_CPU
    assert analysis.operation == "language_model_analysis"
    assert analysis_estimate.maximum_fluency_evaluations == 2
    assert analysis_estimate.maximum_fluency_tokens == 1_024
    assert analysis_estimate.maximum_perplexity_tokens == 1_024
    assert analysis_estimate.composite_scoring_enabled is True
    assert analysis_estimate.composite_reuses_fluency_scores is True
    assert analysis_estimate.network_during_estimate is False

    assert (
        HostedModelPolicy(
            provider="openai",
            model="openai/demo-model",
            connection_id="zero-delay",
            credential_ref=SECRET,
            input_cost_per_million_usd=0,
            output_cost_per_million_usd=0,
            max_output_tokens_per_request=1,
        ).request_delay_seconds
        == 0.0
    )


def test_policy_is_default_deny_and_enforces_exact_limits_and_worker_profiles() -> None:
    with pytest.raises(InvalidRequestError) as hosted_denied:
        ModelRuntimePolicy().validate_hosted(hosted_request())
    assert hosted_denied.value.operation == "model_runtime.hosted.allowlist"

    wrong_connection = "other-provider"
    with pytest.raises(InvalidRequestError):
        ModelRuntimePolicy(hosted_models=(hosted_policy(),)).validate_hosted(
            hosted_request(
                selection=HostedModelSelection(
                    provider="openai",
                    model="openai/demo-model",
                    connection_id=wrong_connection,
                )
            )
        )
    with pytest.raises(InvalidRequestError) as output_limit:
        ModelRuntimePolicy(hosted_models=(hosted_policy(),)).validate_hosted(
            hosted_request(max_tokens_per_request=513)
        )
    assert output_limit.value.operation == "model_runtime.hosted.output_limit"
    with pytest.raises(InvalidRequestError) as budget:
        ModelRuntimePolicy(hosted_models=(hosted_policy(),)).validate_hosted(
            hosted_request(
                budget=HostedRunBudget(
                    max_requests=1,
                    max_input_tokens=1,
                    max_output_tokens=1,
                    max_cost_usd=Decimal("0.01"),
                ),
                max_tokens_per_request=2,
            )
        )
    assert budget.value.operation == "model_runtime.hosted.budget"

    gpu_policy = local_policy(
        devices=(ModelDevice.CUDA,),
        quantizations=(ModelQuantization.FOUR_BIT,),
    )
    gpu_request = local_request(
        selection=LocalModelSelection(
            pin=PIN,
            device=ModelDevice.CUDA,
            quantization=ModelQuantization.FOUR_BIT,
        )
    )
    with pytest.raises(InvalidRequestError) as profile:
        ModelRuntimePolicy(local_models=(gpu_policy,)).validate_local(gpu_request)
    assert profile.value.operation == "model_runtime.local.worker_profile"
    assert (
        ModelRuntimePolicy(
            local_models=(gpu_policy,),
            worker_profile=WorkerModelProfile.LOCAL_GPU,
        )
        .validate_local(gpu_request)
        .required_profile
        is WorkerModelProfile.LOCAL_GPU
    )


def test_policy_rejects_duplicate_server_allowlist_keys() -> None:
    with pytest.raises(ValueError, match="unique"):
        ModelRuntimePolicy(hosted_models=(hosted_policy(), hosted_policy()))
    with pytest.raises(ValueError, match="unique"):
        ModelRuntimePolicy(local_models=(local_policy(), local_policy()))


class NeverEngine:
    def run_hosted(self, *_: object) -> object:
        raise AssertionError("execution engine reached")

    def run_local(self, *_: object) -> object:
        raise AssertionError("execution engine reached")

    def analyze_language_model(self, *_: object) -> object:
        raise AssertionError("execution engine reached")


class FailingEngine(NeverEngine):
    def run_hosted(self, *_: object) -> object:
        raise RuntimeError("provider secret should never escape")


def test_coordinator_sanitizes_unknown_engine_failures() -> None:
    coordinator = ModelRuntimeCoordinator(
        ModelRuntimePolicy(hosted_models=(hosted_policy(),)),
        FailingEngine(),  # type: ignore[arg-type]
    )
    with pytest.raises(EngineUnavailableError) as caught:
        coordinator.run_hosted(hosted_request())
    assert "provider secret" not in str(caught.value)


@pytest_asyncio.fixture
async def runtime_client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    policy = ModelRuntimePolicy(
        hosted_models=(hosted_policy(),),
        local_models=(local_policy(),),
    )
    app.include_router(model_runtime_router(policy), prefix="/api/v1")

    @app.exception_handler(ApplicationError)
    async def application_error(_: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": error.code.value, "operation": error.operation},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_http_exposes_only_network_free_validation_and_estimation(
    runtime_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def forbidden_network(*_: object, **__: object) -> object:
        raise AssertionError("validation attempted network I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    hosted_payload = hosted_request().model_dump(mode="json")
    local_payload = local_request().model_dump(mode="json")
    analysis_payload = analysis_request().model_dump(mode="json")

    responses = [
        await runtime_client.post("/api/v1/model-runtime/hosted/validate", json=hosted_payload),
        await runtime_client.post("/api/v1/model-runtime/hosted/estimate", json=hosted_payload),
        await runtime_client.post("/api/v1/model-runtime/local/validate", json=local_payload),
        await runtime_client.post("/api/v1/model-runtime/analysis/validate", json=analysis_payload),
        await runtime_client.post("/api/v1/model-runtime/analysis/estimate", json=analysis_payload),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]
    assert responses[0].json()["network_during_validation"] is False
    assert responses[1].json()["network_during_estimate"] is False
    assert responses[4].json()["network_during_estimate"] is False
    assert all("secret://" not in response.text for response in responses)
    assert (
        await runtime_client.post("/api/v1/model-runtime/hosted/execute", json=hosted_payload)
    ).status_code == 404
