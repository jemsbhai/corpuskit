"""Fail-closed policy and worker coordination for optional model runtimes."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Protocol, TypeVar

from corpuskit.domain.errors import (
    ApplicationError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.model_runtime import (
    DEFAULT_HOSTED_PROMPT_TEMPLATE,
    HostedCostEstimate,
    HostedGenerationRequest,
    HostedGenerationResult,
    HostedModelPolicy,
    HostedPromptTemplatePolicy,
    LanguageModelAnalysisEstimate,
    LanguageModelAnalysisRequest,
    LanguageModelAnalysisResult,
    LocalGenerationRequest,
    LocalGenerationResult,
    LocalModelPolicy,
    ModelDevice,
    ModelQuantization,
    RuntimeValidationResult,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import PhonRlCheckpointCompatibility

_MILLION = Decimal(1_000_000)


class ModelRuntimeEngine(Protocol):
    """Worker-side adapter contract; implementations may perform network or model I/O."""

    def run_hosted(
        self,
        request: HostedGenerationRequest,
        policy: HostedModelPolicy,
    ) -> HostedGenerationResult: ...

    def run_local(
        self,
        request: LocalGenerationRequest,
        policy: LocalModelPolicy,
        profile: WorkerModelProfile,
    ) -> LocalGenerationResult: ...

    def analyze_language_model(
        self,
        request: LanguageModelAnalysisRequest,
        policy: LocalModelPolicy,
        profile: WorkerModelProfile,
    ) -> LanguageModelAnalysisResult: ...

    def run_local_phon_rl(
        self,
        request: LocalGenerationRequest,
        policy: LocalModelPolicy,
        profile: WorkerModelProfile,
        *,
        adapter_root: Path,
        compatibility: PhonRlCheckpointCompatibility,
    ) -> LocalGenerationResult: ...


class ModelRuntimePolicy:
    """Pure authorization and estimation boundary used safely by HTTP handlers."""

    def __init__(
        self,
        *,
        hosted_models: tuple[HostedModelPolicy, ...] = (),
        local_models: tuple[LocalModelPolicy, ...] = (),
        worker_profile: WorkerModelProfile = WorkerModelProfile.LOCAL_CPU,
    ) -> None:
        self._hosted_models = hosted_models
        self._local_models = local_models
        self._worker_profile = worker_profile
        hosted_keys = [(item.provider, item.model, item.connection_id) for item in hosted_models]
        local_keys = [(item.pin.model, item.pin.revision) for item in local_models]
        if len(hosted_keys) != len(set(hosted_keys)) or len(local_keys) != len(set(local_keys)):
            raise ValueError("Model allowlist keys must be unique.")

    @property
    def worker_profile(self) -> WorkerModelProfile:
        return self._worker_profile

    def validate_hosted(self, request: HostedGenerationRequest) -> RuntimeValidationResult:
        policy = self.authorize_hosted(request)
        self._require_first_hosted_call_fits(request, policy)
        return RuntimeValidationResult(
            operation="hosted_generation",
            model=request.selection.model,
            provider=request.selection.provider,
            maximum_authorized_cost_usd=request.budget.max_cost_usd,
            maximum_requests=request.budget.max_requests,
            request_delay_seconds=policy.request_delay_seconds,
            whole_activity_timeout_seconds=request.activity_timeout_seconds,
        )

    def estimate_hosted(self, request: HostedGenerationRequest) -> HostedCostEstimate:
        policy = self.authorize_hosted(request)
        self._require_first_hosted_call_fits(request, policy)
        prompt_tokens = _conservative_prompt_tokens(
            request,
            self.authorize_hosted_prompt(request, policy),
        )
        iteration_calls = request.stopping.max_iterations or request.budget.max_requests
        maximum_requests = min(
            request.budget.max_requests,
            iteration_calls * (request.retry.max_retries + 1),
        )
        reserved_input = min(
            request.budget.max_input_tokens,
            prompt_tokens * maximum_requests,
        )
        reserved_output = min(
            request.budget.max_output_tokens,
            request.max_tokens_per_request * maximum_requests,
        )
        estimated_cost = _price(policy, reserved_input, reserved_output)
        return HostedCostEstimate(
            provider=request.selection.provider,
            model=request.selection.model,
            maximum_requests=maximum_requests,
            request_delay_seconds=policy.request_delay_seconds,
            reserved_input_tokens=reserved_input,
            reserved_output_tokens=reserved_output,
            estimated_ceiling_usd=min(estimated_cost, request.budget.max_cost_usd),
            authorized_ceiling_usd=request.budget.max_cost_usd,
        )

    def validate_local(self, request: LocalGenerationRequest) -> RuntimeValidationResult:
        policy = self.authorize_local(
            request.selection.pin.model,
            request.selection.pin.revision,
        )
        self._validate_local_execution(
            policy,
            request.selection.device,
            request.selection.quantization,
        )
        if request.phon_rl_adapter is not None and not policy.allow_phon_rl_adapters:
            raise InvalidRequestError("model_runtime.local.phon_rl_adapter_policy")
        return RuntimeValidationResult(
            operation="local_generation",
            model=request.selection.pin.model,
            required_profile=self._required_profile(request.selection.device),
            whole_activity_timeout_seconds=request.activity_timeout_seconds,
        )

    def validate_analysis(
        self,
        request: LanguageModelAnalysisRequest,
    ) -> RuntimeValidationResult:
        policy = self.authorize_local(
            request.selection.pin.model,
            request.selection.pin.revision,
        )
        self._validate_local_execution(
            policy,
            request.selection.device,
            request.selection.quantization,
        )
        return RuntimeValidationResult(
            operation="language_model_analysis",
            model=request.selection.pin.model,
            required_profile=self._required_profile(request.selection.device),
            whole_activity_timeout_seconds=request.activity_timeout_seconds,
        )

    def estimate_analysis(
        self,
        request: LanguageModelAnalysisRequest,
    ) -> LanguageModelAnalysisEstimate:
        """Authorize and bound local work without loading a model or touching the network."""

        self.validate_analysis(request)
        count = len(request.texts)
        return LanguageModelAnalysisEstimate(
            model=request.selection.pin.model,
            revision=request.selection.pin.revision,
            required_profile=self._required_profile(request.selection.device),
            input_sentence_count=count,
            maximum_fluency_evaluations=count,
            maximum_fluency_tokens=count * 512,
            maximum_perplexity_tokens=count * request.max_length,
            composite_scoring_enabled=request.composite_scoring is not None,
            whole_activity_timeout_seconds=request.activity_timeout_seconds,
        )

    def authorize_hosted(self, request: HostedGenerationRequest) -> HostedModelPolicy:
        policy = next(
            (
                item
                for item in self._hosted_models
                if item.provider == request.selection.provider
                and item.model == request.selection.model
                and item.connection_id == request.selection.connection_id
            ),
            None,
        )
        if policy is None:
            raise InvalidRequestError("model_runtime.hosted.allowlist")
        if request.max_tokens_per_request > policy.max_output_tokens_per_request:
            raise InvalidRequestError("model_runtime.hosted.output_limit")
        self.authorize_hosted_prompt(request, policy)
        return policy

    @staticmethod
    def authorize_hosted_prompt(
        request: HostedGenerationRequest,
        policy: HostedModelPolicy,
    ) -> HostedPromptTemplatePolicy | None:
        if request.prompt_template_id is None:
            return None
        prompt_policy = next(
            (
                item
                for item in policy.prompt_templates
                if item.template_id == request.prompt_template_id
            ),
            None,
        )
        if prompt_policy is None:
            raise InvalidRequestError("model_runtime.hosted.prompt_allowlist")
        return prompt_policy

    def authorize_local(self, model: str, revision: str) -> LocalModelPolicy:
        policy = next(
            (
                item
                for item in self._local_models
                if item.pin.model == model and item.pin.revision == revision
            ),
            None,
        )
        if policy is None:
            raise InvalidRequestError("model_runtime.local.allowlist")
        return policy

    def _validate_local_execution(
        self,
        policy: LocalModelPolicy,
        device: ModelDevice,
        quantization: ModelQuantization,
    ) -> None:
        if device not in policy.allowed_devices or quantization not in policy.allowed_quantizations:
            raise InvalidRequestError("model_runtime.local.execution_allowlist")
        if device is ModelDevice.CUDA and self._worker_profile is not WorkerModelProfile.LOCAL_GPU:
            raise InvalidRequestError("model_runtime.local.worker_profile")
        if quantization is not ModelQuantization.NONE and device is not ModelDevice.CUDA:
            raise InvalidRequestError("model_runtime.local.quantization")

    @staticmethod
    def _required_profile(device: ModelDevice) -> WorkerModelProfile:
        if device is ModelDevice.CUDA:
            return WorkerModelProfile.LOCAL_GPU
        return WorkerModelProfile.LOCAL_CPU

    @staticmethod
    def _require_first_hosted_call_fits(
        request: HostedGenerationRequest,
        policy: HostedModelPolicy,
    ) -> None:
        prompt_tokens = _conservative_prompt_tokens(
            request,
            ModelRuntimePolicy.authorize_hosted_prompt(request, policy),
        )
        if (
            prompt_tokens > request.budget.max_input_tokens
            or request.max_tokens_per_request > request.budget.max_output_tokens
            or _price(policy, prompt_tokens, request.max_tokens_per_request)
            > request.budget.max_cost_usd
        ):
            raise InvalidRequestError("model_runtime.hosted.budget")


class ModelRuntimeCoordinator:
    """Worker-only execution boundary with sanitized adapter failures."""

    def __init__(self, policy: ModelRuntimePolicy, engine: ModelRuntimeEngine) -> None:
        self.policy = policy
        self._engine = engine

    def run_hosted(self, request: HostedGenerationRequest) -> HostedGenerationResult:
        policy = self.policy.authorize_hosted(request)
        self.policy.validate_hosted(request)
        return self._call(
            "model_runtime.hosted.execute",
            lambda: self._engine.run_hosted(request, policy),
        )

    def run_local(self, request: LocalGenerationRequest) -> LocalGenerationResult:
        policy = self.policy.authorize_local(
            request.selection.pin.model,
            request.selection.pin.revision,
        )
        self.policy.validate_local(request)
        return self._call(
            "model_runtime.local.execute",
            lambda: self._engine.run_local(request, policy, self.policy.worker_profile),
        )

    def run_local_phon_rl(
        self,
        request: LocalGenerationRequest,
        *,
        adapter_root: Path,
        compatibility: PhonRlCheckpointCompatibility,
    ) -> LocalGenerationResult:
        policy = self.policy.authorize_local(
            request.selection.pin.model,
            request.selection.pin.revision,
        )
        self.policy.validate_local(request)
        return self._call(
            "model_runtime.local.phon_rl.execute",
            lambda: self._engine.run_local_phon_rl(
                request,
                policy,
                self.policy.worker_profile,
                adapter_root=adapter_root,
                compatibility=compatibility,
            ),
        )

    def analyze(
        self,
        request: LanguageModelAnalysisRequest,
    ) -> LanguageModelAnalysisResult:
        policy = self.policy.authorize_local(
            request.selection.pin.model,
            request.selection.pin.revision,
        )
        self.policy.validate_analysis(request)
        return self._call(
            "model_runtime.analysis.execute",
            lambda: self._engine.analyze_language_model(
                request,
                policy,
                self.policy.worker_profile,
            ),
        )

    @staticmethod
    def _call(operation: str, callback: _ResultCallback[_T]) -> _T:
        try:
            return callback()
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError(operation) from None


_T = TypeVar("_T", covariant=True)


class _ResultCallback(Protocol[_T]):
    def __call__(self) -> _T: ...


def _conservative_prompt_tokens(
    request: HostedGenerationRequest,
    prompt_policy: HostedPromptTemplatePolicy | None,
) -> int:
    if prompt_policy is not None:
        return prompt_policy.max_rendered_bytes
    prompt = DEFAULT_HOSTED_PROMPT_TEMPLATE.format(
        target_units=", ".join(request.target.phonemes),
        language=request.language,
        k=request.candidates_per_iteration,
    )
    # Every tokenizer token consumes at least one encoded byte. This is deliberately pessimistic.
    return max(1, len(prompt.encode("utf-8")))


def _price(policy: HostedModelPolicy, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * policy.input_cost_per_million_usd
        + Decimal(output_tokens) * policy.output_cost_per_million_usd
    ) / _MILLION


__all__ = [
    "ModelRuntimeCoordinator",
    "ModelRuntimeEngine",
    "ModelRuntimePolicy",
]
