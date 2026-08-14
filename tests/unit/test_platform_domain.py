"""Fail-closed quota classification and canonical audit contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    HostedModelSelection,
    HostedRunBudget,
    ImmutableModelPin,
    LocalGenerationRequest,
    LocalModelSelection,
)
from corpuskit.domain.phon_rl import (
    PhonRlDynamicPromptSource,
    PhonRlTrainingParameters,
    PhonRlTrainingRequest,
)
from corpuskit.domain.platform import (
    AUDIT_GENESIS_HASH,
    RUN_QUOTA_CLASSES,
    AuditAction,
    AuditActorKind,
    AuditResourceType,
    QuotaPolicyValues,
    RunQuotaClass,
    audit_event_hash,
    normalize_audit_metadata,
    run_quota_class,
    safe_audit_actor,
    safe_correlation_id,
    validate_run_resource_policy,
)


def _stopping(*, sentences: int = 1, iterations: int = 1) -> GenerationStoppingCriteria:
    return GenerationStoppingCriteria(
        max_sentences=sentences,
        max_iterations=iterations,
        timeout_seconds=1,
    )


def _local(*, deadline: float = 10, tokens: int = 8, candidates: int = 1) -> dict[str, object]:
    return LocalGenerationRequest(
        selection=LocalModelSelection(pin=ImmutableModelPin(model="acme/tiny", revision="a" * 40)),
        target=GenerationTarget(phonemes=("p",)),
        stopping=_stopping(),
        max_new_tokens=tokens,
        candidates_per_iteration=candidates,
        activity_timeout_seconds=deadline,
    ).model_dump(mode="json")


def _hosted(
    *,
    input_tokens: int = 100,
    output_tokens: int = 100,
    cost: Decimal = Decimal("0.000001"),
) -> dict[str, object]:
    return HostedGenerationRequest(
        selection=HostedModelSelection(
            provider="openai",
            model="openai/model",
            connection_id="primary",
        ),
        target=GenerationTarget(phonemes=("p",)),
        stopping=_stopping(),
        budget=HostedRunBudget(
            max_input_tokens=input_tokens,
            max_output_tokens=output_tokens,
            max_cost_usd=cost,
        ),
        activity_timeout_seconds=10,
        external_processing_confirmed=True,
    ).model_dump(mode="json")


def _rl(*, steps: int = 2, batch: int = 1, tokens: int = 2) -> dict[str, object]:
    return PhonRlTrainingRequest(
        runtime_id="rl-runtime",
        target_phonemes=("p",),
        prompt_source=PhonRlDynamicPromptSource(strategy_id="seed", requested_prompts=1),
        parameters=PhonRlTrainingParameters(
            seed=1,
            num_steps=steps,
            batch_size=batch,
            max_new_tokens=tokens,
            activity_timeout_seconds=10,
        ),
    ).model_dump(mode="json")


def test_every_run_kind_has_an_explicit_fail_closed_classification() -> None:
    assert set(RUN_QUOTA_CLASSES) == set(RunKind)
    assert run_quota_class(RunKind.EVALUATE) is RunQuotaClass.CPU
    assert run_quota_class(RunKind.GENERATE_LLM) is RunQuotaClass.EXPENSIVE
    with pytest.raises(ValueError, match="no quota classification"):
        run_quota_class("future-kind")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_quota_deadline_must_be_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        QuotaPolicyValues(max_activity_deadline_seconds=value)


def test_resource_policy_validates_every_specialized_contract() -> None:
    policy = QuotaPolicyValues(max_activity_deadline_seconds=20)
    repository = {
        "source": {
            "kind": "raw_text",
            "entries": [{"source_id": "one", "text": "hello"}],
            "language": "en-us",
        },
        "target": {"phonemes": ["p"]},
        "stopping": {
            "max_sentences": 1,
            "max_iterations": 1,
            "timeout_seconds": 1,
        },
        "activity_timeout_seconds": 10,
    }
    analysis = {
        "selection": {
            "pin": {"model": "acme/tiny", "revision": "a" * 40},
            "device": "cpu",
            "quantization": "none",
        },
        "texts": [{"source_id": "one", "text": "hello"}],
        "max_length": 8,
        "activity_timeout_seconds": 10,
    }
    datg_index = {"runtime_id": "datg-runtime", "activity_timeout_seconds": 10}
    datg_generation = {
        "runtime_id": "datg-runtime",
        "index_cache_key_sha256": "a" * 64,
        "target_phonemes": ["p"],
        "target_units": ["p"],
        "candidates": 1,
        "max_new_tokens": 2,
        "activity_timeout_seconds": 10,
    }
    assert validate_run_resource_policy(RunKind.GENERATE_REPOSITORY, repository, policy) == 10
    assert validate_run_resource_policy(RunKind.GENERATE_LLM, _hosted(), policy) == 10
    assert validate_run_resource_policy(RunKind.GENERATE_LOCAL, _local(), policy) == 10
    assert validate_run_resource_policy(RunKind.PERPLEXITY, analysis, policy) == 10
    assert validate_run_resource_policy(RunKind.BUILD_DATG_INDEX, datg_index, policy) == 10
    assert validate_run_resource_policy(RunKind.GENERATE_DATG, datg_generation, policy) == 10
    assert validate_run_resource_policy(RunKind.TRAIN_PHON_RL, _rl(), policy) == 10
    assert validate_run_resource_policy(RunKind.EVALUATE, {}, policy) is None


@pytest.mark.parametrize(
    ("kind", "spec", "policy"),
    [
        (
            RunKind.GENERATE_REPOSITORY,
            {
                "source": {
                    "kind": "raw_text",
                    "entries": [{"source_id": "one", "text": "hello"}],
                },
                "target": {"phonemes": ["p"]},
                "stopping": {
                    "max_sentences": 2,
                    "max_iterations": 1,
                    "timeout_seconds": 1,
                },
                "activity_timeout_seconds": 10,
            },
            QuotaPolicyValues(max_generation_accepted_sentences=1),
        ),
        (
            RunKind.GENERATE_LLM,
            _hosted(input_tokens=100),
            QuotaPolicyValues(max_provider_input_tokens=99),
        ),
        (
            RunKind.GENERATE_LLM,
            _hosted(output_tokens=100),
            QuotaPolicyValues(max_provider_output_tokens=99),
        ),
        (
            RunKind.GENERATE_LLM,
            _hosted(cost=Decimal("0.000002")),
            QuotaPolicyValues(max_provider_cost_microusd=1),
        ),
        (
            RunKind.GENERATE_LOCAL,
            _local(tokens=8, candidates=2),
            QuotaPolicyValues(max_provider_output_tokens=15),
        ),
        (
            RunKind.GENERATE_LOCAL,
            _local(deadline=10),
            QuotaPolicyValues(max_activity_deadline_seconds=9),
        ),
        (
            RunKind.TRAIN_PHON_RL,
            _rl(steps=2),
            QuotaPolicyValues(max_rl_steps=1),
        ),
        (
            RunKind.TRAIN_PHON_RL,
            _rl(steps=2, batch=2, tokens=2),
            QuotaPolicyValues(max_rl_tokens=7),
        ),
    ],
)
def test_resource_policy_rejects_server_ceiling_violations(
    kind: RunKind,
    spec: dict[str, object],
    policy: QuotaPolicyValues,
) -> None:
    with pytest.raises(ValueError, match="quota exceeded"):
        validate_run_resource_policy(kind, spec, policy)


def test_resource_policy_rejects_sub_micro_usd_precision() -> None:
    with pytest.raises(ValueError, match="provider cost"):
        validate_run_resource_policy(
            RunKind.GENERATE_LLM,
            _hosted(cost=Decimal("0.0000001")),
            QuotaPolicyValues(),
        )


@pytest.mark.parametrize("value", ["", " unsafe", "a\n", "x" * 129])
def test_correlation_ids_are_strictly_bounded(value: str) -> None:
    with pytest.raises(ValueError, match="correlation"):
        safe_correlation_id(value)
    assert safe_correlation_id(None) is None
    assert safe_correlation_id("request-1:child") == "request-1:child"


@pytest.mark.parametrize("value", ["", " bad", "a\n", "x" * 256])
def test_audit_actor_ids_are_strictly_bounded(value: str) -> None:
    with pytest.raises(ValueError, match="actor"):
        safe_audit_actor(value)
    assert safe_audit_actor("service:worker") == "service:worker"


def test_audit_metadata_is_allowlisted_nonfinite_safe_and_bounded() -> None:
    assert normalize_audit_metadata(AuditAction.PROJECT_CREATED, {}) == {}
    with pytest.raises(ValueError, match="allowlist"):
        normalize_audit_metadata(AuditAction.PROJECT_CREATED, {"prompt": "secret"})
    with pytest.raises(ValueError, match=r"range|finite"):
        normalize_audit_metadata(AuditAction.RUN_SUCCEEDED, {"kind": float("nan")})
    with pytest.raises(ValueError, match="size limit"):
        normalize_audit_metadata(
            AuditAction.RUN_SUCCEEDED,
            {"kind": "x" * 2_100},
        )
    assert (
        normalize_audit_metadata(
            AuditAction.CORPUS_VERSION_CREATED,
            {
                "content_sha256": "a" * 64,
                "language": "en-us",
                "parent_version_id": "00000000-0000-4000-8000-000000000001",
                "sentence_count": 2,
                "version_number": 3,
            },
        )["version_number"]
        == 3
    )


def test_audit_hash_is_canonical_across_sqlite_timezone_round_trip() -> None:
    organization_id = uuid4()
    resource_id = uuid4()
    common = {
        "organization_id": organization_id,
        "sequence": 1,
        "actor_kind": AuditActorKind.SERVICE,
        "actor_id": "service:worker",
        "action": AuditAction.RUN_SUCCEEDED,
        "resource_type": AuditResourceType.RUN,
        "resource_id": resource_id,
        "request_id": "request-1",
        "metadata": {"kind": "evaluate"},
        "previous_hash": AUDIT_GENESIS_HASH,
    }
    aware = audit_event_hash(
        **common,
        occurred_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    naive = audit_event_hash(
        **common,
        occurred_at=datetime(2026, 8, 11, 12),  # noqa: DTZ001 - emulates SQLite
    )
    assert aware == naive
    assert len(aware) == 64
    assert UUID(str(organization_id)) == organization_id
