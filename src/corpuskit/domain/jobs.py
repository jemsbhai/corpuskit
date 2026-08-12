"""Durable run kinds, states, transitions, and reproducible spec hashing."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from corpuskit.domain.errors import InvalidStateTransitionError

MAX_RUN_SPEC_BYTES = 256 * 1024
MAX_RUN_SPEC_DEPTH = 20
MAX_RUN_SPEC_NODES = 20_000
MAX_RESULT_SUMMARY_BYTES = 64 * 1024
MAX_RESULT_SUMMARY_DEPTH = 10
MAX_RESULT_SUMMARY_NODES = 5_000
_KEY_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[^A-Za-z0-9]+")
_REFERENCE_SUFFIXES = frozenset({"id", "ref", "reference"})
_SENSITIVE_COMPONENTS = frozenset({"authorization", "credential", "password", "secret"})
_SENSITIVE_SUFFIXES = (
    ("access", "token"),
    ("api", "key"),
    ("bearer", "token"),
    ("client", "secret"),
    ("private", "key"),
    ("refresh", "token"),
)


class RunKind(StrEnum):
    PHONEMIZE = "phonemize"
    EVALUATE = "evaluate"
    DISTRIBUTION = "distribution"
    TRAJECTORY = "trajectory"
    ERROR_RATES = "error-rates"
    PERPLEXITY = "perplexity"
    SELECT = "select"
    GENERATE_REPOSITORY = "generate-repository"
    GENERATE_LLM = "generate-llm"
    GENERATE_LOCAL = "generate-local"
    BUILD_DATG_INDEX = "build-datg-index"
    GENERATE_DATG = "generate-datg"
    TRAIN_PHON_RL = "train-phon-rl"
    EXPORT = "export"


class RunState(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.DRAFT: frozenset({RunState.QUEUED, RunState.CANCELLED}),
    RunState.QUEUED: frozenset(
        {
            RunState.PROVISIONING,
            RunState.CANCELLING,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.PROVISIONING: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.FAILED}),
    RunState.RUNNING: frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLING}),
    RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.CANCELLED: frozenset(),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
}


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    """Return every directly reachable state from ``state``."""

    return _TRANSITIONS[state]


def ensure_transition(current: RunState, target: RunState) -> None:
    """Fail closed when a durable job attempts an invalid state change."""

    if target not in _TRANSITIONS[current]:
        raise InvalidStateTransitionError(f"{current.value}->{target.value}")


def is_terminal(state: RunState) -> bool:
    """Return whether a run can no longer transition."""

    return not _TRANSITIONS[state]


def canonical_spec_sha256(spec: dict[str, Any]) -> str:
    """Hash a JSON run spec independent of dictionary insertion order."""

    encoded = json.dumps(
        spec,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_run_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return a bounded JSON-only spec and hash while rejecting embedded credentials."""

    nodes = 0

    def inspect(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_RUN_SPEC_NODES or depth > MAX_RUN_SPEC_DEPTH:
            raise ValueError("run spec exceeds structural limits")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("run spec object keys must be strings")
                if _is_sensitive_key(key):
                    raise ValueError(
                        "run spec must use an opaque secret_ref instead of credentials"
                    )
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
        elif value is not None and not isinstance(value, (bool, int, float, str)):
            raise ValueError("run spec values must use JSON-compatible types")

    inspect(spec, 0)
    encoded = json.dumps(
        spec,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_RUN_SPEC_BYTES:
        raise ValueError("run spec exceeds the persisted size limit")
    normalized = json.loads(encoded)
    if not isinstance(normalized, dict):
        raise ValueError("run spec must be a JSON object")
    return normalized, hashlib.sha256(encoded).hexdigest()


def normalize_result_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Return bounded JSON output that is safe for run projections and events."""

    nodes = 0

    def inspect(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_RESULT_SUMMARY_NODES or depth > MAX_RESULT_SUMMARY_DEPTH:
            raise ValueError("result summary exceeds structural limits")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("result summary object keys must be strings")
                if _is_sensitive_key(key):
                    raise ValueError("result summary must not contain credentials")
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
        elif value is not None and not isinstance(value, (bool, int, float, str)):
            raise ValueError("result summary values must use JSON-compatible types")

    inspect(summary, 0)
    encoded = json.dumps(
        summary,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_RESULT_SUMMARY_BYTES:
        raise ValueError("result summary exceeds the persisted size limit")
    normalized = json.loads(encoded)
    if not isinstance(normalized, dict):
        raise ValueError("result summary must be a JSON object")
    return normalized


def _is_sensitive_key(key: str) -> bool:
    """Identify credential-bearing fields while permitting explicit opaque references."""

    components = tuple(part.casefold() for part in _KEY_BOUNDARY.split(key) if part)
    if not components:
        return False
    if components[-1] in _REFERENCE_SUFFIXES:
        return False
    if any(component in _SENSITIVE_COMPONENTS for component in components):
        return True
    if components[-1] == "token":
        return True
    compact = "".join(components)
    return any("".join(suffix) in compact for suffix in _SENSITIVE_SUFFIXES)
