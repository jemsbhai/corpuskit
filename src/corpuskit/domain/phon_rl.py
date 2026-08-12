"""Bounded, JSON-safe contracts for the Phon-RL laboratory and worker."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from enum import StrEnum
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from corpuskit.domain.corpus import FrozenDomainModel

MAX_RL_TARGET_PHONEMES = 64
MAX_RL_STATE_SENTENCES = 2_000
MAX_RL_PHONEMES_PER_SENTENCE = 1_000
MAX_RL_TEXT_CHARACTERS = 4_000
MAX_RL_TOKENS = 512
MAX_RL_TENSOR_BATCH = 8
MAX_RL_TENSOR_SEQUENCE = 128
MAX_RL_TENSOR_VOCABULARY = 512
MAX_RL_HIDDEN_SIZE = 4_096
MAX_RL_TRAINING_STEPS = 10_000
MAX_RL_TRAINING_BATCH = 32
MAX_RL_ACTIVITY_SECONDS = 86_400.0
MAX_RL_CHECKPOINT_BYTES = 60 * 1024 * 1024
MAX_RL_RESULT_BYTES = 100 * 1024 * 1024
MAX_RL_RESULT_OVERHEAD_BYTES = 20 * 1024 * 1024
MAX_RL_PROMPT_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RL_CHECKPOINT_BASE64_BYTES = ((MAX_RL_CHECKPOINT_BYTES + 2) // 3) * 4
assert MAX_RL_CHECKPOINT_BASE64_BYTES + MAX_RL_RESULT_OVERHEAD_BYTES <= MAX_RL_RESULT_BYTES

_SAFE_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
    re.ASCII,
)
_SAFE_ID = re.compile(r"^[a-z][a-z0-9._-]{1,63}$", re.ASCII)
_SAFE_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$", re.ASCII)
_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$", re.ASCII)
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$", re.ASCII)
_REVISION = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ALLOWED_CHECKPOINT_SUFFIXES = frozenset({".json", ".model", ".safetensors", ".txt", ".tiktoken"})


class PhonRlModel(FrozenDomainModel):
    """Strict immutable base for public Phon-RL artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class PhonRlUnit(StrEnum):
    PHONEME = "phoneme"
    DIPHONE = "diphone"
    TRIPHONE = "triphone"


class PhonRlWorkerProfile(StrEnum):
    LOCAL_GPU = "local_gpu"


class PhonRlPromptKind(StrEnum):
    ARTIFACT = "artifact"
    STRATEGY = "strategy"


class PhonRlPhonemeSequence(PhonRlModel):
    source_id: str = Field(min_length=1, max_length=192)
    phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_RL_PHONEMES_PER_SENTENCE)

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        _validate_source_id(self.source_id)
        _validate_phonemes(self.phonemes)
        return self


class PhonRlRewardState(PhonRlModel):
    """Complete immutable state; callers never share a mutable CorpusGen inventory."""

    schema_id: Literal["corpuskit.phon-rl-reward-state.v1"] = "corpuskit.phon-rl-reward-state.v1"
    target_phonemes: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_RL_TARGET_PHONEMES,
    )
    unit: PhonRlUnit = PhonRlUnit.PHONEME
    committed: tuple[PhonRlPhonemeSequence, ...] = Field(
        default=(),
        max_length=MAX_RL_STATE_SENTENCES,
    )
    revision: int = Field(default=0, ge=0, le=MAX_RL_STATE_SENTENCES)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        _validate_phonemes(self.target_phonemes)
        if len(set(self.target_phonemes)) != len(self.target_phonemes):
            raise ValueError("Phon-RL target phonemes must be unique.")
        source_ids = tuple(item.source_id for item in self.committed)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Phon-RL committed source IDs must be unique.")
        if self.revision != len(self.committed):
            raise ValueError("Phon-RL state revision must equal the committed sentence count.")
        return self


class PhonRlRewardWeights(PhonRlModel):
    coverage: float = Field(default=1.0, ge=0.0, le=100.0)
    phonotactic: float = Field(default=0.0, ge=0.0, le=100.0)
    fluency: float = Field(default=0.0, ge=0.0, le=100.0)


