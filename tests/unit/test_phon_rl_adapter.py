from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import logging
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

try:
    import torch as torch_runtime
except ImportError:  # pragma: no cover - exercised by the explicit dependency test below
    torch_runtime = None

from corpuskit.adapters.corpusgen.client import CorpusgenAdapter
from corpuskit.adapters.corpusgen.model_runtime import compute_snapshot_digest
from corpuskit.adapters.corpusgen.phon_rl import (
    BindingTrainingResult,
    CorpusgenPhonRlAdapter,
    CorpusgenPhonRlTrainingBindings,
    MissingPromptArtifactReader,
    OfflinePhonRlSnapshotResolver,
    PhonRlSnapshotLocation,
    require_peft_generation_support,
    validate_checkpoint_compatibility,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.phon_rl import (
    PhonRlBoolMatrix,
    PhonRlCheckpointBundle,
    PhonRlCheckpointCompatibility,
    PhonRlCheckpointFile,
    PhonRlClipLossRequest,
    PhonRlDynamicPromptSource,
    PhonRlExternalScores,
    PhonRlFloatMatrix,
    PhonRlGaeRequest,
    PhonRlHiddenMatrix,
    PhonRlIntMatrix,
    PhonRlKlRequest,
    PhonRlLogProbRequest,
    PhonRlProgressPoint,
    PhonRlPromptArtifact,
    PhonRlRewardState,
    PhonRlRewardWeights,
    PhonRlRuntimePolicyEntry,
    PhonRlSentenceRewardRequest,
    PhonRlSnapshotPin,
    PhonRlStaticPromptSource,
    PhonRlTokenPiece,
    PhonRlTokenRewardRequest,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
    PhonRlUnit,
    PhonRlValueHeadRequest,
    PhonRlWorkerProfile,
)
from corpuskit.services.phon_rl import PhonRlLabService, PhonRlTrainingCoordinator
from corpuskit.services.phon_rl import PhonRlRuntimePolicy as RuntimePolicy

REVISION = "a" * 40


class StaticSnapshotResolver:
    def __init__(self, location: PhonRlSnapshotLocation) -> None:
        self.location = location
        self.calls: list[tuple[PhonRlSnapshotPin, str]] = []

    def resolve(
        self,
        pin: PhonRlSnapshotPin,
        *,
        cache_root_id: str,
    ) -> PhonRlSnapshotLocation:
        self.calls.append((pin, cache_root_id))
        return self.location


class StaticPromptReader:
    def __init__(self, prompts: tuple[str, ...]) -> None:
        self.prompts = prompts
        self.calls: list[PhonRlStaticPromptSource] = []

    def read(self, source: PhonRlStaticPromptSource) -> tuple[str, ...]:
        self.calls.append(source)
        return self.prompts


class FakeTrainingBindings:
    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.files: dict[str, bytes] = {"model.safetensors": b"safe weights", "config.json": b"{}"}
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "snapshot": snapshot,
                "request": request,
                "prompts": prompts,
                "dynamic_strategy_id": dynamic_strategy_id,
            }
        )
        if self.fail is not None:
            raise self.fail
        output_dir.mkdir(parents=True)
        for relative, content in self.files.items():
            path = output_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        rewards: list[float] = []
        for step in range(request.parameters.num_steps):
            reward = (step + 1) / request.parameters.num_steps
            rewards.append(reward)
            step_callback(step, reward, -0.1 * (step + 1))
        return BindingTrainingResult(
            mean_rewards=tuple(rewards),
            total_steps=request.parameters.num_steps,
            final_coverage=0.75,
        )


def snapshot(tmp_path: Path, **extra: bytes) -> tuple[PhonRlSnapshotLocation, PhonRlSnapshotPin]:
    root = tmp_path / "models--acme--tiny-rl"
    value = root / "snapshots" / REVISION
    value.mkdir(parents=True)
    (value / "model.safetensors").write_bytes(b"safe weights")
    (value / "config.json").write_text("{}", encoding="utf-8")
    (value / "tokenizer.json").write_text("{}", encoding="utf-8")
    for relative, content in extra.items():
        path = value / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    digest = compute_snapshot_digest(value, approved_cache_root=root)
    return (
        PhonRlSnapshotLocation(snapshot=value, approved_cache_root=root),
        PhonRlSnapshotPin(
            repository_id="acme/tiny-rl",
            revision=REVISION,
            snapshot_sha256=digest,
        ),
    )


def policy(pin: PhonRlSnapshotPin, **changes: Any) -> PhonRlRuntimePolicyEntry:
    values: dict[str, Any] = {
        "runtime_id": "tiny-rl-v1",
        "model": pin,
        "tokenizer": pin,
        "cache_root_id": "models-ro",
        "cache_mount_read_only": True,
        "allow_peft": True,
        "allowed_peft_ranks": (8,),
        "allowed_peft_alphas": (16,),
        "allowed_prompt_strategies": ("missing-units-v1",),
    }
    values.update(changes)
    return PhonRlRuntimePolicyEntry(**values)


def dynamic_request(*, peft: bool = False, steps: int = 2) -> PhonRlTrainingRequest:
    return PhonRlTrainingRequest(
        runtime_id="tiny-rl-v1",
        target_phonemes=("a", "b"),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="missing-units-v1"),
        parameters=PhonRlTrainingParameters(
            seed=42,
            num_steps=steps,
            batch_size=2,
            use_peft=peft,
            peft_rank=8,
            peft_alpha=16,
        ),
    )


