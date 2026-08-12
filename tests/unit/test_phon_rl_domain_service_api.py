from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import socket
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from corpuskit.adapters.corpusgen.phon_rl import CorpusgenPhonRlAdapter
from corpuskit.api.phon_rl_lab import phon_rl_lab_router
from corpuskit.domain.errors import EngineContractError, EngineUnavailableError, InvalidRequestError
from corpuskit.domain.phon_rl import (
    MAX_RL_CHECKPOINT_BASE64_BYTES,
    MAX_RL_CHECKPOINT_BYTES,
    MAX_RL_RESULT_BYTES,
    MAX_RL_RESULT_OVERHEAD_BYTES,
    PhonRlBoolMatrix,
    PhonRlCheckpointBundle,
    PhonRlCheckpointCompatibility,
    PhonRlCheckpointFile,
    PhonRlClipLossRequest,
    PhonRlDynamicPromptSource,
    PhonRlExternalScores,
    PhonRlFloatMatrix,
    PhonRlGaeRequest,
    PhonRlGaeResult,
    PhonRlHiddenMatrix,
    PhonRlHierarchicalRewardRequest,
    PhonRlIntMatrix,
    PhonRlKlRequest,
    PhonRlLogProbRequest,
    PhonRlMatrixResult,
    PhonRlPhonemeSequence,
    PhonRlPromptArtifact,
    PhonRlRewardBreakdown,
    PhonRlRewardState,
    PhonRlRewardWeights,
    PhonRlRuntimePolicyEntry,
    PhonRlSentenceRewardRequest,
    PhonRlSnapshotPin,
    PhonRlStaticPromptSource,
    PhonRlTokenPiece,
    PhonRlTokenRewardRequest,
    PhonRlTokenRewardResult,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
    PhonRlUnit,
    PhonRlValueHeadRequest,
    PhonRlValueHeadResult,
    PhonRlWorkerProfile,
)
from corpuskit.services.phon_rl import PhonRlLabService, PhonRlRuntimePolicy

PIN = PhonRlSnapshotPin(
    repository_id="acme/tiny-rl",
    revision="a" * 40,
    snapshot_sha256="b" * 64,
)


def state(unit: PhonRlUnit = PhonRlUnit.PHONEME) -> PhonRlRewardState:
    return PhonRlRewardState(target_phonemes=("a", "b", "c"), unit=unit)


def sentence_request(**changes: Any) -> PhonRlSentenceRewardRequest:
    values: dict[str, Any] = {
        "state": state(),
        "source_id": "sentence:1",
        "phonemes": ("a", "b"),
        "text": "safe text",
    }
    values.update(changes)
    return PhonRlSentenceRewardRequest(**values)


def policy_entry(**changes: Any) -> PhonRlRuntimePolicyEntry:
    values: dict[str, Any] = {
        "runtime_id": "tiny-rl-v1",
        "model": PIN,
        "tokenizer": PIN,
        "cache_root_id": "models-ro",
        "cache_mount_read_only": True,
        "allow_peft": True,
        "allowed_peft_ranks": (8,),
        "allowed_peft_alphas": (16,),
        "allowed_prompt_strategies": ("missing-units-v1",),
    }
    values.update(changes)
    return PhonRlRuntimePolicyEntry(**values)


def training_request(**changes: Any) -> PhonRlTrainingRequest:
    values: dict[str, Any] = {
        "runtime_id": "tiny-rl-v1",
        "target_phonemes": ("a", "b"),
        "prompt_source": PhonRlDynamicPromptSource(
            strategy_id="missing-units-v1",
            requested_prompts=2,
        ),
        "parameters": PhonRlTrainingParameters(seed=7, num_steps=2, batch_size=2),
    }
    values.update(changes)
    return PhonRlTrainingRequest(**values)