class PhonRlExternalScores(PhonRlModel):
    """Precomputed scalar hooks; no callback or import path crosses HTTP."""

    phonotactic: float | None = Field(default=None, ge=-1_000_000.0, le=1_000_000.0)
    fluency: float | None = Field(default=None, ge=-1_000_000.0, le=1_000_000.0)
    reference_log_probability: float | None = Field(
        default=None,
        ge=-1_000_000.0,
        le=0.0,
    )


class PhonRlSentenceRewardRequest(PhonRlModel):
    state: PhonRlRewardState
    source_id: str = Field(min_length=1, max_length=192)
    phonemes: tuple[str, ...] = Field(min_length=1, max_length=MAX_RL_PHONEMES_PER_SENTENCE)
    text: str | None = Field(default=None, max_length=MAX_RL_TEXT_CHARACTERS)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    weights: PhonRlRewardWeights = PhonRlRewardWeights()
    scores: PhonRlExternalScores = PhonRlExternalScores()

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_source_id(self.source_id)
        _validate_phonemes(self.phonemes)
        _validate_language(self.language)
        if self.text is not None and not self.text.strip():
            raise ValueError("Phon-RL text must be non-blank when supplied.")
        if self.weights.phonotactic > 0.0 and self.scores.phonotactic is None:
            raise ValueError("A weighted phonotactic component requires a score.")
        if (
            self.weights.fluency > 0.0
            and self.scores.fluency is None
            and self.scores.reference_log_probability is None
        ):
            raise ValueError("A weighted fluency component requires a score or reference log-prob.")
        return self


class PhonRlRewardBreakdown(PhonRlModel):
    coverage_reward: float
    phonotactic_reward: float
    fluency_reward: float
    composite_reward: float
    new_units: tuple[str, ...] = Field(max_length=MAX_RL_PHONEMES_PER_SENTENCE)
    coverage_gain: int = Field(ge=0, le=MAX_RL_PHONEMES_PER_SENTENCE)
    target_size: int = Field(ge=1, le=MAX_RL_TARGET_PHONEMES**3)
    fluency_signal: Literal["explicit", "reference_log_probability", "none"]

    @model_validator(mode="after")
    def validate_breakdown(self) -> Self:
        if tuple(sorted(set(self.new_units))) != self.new_units:
            raise ValueError("Phon-RL reward units must be sorted and unique.")
        if self.coverage_gain != len(self.new_units):
            raise ValueError("Phon-RL coverage gain must match new units.")
        if self.coverage_reward != self.coverage_gain / self.target_size:
            raise ValueError("Phon-RL coverage reward normalization is inconsistent.")
        return self


class PhonRlSentenceRewardResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-sentence-reward.v1"] = (
        "corpuskit.phon-rl-sentence-reward.v1"
    )
    breakdown: PhonRlRewardBreakdown
    committed: bool
    state: PhonRlRewardState


class PhonRlTokenPiece(PhonRlModel):
    token_id: int = Field(ge=0, le=10_000_000)
    decoded_text: str = Field(max_length=256)
    raw_token: str = Field(max_length=256)


class PhonRlTokenRewardRequest(PhonRlModel):
    state: PhonRlRewardState
    pieces: tuple[PhonRlTokenPiece, ...] = Field(max_length=MAX_RL_TOKENS)
    language: str = Field(default="en-us", min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_language(self.language)
        identities: dict[int, tuple[str, str]] = {}
        for item in self.pieces:
            value = (item.decoded_text, item.raw_token)
            if item.token_id in identities and identities[item.token_id] != value:
                raise ValueError("Repeated Phon-RL token IDs must have one tokenizer identity.")
            identities[item.token_id] = value
        return self


class PhonRlTokenRewardResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-token-reward.v1"] = "corpuskit.phon-rl-token-reward.v1"
    token_ids: tuple[int, ...] = Field(max_length=MAX_RL_TOKENS)
    per_token_rewards: tuple[float, ...] = Field(max_length=MAX_RL_TOKENS)
    word_boundaries: tuple[int, ...] = Field(max_length=MAX_RL_TOKENS)
    words_phonemized: tuple[str, ...] = Field(max_length=MAX_RL_TOKENS)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if len(self.token_ids) != len(self.per_token_rewards):
            raise ValueError("Phon-RL token rewards must align with token IDs.")
        if len(self.word_boundaries) != len(self.words_phonemized):
            raise ValueError("Phon-RL word boundaries must align with phonemized words.")
        if any(index < 0 or index >= len(self.token_ids) for index in self.word_boundaries):
            raise ValueError("Phon-RL word boundary index is invalid.")
        if tuple(sorted(set(self.word_boundaries))) != self.word_boundaries:
            raise ValueError("Phon-RL word boundaries must be sorted and unique.")
        return self


class PhonRlHierarchicalRewardRequest(PhonRlModel):
    sentence: PhonRlSentenceRewardRequest
    pieces: tuple[PhonRlTokenPiece, ...] = Field(max_length=MAX_RL_TOKENS)


class PhonRlHierarchicalRewardResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-hierarchical-reward.v1"] = (
        "corpuskit.phon-rl-hierarchical-reward.v1"
    )
    sentence: PhonRlRewardBreakdown
    tokens: PhonRlTokenRewardResult
    state_revision: int = Field(ge=0)