def reward_request(unit: PhonRlUnit, **changes: Any) -> PhonRlSentenceRewardRequest:
    values: dict[str, Any] = {
        "state": PhonRlRewardState(target_phonemes=("a", "b", "c"), unit=unit),
        "source_id": "sentence:1",
        "phonemes": ("a", "b", "c"),
        "text": "abc",
    }
    values.update(changes)
    return PhonRlSentenceRewardRequest(**values)


@pytest.mark.parametrize(
    ("unit", "gain", "target_size"),
    [
        pytest.param(PhonRlUnit.PHONEME, 3, 3, id="phoneme"),
        pytest.param(PhonRlUnit.DIPHONE, 2, 9, id="diphone"),
        pytest.param(PhonRlUnit.TRIPHONE, 1, 27, id="triphone"),
    ],
)
def test_sentence_reward_peek_commit_normalization_and_atomic_state(
    unit: PhonRlUnit,
    gain: int,
    target_size: int,
) -> None:
    adapter = CorpusgenPhonRlAdapter()
    service = PhonRlLabService(adapter)
    request = reward_request(unit)
    peek = service.peek(request)
    commit = service.commit(request)
    assert peek.committed is False
    assert peek.state is request.state
    assert peek.breakdown.coverage_gain == gain
    assert peek.breakdown.target_size == target_size
    assert peek.breakdown.coverage_reward == pytest.approx(gain / target_size)
    assert commit.breakdown == peek.breakdown
    assert commit.state.revision == 1
    assert commit.state.committed[0].source_id == "sentence:1"
    assert request.state.revision == 0


def test_reward_components_fluency_precedence_and_reference_fallback() -> None:
    adapter = CorpusgenPhonRlAdapter()
    weighted = PhonRlRewardWeights(coverage=2.0, phonotactic=3.0, fluency=4.0)
    explicit = adapter.sentence_reward(
        reward_request(
            PhonRlUnit.PHONEME,
            weights=weighted,
            scores=PhonRlExternalScores(
                phonotactic=0.25,
                fluency=0.5,
                reference_log_probability=-10.0,
            ),
        ),
        commit=False,
    )
    assert explicit.breakdown.phonotactic_reward == 0.25
    assert explicit.breakdown.fluency_reward == 0.5
    assert explicit.breakdown.composite_reward == pytest.approx(2 + 0.75 + 2)
    assert explicit.breakdown.fluency_signal == "explicit"

    reference = adapter.sentence_reward(
        reward_request(
            PhonRlUnit.PHONEME,
            weights=PhonRlRewardWeights(fluency=1.0),
            scores=PhonRlExternalScores(reference_log_probability=-2.5),
        ),
        commit=False,
    )
    assert reference.breakdown.fluency_reward == -2.5
    assert reference.breakdown.fluency_signal == "reference_log_probability"


def _word_target(*words: str) -> tuple[str, ...]:
    client = CorpusgenAdapter()
    phonemes = {
        phoneme for word in words for phoneme in client.phonemize(word, language="en-us").phonemes
    }
    return tuple(sorted(phonemes))


@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param(
            (
                PhonRlTokenPiece(token_id=1, decoded_text="hello ", raw_token="hello "),
                PhonRlTokenPiece(token_id=2, decoded_text="world", raw_token="world"),
            ),
            id="whitespace",
        ),
        pytest.param(
            (
                PhonRlTokenPiece(token_id=1, decoded_text="hello", raw_token="hello"),
                PhonRlTokenPiece(token_id=2, decoded_text=" world", raw_token="Ġworld"),
            ),
            id="gpt",
        ),
        pytest.param(
            (
                PhonRlTokenPiece(token_id=1, decoded_text="hello", raw_token="▁hello"),
                PhonRlTokenPiece(token_id=2, decoded_text="world", raw_token="▁world"),
            ),
            id="sentencepiece",
        ),
    ],
)
def test_real_espeak_token_reward_boundaries(pieces: tuple[PhonRlTokenPiece, ...]) -> None:
    target = _word_target("hello", "world")
    request = PhonRlTokenRewardRequest(
        state=PhonRlRewardState(target_phonemes=target),
        pieces=pieces,
    )
    result = PhonRlLabService(CorpusgenPhonRlAdapter()).token_rewards(request)
    assert result.word_boundaries == (0, 1)
    assert result.words_phonemized == ("hello", "world")
    assert sum(result.per_token_rewards) <= 1.0
    assert sum(result.per_token_rewards) > 0.0


def test_token_missing_units_rewarded_exactly_once_and_hierarchical_is_peek() -> None:
    target = _word_target("hello")
    pieces = (
        PhonRlTokenPiece(token_id=1, decoded_text="hello ", raw_token="hello "),
        PhonRlTokenPiece(token_id=2, decoded_text="hello", raw_token="hello"),
    )
    adapter = CorpusgenPhonRlAdapter()
    token_request = PhonRlTokenRewardRequest(
        state=PhonRlRewardState(target_phonemes=target),
        pieces=pieces,
    )
    tokens = adapter.token_rewards(token_request)
    assert tokens.per_token_rewards[0] == pytest.approx(1.0)
    assert tokens.per_token_rewards[1] == 0.0

    sentence = PhonRlSentenceRewardRequest(
        state=token_request.state,
        source_id="hierarchy:1",
        phonemes=CorpusgenAdapter().phonemize("hello", language="en-us").phonemes,
        text="hello hello",
    )
    from corpuskit.domain.phon_rl import PhonRlHierarchicalRewardRequest

    result = PhonRlLabService(adapter).hierarchical(
        PhonRlHierarchicalRewardRequest(sentence=sentence, pieces=pieces)
    )
    assert result.sentence.coverage_reward == 1.0
    assert result.tokens.per_token_rewards == tokens.per_token_rewards
    assert result.state_revision == 0
    assert sentence.state.revision == 0