@pytest.mark.parametrize("unit", list(PhonRlUnit))
def test_reward_state_roundtrip_all_units_and_duplicate_state_rejection(
    unit: PhonRlUnit,
) -> None:
    committed = PhonRlPhonemeSequence(source_id="source:1", phonemes=("a", "b", "c"))
    value = PhonRlRewardState(
        target_phonemes=("a", "b", "c"),
        unit=unit,
        committed=(committed,),
        revision=1,
    )
    assert PhonRlRewardState.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError, match="source IDs must be unique"):
        PhonRlRewardState(
            target_phonemes=("a", "b"),
            committed=(committed, committed),
            revision=2,
        )
    with pytest.raises(ValidationError, match="revision"):
        PhonRlRewardState(target_phonemes=("a",), revision=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("coverage", math.nan),
        ("phonotactic", math.inf),
        ("fluency", -math.inf),
    ],
)
def test_reward_weights_reject_non_finite_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        PhonRlRewardWeights(**{field: value})


def test_sentence_request_requires_component_signals_and_safe_content() -> None:
    with pytest.raises(ValidationError, match="weighted phonotactic"):
        sentence_request(weights=PhonRlRewardWeights(phonotactic=1.0))
    with pytest.raises(ValidationError, match="weighted fluency"):
        sentence_request(weights=PhonRlRewardWeights(fluency=1.0))
    with pytest.raises(ValidationError, match="non-blank"):
        sentence_request(text="  ")
    valid = sentence_request(
        weights=PhonRlRewardWeights(phonotactic=2.0, fluency=3.0),
        scores=PhonRlExternalScores(
            phonotactic=0.25,
            fluency=0.5,
            reference_log_probability=-9.0,
        ),
    )
    assert valid.scores.fluency == 0.5


def test_token_request_allows_repetition_but_rejects_conflicting_token_identity() -> None:
    piece = PhonRlTokenPiece(token_id=1, decoded_text="a", raw_token="a")
    assert len(PhonRlTokenRewardRequest(state=state(), pieces=(piece, piece)).pieces) == 2
    with pytest.raises(ValidationError, match="one tokenizer identity"):
        PhonRlTokenRewardRequest(
            state=state(),
            pieces=(piece, PhonRlTokenPiece(token_id=1, decoded_text="b", raw_token="b")),
        )


def test_ppo_dtos_enforce_rectangular_shapes_masks_actions_and_finite_values() -> None:
    with pytest.raises(ValidationError, match="rectangular"):
        PhonRlFloatMatrix(values=((1.0,), (1.0, 2.0)))
    with pytest.raises(ValidationError):
        PhonRlFloatMatrix(values=((math.nan,),))
    with pytest.raises(ValidationError, match="action IDs"):
        PhonRlLogProbRequest(
            logits=(((1.0, 2.0),),),
            actions=PhonRlIntMatrix(values=((2,),)),
        )
    with pytest.raises(ValidationError, match="shapes must match"):
        PhonRlKlRequest(
            policy_log_probs=PhonRlFloatMatrix(values=((1.0,),)),
            reference_log_probs=PhonRlFloatMatrix(values=((1.0, 2.0),)),
        )
    with pytest.raises(ValidationError, match="mask must match"):
        PhonRlGaeRequest(
            rewards=PhonRlFloatMatrix(values=((1.0, 2.0),)),
            values=PhonRlFloatMatrix(values=((0.0, 0.0),)),
            mask=PhonRlBoolMatrix(values=((True,),)),
        )
    with pytest.raises(ValidationError, match="one shape"):
        PhonRlClipLossRequest(
            advantages=PhonRlFloatMatrix(values=((1.0,),)),
            old_log_probs=PhonRlFloatMatrix(values=((1.0,),)),
            new_log_probs=PhonRlFloatMatrix(values=((1.0, 2.0),)),
        )


def test_value_head_dto_supports_2d_and_3d_exclusively() -> None:
    two = PhonRlValueHeadRequest(hidden_states_2d=PhonRlHiddenMatrix(values=((1.0, 2.0),)))
    three = PhonRlValueHeadRequest(hidden_states_3d=(((1.0, 2.0),),))
    assert two.hidden_states_2d is not None
    assert three.hidden_states_3d is not None
    with pytest.raises(ValidationError, match="exactly one"):
        PhonRlValueHeadRequest()
    with pytest.raises(ValidationError, match="exactly one"):
        PhonRlValueHeadRequest(
            hidden_states_2d=PhonRlHiddenMatrix(values=((1.0,),)),
            hidden_states_3d=(((1.0,),),),
        )


