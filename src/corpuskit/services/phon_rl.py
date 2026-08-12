"""Pure Phon-RL policy plus sanitized lab and worker coordination."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from corpuskit.domain.errors import (
    ApplicationError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.phon_rl import (
    PhonRlClipLossRequest,
    PhonRlGaeRequest,
    PhonRlGaeResult,
    PhonRlHierarchicalRewardRequest,
    PhonRlHierarchicalRewardResult,
    PhonRlKlRequest,
    PhonRlLogProbRequest,
    PhonRlMatrixResult,
    PhonRlPhonemeSequence,
    PhonRlProgressPoint,
    PhonRlResourceEstimate,
    PhonRlRuntimePolicyEntry,
    PhonRlScalarResult,
    PhonRlSentenceRewardRequest,
    PhonRlSentenceRewardResult,
    PhonRlStaticPromptSource,
    PhonRlTokenRewardRequest,
    PhonRlTokenRewardResult,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
    PhonRlTrainingValidationResult,
    PhonRlValueHeadRequest,
    PhonRlValueHeadResult,
    PhonRlWorkerProfile,
)


class PhonRlAuthorizedPromptReader(Protocol):
    def read(self, source: PhonRlStaticPromptSource) -> tuple[str, ...]: ...


class PhonRlLabEngine(Protocol):
    """CorpusGen adapter methods safe for bounded synchronous laboratory calls."""

    def sentence_reward(
        self,
        request: PhonRlSentenceRewardRequest,
        *,
        commit: bool,
    ) -> PhonRlSentenceRewardResult: ...

    def token_rewards(self, request: PhonRlTokenRewardRequest) -> PhonRlTokenRewardResult: ...

    def hierarchical_reward(
        self,
        request: PhonRlHierarchicalRewardRequest,
    ) -> PhonRlHierarchicalRewardResult: ...

    def log_probs(self, request: PhonRlLogProbRequest) -> PhonRlMatrixResult: ...

    def kl_penalty(self, request: PhonRlKlRequest) -> PhonRlMatrixResult: ...

    def gae(self, request: PhonRlGaeRequest) -> PhonRlGaeResult: ...

    def clip_loss(self, request: PhonRlClipLossRequest) -> PhonRlScalarResult: ...

    def value_head(self, request: PhonRlValueHeadRequest) -> PhonRlValueHeadResult: ...


class PhonRlTrainingEngine(Protocol):
    """Worker-only engine; implementations may load an offline local model."""

    def train(
        self,
        request: PhonRlTrainingRequest,
        policy: PhonRlRuntimePolicyEntry,
        *,
        emit: Callable[[PhonRlProgressPoint], None] | None = None,
        prompt_reader: PhonRlAuthorizedPromptReader | None = None,
    ) -> PhonRlTrainingResult: ...


class PhonRlLabService:
    """No-state façade; every request carries and returns its complete reward state."""

    def __init__(self, engine: PhonRlLabEngine) -> None:
        self._engine = engine

    def peek(self, request: PhonRlSentenceRewardRequest) -> PhonRlSentenceRewardResult:
        return _safe_call(
            "phon_rl.reward.peek",
            lambda: self._sentence_reward(request, commit=False),
        )

    def commit(self, request: PhonRlSentenceRewardRequest) -> PhonRlSentenceRewardResult:
        if request.source_id in {item.source_id for item in request.state.committed}:
            raise InvalidRequestError("phon_rl.reward.duplicate_source")
        return _safe_call(
            "phon_rl.reward.commit",
            lambda: self._sentence_reward(request, commit=True),
        )

    def token_rewards(self, request: PhonRlTokenRewardRequest) -> PhonRlTokenRewardResult:
        result = _safe_call(
            "phon_rl.reward.tokens",
            lambda: self._engine.token_rewards(request),
        )
        if result.token_ids != tuple(item.token_id for item in request.pieces):
            raise EngineContractError("phon_rl.reward.result")
        return result

    def hierarchical(
        self,
        request: PhonRlHierarchicalRewardRequest,
    ) -> PhonRlHierarchicalRewardResult:
        result = _safe_call(
            "phon_rl.reward.hierarchical",
            lambda: self._engine.hierarchical_reward(request),
        )
        if (
            result.state_revision != request.sentence.state.revision
            or result.tokens.token_ids != tuple(item.token_id for item in request.pieces)
        ):
            raise EngineContractError("phon_rl.reward.result")
        return result

    def _sentence_reward(
        self,
        request: PhonRlSentenceRewardRequest,
        *,
        commit: bool,
    ) -> PhonRlSentenceRewardResult:
        result = self._engine.sentence_reward(request, commit=commit)
        expected_state = request.state
        if commit:
            expected_state = request.state.model_copy(
                update={
                    "committed": (
                        *request.state.committed,
                        PhonRlPhonemeSequence(
                            source_id=request.source_id,
                            phonemes=request.phonemes,
                        ),
                    ),
                    "revision": request.state.revision + 1,
                }
            )
        if result.committed is not commit or result.state != expected_state:
            raise EngineContractError("phon_rl.reward.result")
        return result

    def log_probs(self, request: PhonRlLogProbRequest) -> PhonRlMatrixResult:
        return _safe_call("phon_rl.ppo.log_probs", lambda: self._engine.log_probs(request))

    def kl_penalty(self, request: PhonRlKlRequest) -> PhonRlMatrixResult:
        return _safe_call("phon_rl.ppo.kl", lambda: self._engine.kl_penalty(request))

    def gae(self, request: PhonRlGaeRequest) -> PhonRlGaeResult:
        return _safe_call("phon_rl.ppo.gae", lambda: self._engine.gae(request))

    def clip_loss(self, request: PhonRlClipLossRequest) -> PhonRlScalarResult:
        return _safe_call("phon_rl.ppo.clip_loss", lambda: self._engine.clip_loss(request))

    def value_head(self, request: PhonRlValueHeadRequest) -> PhonRlValueHeadResult:
        return _safe_call("phon_rl.ppo.value_head", lambda: self._engine.value_head(request))


class PhonRlRuntimePolicy:
    """Exact server allowlist; safe validation and estimation never resolve a snapshot."""

    def __init__(
        self,
        entries: tuple[PhonRlRuntimePolicyEntry, ...],
        *,
        worker_profile: PhonRlWorkerProfile,
    ) -> None:
        identifiers = tuple(item.runtime_id for item in entries)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Phon-RL runtime policy IDs must be unique.")
        self._entries = entries
        self._worker_profile = worker_profile

    @property
    def worker_profile(self) -> PhonRlWorkerProfile:
        return self._worker_profile

    def authorize(self, runtime_id: str) -> PhonRlRuntimePolicyEntry:
        entry = next((item for item in self._entries if item.runtime_id == runtime_id), None)
        if entry is None:
            raise InvalidRequestError("phon_rl.runtime.allowlist")
        return entry

    def validate(self, request: PhonRlTrainingRequest) -> PhonRlTrainingValidationResult:
        entry = self.authorize(request.runtime_id)
        parameters = request.parameters
        if self._worker_profile is not PhonRlWorkerProfile.LOCAL_GPU:
            raise InvalidRequestError("phon_rl.runtime.worker_profile")
        if parameters.use_peft and (
            not entry.allow_peft
            or parameters.peft_rank not in entry.allowed_peft_ranks
            or parameters.peft_alpha not in entry.allowed_peft_alphas
        ):
            raise InvalidRequestError("phon_rl.runtime.peft_allowlist")
        if isinstance(request.prompt_source, PhonRlStaticPromptSource):
            if not entry.allow_static_prompts:
                raise InvalidRequestError("phon_rl.runtime.static_prompt_policy")
        elif request.prompt_source.strategy_id not in entry.allowed_prompt_strategies:
            raise InvalidRequestError("phon_rl.runtime.prompt_strategy_allowlist")
        return PhonRlTrainingValidationResult(
            runtime_id=request.runtime_id,
            activity_timeout_seconds=parameters.activity_timeout_seconds,
        )

    def estimate(self, request: PhonRlTrainingRequest) -> PhonRlResourceEstimate:
        self.validate(request)
        parameters = request.parameters
        generated = parameters.num_steps * parameters.batch_size * parameters.max_new_tokens
        return PhonRlResourceEstimate(
            generated_token_ceiling=generated,
            policy_forward_passes=parameters.num_steps * 2,
            reference_forward_passes=parameters.num_steps,
            optimizer_steps=parameters.num_steps,
            minimum_checkpoint_budget_bytes=1,
        )


class PhonRlTrainingCoordinator:
    """Worker-only execution boundary with stable, redacted failure behavior."""

    def __init__(self, policy: PhonRlRuntimePolicy, engine: PhonRlTrainingEngine) -> None:
        self.policy = policy
        self._engine = engine

    def train(
        self,
        request: PhonRlTrainingRequest,
        *,
        emit: Callable[[PhonRlProgressPoint], None] | None = None,
        prompt_reader: PhonRlAuthorizedPromptReader | None = None,
    ) -> PhonRlTrainingResult:
        policy = self.policy.authorize(request.runtime_id)
        self.policy.validate(request)
        return _safe_call(
            "phon_rl.training.execute",
            lambda: self._train_engine(
                request,
                policy,
                emit=emit,
                prompt_reader=prompt_reader,
            ),
        )

    def _train_engine(
        self,
        request: PhonRlTrainingRequest,
        policy: PhonRlRuntimePolicyEntry,
        *,
        emit: Callable[[PhonRlProgressPoint], None] | None,
        prompt_reader: PhonRlAuthorizedPromptReader | None,
    ) -> PhonRlTrainingResult:
        if emit is not None and prompt_reader is not None:
            return self._engine.train(
                request,
                policy,
                emit=emit,
                prompt_reader=prompt_reader,
            )
        if emit is not None:
            return self._engine.train(request, policy, emit=emit)
        if prompt_reader is not None:
            return self._engine.train(request, policy, prompt_reader=prompt_reader)
        return self._engine.train(request, policy)


def _safe_call[T](operation: str, callback: Callable[[], T]) -> T:
    try:
        return callback()
    except ApplicationError:
        raise
    except Exception:
        raise EngineUnavailableError(operation) from None


__all__ = [
    "PhonRlLabEngine",
    "PhonRlLabService",
    "PhonRlRuntimePolicy",
    "PhonRlTrainingCoordinator",
    "PhonRlTrainingEngine",
]
