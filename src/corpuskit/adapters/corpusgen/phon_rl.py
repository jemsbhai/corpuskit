"""Secure CorpusGen Phon-RL reward, PPO, and offline-training adapters."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, cast

from corpuskit.adapters.corpusgen.model_runtime import compute_snapshot_digest
from corpuskit.domain.errors import (
    ApplicationError,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.phon_rl import (
    MAX_RL_CHECKPOINT_BYTES,
    PhonRlCheckpointBundle,
    PhonRlCheckpointCompatibility,
    PhonRlCheckpointFile,
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
    PhonRlPromptArtifact,
    PhonRlRewardBreakdown,
    PhonRlRewardState,
    PhonRlRuntimePolicyEntry,
    PhonRlScalarResult,
    PhonRlSentenceRewardRequest,
    PhonRlSentenceRewardResult,
    PhonRlSnapshotPin,
    PhonRlStaticPromptSource,
    PhonRlTokenPiece,
    PhonRlTokenRewardRequest,
    PhonRlTokenRewardResult,
    PhonRlTrainingManifest,
    PhonRlTrainingRequest,
    PhonRlTrainingResult,
    PhonRlValueHeadRequest,
    PhonRlValueHeadResult,
    prompt_source_sha256,
    target_sha256,
)

_EXECUTABLE_SUFFIXES = frozenset(
    {".bat", ".cmd", ".com", ".dll", ".dylib", ".exe", ".js", ".ps1", ".py", ".pyd", ".sh", ".so"}
)
_SUPPORTED_DYNAMIC_PROMPT_STRATEGIES = frozenset({"missing-units-v1"})
_MAX_PROGRESS_POINTS = 10_000
_VALUE_HEAD_RNG_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class PhonRlSnapshotLocation:
    snapshot: Path
    approved_cache_root: Path


class PhonRlSnapshotResolver(Protocol):
    def resolve(
        self,
        pin: PhonRlSnapshotPin,
        *,
        cache_root_id: str,
    ) -> PhonRlSnapshotLocation: ...


class PhonRlPromptArtifactReader(Protocol):
    """Read an already authorized immutable prompt artifact inside the worker."""

    def read(self, source: PhonRlStaticPromptSource) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class BindingTrainingResult:
    mean_rewards: tuple[float, ...]
    total_steps: int
    final_coverage: float


class PhonRlTrainingBindings(Protocol):
    """Test seam around the public CorpusGen trainer constructor and train method."""

    def train(
        self,
        *,
        snapshot: Path,
        request: PhonRlTrainingRequest,
        prompts: tuple[str, ...] | None,
        dynamic_strategy_id: str | None,
        output_dir: Path,
        step_callback: Callable[[int, float, float], None],
    ) -> BindingTrainingResult: ...


class OfflinePhonRlSnapshotResolver:
    """Resolve exact commits from operator-configured local cache roots only."""

    def __init__(self, cache_roots: dict[str, Path]) -> None:
        if not cache_roots:
            raise ValueError("At least one Phon-RL cache root is required.")
        self._cache_roots = {key: value.absolute() for key, value in cache_roots.items()}

    def resolve(
        self,
        pin: PhonRlSnapshotPin,
        *,
        cache_root_id: str,
    ) -> PhonRlSnapshotLocation:
        root = self._cache_roots.get(cache_root_id)
        if root is None:
            raise EngineUnavailableError("phon_rl.snapshot.cache_root")
        try:
            hub = importlib.import_module("huggingface_hub")
            snapshot_download = cast(Callable[..., str], hub.snapshot_download)
        except (ImportError, AttributeError):
            raise DependencyUnavailableError("phon_rl.snapshot.dependency") from None
        try:
            snapshot = Path(
                snapshot_download(
                    repo_id=pin.repository_id,
                    revision=pin.revision,
                    cache_dir=str(root),
                    local_files_only=True,
                )
            ).absolute()
            if snapshot.name != pin.revision or snapshot.parent.name != "snapshots":
                raise EngineUnavailableError("phon_rl.snapshot.layout")
            return PhonRlSnapshotLocation(snapshot=snapshot, approved_cache_root=root)
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("phon_rl.snapshot.resolve") from None


class MissingPromptArtifactReader:
    """Fail closed until the worker is given an authorized prompt-artifact reader."""

    def read(self, source: PhonRlStaticPromptSource) -> tuple[str, ...]:
        del source
        raise EngineUnavailableError("phon_rl.prompt_artifact.reader")


class CorpusgenPhonRlTrainingBindings:
    """Public CorpusGen composition using a verified local path and no loader patching."""

    def __init__(
        self,
        *,
        _device: Literal["cpu", "cuda"] = "cuda",
    ) -> None:
        """Keep production on CUDA while allowing a locked offline CPU acceptance."""

        if _device not in {"cpu", "cuda"}:
            raise ValueError("Phon-RL training device must be cpu or cuda.")
        self._device = _device

    def train(
        self,
        *,
        snapshot: Path,
        request: PhonRlTrainingRequest,
        prompts: tuple[str, ...] | None,
        dynamic_strategy_id: str | None,
        output_dir: Path,
        step_callback: Callable[[int, float, float], None],
    ) -> BindingTrainingResult:
        from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory
        from corpusgen.generate.phon_rl.reward import PhoneticReward
        from corpusgen.generate.phon_rl.trainer import PhonRLTrainer, TrainingConfig

        targets = PhoneticTargetInventory(
            target_phonemes=list(request.target_phonemes),
            unit=request.unit.value,
        )
        reward = PhoneticReward(
            targets,
            coverage_weight=request.weights.coverage,
            phonotactic_weight=request.weights.phonotactic,
            fluency_weight=request.weights.fluency,
            language=request.language,
        )
        parameters = request.parameters
        config = TrainingConfig(
            model_name=str(snapshot),
            num_steps=parameters.num_steps,
            batch_size=parameters.batch_size,
            learning_rate=parameters.learning_rate,
            kl_coeff=parameters.kl_coefficient,
            clip_epsilon=parameters.clip_epsilon,
            gae_gamma=parameters.gae_gamma,
            gae_lambda=parameters.gae_lambda,
            value_loss_coeff=parameters.value_loss_coefficient,
            output_dir=str(output_dir),
            seed=parameters.seed,
            max_new_tokens=parameters.max_new_tokens,
            temperature=parameters.temperature,
            device=self._device,
            language=request.language,
            use_peft=parameters.use_peft,
            peft_r=parameters.peft_rank,
            peft_alpha=parameters.peft_alpha,
        )
        trainer = PhonRLTrainer(reward, config)

        def on_step(**values: object) -> None:
            try:
                step = int(cast(int, values["step"]))
                mean_reward = float(cast(float, values["mean_reward"]))
                policy_loss = float(cast(float, values["policy_loss"]))
            except (KeyError, TypeError, ValueError):
                raise EngineContractError("phon_rl.training.progress") from None
            step_callback(step, mean_reward, policy_loss)

        prompt_fn: Callable[[Any], str] | None = None
        prompt_list: list[str] | None = None
        if dynamic_strategy_id is not None:
            if dynamic_strategy_id not in _SUPPORTED_DYNAMIC_PROMPT_STRATEGIES:
                raise InvalidRequestError("phon_rl.prompt_strategy.unsupported")

            def missing_units_prompt(inventory: Any) -> str:
                units = inventory.next_targets(8)
                if not units:
                    return "Write one short, natural sentence."
                rendered = ", ".join(str(unit) for unit in units)
                return f"Write one short, natural sentence containing these sounds: {rendered}."

            prompt_fn = missing_units_prompt
        else:
            if prompts is None:
                raise EngineContractError("phon_rl.prompt_source")
            prompt_list = list(prompts)

        with _offline_transformers_environment(), _suppress_dependency_logging():
            result = trainer.train(
                prompts=prompt_list,
                prompt_fn=prompt_fn,
                step_callback=on_step,
            )
        if (
            not trainer.is_initialized
            or trainer.reward is not reward
            or trainer.config is not config
        ):
            raise EngineContractError("phon_rl.training.state")
        return BindingTrainingResult(
            mean_rewards=tuple(float(value) for value in result.mean_rewards),
            total_steps=int(result.total_steps),
            final_coverage=float(result.final_coverage),
        )


class _PieceTokenizer:
    def __init__(self, pieces: tuple[PhonRlTokenPiece, ...]) -> None:
        self._pieces = {item.token_id: item for item in pieces}

    def decode(self, token_id: int, *, skip_special_tokens: bool) -> str:
        del skip_special_tokens
        return self._pieces[token_id].decoded_text

    def convert_ids_to_tokens(self, token_id: int) -> str:
        return self._pieces[token_id].raw_token


class CorpusgenPhonRlAdapter:
    """One adapter with model-free lab calls and a separately invoked worker path."""

    def __init__(
        self,
        *,
        snapshot_resolver: PhonRlSnapshotResolver | None = None,
        prompt_reader: PhonRlPromptArtifactReader | None = None,
        training_bindings: PhonRlTrainingBindings | None = None,
    ) -> None:
        self._snapshot_resolver = snapshot_resolver
        self._prompt_reader = prompt_reader or MissingPromptArtifactReader()
        self._training_bindings = training_bindings or CorpusgenPhonRlTrainingBindings()

    def sentence_reward(
        self,
        request: PhonRlSentenceRewardRequest,
        *,
        commit: bool,
    ) -> PhonRlSentenceRewardResult:
        reward = _reward(request)
        if commit:
            raw = reward.commit_sentence_reward(
                list(request.phonemes),
                text=request.text,
                sentence_index=request.state.revision,
            )
            state = PhonRlRewardState(
                target_phonemes=request.state.target_phonemes,
                unit=request.state.unit,
                committed=(
                    *request.state.committed,
                    PhonRlPhonemeSequence(
                        source_id=request.source_id,
                        phonemes=request.phonemes,
                    ),
                ),
                revision=request.state.revision + 1,
            )
        else:
            raw = reward.sentence_reward(list(request.phonemes), text=request.text)
            state = request.state
        return PhonRlSentenceRewardResult(
            breakdown=_normalize_breakdown(raw, request),
            committed=commit,
            state=state,
        )

    def token_rewards(self, request: PhonRlTokenRewardRequest) -> PhonRlTokenRewardResult:
        reward = _token_reward(request)
        raw = reward.token_rewards(
            token_ids=[item.token_id for item in request.pieces],
            tokenizer=_PieceTokenizer(request.pieces),
        )
        return _normalize_token_result(request.pieces, raw)

    def hierarchical_reward(
        self,
        request: PhonRlHierarchicalRewardRequest,
    ) -> PhonRlHierarchicalRewardResult:
        reward = _reward(request.sentence)
        tokenizer = _PieceTokenizer(request.pieces)
        raw_sentence, raw_tokens = reward.hierarchical_reward(
            text=request.sentence.text or "",
            phonemes=list(request.sentence.phonemes),
            token_ids=[item.token_id for item in request.pieces],
            tokenizer=tokenizer,
        )
        return PhonRlHierarchicalRewardResult(
            sentence=_normalize_breakdown(raw_sentence, request.sentence),
            tokens=_normalize_token_result(request.pieces, raw_tokens),
            state_revision=request.sentence.state.revision,
        )

    def log_probs(self, request: PhonRlLogProbRequest) -> PhonRlMatrixResult:
        torch = _torch()
        from corpusgen.generate.phon_rl.trainer import compute_log_probs_from_logits

        logits = torch.tensor(request.logits, dtype=torch.float64, device="cpu")
        actions = torch.tensor(request.actions.values, dtype=torch.long, device="cpu")
        values = compute_log_probs_from_logits(logits, actions)
        return PhonRlMatrixResult(values=_float_matrix(values))

    def kl_penalty(self, request: PhonRlKlRequest) -> PhonRlMatrixResult:
        torch = _torch()
        from corpusgen.generate.phon_rl.trainer import compute_kl_penalty

        policy = torch.tensor(request.policy_log_probs.values, dtype=torch.float64, device="cpu")
        reference = torch.tensor(
            request.reference_log_probs.values,
            dtype=torch.float64,
            device="cpu",
        )
        values = compute_kl_penalty(policy, reference)
        return PhonRlMatrixResult(values=_float_matrix(values))

    def gae(self, request: PhonRlGaeRequest) -> PhonRlGaeResult:
        torch = _torch()
        from corpusgen.generate.phon_rl.trainer import compute_gae

        rewards = torch.tensor(request.rewards.values, dtype=torch.float64, device="cpu")
        values = torch.tensor(request.values.values, dtype=torch.float64, device="cpu")
        mask = (
            None
            if request.mask is None
            else torch.tensor(request.mask.values, dtype=torch.bool, device="cpu")
        )
        advantages, returns = compute_gae(
            rewards,
            values,
            gamma=request.gamma,
            lam=request.lambda_,
            mask=mask,
        )
        return PhonRlGaeResult(
            advantages=_float_matrix(advantages),
            returns=_float_matrix(returns),
        )

    def clip_loss(self, request: PhonRlClipLossRequest) -> PhonRlScalarResult:
        torch = _torch()
        from corpusgen.generate.phon_rl.trainer import ppo_clip_loss

        advantages = torch.tensor(request.advantages.values, dtype=torch.float64, device="cpu")
        old_log_probs = torch.tensor(
            request.old_log_probs.values,
            dtype=torch.float64,
            device="cpu",
        )
        new_log_probs = torch.tensor(
            request.new_log_probs.values,
            dtype=torch.float64,
            device="cpu",
        )
        mask = (
            None
            if request.mask is None
            else torch.tensor(request.mask.values, dtype=torch.bool, device="cpu")
        )
        result = ppo_clip_loss(
            advantages,
            old_log_probs,
            new_log_probs,
            clip_epsilon=request.clip_epsilon,
            mask=mask,
        )
        return PhonRlScalarResult(value=float(result.detach().cpu().item()))

    def value_head(self, request: PhonRlValueHeadRequest) -> PhonRlValueHeadResult:
        torch = _torch()
        from corpusgen.generate.phon_rl.value_head import ValueHead

        with _VALUE_HEAD_RNG_LOCK, torch.random.fork_rng(devices=[]):
            torch.manual_seed(request.seed)
            if request.hidden_states_2d is not None:
                raw_values: object = request.hidden_states_2d.values
                hidden_size = len(request.hidden_states_2d.values[0])
                rank: Literal[1, 2] = 1
            else:
                assert request.hidden_states_3d is not None
                raw_values = request.hidden_states_3d
                hidden_size = len(request.hidden_states_3d[0][0])
                rank = 2
            head = ValueHead(hidden_size=hidden_size, dropout=request.dropout)
            if head.hidden_size != hidden_size or head.dropout_rate != request.dropout:
                raise EngineContractError("phon_rl.ppo.value_head.properties")
            head.eval()
            with torch.no_grad():
                values = head(torch.tensor(raw_values, dtype=torch.float32, device="cpu"))
        normalized: tuple[float, ...] | tuple[tuple[float, ...], ...]
        if rank == 1:
            normalized = tuple(float(item) for item in values.detach().cpu().tolist())
        else:
            normalized = _float_matrix(values)
        return PhonRlValueHeadResult(
            hidden_size=hidden_size,
            dropout=request.dropout,
            rank=rank,
            values=normalized,
        )

    def train(
        self,
        request: PhonRlTrainingRequest,
        policy: PhonRlRuntimePolicyEntry,
        *,
        emit: Callable[[PhonRlProgressPoint], None] | None = None,
        prompt_reader: PhonRlPromptArtifactReader | None = None,
    ) -> PhonRlTrainingResult:
        if self._snapshot_resolver is None:
            raise EngineUnavailableError("phon_rl.snapshot.resolver")
        location = self._snapshot_resolver.resolve(
            policy.model,
            cache_root_id=policy.cache_root_id,
        )
        snapshot = _verify_snapshot(location, policy.model)
        prompts, strategy_id = self._prompt_source(
            request,
            prompt_reader=(prompt_reader if prompt_reader is not None else self._prompt_reader),
        )
        progress: list[PhonRlProgressPoint] = []

        def capture(step: int, mean_reward: float, policy_loss: float) -> None:
            if len(progress) >= _MAX_PROGRESS_POINTS or step != len(progress):
                raise EngineContractError("phon_rl.training.progress")
            point = PhonRlProgressPoint(
                step=step,
                mean_reward=mean_reward,
                policy_loss=policy_loss,
            )
            progress.append(point)
            if emit is not None:
                emit(point)

        with tempfile.TemporaryDirectory(prefix="corpuskit-phon-rl-") as temporary:
            output_dir = Path(temporary) / "checkpoint"
            raw = self._training_bindings.train(
                snapshot=snapshot,
                request=request,
                prompts=prompts,
                dynamic_strategy_id=strategy_id,
                output_dir=output_dir,
                step_callback=capture,
            )
            compatibility = _compatibility(policy, request.parameters.use_peft)
            checkpoint = _checkpoint_bundle(output_dir, compatibility)

        if (
            raw.total_steps != request.parameters.num_steps
            or len(raw.mean_rewards) != raw.total_steps
            or len(progress) != raw.total_steps
        ):
            raise EngineContractError("phon_rl.training.result_steps")
        _assert_strategy_contract(request.parameters.use_peft)
        manifest = PhonRlTrainingManifest(
            runtime_id=request.runtime_id,
            model=policy.model,
            tokenizer=policy.tokenizer,
            language=request.language,
            unit=request.unit,
            target_sha256=target_sha256(request.target_phonemes, request.unit),
            prompt_source_kind=request.prompt_source.kind,
            prompt_source_sha256=(
                request.prompt_source.content_sha256
                if isinstance(request.prompt_source, PhonRlStaticPromptSource)
                else prompt_source_sha256(request.prompt_source)
            ),
            parameters=request.parameters,
            corpusgen_version=_version("corpusgen"),
            torch_version=_version("torch"),
            transformers_version=_version("transformers"),
            peft_version=_version("peft") if request.parameters.use_peft else None,
        )
        return PhonRlTrainingResult(
            manifest=manifest,
            progress=tuple(progress),
            mean_rewards=raw.mean_rewards,
            total_steps=raw.total_steps,
            final_coverage=raw.final_coverage,
            checkpoint=checkpoint,
            peft_inference_status=(
                "application_loader_ready" if request.parameters.use_peft else "not_requested"
            ),
        )

    def _prompt_source(
        self,
        request: PhonRlTrainingRequest,
        *,
        prompt_reader: PhonRlPromptArtifactReader,
    ) -> tuple[tuple[str, ...] | None, str | None]:
        source = request.prompt_source
        if isinstance(source, PhonRlStaticPromptSource):
            prompts = prompt_reader.read(source)
            if len(prompts) != source.prompt_count:
                raise EngineContractError("phon_rl.prompt_artifact.count")
            if any(not prompt.strip() or len(prompt) > 4_000 for prompt in prompts):
                raise EngineContractError("phon_rl.prompt_artifact.content")
            digest = PhonRlPromptArtifact(prompts=prompts).sha256
            if digest != source.content_sha256:
                raise EngineContractError("phon_rl.prompt_artifact.digest")
            return prompts, None
        if source.strategy_id not in _SUPPORTED_DYNAMIC_PROMPT_STRATEGIES:
            raise InvalidRequestError("phon_rl.prompt_strategy.unsupported")
        return None, source.strategy_id


def _targets(state: PhonRlRewardState) -> Any:
    from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory

    targets = PhoneticTargetInventory(
        target_phonemes=list(state.target_phonemes),
        unit=state.unit.value,
    )
    for sentence_index, sequence in enumerate(state.committed):
        targets.update(list(sequence.phonemes), sentence_index)
    return targets


def _reward(request: PhonRlSentenceRewardRequest) -> Any:
    from corpusgen.generate.phon_rl.reward import PhoneticReward

    def phonotactic(_: list[str]) -> float:
        assert request.scores.phonotactic is not None
        return request.scores.phonotactic

    def fluency(_: str | None) -> float:
        assert request.scores.fluency is not None
        return request.scores.fluency

    def reference(_: str) -> float:
        assert request.scores.reference_log_probability is not None
        return request.scores.reference_log_probability

    return PhoneticReward(
        _targets(request.state),
        phonotactic_scorer=phonotactic if request.scores.phonotactic is not None else None,
        fluency_scorer=fluency if request.scores.fluency is not None else None,
        ref_log_probs_fn=(
            reference if request.scores.reference_log_probability is not None else None
        ),
        coverage_weight=request.weights.coverage,
        phonotactic_weight=request.weights.phonotactic,
        fluency_weight=request.weights.fluency,
        language=request.language,
    )


def _token_reward(request: PhonRlTokenRewardRequest) -> Any:
    from corpusgen.generate.phon_rl.reward import PhoneticReward

    return PhoneticReward(_targets(request.state), language=request.language)


def _normalize_breakdown(raw: Any, request: PhonRlSentenceRewardRequest) -> PhonRlRewardBreakdown:
    if request.scores.fluency is not None:
        signal: Literal["explicit", "reference_log_probability", "none"] = "explicit"
    elif request.scores.reference_log_probability is not None and request.text is not None:
        signal = "reference_log_probability"
    else:
        signal = "none"
    target_size = _targets(request.state).target_size
    return PhonRlRewardBreakdown(
        coverage_reward=float(raw.coverage_reward),
        phonotactic_reward=float(raw.phonotactic_reward),
        fluency_reward=float(raw.fluency_reward),
        composite_reward=float(raw.composite_reward),
        new_units=tuple(sorted(str(item) for item in raw.new_units)),
        coverage_gain=int(raw.coverage_gain),
        target_size=target_size,
        fluency_signal=signal,
    )


def _normalize_token_result(
    pieces: tuple[PhonRlTokenPiece, ...],
    raw: Any,
) -> PhonRlTokenRewardResult:
    return PhonRlTokenRewardResult(
        token_ids=tuple(item.token_id for item in pieces),
        per_token_rewards=tuple(float(value) for value in raw.per_token_rewards),
        word_boundaries=tuple(int(value) for value in raw.word_boundaries),
        words_phonemized=tuple(str(value) for value in raw.words_phonemized),
    )


def _torch() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError:
        raise DependencyUnavailableError("phon_rl.ppo.torch") from None


def _float_matrix(tensor: Any) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(float(value) for value in row) for row in tensor.detach().cpu().tolist())


def _verify_snapshot(
    location: PhonRlSnapshotLocation,
    pin: PhonRlSnapshotPin,
) -> Path:
    digest = compute_snapshot_digest(
        location.snapshot,
        approved_cache_root=location.approved_cache_root,
    )
    if digest != pin.snapshot_sha256:
        raise EngineUnavailableError("phon_rl.snapshot.digest")
    try:
        snapshot = location.snapshot.resolve(strict=True)
        root = location.approved_cache_root.resolve(strict=True)
        if not snapshot.is_relative_to(root):
            raise EngineUnavailableError("phon_rl.snapshot.boundary")
        files = tuple(item for item in snapshot.rglob("*") if item.is_file())
        if any(item.suffix.casefold() in _EXECUTABLE_SUFFIXES for item in files):
            raise EngineUnavailableError("phon_rl.snapshot.executable_content")
        for file in files:
            if file.name in {"config.json", "tokenizer_config.json"}:
                content = json.loads(file.read_text(encoding="utf-8"))
                if isinstance(content, dict) and content.get("auto_map"):
                    raise EngineUnavailableError("phon_rl.snapshot.remote_code")
        return snapshot
    except ApplicationError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise EngineUnavailableError("phon_rl.snapshot.contract") from None


def _checkpoint_bundle(
    output_dir: Path,
    compatibility: PhonRlCheckpointCompatibility,
) -> PhonRlCheckpointBundle:
    try:
        root = output_dir.resolve(strict=True)
        discovered_files = tuple(sorted(item for item in root.rglob("*") if item.is_file()))
        if any(
            item.suffix.casefold() in {".bin", ".pkl", ".pickle", ".pt", ".pth"}
            for item in discovered_files
        ):
            raise EngineContractError("phon_rl.checkpoint.contract")
        raw_files = tuple(
            sorted(
                item
                for item in discovered_files
                if _checkpoint_file_permitted(item.relative_to(root), compatibility.peft_adapter)
            )
        )
        if not raw_files:
            raise EngineContractError("phon_rl.checkpoint.empty")
        files: list[PhonRlCheckpointFile] = []
        total = 0
        for file in raw_files:
            resolved = file.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise EngineContractError("phon_rl.checkpoint.boundary")
            content = _checkpoint_content(
                file.relative_to(root).as_posix(),
                resolved.read_bytes(),
                compatibility,
            )
            total += len(content)
            if total > MAX_RL_CHECKPOINT_BYTES:
                raise EngineContractError("phon_rl.checkpoint.size")
            files.append(
                PhonRlCheckpointFile(
                    path=file.relative_to(root).as_posix(),
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    content_base64=base64.b64encode(content).decode("ascii"),
                )
            )
        return PhonRlCheckpointBundle.create(
            compatibility=compatibility,
            files=tuple(files),
        )
    except ApplicationError:
        raise
    except (OSError, ValueError):
        raise EngineContractError("phon_rl.checkpoint.contract") from None


def _checkpoint_file_permitted(path: Path, peft_adapter: bool) -> bool:
    relative = path.as_posix()
    if peft_adapter:
        return relative in {"adapter_config.json", "adapter_model.safetensors"}
    return relative in {"config.json", "generation_config.json", "model.safetensors"} or (
        relative.startswith("model-") and relative.endswith(".safetensors")
    )


def _checkpoint_content(
    relative: str,
    content: bytes,
    compatibility: PhonRlCheckpointCompatibility,
) -> bytes:
    if relative != "adapter_config.json":
        return content
    try:
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError
        value["base_model_name_or_path"] = compatibility.base_model_id
        value["revision"] = compatibility.base_model_revision
        value["auto_mapping"] = None
        return _canonical_json(value)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise EngineContractError("phon_rl.checkpoint.adapter_config") from None


def _compatibility(
    policy: PhonRlRuntimePolicyEntry,
    use_peft: bool,
) -> PhonRlCheckpointCompatibility:
    return PhonRlCheckpointCompatibility(
        base_model_id=policy.model.repository_id,
        base_model_revision=policy.model.revision,
        base_model_snapshot_sha256=policy.model.snapshot_sha256,
        tokenizer_id=policy.tokenizer.repository_id,
        tokenizer_revision=policy.tokenizer.revision,
        tokenizer_snapshot_sha256=policy.tokenizer.snapshot_sha256,
        corpusgen_version=_version("corpusgen"),
        torch_version=_version("torch"),
        transformers_version=_version("transformers"),
        peft_version=_version("peft") if use_peft else None,
        peft_adapter=use_peft,
    )


def validate_checkpoint_compatibility(
    checkpoint: PhonRlCheckpointBundle,
    policy: PhonRlRuntimePolicyEntry,
    *,
    require_peft: bool,
) -> None:
    """Fail closed before a worker composes checkpoint bytes with an allowlisted base."""

    compatibility = checkpoint.compatibility
    expected_peft_version = _version("peft") if require_peft else None
    if (
        compatibility.base_model_id != policy.model.repository_id
        or compatibility.base_model_revision != policy.model.revision
        or compatibility.base_model_snapshot_sha256 != policy.model.snapshot_sha256
        or compatibility.tokenizer_id != policy.tokenizer.repository_id
        or compatibility.tokenizer_revision != policy.tokenizer.revision
        or compatibility.tokenizer_snapshot_sha256 != policy.tokenizer.snapshot_sha256
        or compatibility.peft_adapter != require_peft
        or compatibility.corpusgen_version != _version("corpusgen")
        or compatibility.torch_version != _version("torch")
        or compatibility.transformers_version != _version("transformers")
        or compatibility.peft_version != expected_peft_version
    ):
        raise InvalidRequestError("phon_rl.checkpoint.compatibility")


def require_peft_generation_support(checkpoint: PhonRlCheckpointBundle) -> None:
    """Compatibility shim: neither base nor PEFT checkpoints are blocked at inference."""

    del checkpoint


def _assert_strategy_contract(use_peft: bool) -> None:
    from corpusgen.generate.phon_rl.policy import PhonRLStrategy

    strategy = PhonRLStrategy()
    marker = object()
    if strategy.name != "phon_rl" or strategy.modify_logits(object(), marker) is not marker:
        raise EngineContractError("phon_rl.strategy.identity")
    # The trainer and application-owned inference loader share this public strategy contract.
    # PEFT loading itself is intentionally outside CorpusGen's buggy return-assignment path.
    del use_peft


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        raise DependencyUnavailableError("phon_rl.runtime.version") from None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@contextmanager
def _offline_transformers_environment() -> Any:
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "1"
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _suppress_dependency_logging() -> Any:
    """Prevent local paths or generated text from reaching third-party log handlers."""

    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        yield
    finally:
        logging.disable(previous)


__all__ = [
    "BindingTrainingResult",
    "CorpusgenPhonRlAdapter",
    "CorpusgenPhonRlTrainingBindings",
    "MissingPromptArtifactReader",
    "OfflinePhonRlSnapshotResolver",
    "PhonRlPromptArtifactReader",
    "PhonRlSnapshotLocation",
    "PhonRlSnapshotResolver",
    "PhonRlTrainingBindings",
    "require_peft_generation_support",
    "validate_checkpoint_compatibility",
]