def test_training_pin_prompt_and_policy_contracts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="namespaced"):
        PhonRlSnapshotPin(
            repository_id="C:/model",
            revision="a" * 40,
            snapshot_sha256="b" * 64,
        )
    with pytest.raises(ValidationError, match="immutable"):
        PhonRlSnapshotPin(
            repository_id="acme/model",
            revision="main".ljust(40, "a"),
            snapshot_sha256="b" * 64,
        )
    with pytest.raises(ValidationError, match="one model/tokenizer snapshot"):
        policy_entry(
            tokenizer=PhonRlSnapshotPin(
                repository_id="acme/other",
                revision="a" * 40,
                snapshot_sha256="b" * 64,
            )
        )
    with pytest.raises(ValidationError, match="safe identifier"):
        PhonRlDynamicPromptSource(strategy_id="module:callback")
    static = PhonRlStaticPromptSource(
        artifact_id="11111111-1111-1111-1111-111111111111",
        content_sha256="c" * 64,
        prompt_count=2,
    )
    assert static.model_dump(mode="json")["kind"] == "artifact"

    with pytest.raises(ValidationError, match="only the coverage reward"):
        training_request(weights=PhonRlRewardWeights(fluency=1.0))


def test_checkpoint_base64_ceiling_has_proven_result_envelope_headroom() -> None:
    assert MAX_RL_CHECKPOINT_BYTES == 60 * 1024 * 1024
    assert MAX_RL_CHECKPOINT_BASE64_BYTES + MAX_RL_RESULT_OVERHEAD_BYTES <= MAX_RL_RESULT_BYTES


def test_normalized_reward_and_token_results_reject_inconsistent_artifacts() -> None:
    common = {
        "coverage_reward": 0.5,
        "phonotactic_reward": 0.0,
        "fluency_reward": 0.0,
        "composite_reward": 0.5,
        "new_units": ("a",),
        "coverage_gain": 1,
        "target_size": 2,
        "fluency_signal": "none",
    }
    with pytest.raises(ValidationError, match="sorted and unique"):
        PhonRlRewardBreakdown(**{**common, "new_units": ("b", "a")})
    with pytest.raises(ValidationError, match="coverage gain"):
        PhonRlRewardBreakdown(**{**common, "coverage_gain": 0})
    with pytest.raises(ValidationError, match="normalization"):
        PhonRlRewardBreakdown(**{**common, "coverage_reward": 0.25})

    token_common = {
        "token_ids": (1, 2),
        "per_token_rewards": (0.5, 0.0),
        "word_boundaries": (0,),
        "words_phonemized": ("word",),
    }
    with pytest.raises(ValidationError, match="align with token IDs"):
        PhonRlTokenRewardResult(**{**token_common, "per_token_rewards": (0.5,)})
    with pytest.raises(ValidationError, match="align with phonemized"):
        PhonRlTokenRewardResult(**{**token_common, "words_phonemized": ()})
    with pytest.raises(ValidationError, match="index is invalid"):
        PhonRlTokenRewardResult(**{**token_common, "word_boundaries": (2,)})
    with pytest.raises(ValidationError, match="sorted and unique"):
        PhonRlTokenRewardResult(
            **{
                **token_common,
                "word_boundaries": (1, 0),
                "words_phonemized": ("a", "b"),
            }
        )
    with pytest.raises(ValidationError, match="rectangular"):
        PhonRlMatrixResult(values=((1.0,), (1.0, 2.0)))
    with pytest.raises(ValidationError, match="one shape"):
        PhonRlGaeResult(advantages=((1.0,),), returns=((1.0, 2.0),))
    with pytest.raises(ValidationError, match="rank-1"):
        PhonRlValueHeadResult(
            hidden_size=1,
            dropout=0.0,
            rank=1,
            values=((1.0,),),
        )
    with pytest.raises(ValidationError, match="rank-2"):
        PhonRlValueHeadResult(
            hidden_size=1,
            dropout=0.0,
            rank=2,
            values=(1.0,),
        )


