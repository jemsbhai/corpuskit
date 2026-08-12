"""Trusted execution-fact and replay projection contract tests."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corpuskit.domain.artifacts import ContentDigest, DeterminismClass
from corpuskit.domain.reproducibility import TrustedExecutionFacts


def _facts(**updates: object) -> TrustedExecutionFacts:
    values: dict[str, object] = {
        "corpuskit_version": "0.1.0a1",
        "corpusgen_version": "0.1.7",
        "worker_profile": "batch-cpu",
        "worker_image_digest": f"sha256:{'a' * 64}",
        "worker_policy": ContentDigest(
            name="worker-policy",
            sha256="b" * 64,
            size_bytes=128,
        ),
        "determinism": DeterminismClass.EXACT,
    }
    values.update(updates)
    return TrustedExecutionFacts.model_validate(values)


def test_execution_facts_are_canonical_and_survive_strict_json_roundtrip() -> None:
    facts = _facts(
        input_attestations=(ContentDigest(name="cache-snapshot", sha256="c" * 64, size_bytes=10),)
    )
    decoded = json.loads(facts.canonical_bytes())

    assert (
        facts.canonical_bytes()
        == json.dumps(
            decoded,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert len(facts.sha256) == 64
    assert TrustedExecutionFacts.model_validate_json(facts.canonical_bytes(), strict=True) == facts


@pytest.mark.parametrize(
    "updates",
    [
        {"worker_policy": ContentDigest(name="other", sha256="b" * 64, size_bytes=1)},
        {"input_attestations": (ContentDigest(name="run-spec", sha256="b" * 64, size_bytes=1),)},
        {
            "input_attestations": (
                ContentDigest(name="same", sha256="b" * 64, size_bytes=1),
                ContentDigest(name="same", sha256="c" * 64, size_bytes=2),
            )
        },
    ],
)
def test_execution_facts_reject_ambiguous_semantic_names(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _facts(**updates)


def test_execution_facts_reject_duplicate_artifact_ids_and_authority_or_secret_extras() -> None:
    artifact_id = uuid4()
    with pytest.raises(ValidationError, match="artifact IDs"):
        _facts(input_artifact_ids=(artifact_id, artifact_id))
    with pytest.raises(ValidationError, match="extra"):
        TrustedExecutionFacts.model_validate(
            {
                **_facts().model_dump(),
                "organization_id": str(uuid4()),
                "api_key": "must-not-persist",
            }
        )