def test_token_g2p_failure_is_sanitized_by_service() -> None:
    service = PhonRlLabService(CorpusgenPhonRlAdapter())
    request = PhonRlTokenRewardRequest(
        state=PhonRlRewardState(target_phonemes=("a",)),
        language="zz",
        pieces=(PhonRlTokenPiece(token_id=1, decoded_text="secret", raw_token="secret"),),
    )
    with pytest.raises(EngineUnavailableError) as error:
        service.token_rewards(request)
    assert error.value.operation == "phon_rl.reward.tokens"
    assert "secret" not in str(error.value)


def test_ppo_public_primitives_match_hand_computed_goldens_and_masks() -> None:
    if torch_runtime is None:
        pytest.skip("the locked local-model extra is not installed")
    adapter = CorpusgenPhonRlAdapter()
    service = PhonRlLabService(adapter)
    log_probs = service.log_probs(
        PhonRlLogProbRequest(
            logits=(((1.0, 2.0), (3.0, 1.0)),),
            actions=PhonRlIntMatrix(values=((1, 0),)),
        )
    )
    assert log_probs.values[0] == pytest.approx((-0.3132616875, -0.1269280110))

    kl = service.kl_penalty(
        PhonRlKlRequest(
            policy_log_probs=PhonRlFloatMatrix(values=((0.0, math.log(2.0)),)),
            reference_log_probs=PhonRlFloatMatrix(values=((0.0, 0.0),)),
        )
    )
    assert kl.values[0] == pytest.approx((0.0, 1.0 - math.log(2.0)))

    gae = service.gae(
        PhonRlGaeRequest(
            rewards=PhonRlFloatMatrix(values=((0.0, 1.0, 99.0), (2.0, 99.0, 99.0))),
            values=PhonRlFloatMatrix(values=((0.0, 0.0, 8.0), (0.0, 8.0, 8.0))),
            gamma=1.0,
            **{"lambda": 1.0},
            mask=PhonRlBoolMatrix(values=((True, True, False), (True, False, False))),
        )
    )
    assert gae.advantages[0] == pytest.approx((1.0, 1.0, 0.0))
    assert gae.advantages[1] == pytest.approx((2.0, 0.0, 0.0))
    assert gae.returns[0] == pytest.approx((1.0, 1.0, 0.0))
    assert gae.returns[1] == pytest.approx((2.0, 0.0, 0.0))

    clip = service.clip_loss(
        PhonRlClipLossRequest(
            advantages=PhonRlFloatMatrix(values=((1.0, -1.0),)),
            old_log_probs=PhonRlFloatMatrix(values=((0.0, 0.0),)),
            new_log_probs=PhonRlFloatMatrix(values=((math.log(1.5), math.log(0.5)),)),
            clip_epsilon=0.2,
        )
    )
    assert clip.value == pytest.approx(-0.2)
    masked = service.clip_loss(
        PhonRlClipLossRequest(
            advantages=PhonRlFloatMatrix(values=((1.0, -1.0),)),
            old_log_probs=PhonRlFloatMatrix(values=((0.0, 0.0),)),
            new_log_probs=PhonRlFloatMatrix(values=((math.log(1.5), math.log(0.5)),)),
            clip_epsilon=0.2,
            mask=PhonRlBoolMatrix(values=((True, False),)),
        )
    )
    assert masked.value == pytest.approx(-1.2)


def test_value_head_real_torch_is_seeded_cpu_and_supports_both_ranks() -> None:
    if torch_runtime is None:
        pytest.skip("the locked local-model extra is not installed")
    adapter = CorpusgenPhonRlAdapter()
    service = PhonRlLabService(adapter)
    request_2d = PhonRlValueHeadRequest(
        hidden_states_2d=PhonRlHiddenMatrix(values=((1.0, 2.0), (3.0, 4.0))),
        seed=123,
    )
    first = service.value_head(request_2d)
    second = service.value_head(request_2d)
    assert first == second
    assert first.rank == 1
    assert len(first.values) == 2

    three = service.value_head(
        PhonRlValueHeadRequest(
            hidden_states_3d=(((1.0, 2.0), (3.0, 4.0)),),
            dropout=0.5,
            seed=5,
        )
    )
    assert three.rank == 2
    assert len(three.values) == 1
    assert len(three.values[0]) == 2  # type: ignore[index]

    assert torch_runtime is not None
    torch_runtime.manual_seed(999)
    before = torch_runtime.get_rng_state().clone()
    service.value_head(request_2d)
    after = torch_runtime.get_rng_state()
    assert torch_runtime.equal(before, after)