def test_policy_checkpoint_and_identifier_adversarial_validation() -> None:
    with pytest.raises(ValidationError, match="target phonemes must be unique"):
        PhonRlRewardState(target_phonemes=("a", "a"))
    with pytest.raises(ValidationError, match="language"):
        sentence_request(language="../../etc")
    with pytest.raises(ValidationError, match="source IDs"):
        sentence_request(source_id="bad/source")
    with pytest.raises(ValidationError, match="phonemes"):
        sentence_request(phonemes=(" ",))
    with pytest.raises(ValidationError, match="SHA-256"):
        PhonRlSnapshotPin(
            repository_id="acme/model",
            revision="a" * 40,
            snapshot_sha256="A" * 64,
        )
    with pytest.raises(ValidationError, match="rank allowlists"):
        policy_entry(allowed_peft_ranks=(8, 8))
    with pytest.raises(ValidationError, match="alpha allowlists"):
        policy_entry(allowed_peft_alphas=(0,))
    with pytest.raises(ValidationError, match="strategy allowlists"):
        policy_entry(allowed_prompt_strategies=("missing-units-v1", "missing-units-v1"))
    with pytest.raises(ValidationError, match="PEFT options require"):
        policy_entry(allow_peft=False)
    with pytest.raises(ValidationError, match="training targets must be unique"):
        training_request(target_phonemes=("a", "a"))
    with pytest.raises(ValidationError, match="non-blank and bounded"):
        PhonRlPromptArtifact(prompts=("\ud800",))

    compatibility = PhonRlCheckpointCompatibility(
        base_model_id=PIN.repository_id,
        base_model_revision=PIN.revision,
        base_model_snapshot_sha256=PIN.snapshot_sha256,
        tokenizer_id=PIN.repository_id,
        tokenizer_revision=PIN.revision,
        tokenizer_snapshot_sha256=PIN.snapshot_sha256,
        corpusgen_version="0.1.7",
        torch_version="2",
        transformers_version="5",
        peft_adapter=False,
    )
    with pytest.raises(ValidationError, match="canonical base64"):
        PhonRlCheckpointFile(
            path="model.safetensors",
            size_bytes=1,
            sha256="0" * 64,
            content_base64="***",
        )
    content = b"x"
    safe_file = PhonRlCheckpointFile(
        path="model.safetensors",
        size_bytes=1,
        sha256=hashlib.sha256(content).hexdigest(),
        content_base64=base64.b64encode(content).decode(),
    )
    bundle = PhonRlCheckpointBundle.create(compatibility=compatibility, files=(safe_file,))
    with pytest.raises(ValidationError, match="integrity"):
        PhonRlCheckpointBundle.model_validate(
            {**bundle.model_dump(mode="json"), "content_sha256": "0" * 64}
        )


def test_policy_validates_exact_allowlists_profile_peft_and_estimate() -> None:
    policy = PhonRlRuntimePolicy((policy_entry(),), worker_profile=PhonRlWorkerProfile.LOCAL_GPU)
    request = training_request()
    validation = policy.validate(request)
    estimate = policy.estimate(request)
    assert validation.required_profile == "gpu-training"
    assert estimate.generated_token_ceiling == 2 * 2 * 64
    assert estimate.model_copies == 2

    with pytest.raises(InvalidRequestError) as unknown:
        policy.validate(training_request(runtime_id="unknown-runtime"))
    assert unknown.value.operation == "phon_rl.runtime.allowlist"

    denied_peft = training_request(
        parameters=PhonRlTrainingParameters(
            seed=7,
            num_steps=2,
            use_peft=True,
            peft_rank=16,
        )
    )
    with pytest.raises(InvalidRequestError) as peft:
        policy.validate(denied_peft)
    assert peft.value.operation == "phon_rl.runtime.peft_allowlist"

    with pytest.raises(InvalidRequestError) as strategy:
        policy.validate(
            training_request(
                prompt_source=PhonRlDynamicPromptSource(strategy_id="another-strategy")
            )
        )
    assert strategy.value.operation == "phon_rl.runtime.prompt_strategy_allowlist"

    with pytest.raises(InvalidRequestError) as static:
        policy.validate(
            training_request(
                prompt_source=PhonRlStaticPromptSource(
                    artifact_id="11111111-1111-1111-1111-111111111111",
                    content_sha256="c" * 64,
                    prompt_count=1,
                )
            )
        )
    assert static.value.operation == "phon_rl.runtime.static_prompt_policy"


