"""Worker-only hosted and local language-model runtime contracts."""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from corpuskit.domain.corpus import FrozenDomainModel
from corpuskit.domain.generation import (
    MAX_ACTIVITY_SECONDS,
    MAX_CANDIDATES_PER_ITERATION,
    MAX_SENTENCE_CHARACTERS,
    AcceptedCandidate,
    CompositeScoringRequest,
    CompositeScoringResult,
    GenerationDomainModel,
    GenerationScoringOptions,
    GenerationStoppingCriteria,
    GenerationStopReason,
    GenerationTarget,
)

MAX_MODEL_ANALYSIS_SENTENCES = 250
MAX_FLUENCY_SCORING_EVALUATIONS = 250
MAX_HOSTED_REQUESTS = 50
MAX_HOSTED_TOKENS = 100_000
MAX_MODEL_OUTPUT_TOKENS = 512
MAX_HOSTED_REQUEST_DELAY_SECONDS = 30.0
DEFAULT_HOSTED_PROMPT_TEMPLATE = (
    "Generate {k} short, natural sentences in {language} that contain the following "
    "sounds (IPA phonemes):\n"
    "\n"
    "Target sounds: {target_units}\n"
    "\n"
    "Requirements:\n"
    "- Each sentence should be a complete, grammatically correct sentence.\n"
    "- Sentences should sound natural, not contrived.\n"
    "- Try to include as many of the target sounds as possible in each sentence.\n"
    "- One sentence per line, no numbering or bullet points.\n"
)

_SAFE_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{1,31}$", re.ASCII)
_SAFE_MODEL = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
    re.ASCII,
)
_IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_SAFE_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$", re.ASCII)
_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,191}$", re.ASCII)