def test_real_upstream_trainer_handles_variable_eos_pad_and_atomic_batch_g2p(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if torch_runtime is None:
        pytest.skip("the locked local-model extra is not installed")

    trainer_module = importlib.import_module("corpusgen.generate.phon_rl.trainer")
    reward_module = importlib.import_module("corpusgen.generate.phon_rl.reward")
    targets_module = importlib.import_module("corpusgen.generate.phon_ctg.targets")
    g2p_module = importlib.import_module("corpusgen.g2p.manager")

    class TinyTokenizer:
        eos_token_id = 2
        pad_token_id = 0
        eos_token = "<eos>"
        pad_token = "<pad>"

        @staticmethod
        def encode(prompt: str) -> list[int]:
            assert prompt == "safe prompt"
            return [1]

        @staticmethod
        def decode(token_ids: Any, *, skip_special_tokens: bool) -> str:
            assert skip_special_tokens is True
            values = tuple(int(value) for value in token_ids.tolist())
            if values == (3, 2):
                return "alpha"
            if values == (4, 5):
                return "beta"
            raise AssertionError(f"response masking produced unexpected IDs: {values!r}")

        @staticmethod
        def save_pretrained(path: str) -> None:
            destination = Path(path)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "tokenizer.json").write_text("{}", encoding="utf-8")

    class TinyPolicy(torch_runtime.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch_runtime.nn.Embedding(8, 4)
            self.output = torch_runtime.nn.Linear(4, 8)
            self.config = SimpleNamespace(hidden_size=4)

        def generate(self, input_ids: Any, **kwargs: object) -> Any:
            assert kwargs["max_new_tokens"] == 3
            responses = torch_runtime.tensor(
                ((3, 2, 2), (4, 5, 0)),
                dtype=torch_runtime.long,
                device=input_ids.device,
            )
            return torch_runtime.cat((input_ids, responses), dim=1)

        def forward(self, input_ids: Any, *, output_hidden_states: bool) -> Any:
            assert output_hidden_states is True
            hidden = self.embedding(input_ids)
            return SimpleNamespace(logits=self.output(hidden), hidden_states=(hidden,))

        @staticmethod
        def save_pretrained(path: str) -> None:
            destination = Path(path)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "model.safetensors").write_bytes(b"test-only-safe-weights")

    def fake_loader(*_args: object, **_kwargs: object) -> tuple[TinyPolicy, TinyTokenizer]:
        return TinyPolicy(), TinyTokenizer()

    monkeypatch.setattr(trainer_module, "_load_model_and_tokenizer", fake_loader)

    def phonemize(_self: object, text: str, *, language: str) -> object:
        assert language == "en-us"
        return SimpleNamespace(phonemes=[{"alpha": "a", "beta": "b"}[text]])

    monkeypatch.setattr(g2p_module.G2PManager, "phonemize", phonemize)
    targets = targets_module.PhoneticTargetInventory(
        target_phonemes=["a", "b"],
        unit="phoneme",
    )
    trainer = trainer_module.PhonRLTrainer(
        reward_module.PhoneticReward(targets, language="en-us"),
        trainer_module.TrainingConfig(
            model_name="verified-local-snapshot",
            num_steps=1,
            batch_size=2,
            output_dir=str(tmp_path / "success"),
            max_new_tokens=3,
            device="cpu",
            seed=17,
        ),
    )
    result = trainer.train(prompts=["safe prompt"])
    assert result.mean_rewards == pytest.approx([0.5])
    assert result.final_coverage == 1.0
    assert targets.covered_units == {"a", "b"}
    assert (tmp_path / "success" / "model.safetensors").is_file()

    calls = 0

    def fail_second_g2p(_self: object, text: str, *, language: str) -> object:
        nonlocal calls
        calls += 1
        assert language == "en-us"
        if calls == 2:
            raise OSError("private backend detail")
        return SimpleNamespace(phonemes=[{"alpha": "a", "beta": "b"}[text]])

    monkeypatch.setattr(g2p_module.G2PManager, "phonemize", fail_second_g2p)
    atomic_targets = targets_module.PhoneticTargetInventory(
        target_phonemes=["a", "b"],
        unit="phoneme",
    )
    failing_trainer = trainer_module.PhonRLTrainer(
        reward_module.PhoneticReward(atomic_targets, language="en-us"),
        trainer_module.TrainingConfig(
            model_name="verified-local-snapshot",
            num_steps=1,
            batch_size=2,
            output_dir=str(tmp_path / "failure"),
            max_new_tokens=3,
            device="cpu",
            seed=17,
        ),
    )
    with pytest.raises(RuntimeError) as failure:
        failing_trainer.train(prompts=["safe prompt"])
    assert "step 0, row 1, language 'en-us'" in str(failure.value)
    assert atomic_targets.covered_units == set()
    assert not (tmp_path / "failure").exists()


def _prompt_digest(prompts: tuple[str, ...]) -> str:
    return PhonRlPromptArtifact(prompts=prompts).sha256