def test_policy_rejects_duplicate_entries_and_wrong_profile() -> None:
    with pytest.raises(ValueError, match="unique"):
        PhonRlRuntimePolicy(
            (policy_entry(), policy_entry()),
            worker_profile=PhonRlWorkerProfile.LOCAL_GPU,
        )
    policy = PhonRlRuntimePolicy((policy_entry(),), worker_profile=PhonRlWorkerProfile.LOCAL_GPU)
    object.__setattr__(policy, "_worker_profile", "invalid")
    with pytest.raises(InvalidRequestError) as error:
        policy.validate(training_request())
    assert error.value.operation == "phon_rl.runtime.worker_profile"


class FailingEngine(CorpusgenPhonRlAdapter):
    def sentence_reward(
        self,
        request: PhonRlSentenceRewardRequest,
        *,
        commit: bool,
    ) -> Any:
        del request, commit
        raise RuntimeError("secret prompt and filesystem path")


def test_service_duplicate_commit_atomicity_and_error_sanitization() -> None:
    committed_state = PhonRlRewardState(
        target_phonemes=("a",),
        committed=(PhonRlPhonemeSequence(source_id="sentence:1", phonemes=("a",)),),
        revision=1,
    )
    service = PhonRlLabService(FailingEngine())
    duplicate = sentence_request(state=committed_state, phonemes=("a",))
    with pytest.raises(InvalidRequestError) as error:
        service.commit(duplicate)
    assert error.value.operation == "phon_rl.reward.duplicate_source"
    assert committed_state.revision == 1

    clean = sentence_request(source_id="sentence:2")
    with pytest.raises(EngineUnavailableError) as sanitized:
        service.commit(clean)
    assert sanitized.value.operation == "phon_rl.reward.commit"
    assert "secret" not in str(sanitized.value)
    assert clean.state.revision == 0


def test_service_rejects_mutated_reward_state_and_token_alignment() -> None:
    class MutatingResultEngine(CorpusgenPhonRlAdapter):
        def sentence_reward(
            self,
            request: PhonRlSentenceRewardRequest,
            *,
            commit: bool,
        ) -> Any:
            result = super().sentence_reward(request, commit=commit)
            return result.model_copy(update={"committed": not commit})

        def token_rewards(self, request: PhonRlTokenRewardRequest) -> Any:
            result = super().token_rewards(request)
            return result.model_copy(update={"token_ids": (9_999,)})

        def hierarchical_reward(self, request: PhonRlHierarchicalRewardRequest) -> Any:
            result = super().hierarchical_reward(request)
            return result.model_copy(update={"state_revision": result.state_revision + 1})

    service = PhonRlLabService(MutatingResultEngine())
    with pytest.raises(EngineContractError) as sentence_error:
        service.peek(sentence_request())
    assert sentence_error.value.operation == "phon_rl.reward.result"

    piece = PhonRlTokenPiece(token_id=1, decoded_text="a", raw_token="a")
    with pytest.raises(EngineContractError) as token_error:
        service.token_rewards(PhonRlTokenRewardRequest(state=state(), pieces=(piece,)))
    assert token_error.value.operation == "phon_rl.reward.result"

    with pytest.raises(EngineContractError) as hierarchy_error:
        service.hierarchical(
            PhonRlHierarchicalRewardRequest(
                sentence=sentence_request(text="a"),
                pieces=(piece,),
            )
        )
    assert hierarchy_error.value.operation == "phon_rl.reward.result"