class PhonRlFloatMatrix(PhonRlModel):
    values: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        _matrix_shape(self.values)
        return self


class PhonRlIntMatrix(PhonRlModel):
    values: tuple[tuple[int, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        _matrix_shape(self.values)
        return self


class PhonRlBoolMatrix(PhonRlModel):
    values: tuple[tuple[bool, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        _matrix_shape(self.values)
        return self


class PhonRlHiddenMatrix(PhonRlModel):
    values: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        hidden = len(self.values[0])
        if not 1 <= hidden <= MAX_RL_HIDDEN_SIZE or any(len(row) != hidden for row in self.values):
            raise ValueError("ValueHead hidden-state tensors must be rectangular and bounded.")
        return self


class PhonRlLogProbRequest(PhonRlModel):
    logits: tuple[tuple[tuple[float, ...], ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )
    actions: PhonRlIntMatrix

    @model_validator(mode="after")
    def validate_shapes(self) -> Self:
        batch, sequence, vocabulary = _logit_shape(self.logits)
        if _matrix_shape(self.actions.values) != (batch, sequence):
            raise ValueError("PPO actions must match the logits batch and sequence dimensions.")
        if any(action < 0 or action >= vocabulary for row in self.actions.values for action in row):
            raise ValueError("PPO action IDs must index the supplied vocabulary.")
        return self


class PhonRlKlRequest(PhonRlModel):
    policy_log_probs: PhonRlFloatMatrix
    reference_log_probs: PhonRlFloatMatrix

    @model_validator(mode="after")
    def validate_shapes(self) -> Self:
        if _matrix_shape(self.policy_log_probs.values) != _matrix_shape(
            self.reference_log_probs.values
        ):
            raise ValueError("PPO policy and reference log-prob shapes must match.")
        return self


class PhonRlGaeRequest(PhonRlModel):
    rewards: PhonRlFloatMatrix
    values: PhonRlFloatMatrix
    gamma: float = Field(default=1.0, ge=0.0, le=1.0)
    lambda_: float = Field(default=0.95, ge=0.0, le=1.0, alias="lambda")
    mask: PhonRlBoolMatrix | None = None

    @model_validator(mode="after")
    def validate_shapes(self) -> Self:
        shape = _matrix_shape(self.rewards.values)
        if _matrix_shape(self.values.values) != shape:
            raise ValueError("PPO reward and value shapes must match.")
        if self.mask is not None and _matrix_shape(self.mask.values) != shape:
            raise ValueError("PPO GAE mask must match the reward shape.")
        return self


class PhonRlClipLossRequest(PhonRlModel):
    advantages: PhonRlFloatMatrix
    old_log_probs: PhonRlFloatMatrix
    new_log_probs: PhonRlFloatMatrix
    clip_epsilon: float = Field(default=0.2, ge=0.0, le=1.0)
    mask: PhonRlBoolMatrix | None = None

    @model_validator(mode="after")
    def validate_shapes(self) -> Self:
        shape = _matrix_shape(self.advantages.values)
        if (
            _matrix_shape(self.old_log_probs.values) != shape
            or _matrix_shape(self.new_log_probs.values) != shape
            or (self.mask is not None and _matrix_shape(self.mask.values) != shape)
        ):
            raise ValueError("PPO clip-loss tensors and optional mask must have one shape.")
        return self


class PhonRlValueHeadRequest(PhonRlModel):
    hidden_states_2d: PhonRlHiddenMatrix | None = None
    hidden_states_3d: tuple[tuple[tuple[float, ...], ...], ...] | None = None
    dropout: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: int = Field(default=0, ge=0, le=4_294_967_295)

    @model_validator(mode="after")
    def validate_hidden_states(self) -> Self:
        if (self.hidden_states_2d is None) == (self.hidden_states_3d is None):
            raise ValueError("ValueHead requires exactly one 2D or 3D hidden-state tensor.")
        if self.hidden_states_3d is not None:
            _hidden_shape(self.hidden_states_3d)
        return self


class PhonRlMatrixResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-matrix.v1"] = "corpuskit.phon-rl-matrix.v1"
    values: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        _matrix_shape(self.values)
        return self


class PhonRlGaeResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-gae.v1"] = "corpuskit.phon-rl-gae.v1"
    advantages: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )
    returns: tuple[tuple[float, ...], ...] = Field(
        min_length=1,
        max_length=MAX_RL_TENSOR_BATCH,
    )

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if _matrix_shape(self.advantages) != _matrix_shape(self.returns):
            raise ValueError("PPO GAE result tensors must have one shape.")
        return self


class PhonRlScalarResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-scalar.v1"] = "corpuskit.phon-rl-scalar.v1"
    value: float


class PhonRlValueHeadResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-value-head.v1"] = "corpuskit.phon-rl-value-head.v1"
    hidden_size: int = Field(ge=1, le=MAX_RL_HIDDEN_SIZE)
    dropout: float = Field(ge=0.0, le=1.0)
    rank: Literal[1, 2]
    values: tuple[float, ...] | tuple[tuple[float, ...], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        first = self.values[0]
        nested = isinstance(first, tuple)
        if self.rank == 1:
            if nested or len(self.values) > MAX_RL_TENSOR_BATCH:
                raise ValueError("ValueHead rank-1 results must be a bounded vector.")
        else:
            if not nested or len(self.values) > MAX_RL_TENSOR_BATCH:
                raise ValueError("ValueHead rank-2 results must be a bounded matrix.")
            _matrix_shape(cast(tuple[tuple[object, ...], ...], self.values))
        return self


class PhonRlSnapshotPin(PhonRlModel):
    repository_id: str = Field(min_length=3, max_length=192)
    revision: str = Field(min_length=40, max_length=40)
    snapshot_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_pin(self) -> Self:
        if _SAFE_REPOSITORY.fullmatch(self.repository_id) is None:
            raise ValueError("Phon-RL models require a namespaced identifier, not a path or URL.")
        if _REVISION.fullmatch(self.revision) is None:
            raise ValueError("Phon-RL revisions require an immutable lowercase commit.")
        if _SHA256.fullmatch(self.snapshot_sha256) is None:
            raise ValueError("Phon-RL snapshots require a lowercase SHA-256 digest.")
        return self


class PhonRlRuntimePolicyEntry(PhonRlModel):
    runtime_id: str = Field(min_length=2, max_length=64)
    model: PhonRlSnapshotPin
    tokenizer: PhonRlSnapshotPin
    cache_root_id: str = Field(min_length=2, max_length=64)
    cache_mount_read_only: Literal[True]
    allow_static_prompts: bool = False
    allow_peft: bool = False
    allowed_peft_ranks: tuple[int, ...] = Field(default=(), max_length=16)
    allowed_peft_alphas: tuple[int, ...] = Field(default=(), max_length=16)
    allowed_prompt_strategies: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        _validate_safe_id(self.runtime_id, "runtime")
        _validate_safe_id(self.cache_root_id, "cache root")
        if self.model != self.tokenizer:
            raise ValueError("The public CorpusGen trainer requires one model/tokenizer snapshot.")
        if len(set(self.allowed_peft_ranks)) != len(self.allowed_peft_ranks) or any(
            value < 1 or value > 256 for value in self.allowed_peft_ranks
        ):
            raise ValueError("Phon-RL PEFT rank allowlists must be unique and bounded.")
        if len(set(self.allowed_peft_alphas)) != len(self.allowed_peft_alphas) or any(
            value < 1 or value > 1_024 for value in self.allowed_peft_alphas
        ):
            raise ValueError("Phon-RL PEFT alpha allowlists must be unique and bounded.")
        if len(set(self.allowed_prompt_strategies)) != len(self.allowed_prompt_strategies):
            raise ValueError("Phon-RL prompt strategy allowlists must be unique.")
        for strategy in self.allowed_prompt_strategies:
            _validate_safe_id(strategy, "prompt strategy")
        if not self.allow_peft and (self.allowed_peft_ranks or self.allowed_peft_alphas):
            raise ValueError("Phon-RL PEFT options require PEFT to be enabled by policy.")
        return self


class PhonRlStaticPromptSource(PhonRlModel):
    kind: Literal[PhonRlPromptKind.ARTIFACT] = PhonRlPromptKind.ARTIFACT
    artifact_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_count: int = Field(ge=1, le=10_000)


class PhonRlPromptArtifact(PhonRlModel):
    """Canonical immutable prompt bytes; prompt text never belongs in a run spec."""

    schema_id: Literal["corpuskit.phon-rl-prompt-artifact.v1"] = (
        "corpuskit.phon-rl-prompt-artifact.v1"
    )
    prompts: tuple[str, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_prompts(self) -> Self:
        try:
            valid = all(
                prompt.strip()
                and len(prompt) <= MAX_RL_TEXT_CHARACTERS
                and bool(prompt.encode("utf-8"))
                for prompt in self.prompts
            )
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise ValueError("Phon-RL static prompts must be non-blank and bounded.")
        if len(self.canonical_bytes()) > MAX_RL_PROMPT_ARTIFACT_BYTES:
            raise ValueError("Phon-RL static prompt artifacts exceed the byte limit.")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class PhonRlDynamicPromptSource(PhonRlModel):
    kind: Literal[PhonRlPromptKind.STRATEGY] = PhonRlPromptKind.STRATEGY
    strategy_id: str = Field(min_length=2, max_length=64)
    requested_prompts: int = Field(default=1, ge=1, le=10_000)

    @field_validator("strategy_id")
    @classmethod
    def safe_strategy(cls, value: str) -> str:
        _validate_safe_id(value, "prompt strategy")
        return value


class PhonRlTrainingParameters(PhonRlModel):
    num_steps: int = Field(default=100, ge=1, le=MAX_RL_TRAINING_STEPS)
    batch_size: int = Field(default=4, ge=1, le=MAX_RL_TRAINING_BATCH)
    learning_rate: float = Field(default=1.41e-5, ge=1e-8, le=1e-2)
    kl_coefficient: float = Field(default=0.1, ge=0.0, le=10.0)
    clip_epsilon: float = Field(default=0.2, ge=0.0, le=1.0)
    gae_gamma: float = Field(default=1.0, ge=0.0, le=1.0)
    gae_lambda: float = Field(default=0.95, ge=0.0, le=1.0)
    value_loss_coefficient: float = Field(default=0.5, ge=0.0, le=10.0)
    max_new_tokens: int = Field(default=64, ge=1, le=512)
    temperature: float = Field(default=0.8, gt=0.0, le=2.0)
    use_peft: bool = False
    peft_rank: int = Field(default=8, ge=1, le=256)
    peft_alpha: int = Field(default=16, ge=1, le=1_024)
    seed: int = Field(ge=0, le=4_294_967_295)
    activity_timeout_seconds: float = Field(
        default=3_600.0,
        gt=0.0,
        le=MAX_RL_ACTIVITY_SECONDS,
    )


class PhonRlTrainingRequest(PhonRlModel):
    runtime_id: str = Field(min_length=2, max_length=64)
    language: str = Field(default="en-us", min_length=2, max_length=32)
    unit: PhonRlUnit = PhonRlUnit.PHONEME
    target_phonemes: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_RL_TARGET_PHONEMES,
    )
    weights: PhonRlRewardWeights = PhonRlRewardWeights()
    prompt_source: PhonRlStaticPromptSource | PhonRlDynamicPromptSource = Field(
        discriminator="kind"
    )
    parameters: PhonRlTrainingParameters

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _validate_safe_id(self.runtime_id, "runtime")
        _validate_language(self.language)
        _validate_phonemes(self.target_phonemes)
        if len(set(self.target_phonemes)) != len(self.target_phonemes):
            raise ValueError("Phon-RL training targets must be unique.")
        if self.weights.phonotactic != 0.0 or self.weights.fluency != 0.0:
            raise ValueError(
                "Phon-RL training currently permits only the coverage reward component."
            )
        return self


class PhonRlTrainingValidationResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-training-validation.v1"] = (
        "corpuskit.phon-rl-training-validation.v1"
    )
    valid: Literal[True] = True
    runtime_id: str
    required_profile: Literal["gpu-training"] = "gpu-training"
    worker_only: Literal[True] = True
    network_during_validation: Literal[False] = False
    activity_timeout_seconds: float = Field(gt=0.0, le=MAX_RL_ACTIVITY_SECONDS)


class PhonRlResourceEstimate(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-resource-estimate.v1"] = (
        "corpuskit.phon-rl-resource-estimate.v1"
    )
    generated_token_ceiling: int = Field(ge=1)
    policy_forward_passes: int = Field(ge=1)
    reference_forward_passes: int = Field(ge=1)
    optimizer_steps: int = Field(ge=1)
    model_copies: Literal[2] = 2
    minimum_checkpoint_budget_bytes: int = Field(ge=1)
    network_during_estimate: Literal[False] = False


class PhonRlProgressPoint(PhonRlModel):
    step: int = Field(ge=0, le=MAX_RL_TRAINING_STEPS)
    mean_reward: float
    policy_loss: float


class PhonRlCheckpointCompatibility(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-checkpoint-compatibility.v1"] = (
        "corpuskit.phon-rl-checkpoint-compatibility.v1"
    )
    base_model_id: str
    base_model_revision: str
    base_model_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokenizer_id: str
    tokenizer_revision: str
    tokenizer_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpusgen_version: str = Field(min_length=1, max_length=128)
    torch_version: str = Field(min_length=1, max_length=128)
    transformers_version: str = Field(min_length=1, max_length=128)
    peft_version: str | None = Field(default=None, min_length=1, max_length=128)
    peft_adapter: bool

    @model_validator(mode="after")
    def validate_compatibility(self) -> Self:
        PhonRlSnapshotPin(
            repository_id=self.base_model_id,
            revision=self.base_model_revision,
            snapshot_sha256=self.base_model_snapshot_sha256,
        )
        PhonRlSnapshotPin(
            repository_id=self.tokenizer_id,
            revision=self.tokenizer_revision,
            snapshot_sha256=self.tokenizer_snapshot_sha256,
        )
        if self.peft_adapter != (self.peft_version is not None):
            raise ValueError("PEFT checkpoint compatibility must include the PEFT version.")
        return self


class PhonRlCheckpointFile(PhonRlModel):
    path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, le=MAX_RL_CHECKPOINT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_base64: str = Field(max_length=MAX_RL_CHECKPOINT_BASE64_BYTES)

    @model_validator(mode="after")
    def validate_file(self) -> Self:
        if (
            _SAFE_FILE.fullmatch(self.path) is None
            or self.path.startswith("/")
            or ".." in self.path.split("/")
            or _suffix(self.path) not in _ALLOWED_CHECKPOINT_SUFFIXES
        ):
            raise ValueError("Phon-RL checkpoint paths or file types are not safe.")
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except ValueError:
            raise ValueError("Phon-RL checkpoint content is not canonical base64.") from None
        if base64.b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("Phon-RL checkpoint content is not canonical base64.")
        if len(content) != self.size_bytes or hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("Phon-RL checkpoint file integrity validation failed.")
        if self.path.endswith((".bin", ".pkl", ".pickle", ".pt", ".pth")):
            raise ValueError("Pickle-compatible checkpoint weights are forbidden.")
        return self


class PhonRlCheckpointBundle(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-checkpoint-bundle.v1"] = (
        "corpuskit.phon-rl-checkpoint-bundle.v1"
    )
    compatibility: PhonRlCheckpointCompatibility
    files: tuple[PhonRlCheckpointFile, ...] = Field(min_length=1, max_length=256)
    total_size_bytes: int = Field(ge=1, le=MAX_RL_CHECKPOINT_BYTES)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        compatibility: PhonRlCheckpointCompatibility,
        files: tuple[PhonRlCheckpointFile, ...],
    ) -> PhonRlCheckpointBundle:
        total = sum(item.size_bytes for item in files)
        digest = _checkpoint_digest(compatibility, files)
        return cls(
            compatibility=compatibility,
            files=files,
            total_size_bytes=total,
            content_sha256=digest,
        )

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        paths = tuple(item.path for item in self.files)
        if len(paths) != len(set(paths)) or tuple(sorted(paths)) != paths:
            raise ValueError("Phon-RL checkpoint paths must be sorted and unique.")
        if self.total_size_bytes != sum(item.size_bytes for item in self.files):
            raise ValueError("Phon-RL checkpoint total size is inconsistent.")
        if self.content_sha256 != _checkpoint_digest(self.compatibility, self.files):
            raise ValueError("Phon-RL checkpoint bundle integrity validation failed.")
        if not any(item.path.endswith(".safetensors") for item in self.files):
            raise ValueError("Phon-RL checkpoints require safetensors weights.")
        return self


class PhonRlTrainingManifest(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-training-manifest.v1"] = (
        "corpuskit.phon-rl-training-manifest.v1"
    )
    runtime_id: str
    model: PhonRlSnapshotPin
    tokenizer: PhonRlSnapshotPin
    language: str
    unit: PhonRlUnit
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_source_kind: PhonRlPromptKind
    prompt_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters: PhonRlTrainingParameters
    corpusgen_version: str
    torch_version: str
    transformers_version: str
    peft_version: str | None = None
    local_files_only: Literal[True] = True
    trust_remote_code: Literal[False] = False
    safetensors_only: Literal[True] = True
    cache_mount_read_only: Literal[True] = True
    worker_profile: Literal["gpu-training"] = "gpu-training"
    reproducibility: Literal["best_effort"] = "best_effort"
    prompts_persisted_in_manifest: Literal[False] = False


class PhonRlTrainingResult(PhonRlModel):
    schema_id: Literal["corpuskit.phon-rl-training-result.v1"] = (
        "corpuskit.phon-rl-training-result.v1"
    )
    manifest: PhonRlTrainingManifest
    progress: tuple[PhonRlProgressPoint, ...] = Field(
        min_length=1,
        max_length=MAX_RL_TRAINING_STEPS,
    )
    mean_rewards: tuple[float, ...] = Field(
        min_length=1,
        max_length=MAX_RL_TRAINING_STEPS,
    )
    total_steps: int = Field(ge=1, le=MAX_RL_TRAINING_STEPS)
    final_coverage: float = Field(ge=0.0, le=1.0)
    checkpoint: PhonRlCheckpointBundle
    strategy_modify_logits_identity: Literal[True] = True
    peft_inference_status: Literal["not_requested", "application_loader_ready"]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.total_steps != len(self.mean_rewards) or self.total_steps != len(self.progress):
            raise ValueError("Phon-RL result step counts are inconsistent.")
        if tuple(item.step for item in self.progress) != tuple(range(self.total_steps)):
            raise ValueError("Phon-RL progress steps must be complete and ordered.")
        expected_peft = self.manifest.parameters.use_peft
        if expected_peft != self.checkpoint.compatibility.peft_adapter:
            raise ValueError("Phon-RL checkpoint PEFT mode does not match the run manifest.")
        if expected_peft != (self.peft_inference_status == "application_loader_ready"):
            raise ValueError("Phon-RL PEFT inference disclosure is inconsistent.")
        return self


def prompt_source_sha256(source: PhonRlStaticPromptSource | PhonRlDynamicPromptSource) -> str:
    payload = source.model_dump(mode="json")
    return hashlib.sha256(_canonical(payload)).hexdigest()


def target_sha256(target_phonemes: tuple[str, ...], unit: PhonRlUnit) -> str:
    return hashlib.sha256(
        _canonical({"target_phonemes": list(target_phonemes), "unit": unit.value})
    ).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _checkpoint_digest(
    compatibility: PhonRlCheckpointCompatibility,
    files: tuple[PhonRlCheckpointFile, ...],
) -> str:
    payload = {
        "compatibility": compatibility.model_dump(mode="json"),
        "files": [
            {"path": item.path, "sha256": item.sha256, "size_bytes": item.size_bytes}
            for item in files
        ],
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _suffix(path: str) -> str:
    name = path.rsplit("/", maxsplit=1)[-1]
    return f".{name.rsplit('.', maxsplit=1)[-1].casefold()}" if "." in name else ""


def _validate_language(value: str) -> None:
    if _SAFE_LANGUAGE.fullmatch(value) is None:
        raise ValueError("Phon-RL language must use a safe language-tag grammar.")


def _validate_safe_id(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"Phon-RL {label} IDs must use the safe identifier grammar.")


def _validate_source_id(value: str) -> None:
    if _SAFE_SOURCE.fullmatch(value) is None:
        raise ValueError("Phon-RL source IDs must use the safe identifier grammar.")


def _validate_phonemes(values: tuple[str, ...]) -> None:
    if any(not item.strip() or len(item) > 64 for item in values):
        raise ValueError("Phon-RL phonemes must be non-empty and bounded.")


def _matrix_shape(values: tuple[tuple[object, ...], ...]) -> tuple[int, int]:
    columns = len(values[0])
    if not 1 <= columns <= MAX_RL_TENSOR_SEQUENCE or any(len(row) != columns for row in values):
        raise ValueError("PPO tensors must be non-empty, rectangular, and bounded.")
    return len(values), columns


def _logit_shape(
    values: tuple[tuple[tuple[float, ...], ...], ...],
) -> tuple[int, int, int]:
    sequence = len(values[0])
    if not 1 <= sequence <= MAX_RL_TENSOR_SEQUENCE or any(len(row) != sequence for row in values):
        raise ValueError("PPO logits must have bounded rectangular batch/sequence dimensions.")
    vocabulary = len(values[0][0])
    if not 1 <= vocabulary <= MAX_RL_TENSOR_VOCABULARY or any(
        len(token) != vocabulary for row in values for token in row
    ):
        raise ValueError("PPO logits must have a bounded rectangular vocabulary dimension.")
    return len(values), sequence, vocabulary


def _hidden_shape(
    values: tuple[tuple[tuple[float, ...], ...], ...],
) -> tuple[int, int, int]:
    batch = len(values)
    if not 1 <= batch <= MAX_RL_TENSOR_BATCH:
        raise ValueError("ValueHead batch size exceeds the lab limit.")
    sequence = len(values[0])
    if not 1 <= sequence <= MAX_RL_TENSOR_SEQUENCE or any(len(row) != sequence for row in values):
        raise ValueError("ValueHead sequence tensors must be rectangular and bounded.")
    hidden = len(values[0][0])
    if not 1 <= hidden <= MAX_RL_HIDDEN_SIZE or any(
        len(token) != hidden for row in values for token in row
    ):
        raise ValueError("ValueHead hidden tensors must be rectangular and bounded.")
    return batch, sequence, hidden


__all__ = [
    "MAX_RL_ACTIVITY_SECONDS",
    "MAX_RL_CHECKPOINT_BASE64_BYTES",
    "MAX_RL_CHECKPOINT_BYTES",
    "MAX_RL_PROMPT_ARTIFACT_BYTES",
    "MAX_RL_RESULT_BYTES",
    "MAX_RL_RESULT_OVERHEAD_BYTES",
    "PhonRlBoolMatrix",
    "PhonRlCheckpointBundle",
    "PhonRlCheckpointCompatibility",
    "PhonRlCheckpointFile",
    "PhonRlClipLossRequest",
    "PhonRlDynamicPromptSource",
    "PhonRlExternalScores",
    "PhonRlFloatMatrix",
    "PhonRlGaeRequest",
    "PhonRlGaeResult",
    "PhonRlHiddenMatrix",
    "PhonRlHierarchicalRewardRequest",
    "PhonRlHierarchicalRewardResult",
    "PhonRlIntMatrix",
    "PhonRlKlRequest",
    "PhonRlLogProbRequest",
    "PhonRlMatrixResult",
    "PhonRlPhonemeSequence",
    "PhonRlProgressPoint",
    "PhonRlPromptArtifact",
    "PhonRlPromptKind",
    "PhonRlResourceEstimate",
    "PhonRlRewardBreakdown",
    "PhonRlRewardState",
    "PhonRlRewardWeights",
    "PhonRlRuntimePolicyEntry",
    "PhonRlScalarResult",
    "PhonRlSentenceRewardRequest",
    "PhonRlSentenceRewardResult",
    "PhonRlSnapshotPin",
    "PhonRlStaticPromptSource",
    "PhonRlTokenPiece",
    "PhonRlTokenRewardRequest",
    "PhonRlTokenRewardResult",
    "PhonRlTrainingManifest",
    "PhonRlTrainingParameters",
    "PhonRlTrainingRequest",
    "PhonRlTrainingResult",
    "PhonRlTrainingValidationResult",
    "PhonRlUnit",
    "PhonRlValueHeadRequest",
    "PhonRlValueHeadResult",
    "PhonRlWorkerProfile",
    "prompt_source_sha256",
    "target_sha256",
]