def test_fake_full_training_static_artifact_stages_safe_checkpoint_and_manifest(
    locked_local_runtime_metadata: dict[str, str],
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    location, pin = snapshot(tmp_path)
    prompts = ("prompt one", "prompt two")
    source = PhonRlStaticPromptSource(
        artifact_id="11111111-1111-1111-1111-111111111111",
        content_sha256=_prompt_digest(prompts),
        prompt_count=2,
    )
    request = PhonRlTrainingRequest(
        runtime_id="tiny-rl-v1",
        target_phonemes=("a", "b"),
        prompt_source=source,
        parameters=PhonRlTrainingParameters(seed=42, num_steps=2, batch_size=2),
    )
    resolver = StaticSnapshotResolver(location)
    reader = StaticPromptReader(prompts)
    bindings = FakeTrainingBindings()
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=resolver,
        prompt_reader=reader,
        training_bindings=bindings,
    )
    emitted: list[PhonRlProgressPoint] = []
    result = adapter.train(request, policy(pin), emit=emitted.append)
    assert result.total_steps == 2
    assert result.mean_rewards == (0.5, 1.0)
    assert result.progress[1].policy_loss == pytest.approx(-0.2)
    assert emitted == list(result.progress)
    assert result.final_coverage == 0.75
    assert result.checkpoint.total_size_bytes > 0
    assert [item.path for item in result.checkpoint.files] == ["config.json", "model.safetensors"]
    assert result.manifest.prompt_source_sha256 == source.content_sha256
    assert result.manifest.prompts_persisted_in_manifest is False
    assert "prompt one" not in result.model_dump_json()
    assert "C:\\" not in result.model_dump_json()
    assert resolver.calls == [(pin, "models-ro")]
    assert reader.calls == [source]
    assert bindings.calls[0]["prompts"] == prompts
    validate_checkpoint_compatibility(result.checkpoint, policy(pin), require_peft=False)
    require_peft_generation_support(result.checkpoint)


def test_dynamic_peft_training_manifest_and_application_inference_contract(
    locked_local_runtime_metadata: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    location, pin = snapshot(tmp_path)
    bindings = FakeTrainingBindings()
    bindings.files = {"adapter_model.safetensors": b"adapter", "adapter_config.json": b"{}"}
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=bindings,
    )
    request = dynamic_request(peft=True)
    result = adapter.train(request, policy(pin))
    assert result.checkpoint.compatibility.peft_adapter is True
    assert result.checkpoint.compatibility.peft_version is not None
    assert result.peft_inference_status == "application_loader_ready"
    assert bindings.calls[0]["dynamic_strategy_id"] == "missing-units-v1"
    require_peft_generation_support(result.checkpoint)
    validate_checkpoint_compatibility(result.checkpoint, policy(pin), require_peft=True)

    policy_module = importlib.import_module("corpusgen.generate.phon_rl.policy")
    base_model = object()
    loaded_model = object()
    load_calls: list[tuple[object, str]] = []

    def load_adapter(model: object, adapter_path: str) -> object:
        load_calls.append((model, adapter_path))
        return loaded_model

    monkeypatch.setattr(policy_module, "_load_peft_adapter", load_adapter)
    strategy = policy_module.PhonRLStrategy(adapter_path="verified-adapter")
    assert strategy.prepare(["a"], base_model, object()) is None
    assert load_calls == [(base_model, "verified-adapter")]
    assert strategy.is_adapter_loaded is True
    assert loaded_model not in vars(strategy).values()
    logits = object()
    assert strategy.modify_logits(object(), logits) is logits


def test_snapshot_tamper_executable_remote_code_and_digest_fail_closed(tmp_path: Path) -> None:
    location, pin = snapshot(tmp_path)
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=FakeTrainingBindings(),
    )
    (location.snapshot / "config.json").write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as digest:
        adapter.train(dynamic_request(), policy(pin))
    assert digest.value.operation == "phon_rl.snapshot.digest"

    executable_location, executable_pin = snapshot(tmp_path / "executable", **{"custom.py": b"x"})
    executable = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(executable_location),
        training_bindings=FakeTrainingBindings(),
    )
    with pytest.raises(EngineUnavailableError) as unsafe:
        executable.train(dynamic_request(), policy(executable_pin))
    assert unsafe.value.operation == "phon_rl.snapshot.executable_content"

    remote_location, _ = snapshot(tmp_path / "remote")
    (remote_location.snapshot / "config.json").write_text(
        '{"auto_map":{"AutoModel":"custom.Model"}}',
        encoding="utf-8",
    )
    remote_digest = compute_snapshot_digest(
        remote_location.snapshot,
        approved_cache_root=remote_location.approved_cache_root,
    )
    remote_pin = PhonRlSnapshotPin(
        repository_id="acme/tiny-rl",
        revision=REVISION,
        snapshot_sha256=remote_digest,
    )
    remote = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(remote_location),
        training_bindings=FakeTrainingBindings(),
    )
    with pytest.raises(EngineUnavailableError) as remote_code:
        remote.train(dynamic_request(), policy(remote_pin))
    assert remote_code.value.operation == "phon_rl.snapshot.remote_code"


def test_snapshot_symlink_escape_is_rejected(tmp_path: Path) -> None:
    location, _ = snapshot(tmp_path)
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"outside")
    link = location.snapshot / "escape.safetensors"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(EngineUnavailableError) as error:
        compute_snapshot_digest(location.snapshot, approved_cache_root=location.approved_cache_root)
    assert error.value.operation == "model_runtime.local.snapshot_boundary"


@pytest.mark.parametrize(
    ("prompts", "source_count", "source_digest", "operation"),
    [
        pytest.param(("one",), 2, None, "phon_rl.prompt_artifact.count", id="count"),
        pytest.param((" ",), 1, "0" * 64, "phon_rl.prompt_artifact.content", id="blank"),
        pytest.param(("one",), 1, "0" * 64, "phon_rl.prompt_artifact.digest", id="digest"),
    ],
)
def test_prompt_artifact_contract_errors_are_sanitized(
    tmp_path: Path,
    prompts: tuple[str, ...],
    source_count: int,
    source_digest: str | None,
    operation: str,
) -> None:
    location, pin = snapshot(tmp_path)
    digest = source_digest or _prompt_digest(prompts)
    request = PhonRlTrainingRequest(
        runtime_id="tiny-rl-v1",
        target_phonemes=("a",),
        prompt_source=PhonRlStaticPromptSource(
            artifact_id="11111111-1111-1111-1111-111111111111",
            content_sha256=digest,
            prompt_count=source_count,
        ),
        parameters=PhonRlTrainingParameters(seed=1, num_steps=1),
    )
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        prompt_reader=StaticPromptReader(prompts),
        training_bindings=FakeTrainingBindings(),
    )
    with pytest.raises(EngineContractError) as error:
        adapter.train(request, policy(pin))
    assert error.value.operation == operation
    assert "one" not in str(error.value)