def test_lab_router_has_no_training_execution_and_makes_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CpuOnlyLab:
        def kl_penalty(self, request: PhonRlKlRequest) -> PhonRlMatrixResult:
            del request
            return PhonRlMatrixResult(values=((0.0, 1.0),))

    lab = CpuOnlyLab()
    policy = PhonRlRuntimePolicy((policy_entry(),), worker_profile=PhonRlWorkerProfile.LOCAL_GPU)
    app = FastAPI()
    app.include_router(phon_rl_lab_router(lab, policy), prefix="/api/v1")  # type: ignore[arg-type]

    network_calls: list[tuple[object, ...]] = []

    def forbidden_network(*args: object, **kwargs: object) -> None:
        network_calls.append((*args, kwargs))
        raise AssertionError("network access is forbidden in the Phon-RL lab")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    paths = set(app.openapi()["paths"])
    assert "/api/v1/phon-rl/training/execute" not in paths
    assert "/api/v1/phon-rl/training/validate" in paths

    request = PhonRlKlRequest(
        policy_log_probs=PhonRlFloatMatrix(values=((0.0, -1.0),)),
        reference_log_probs=PhonRlFloatMatrix(values=((0.0, -2.0),)),
    )
    response = TestClient(app).post(
        "/api/v1/phon-rl/ppo/kl-penalty",
        json=request.model_dump(mode="json"),
    )
    assert response.status_code == 200
    assert response.json()["values"][0][0] == 0.0
    assert network_calls == []


def test_every_lab_route_delegates_without_exposing_a_training_executor() -> None:
    calls: list[str] = []

    class RecordingDispatch:
        def __getattr__(self, name: str) -> Callable[[object], object]:
            def dispatch(request: object) -> object:
                calls.append(name)
                return request

            return dispatch

    matrix = PhonRlFloatMatrix(values=((0.0,),))
    mask = PhonRlBoolMatrix(values=((True,),))
    piece = PhonRlTokenPiece(token_id=1, decoded_text="word", raw_token="word")
    sentence = sentence_request()
    requests: dict[str, object] = {
        "/phon-rl/reward/peek": sentence,
        "/phon-rl/reward/commit": sentence,
        "/phon-rl/reward/tokens": PhonRlTokenRewardRequest(
            state=state(),
            pieces=(piece,),
        ),
        "/phon-rl/reward/hierarchical": PhonRlHierarchicalRewardRequest(
            sentence=sentence,
            pieces=(piece,),
        ),
        "/phon-rl/ppo/log-probabilities": PhonRlLogProbRequest(
            logits=(((1.0, 0.0),),),
            actions=PhonRlIntMatrix(values=((0,),)),
        ),
        "/phon-rl/ppo/kl-penalty": PhonRlKlRequest(
            policy_log_probs=matrix,
            reference_log_probs=matrix,
        ),
        "/phon-rl/ppo/gae": PhonRlGaeRequest(
            rewards=matrix,
            values=matrix,
            mask=mask,
        ),
        "/phon-rl/ppo/clip-loss": PhonRlClipLossRequest(
            advantages=matrix,
            old_log_probs=matrix,
            new_log_probs=matrix,
            mask=mask,
        ),
        "/phon-rl/ppo/value-head": PhonRlValueHeadRequest(
            hidden_states_2d=PhonRlHiddenMatrix(values=((1.0,),))
        ),
        "/phon-rl/training/validate": training_request(),
        "/phon-rl/training/estimate": training_request(),
    }
    dispatch = RecordingDispatch()
    router = phon_rl_lab_router(dispatch, dispatch)  # type: ignore[arg-type]

    async def invoke_routes() -> None:
        for route in router.routes:
            path = cast(Any, route).path
            assert path in requests
            assert await cast(Any, route).endpoint(requests[path]) == requests[path]

    asyncio.run(invoke_routes())
    assert calls == [
        "peek",
        "commit",
        "token_rewards",
        "hierarchical",
        "log_probs",
        "kl_penalty",
        "gae",
        "clip_loss",
        "value_head",
        "validate",
        "estimate",
    ]


@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(
            lambda: PhonRlTrainingParameters(seed=1, num_steps=0),
            id="zero-steps",
        ),
        pytest.param(
            lambda: PhonRlTrainingParameters(seed=1, batch_size=33),
            id="oversize-batch",
        ),
        pytest.param(
            lambda: PhonRlTrainingParameters(seed=1, learning_rate=math.inf),
            id="infinite-learning-rate",
        ),
        pytest.param(
            lambda: PhonRlTrainingParameters(seed=1, activity_timeout_seconds=100_000),
            id="oversize-deadline",
        ),
    ],
)
def test_training_parameter_bounds(builder: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        builder()