class ModelRuntimeDomainModel(FrozenDomainModel):
    """Strict JSON contract shared by model-worker requests and results."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ModelDevice(StrEnum):
    CPU = "cpu"
    CUDA = "cuda"


class ModelQuantization(StrEnum):
    NONE = "none"
    FOUR_BIT = "4bit"
    EIGHT_BIT = "8bit"


class WorkerModelProfile(StrEnum):
    LOCAL_CPU = "local_cpu"
    LOCAL_GPU = "local_gpu"


class ReproducibilityClass(StrEnum):
    DETERMINISTIC = "deterministic"
    BEST_EFFORT = "best_effort"


class PerplexitySentenceStatus(StrEnum):
    SCORED = "scored"
    SKIPPED_TOO_SHORT = "skipped_too_short"


class SecretReference(ModelRuntimeDomainModel):
    """Opaque server-resolved reference; never a credential value."""

    reference: str = Field(
        min_length=10,
        max_length=192,
        pattern=r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]{0,181}$",
    )


class HostedModelSelection(ModelRuntimeDomainModel):
    provider: str = Field(min_length=2, max_length=32)
    model: str = Field(min_length=3, max_length=192)
    connection_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9._-]+$")

    @model_validator(mode="after")
    def validate_identifiers(self) -> Self:
        if (
            _SAFE_PROVIDER.fullmatch(self.provider) is None
            or _SAFE_MODEL.fullmatch(self.model) is None
        ):
            raise ValueError("Hosted provider and model identifiers must use the safe grammar.")
        if self.model.partition("/")[0] != self.provider:
            raise ValueError("Hosted model namespace must exactly match the selected provider.")
        return self


class HostedPromptTemplatePolicy(ModelRuntimeDomainModel):
    """Server-owned prompt identity backed by a worker-only secret reference."""

    template_id: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9._-]{1,63}$",
    )
    template_ref: SecretReference
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=8_000)
    max_rendered_bytes: int = Field(ge=1, le=MAX_HOSTED_TOKENS)

    @model_validator(mode="after")
    def validate_rendered_ceiling(self) -> Self:
        if self.max_rendered_bytes < self.size_bytes:
            raise ValueError("Prompt rendered-byte ceiling must cover the stored template.")
        return self


class HostedModelPolicy(ModelRuntimeDomainModel):
    """Server-owned allowlist entry and pricing ceiling."""

    provider: str = Field(min_length=2, max_length=32)
    model: str = Field(min_length=3, max_length=192)
    connection_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9._-]+$")
    credential_ref: SecretReference
    input_cost_per_million_usd: Decimal = Field(ge=Decimal("0"), le=Decimal("1000"))
    output_cost_per_million_usd: Decimal = Field(ge=Decimal("0"), le=Decimal("1000"))
    max_output_tokens_per_request: int = Field(ge=1, le=32_768)
    request_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=MAX_HOSTED_REQUEST_DELAY_SECONDS,
    )
    prompt_templates: tuple[HostedPromptTemplatePolicy, ...] = Field(
        default=(),
        max_length=16,
    )

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        selection = HostedModelSelection(
            provider=self.provider,
            model=self.model,
            connection_id=self.connection_id,
        )
        del selection
        template_ids = [item.template_id for item in self.prompt_templates]
        template_refs = [item.template_ref.reference for item in self.prompt_templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("Hosted prompt template IDs must be unique per model policy.")
        if len(template_refs) != len(set(template_refs)):
            raise ValueError("Hosted prompt template secret references must be unique.")
        if self.credential_ref.reference in template_refs:
            raise ValueError("Hosted credentials and prompt templates require distinct secrets.")
        return self


class HostedRunBudget(ModelRuntimeDomainModel):
    max_requests: int = Field(default=10, ge=1, le=MAX_HOSTED_REQUESTS)
    max_input_tokens: int = Field(default=20_000, ge=1, le=MAX_HOSTED_TOKENS)
    max_output_tokens: int = Field(default=10_000, ge=1, le=MAX_HOSTED_TOKENS)
    max_cost_usd: Decimal = Field(
        default=Decimal("1.00"),
        gt=Decimal("0"),
        le=Decimal("1000"),
    )


class ProviderRetryPolicy(ModelRuntimeDomainModel):
    max_retries: int = Field(default=2, ge=0, le=5)
    request_timeout_seconds: float = Field(default=20.0, gt=0.0, le=60.0)
    base_delay_seconds: float = Field(default=0.25, ge=0.0, le=10.0)
    max_delay_seconds: float = Field(default=5.0, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def validate_delays(self) -> Self:
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("Retry base delay must not exceed the maximum delay.")
        return self


class HostedGenerationRequest(GenerationDomainModel):
    selection: HostedModelSelection
    target: GenerationTarget
    language: str = Field(default="en-us", min_length=2, max_length=32)
    stopping: GenerationStoppingCriteria = GenerationStoppingCriteria()
    scoring: GenerationScoringOptions = GenerationScoringOptions()
    candidates_per_iteration: int = Field(default=5, ge=1, le=MAX_CANDIDATES_PER_ITERATION)
    temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    max_tokens_per_request: int = Field(default=512, ge=1, le=4_096)
    prompt_template_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9._-]{1,63}$",
    )
    retry: ProviderRetryPolicy = ProviderRetryPolicy()
    budget: HostedRunBudget = HostedRunBudget()
    activity_timeout_seconds: float = Field(default=60.0, gt=0.0, le=MAX_ACTIVITY_SECONDS)
    external_processing_confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if _SAFE_LANGUAGE.fullmatch(self.language) is None:
            raise ValueError("Language must use a supported language-tag grammar.")
        if self.scoring.weights.fluency > 0:
            raise ValueError(
                "Hosted generation cannot use fluency without a durable local-model policy."
            )
        return self


class HostedUsage(ModelRuntimeDomainModel):
    requests: int = Field(ge=0, le=MAX_HOSTED_REQUESTS)
    retries: int = Field(ge=0, le=MAX_HOSTED_REQUESTS)
    input_tokens: int = Field(ge=0, le=MAX_HOSTED_TOKENS)
    output_tokens: int = Field(ge=0, le=MAX_HOSTED_TOKENS)
    reserved_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    actual_cost_usd: Decimal = Field(ge=Decimal("0"))
    reserved_cost_usd: Decimal = Field(ge=Decimal("0"))


class HostedExecutionManifest(ModelRuntimeDomainModel):
    """Replay metadata that intentionally excludes credential references and values."""

    schema_id: Literal["corpuskit.hosted-execution-manifest.v1"] = (
        "corpuskit.hosted-execution-manifest.v1"
    )
    provider: str
    model: str
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens_per_request: int = Field(ge=1, le=4_096)
    prompt_template_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    prompt_template_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9._-]{1,63}$",
    )
    custom_prompt_template: bool
    external_processing_confirmed: Literal[True] = True
    processing_boundary: Literal["external_provider"] = "external_provider"
    provider_seed_supported: Literal[False] = False
    request_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=MAX_HOSTED_REQUEST_DELAY_SECONDS,
    )
    retry: ProviderRetryPolicy
    budget: HostedRunBudget
    whole_activity_timeout_seconds: float = Field(gt=0.0, le=MAX_ACTIVITY_SECONDS)

    @model_validator(mode="after")
    def validate_prompt_identity(self) -> Self:
        if self.custom_prompt_template != (self.prompt_template_id is not None):
            raise ValueError("Custom prompt manifests require an opaque template ID.")
        return self


class HostedGenerationResult(ModelRuntimeDomainModel):
    schema_id: Literal["corpuskit.hosted-generation-result.v1"] = (
        "corpuskit.hosted-generation-result.v1"
    )
    manifest: HostedExecutionManifest
    credential_mode: Literal["server_secret_reference"] = "server_secret_reference"
    accepted: tuple[AcceptedCandidate, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    iterations: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0.0)
    stop_reason: GenerationStopReason
    usage: HostedUsage
    reproducibility: ReproducibilityClass = ReproducibilityClass.BEST_EFFORT


class ImmutableModelPin(ModelRuntimeDomainModel):
    model: str = Field(min_length=3, max_length=192)
    revision: str = Field(min_length=40, max_length=40)

    @model_validator(mode="after")
    def validate_pin(self) -> Self:
        if _SAFE_MODEL.fullmatch(self.model) is None:
            raise ValueError("Local models must use namespaced Hub identifiers, not paths or URLs.")
        if _IMMUTABLE_REVISION.fullmatch(self.revision) is None:
            raise ValueError("Model revision must be a lowercase 40-character commit SHA.")
        return self


class LocalModelPolicy(ModelRuntimeDomainModel):
    pin: ImmutableModelPin
    artifact_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    allowed_devices: tuple[ModelDevice, ...] = Field(min_length=1, max_length=2)
    allowed_quantizations: tuple[ModelQuantization, ...] = Field(min_length=1, max_length=3)
    allow_phon_rl_adapters: bool = False

    @model_validator(mode="after")
    def validate_unique_options(self) -> Self:
        if len(set(self.allowed_devices)) != len(self.allowed_devices):
            raise ValueError("Allowed model devices must be unique.")
        if len(set(self.allowed_quantizations)) != len(self.allowed_quantizations):
            raise ValueError("Allowed quantization modes must be unique.")
        return self


class LocalModelSelection(ModelRuntimeDomainModel):
    pin: ImmutableModelPin
    device: ModelDevice = ModelDevice.CPU
    quantization: ModelQuantization = ModelQuantization.NONE

    @model_validator(mode="after")
    def validate_device_quantization(self) -> Self:
        if self.device is ModelDevice.CPU and self.quantization is not ModelQuantization.NONE:
            raise ValueError("4-bit and 8-bit quantization require the CUDA worker profile.")
        return self


class PhonRlAdapterSelection(ModelRuntimeDomainModel):
    """Opaque source for a parent-authorized, adopted Phon-RL checkpoint."""

    artifact_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LocalGenerationRequest(GenerationDomainModel):
    selection: LocalModelSelection
    target: GenerationTarget
    language: str = Field(default="en-us", min_length=2, max_length=32)
    stopping: GenerationStoppingCriteria = GenerationStoppingCriteria()
    scoring: GenerationScoringOptions = GenerationScoringOptions()
    candidates_per_iteration: int = Field(default=3, ge=1, le=8)
    max_new_tokens: int = Field(default=128, ge=1, le=MAX_MODEL_OUTPUT_TOKENS)
    temperature: float = Field(default=0.8, gt=0.0, le=2.0)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    do_sample: bool = False
    seed: int = Field(default=0, ge=0, le=4_294_967_295)
    phon_rl_adapter: PhonRlAdapterSelection | None = None
    activity_timeout_seconds: float = Field(default=120.0, gt=0.0, le=MAX_ACTIVITY_SECONDS)

    @model_validator(mode="after")
    def validate_language(self) -> Self:
        if _SAFE_LANGUAGE.fullmatch(self.language) is None:
            raise ValueError("Language must use a supported language-tag grammar.")
        if (
            self.phon_rl_adapter is not None
            and self.selection.quantization is not ModelQuantization.NONE
        ):
            raise ValueError("Phon-RL adapter generation requires an unquantized base model.")
        if self.scoring.weights.fluency > 0:
            iterations = self.stopping.max_iterations
            if (
                iterations is None
                or iterations * self.candidates_per_iteration > MAX_FLUENCY_SCORING_EVALUATIONS
            ):
                raise ValueError(
                    "Fluency-scored generation requires an iteration cap of at most "
                    f"{MAX_FLUENCY_SCORING_EVALUATIONS} candidate evaluations."
                )
        return self


class ModelExecutionManifest(ModelRuntimeDomainModel):
    schema_id: Literal["corpuskit.local-model-execution-manifest.v1"] = (
        "corpuskit.local-model-execution-manifest.v1"
    )
    model: str
    revision: str
    artifact_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    device: ModelDevice
    quantization: ModelQuantization
    local_files_only: Literal[True] = True
    trust_remote_code: Literal[False] = False
    safetensors_only: Literal[True] = True
    fluency_scorer: Literal["perplexity"] | None = None
    sampling_enabled: bool | None = None
    seed: int | None = Field(default=None, ge=0, le=4_294_967_295)
    deterministic_algorithms_enforced: Literal[False] = False
    guidance_strategy: Literal["phon_rl"] | None = None
    adapter_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    adapter_checkpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_generation_seed(self) -> Self:
        if (self.sampling_enabled is None) != (self.seed is None):
            raise ValueError("Generation manifests require both sampling mode and seed.")
        adapter_values = (
            self.guidance_strategy,
            self.adapter_artifact_sha256,
            self.adapter_checkpoint_sha256,
        )
        if any(value is not None for value in adapter_values) and not all(
            value is not None for value in adapter_values
        ):
            raise ValueError("Local adapter provenance must be complete or absent.")
        return self


class LocalGenerationResult(ModelRuntimeDomainModel):
    schema_id: Literal["corpuskit.local-generation-result.v1"] = (
        "corpuskit.local-generation-result.v1"
    )
    model: ModelExecutionManifest
    accepted: tuple[AcceptedCandidate, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    iterations: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0.0)
    stop_reason: GenerationStopReason
    reproducibility: ReproducibilityClass


class AnalysisText(ModelRuntimeDomainModel):
    source_id: str = Field(min_length=1, max_length=192)
    text: str = Field(min_length=1, max_length=MAX_SENTENCE_CHARACTERS)

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        if _SAFE_SOURCE_ID.fullmatch(self.source_id) is None or not self.text.strip():
            raise ValueError("Analysis source IDs and text must be safe and non-empty.")
        return self


class LanguageModelAnalysisRequest(GenerationDomainModel):
    selection: LocalModelSelection
    texts: tuple[AnalysisText, ...] = Field(
        min_length=1,
        max_length=MAX_MODEL_ANALYSIS_SENTENCES,
    )
    batch_size: int = Field(default=8, ge=1, le=32)
    max_length: int = Field(default=512, ge=2, le=512)
    composite_scoring: CompositeScoringRequest | None = None
    activity_timeout_seconds: float = Field(default=120.0, gt=0.0, le=MAX_ACTIVITY_SECONDS)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        source_ids = [item.source_id for item in self.texts]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Analysis source IDs must be unique.")
        composite = self.composite_scoring
        if composite is not None:
            if composite.options.weights.fluency <= 0:
                raise ValueError("Durable composite analysis requires a non-zero fluency weight.")
            analysis_rows = {(item.source_id, item.text) for item in self.texts}
            candidate_rows = {(item.source_id, item.text) for item in composite.candidates}
            if len(composite.candidates) != len(self.texts) or candidate_rows != analysis_rows:
                raise ValueError(
                    "Composite candidates must exactly match the bounded analysis texts."
                )
        return self


class FluencyScore(ModelRuntimeDomainModel):
    source_id: str
    score: float = Field(ge=0.0, le=1.0)


class CorpusPerplexity(ModelRuntimeDomainModel):
    per_sentence: tuple[float, ...]
    corpus_perplexity: float = Field(gt=0.0)
    mean_perplexity: float = Field(gt=0.0)
    median_perplexity: float = Field(gt=0.0)
    std_perplexity: float = Field(ge=0.0)
    min_perplexity: float = Field(gt=0.0)
    max_perplexity: float = Field(gt=0.0)
    num_sentences: int = Field(ge=1)
    num_tokens: int = Field(ge=1)
    total_nll: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if len(self.per_sentence) != self.num_sentences:
            raise ValueError("Per-sentence perplexity count must match num_sentences.")
        return self


class SentencePerplexity(ModelRuntimeDomainModel):
    source_id: str
    status: PerplexitySentenceStatus
    perplexity: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.status is PerplexitySentenceStatus.SCORED) != (self.perplexity is not None):
            raise ValueError("Only scored sentences may have a perplexity value.")
        return self


class LanguageModelAnalysisResult(ModelRuntimeDomainModel):
    schema_id: Literal["corpuskit.language-model-analysis-result.v1"] = (
        "corpuskit.language-model-analysis-result.v1"
    )
    model: ModelExecutionManifest
    shared_model_instance: Literal[True] = True
    fluency: tuple[FluencyScore, ...]
    perplexity: CorpusPerplexity
    sentence_perplexities: tuple[SentencePerplexity, ...]
    input_sentence_count: int = Field(ge=1)
    scored_sentence_count: int = Field(ge=1)
    composite_scoring: CompositeScoringResult | None = None


class LanguageModelAnalysisEstimate(ModelRuntimeDomainModel):
    """No-I/O upper bound for one authorized worker-side analysis."""

    schema_id: Literal["corpuskit.language-model-analysis-estimate.v1"] = (
        "corpuskit.language-model-analysis-estimate.v1"
    )
    model: str
    revision: str
    required_profile: WorkerModelProfile
    input_sentence_count: int = Field(ge=1, le=MAX_MODEL_ANALYSIS_SENTENCES)
    maximum_fluency_evaluations: int = Field(
        ge=1,
        le=MAX_FLUENCY_SCORING_EVALUATIONS,
    )
    maximum_fluency_tokens: int = Field(ge=2)
    maximum_perplexity_tokens: int = Field(ge=2)
    composite_scoring_enabled: bool
    composite_reuses_fluency_scores: Literal[True] = True
    whole_activity_timeout_seconds: float = Field(gt=0.0, le=MAX_ACTIVITY_SECONDS)
    network_during_estimate: Literal[False] = False


class RuntimeValidationResult(ModelRuntimeDomainModel):
    schema_id: Literal["corpuskit.model-runtime-validation.v1"] = (
        "corpuskit.model-runtime-validation.v1"
    )
    valid: Literal[True] = True
    operation: Literal["hosted_generation", "local_generation", "language_model_analysis"]
    worker_only: Literal[True] = True
    network_during_validation: Literal[False] = False
    required_profile: WorkerModelProfile | None = None
    model: str
    provider: str | None = None
    maximum_authorized_cost_usd: Decimal | None = None
    maximum_requests: int | None = None
    request_delay_seconds: float | None = Field(
        default=None,
        ge=0.0,
        le=MAX_HOSTED_REQUEST_DELAY_SECONDS,
    )
    whole_activity_timeout_seconds: float = Field(gt=0.0, le=MAX_ACTIVITY_SECONDS)


class HostedCostEstimate(ModelRuntimeDomainModel):
    """Conservative server-priced ceiling; this operation performs no provider call."""

    schema_id: Literal["corpuskit.hosted-cost-estimate.v1"] = "corpuskit.hosted-cost-estimate.v1"
    provider: str
    model: str
    maximum_requests: int = Field(ge=1, le=MAX_HOSTED_REQUESTS)
    request_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=MAX_HOSTED_REQUEST_DELAY_SECONDS,
    )
    reserved_input_tokens: int = Field(ge=1)
    reserved_output_tokens: int = Field(ge=1)
    estimated_ceiling_usd: Decimal = Field(ge=Decimal("0"))
    authorized_ceiling_usd: Decimal = Field(gt=Decimal("0"))
    network_during_estimate: Literal[False] = False


def quantization_value(value: ModelQuantization) -> str | None:
    """Map the public explicit none value to CorpusGen's nullable option."""

    return None if value is ModelQuantization.NONE else value.value


__all__ = [
    "DEFAULT_HOSTED_PROMPT_TEMPLATE",
    "MAX_FLUENCY_SCORING_EVALUATIONS",
    "MAX_HOSTED_REQUEST_DELAY_SECONDS",
    "AnalysisText",
    "CorpusPerplexity",
    "FluencyScore",
    "HostedCostEstimate",
    "HostedExecutionManifest",
    "HostedGenerationRequest",
    "HostedGenerationResult",
    "HostedModelPolicy",
    "HostedModelSelection",
    "HostedRunBudget",
    "HostedUsage",
    "ImmutableModelPin",
    "LanguageModelAnalysisEstimate",
    "LanguageModelAnalysisRequest",
    "LanguageModelAnalysisResult",
    "LocalGenerationRequest",
    "LocalGenerationResult",
    "LocalModelPolicy",
    "LocalModelSelection",
    "ModelDevice",
    "ModelExecutionManifest",
    "ModelQuantization",
    "PerplexitySentenceStatus",
    "PhonRlAdapterSelection",
    "ProviderRetryPolicy",
    "ReproducibilityClass",
    "RuntimeValidationResult",
    "SecretReference",
    "SentencePerplexity",
    "WorkerModelProfile",
    "quantization_value",
]
