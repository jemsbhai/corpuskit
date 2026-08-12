"""Durable job transition and run-spec reproducibility tests."""

from __future__ import annotations

import math

import pytest

from corpuskit.domain.errors import InvalidStateTransitionError
from corpuskit.domain.jobs import (
    RunState,
    allowed_transitions,
    canonical_spec_sha256,
    ensure_transition,
    is_terminal,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.DRAFT, RunState.QUEUED),
        (RunState.DRAFT, RunState.CANCELLED),
        (RunState.QUEUED, RunState.PROVISIONING),
        (RunState.QUEUED, RunState.CANCELLING),
        (RunState.QUEUED, RunState.CANCELLED),
        (RunState.QUEUED, RunState.FAILED),
        (RunState.PROVISIONING, RunState.RUNNING),
        (RunState.PROVISIONING, RunState.CANCELLING),
        (RunState.PROVISIONING, RunState.FAILED),
        (RunState.RUNNING, RunState.SUCCEEDED),
        (RunState.RUNNING, RunState.FAILED),
        (RunState.RUNNING, RunState.CANCELLING),
        (RunState.CANCELLING, RunState.CANCELLED),
        (RunState.CANCELLING, RunState.FAILED),
    ],
)
def test_documented_state_transitions_are_allowed(current: RunState, target: RunState) -> None:
    ensure_transition(current, target)
    assert target in allowed_transitions(current)


@pytest.mark.parametrize("terminal", [RunState.CANCELLED, RunState.SUCCEEDED, RunState.FAILED])
@pytest.mark.parametrize("target", list(RunState))
def test_terminal_states_never_transition(terminal: RunState, target: RunState) -> None:
    assert is_terminal(terminal) is True
    with pytest.raises(InvalidStateTransitionError) as error:
        ensure_transition(terminal, target)
    assert error.value.operation == f"{terminal.value}->{target.value}"


def test_nonterminal_states_are_reported() -> None:
    for state in (
        RunState.DRAFT,
        RunState.QUEUED,
        RunState.PROVISIONING,
        RunState.RUNNING,
        RunState.CANCELLING,
    ):
        assert is_terminal(state) is False


def test_spec_hash_is_stable_for_key_order_and_unicode() -> None:
    first = {"language": "fr-fr", "targets": ["ʃ", "ʒ"], "limits": {"sentences": 20}}
    second = {"limits": {"sentences": 20}, "targets": ["ʃ", "ʒ"], "language": "fr-fr"}

    assert canonical_spec_sha256(first) == canonical_spec_sha256(second)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_spec_hash_rejects_non_finite_json(value: float) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_spec_sha256({"weight": value})