def test_checkpoint_rejects_pickle_weights_and_compatibility_mismatch(
    locked_local_runtime_metadata: dict[str, str],
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    location, pin = snapshot(tmp_path)
    bindings = FakeTrainingBindings()
    bindings.files = {"pytorch_model.bin": b"pickle"}
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=bindings,
    )
    with pytest.raises(EngineContractError) as unsafe:
        adapter.train(dynamic_request(), policy(pin))
    assert unsafe.value.operation == "phon_rl.checkpoint.contract"

    safe_bindings = FakeTrainingBindings()
    result = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=safe_bindings,
    ).train(dynamic_request(), policy(pin))
    other_pin = pin.model_copy(update={"snapshot_sha256": "f" * 64})
    with pytest.raises(InvalidRequestError) as mismatch:
        validate_checkpoint_compatibility(
            result.checkpoint,
            policy(other_pin),
            require_peft=False,
        )
    assert mismatch.value.operation == "phon_rl.checkpoint.compatibility"

    version_tampered = PhonRlCheckpointBundle.create(
        compatibility=result.checkpoint.compatibility.model_copy(
            update={"torch_version": "0.0-tampered"}
        ),
        files=result.checkpoint.files,
    )
    with pytest.raises(InvalidRequestError) as version_mismatch:
        validate_checkpoint_compatibility(
            version_tampered,
            policy(pin),
            require_peft=False,
        )
    assert version_mismatch.value.operation == "phon_rl.checkpoint.compatibility"


def test_checkpoint_file_integrity_roundtrip_and_tamper_rejection() -> None:
    compatibility = PhonRlCheckpointCompatibility(
        base_model_id="acme/tiny-rl",
        base_model_revision=REVISION,
        base_model_snapshot_sha256="b" * 64,
        tokenizer_id="acme/tiny-rl",
        tokenizer_revision=REVISION,
        tokenizer_snapshot_sha256="b" * 64,
        corpusgen_version="0.1.7",
        torch_version="2",
        transformers_version="5",
        peft_adapter=False,
    )
    content = b"weights"
    file = PhonRlCheckpointFile(
        path="model.safetensors",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_base64="d2VpZ2h0cw==",
    )
    bundle = PhonRlCheckpointBundle.create(compatibility=compatibility, files=(file,))
    assert PhonRlCheckpointBundle.model_validate_json(bundle.model_dump_json()) == bundle
    with pytest.raises(ValueError, match="integrity"):
        PhonRlCheckpointFile(
            path="model.safetensors",
            size_bytes=len(content),
            sha256="0" * 64,
            content_base64="d2VpZ2h0cw==",
        )
    with pytest.raises(ValueError, match="canonical base64"):
        PhonRlCheckpointFile(
            path="model.safetensors",
            size_bytes=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
            content_base64="eB==",
        )


def test_training_failure_and_progress_contract_do_not_leak_prompt(tmp_path: Path) -> None:
    location, pin = snapshot(tmp_path)
    bindings = FakeTrainingBindings()
    bindings.fail = RuntimeError("CUDA out of memory; prompt secret /private/model")
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=bindings,
    )
    runtime_policy = RuntimePolicy(
        (policy(pin),),
        worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
    )
    coordinator = PhonRlTrainingCoordinator(runtime_policy, adapter)
    with pytest.raises(EngineUnavailableError) as error:
        coordinator.train(dynamic_request())
    assert error.value.operation == "phon_rl.training.execute"
    assert "secret" not in str(error.value)
    assert "memory" not in str(error.value)

    class BadProgressBindings(FakeTrainingBindings):
        def train(self, **kwargs: Any) -> BindingTrainingResult:
            callback = kwargs["step_callback"]
            callback(1, 0.0, 0.0)
            raise AssertionError("unreachable")

    bad = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=BadProgressBindings(),
    )
    with pytest.raises(EngineContractError) as progress:
        bad.train(dynamic_request(), policy(pin))
    assert progress.value.operation == "phon_rl.training.progress"


