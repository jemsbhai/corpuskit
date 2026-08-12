"""Decision-branch tests for reproducibility persistence helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corpuskit.domain.artifacts import (
    ArtifactState,
    DeterminismClass,
    ReplayComparison,
    ReplayVerdict,
)
from corpuskit.domain.errors import InvalidRequestError
from corpuskit.domain.jobs import RunKind, RunState, normalize_run_spec
from corpuskit.domain.reproducibility import ReplayLifecycle
from corpuskit.services.reproducibility import (
    ReproducibilityError,
    _content_digests,
    _idempotency_key,
    _insert_for,
    _reference_ids,
    _replay_status,
    _same_manifest_artifact,
    _stop_reason,
    _utc,
    _verify_reference,
)
from corpuskit.workflows.contracts import RunWorkflowReference


@pytest.mark.parametrize("value", ["", "a" * 129, "has space", "has\nnewline", "snowman-☃"])
def test_idempotency_key_rejects_empty_oversized_or_non_ascii_values(value: str) -> None:
    with pytest.raises(InvalidRequestError):
        _idempotency_key(value)
    assert _idempotency_key("visible-ascii-~") == "visible-ascii-~"


@pytest.mark.parametrize(
    "reference",
    [
        RunWorkflowReference("not-a-uuid", str(uuid4()), "a" * 64),
        RunWorkflowReference(str(uuid4()), "not-a-uuid", "a" * 64),
        RunWorkflowReference(str(uuid4()), str(uuid4()), "short"),
    ],
)
def test_reference_ids_fail_closed(reference: RunWorkflowReference) -> None:
    with pytest.raises(ReproducibilityError, match="invalid_workflow_reference"):
        _reference_ids(reference)


def test_reference_verification_recomputes_normalized_spec_digest() -> None:
    spec = {"format": "json", "seed": 7}
    _, digest = normalize_run_spec(spec)
    run = SimpleNamespace(spec=spec, spec_sha256=digest)
    reference = RunWorkflowReference(str(uuid4()), str(uuid4()), digest)
    _verify_reference(run, reference)

    with pytest.raises(ReproducibilityError, match="spec_integrity_violation"):
        _verify_reference(
            run, RunWorkflowReference(reference.organization_id, reference.run_id, "0" * 64)
        )

    run.spec_sha256 = "1" * 64
    with pytest.raises(ReproducibilityError, match="spec_integrity_violation"):
        _verify_reference(
            run, RunWorkflowReference(reference.organization_id, reference.run_id, "1" * 64)
        )

    run.spec = {"secret": "secret://forbidden"}
    with pytest.raises(ReproducibilityError, match="spec_integrity_violation"):
        _verify_reference(
            run, RunWorkflowReference(reference.organization_id, reference.run_id, "1" * 64)
        )


def test_content_digest_reconstruction_rejects_missing_invalid_and_duplicate_names() -> None:
    valid = {"name": "input", "sha256": "a" * 64, "size_bytes": 1, "ignored": True}
    assert _content_digests([valid])[0].name == "input"
    with pytest.raises(ReproducibilityError, match="execution_facts_integrity_violation"):
        _content_digests([{"name": "missing"}])
    with pytest.raises(ReproducibilityError, match="execution_facts_integrity_violation"):
        _content_digests([{**valid, "sha256": "bad"}])
    with pytest.raises(ReproducibilityError, match="execution_facts_integrity_violation"):
        _content_digests([valid, {**valid, "sha256": "b" * 64}])


def test_manifest_artifact_equality_checks_every_integrity_field() -> None:
    artifact = SimpleNamespace(
        storage_key="artifacts/key",
        size_bytes=12,
        media_type="application/json",
        state=ArtifactState.ACTIVE,
    )
    assert _same_manifest_artifact(artifact, key="artifacts/key", size_bytes=12)
    for field, value in (
        ("storage_key", "other"),
        ("size_bytes", 13),
        ("media_type", "text/plain"),
        ("state", ArtifactState.TOMBSTONED),
    ):
        changed = SimpleNamespace(**vars(artifact))
        setattr(changed, field, value)
        assert not _same_manifest_artifact(changed, key="artifacts/key", size_bytes=12)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RunState.QUEUED, ReplayLifecycle.QUEUED),
        (RunState.PROVISIONING, ReplayLifecycle.QUEUED),
        (RunState.DRAFT, ReplayLifecycle.QUEUED),
        (RunState.RUNNING, ReplayLifecycle.RUNNING),
        (RunState.CANCELLING, ReplayLifecycle.RUNNING),
        (RunState.FAILED, ReplayLifecycle.UNAVAILABLE),
    ],
)
def test_replay_status_maps_all_nonterminal_lifecycles(
    state: RunState,
    expected: ReplayLifecycle,
) -> None:
    replay = SimpleNamespace(
        replay_run_id=uuid4(),
        source_run_id=uuid4(),
        source_manifest_artifact_id=uuid4(),
        expected_manifest_sha256="a" * 64,
        observed_manifest_artifact_id=None,
        classification=DeterminismClass.EXACT,
        comparison=None,
    )
    status = _replay_status(replay, SimpleNamespace(state=state))
    assert status.lifecycle is expected
    assert status.comparison is None


def test_replay_status_validates_persisted_comparison() -> None:
    comparison = ReplayComparison(
        classification=DeterminismClass.EXACT,
        verdict=ReplayVerdict.EXACT_MATCH,
        replay_inputs_match=True,
        outputs_match=True,
        differences=(),
    )
    replay = SimpleNamespace(
        replay_run_id=uuid4(),
        source_run_id=uuid4(),
        source_manifest_artifact_id=uuid4(),
        expected_manifest_sha256="a" * 64,
        observed_manifest_artifact_id=uuid4(),
        classification=DeterminismClass.EXACT,
        comparison=comparison.model_dump(mode="json"),
    )
    assert (
        _replay_status(replay, SimpleNamespace(state=RunState.SUCCEEDED)).lifecycle
        is ReplayLifecycle.COMPARED
    )
    replay.comparison = {"verdict": "forged"}
    with pytest.raises(ReproducibilityError, match="replay_comparison_integrity_violation"):
        _replay_status(replay, SimpleNamespace(state=RunState.SUCCEEDED))


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (None, "completed"),
        ({}, "completed"),
        ({"stop_reason": 1}, "completed"),
        ({"stop_reason": "not-real"}, "completed"),
        ({"stop_reason": "cancelled"}, "cancelled"),
    ],
)
def test_stop_reason_has_stable_fallback(summary: dict[str, object] | None, expected: str) -> None:
    assert _stop_reason(summary).value == expected


def test_utc_normalizes_naive_and_aware_datetimes() -> None:
    naive = datetime(2026, 8, 11, 12, 30)  # noqa: DTZ001 - exercising legacy naive values
    assert _utc(naive).tzinfo is UTC
    assert _utc(naive.replace(tzinfo=UTC)) == naive.replace(tzinfo=UTC)


def test_insert_for_rejects_unsupported_database_dialect() -> None:
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    )
    with pytest.raises(RuntimeError, match="PostgreSQL or SQLite"):
        _insert_for(session, SimpleNamespace)


def test_replay_projection_is_strict() -> None:
    with pytest.raises(ValidationError):
        ReplayComparison.model_validate(
            {
                "classification": "exact",
                "verdict": "exact_match",
                "outputs_match": True,
                "differences": [],
                "extra": True,
            }
        )


def test_run_kind_enum_remains_accepted_by_normalized_specs() -> None:
    # Protects the replay contract from accidentally serializing the enum object itself.
    normalized, _ = normalize_run_spec({"kind": RunKind.EXPORT.value})
    assert normalized == {"kind": "export"}