def test_default_training_bindings_public_static_and_dynamic_composition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import corpusgen.generate.phon_ctg.targets as targets_module
    import corpusgen.generate.phon_rl.reward as reward_module
    import corpusgen.generate.phon_rl.trainer as trainer_module

    events: list[tuple[str, object]] = []

    class FakeTargets:
        def __init__(self, **kwargs: object) -> None:
            events.append(("targets", kwargs))

        def next_targets(self, count: int) -> list[str]:
            assert count == 8
            return ["a", "b"]

    class FakeReward:
        def __init__(self, targets: object, **kwargs: object) -> None:
            events.append(("reward", (targets, kwargs)))

    class FakeConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            events.append(("config", kwargs))

    class FakeTrainer:
        def __init__(self, reward: object, config: object) -> None:
            self.reward = reward
            self.config = config
            self.is_initialized = False

        def train(
            self,
            *,
            prompts: list[str] | None,
            prompt_fn: Callable[[object], str] | None,
            step_callback: Callable[..., None],
        ) -> object:
            events.append(("logging_disabled", logging.root.manager.disable))
            if prompt_fn is not None:
                events.append(("dynamic_prompt", prompt_fn(FakeTargets())))
            else:
                events.append(("static_prompts", prompts))
            step_callback(step=0, mean_reward=0.25, policy_loss=-0.5)
            self.is_initialized = True
            return SimpleNamespace(mean_rewards=[0.25], total_steps=1, final_coverage=0.5)

    monkeypatch.setattr(targets_module, "PhoneticTargetInventory", FakeTargets)
    monkeypatch.setattr(reward_module, "PhoneticReward", FakeReward)
    monkeypatch.setattr(trainer_module, "TrainingConfig", FakeConfig)
    monkeypatch.setattr(trainer_module, "PhonRLTrainer", FakeTrainer)
    monkeypatch.setenv("HF_HUB_OFFLINE", "prior")
    prior_logging_disable = logging.root.manager.disable
    progress: list[tuple[int, float, float]] = []

    def callback(step: int, reward: float, loss: float) -> None:
        progress.append((step, reward, loss))

    bindings = CorpusgenPhonRlTrainingBindings()
    static_request = dynamic_request(steps=1).model_copy(
        update={
            "prompt_source": PhonRlStaticPromptSource(
                artifact_id="11111111-1111-1111-1111-111111111111",
                content_sha256="c" * 64,
                prompt_count=1,
            )
        }
    )
    static = bindings.train(
        snapshot=tmp_path,
        request=static_request,
        prompts=("safe prompt",),
        dynamic_strategy_id=None,
        output_dir=tmp_path / "out-static",
        step_callback=callback,
    )
    assert static.mean_rewards == (0.25,)
    assert ("static_prompts", ["safe prompt"]) in events
    assert os.environ["HF_HUB_OFFLINE"] == "prior"
    assert logging.root.manager.disable == prior_logging_disable

    dynamic = bindings.train(
        snapshot=tmp_path,
        request=dynamic_request(steps=1),
        prompts=None,
        dynamic_strategy_id="missing-units-v1",
        output_dir=tmp_path / "out-dynamic",
        step_callback=callback,
    )
    assert dynamic.final_coverage == 0.5
    expected_prompt = "Write one short, natural sentence containing these sounds: a, b."
    assert ("dynamic_prompt", expected_prompt) in events
    assert progress == [(0, 0.25, -0.5), (0, 0.25, -0.5)]
    config = next(value for name, value in events if name == "config")
    assert isinstance(config, dict)
    assert config["model_name"] == str(tmp_path)
    assert config["device"] == "cuda"
    assert [value for name, value in events if name == "logging_disabled"] == [
        logging.CRITICAL,
        logging.CRITICAL,
    ]
    with pytest.raises(ValueError, match="must be cpu or cuda"):
        CorpusgenPhonRlTrainingBindings(_device="mps")  # type: ignore[arg-type]


def test_default_training_bindings_fail_closed_for_prompt_callback_and_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import corpusgen.generate.phon_rl.trainer as trainer_module

    with pytest.raises(InvalidRequestError) as strategy:
        CorpusgenPhonRlTrainingBindings().train(
            snapshot=tmp_path,
            request=dynamic_request(steps=1),
            prompts=None,
            dynamic_strategy_id="unreviewed-strategy",
            output_dir=tmp_path / "out",
            step_callback=lambda *_: None,
        )
    assert strategy.value.operation == "phon_rl.prompt_strategy.unsupported"

    class BadTrainer:
        def __init__(self, reward: object, config: object) -> None:
            self.reward = reward
            self.config = config
            self.is_initialized = False

        def train(self, **kwargs: object) -> object:
            callback = kwargs["step_callback"]
            assert callable(callback)
            callback(step="bad", mean_reward=0.0, policy_loss=0.0)
            raise AssertionError("unreachable")

    monkeypatch.setattr(trainer_module, "PhonRLTrainer", BadTrainer)
    with pytest.raises(EngineContractError) as callback:
        CorpusgenPhonRlTrainingBindings().train(
            snapshot=tmp_path,
            request=dynamic_request(steps=1),
            prompts=None,
            dynamic_strategy_id="missing-units-v1",
            output_dir=tmp_path / "out",
            step_callback=lambda *_: None,
        )
    assert callback.value.operation == "phon_rl.training.progress"


def test_offline_snapshot_resolver_default_deny_layout_dependency_and_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    location, pin = snapshot(tmp_path)
    calls: list[dict[str, object]] = []

    def resolve(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(location.snapshot)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=resolve))
    resolver = OfflinePhonRlSnapshotResolver({"models-ro": location.approved_cache_root})
    assert resolver.resolve(pin, cache_root_id="models-ro") == location
    assert calls == [
        {
            "repo_id": pin.repository_id,
            "revision": pin.revision,
            "cache_dir": str(location.approved_cache_root.absolute()),
            "local_files_only": True,
        }
    ]
    with pytest.raises(EngineUnavailableError) as root:
        resolver.resolve(pin, cache_root_id="unknown")
    assert root.value.operation == "phon_rl.snapshot.cache_root"
    with pytest.raises(ValueError, match="At least one"):
        OfflinePhonRlSnapshotResolver({})

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=lambda **_: str(tmp_path / "wrong")),
    )
    with pytest.raises(EngineUnavailableError) as layout:
        resolver.resolve(pin, cache_root_id="models-ro")
    assert layout.value.operation == "phon_rl.snapshot.layout"

    def failure(**_: object) -> str:
        raise OSError("private cache path")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=failure),
    )
    with pytest.raises(EngineUnavailableError) as failed:
        resolver.resolve(pin, cache_root_id="models-ro")
    assert failed.value.operation == "phon_rl.snapshot.resolve"


def test_missing_worker_dependencies_and_prompt_reader_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = PhonRlStaticPromptSource(
        artifact_id="11111111-1111-1111-1111-111111111111",
        content_sha256="c" * 64,
        prompt_count=1,
    )
    with pytest.raises(EngineUnavailableError) as reader:
        MissingPromptArtifactReader().read(source)
    assert reader.value.operation == "phon_rl.prompt_artifact.reader"

    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(DependencyUnavailableError) as torch_error:
        PhonRlLabService(CorpusgenPhonRlAdapter()).log_probs(
            PhonRlLogProbRequest(
                logits=(((1.0,),),),
                actions=PhonRlIntMatrix(values=((0,),)),
            )
        )
    assert torch_error.value.operation == "phon_rl.ppo.torch"


def test_training_requires_resolver_supported_prompt_and_consistent_steps(
    locked_local_runtime_metadata: dict[str, str],
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    location, pin = snapshot(tmp_path)
    with pytest.raises(EngineUnavailableError) as resolver:
        CorpusgenPhonRlAdapter(training_bindings=FakeTrainingBindings()).train(
            dynamic_request(),
            policy(pin),
        )
    assert resolver.value.operation == "phon_rl.snapshot.resolver"

    unsupported = dynamic_request().model_copy(
        update={"prompt_source": PhonRlDynamicPromptSource(strategy_id="unknown-strategy")}
    )
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=FakeTrainingBindings(),
    )
    with pytest.raises(InvalidRequestError) as prompt:
        adapter.train(unsupported, policy(pin))
    assert prompt.value.operation == "phon_rl.prompt_strategy.unsupported"

    class InconsistentBindings(FakeTrainingBindings):
        def train(self, **kwargs: Any) -> BindingTrainingResult:
            output_dir = kwargs["output_dir"]
            assert isinstance(output_dir, Path)
            output_dir.mkdir(parents=True)
            (output_dir / "model.safetensors").write_bytes(b"weights")
            callback = kwargs["step_callback"]
            assert callable(callback)
            callback(0, 0.1, -0.1)
            return BindingTrainingResult(mean_rewards=(0.1,), total_steps=2, final_coverage=0.1)

    inconsistent = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=InconsistentBindings(),
    )
    with pytest.raises(EngineContractError) as steps:
        inconsistent.train(dynamic_request(), policy(pin))
    assert steps.value.operation == "phon_rl.training.result_steps"


def test_snapshot_invalid_json_and_empty_checkpoint_fail_closed(
    locked_local_runtime_metadata: dict[str, str],
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    location, _ = snapshot(tmp_path)
    (location.snapshot / "config.json").write_text("{", encoding="utf-8")
    digest = compute_snapshot_digest(
        location.snapshot,
        approved_cache_root=location.approved_cache_root,
    )
    pin = PhonRlSnapshotPin(
        repository_id="acme/tiny-rl",
        revision=REVISION,
        snapshot_sha256=digest,
    )
    invalid = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=FakeTrainingBindings(),
    )
    with pytest.raises(EngineUnavailableError) as contract:
        invalid.train(dynamic_request(), policy(pin))
    assert contract.value.operation == "phon_rl.snapshot.contract"

    clean_location, clean_pin = snapshot(tmp_path / "clean")
    bindings = FakeTrainingBindings()
    bindings.files = {}
    empty = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(clean_location),
        training_bindings=bindings,
    )
    with pytest.raises(EngineContractError) as checkpoint:
        empty.train(dynamic_request(), policy(clean_pin))
    assert checkpoint.value.operation == "phon_rl.checkpoint.empty"


def test_static_binding_without_prompts_and_runtime_version_failure_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    static = dynamic_request(steps=1).model_copy(
        update={
            "prompt_source": PhonRlStaticPromptSource(
                artifact_id="11111111-1111-1111-1111-111111111111",
                content_sha256="c" * 64,
                prompt_count=1,
            )
        }
    )
    with pytest.raises(EngineContractError) as prompt:
        CorpusgenPhonRlTrainingBindings().train(
            snapshot=tmp_path,
            request=static,
            prompts=None,
            dynamic_strategy_id=None,
            output_dir=tmp_path / "out",
            step_callback=lambda *_: None,
        )
    assert prompt.value.operation == "phon_rl.prompt_source"

    location, pin = snapshot(tmp_path / "version")
    original = importlib.metadata.version

    def missing_version(distribution: str) -> str:
        if distribution == "torch":
            raise importlib.metadata.PackageNotFoundError(distribution)
        return original(distribution)

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    adapter = CorpusgenPhonRlAdapter(
        snapshot_resolver=StaticSnapshotResolver(location),
        training_bindings=FakeTrainingBindings(),
    )
    with pytest.raises(DependencyUnavailableError) as version:
        adapter.train(dynamic_request(), policy(pin))
    assert version.value.operation == "phon_rl.runtime.version"
