"""Artifact naming and reproducibility-manifest contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from corpuskit.domain.artifacts import (
    ArtifactKind,
    ContentDigest,
    DatasetProvenance,
    DeterminismClass,
    ModelProvenance,
    PhoibleProvenance,
    ReplayVerdict,
    RunManifest,
    StagedArtifactResult,
    StopReason,
    artifact_storage_key,
    compare_replay,
    content_disposition,
    normalize_media_type,
    safe_download_filename,
    staged_artifact_reference,
    staged_artifact_storage_key,
)
from corpuskit.domain.jobs import RunKind

PROJECT_ID = UUID("10000000-0000-4000-8000-000000000001")
RUN_ID = UUID("20000000-0000-4000-8000-000000000002")
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
STARTED = datetime(2026, 8, 11, 12, tzinfo=UTC)


def _manifest(**updates: object) -> RunManifest:
    values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "run_id": RUN_ID,
        "operation": RunKind.SELECT,
        "corpuskit_version": "0.1.0a1",
        "corpusgen_version": "0.1.7",
        "espeak_version": "eSpeak NG 1.52.0",
        "phoible": PhoibleProvenance(revision="b92abff", sha256=DIGEST_A),
        "model": ModelProvenance(
            backend="local",
            identifier="org/model",
            revision="0123456789abcdef",
            artifact_sha256=DIGEST_B,
        ),
        "dataset": DatasetProvenance(
            name="demo-dataset",
            config="default",
            split="train",
            revision="v1",
            selector_sha256=DIGEST_A,
        ),
        "worker_image_digest": f"sha256:{DIGEST_B}",
        "runtime_profile": "batch-cpu",
        "language": "en-us",
        "target_source": "phoible",
        "unit": "phoneme",
        "parameters": {"algorithm": "greedy", "nested": {"weight": 1}},
        "seed": 42,
        "input_digests": (ContentDigest(name="candidates", sha256=DIGEST_A, size_bytes=12),),
        "output_digests": (ContentDigest(name="selected", sha256=DIGEST_B, size_bytes=8),),
        "started_at": STARTED,
        "finished_at": STARTED + timedelta(seconds=2),
        "stop_reason": StopReason.COMPLETED,
        "determinism": DeterminismClass.EXACT,
    }
    values.update(updates)
    return RunManifest.model_validate(values)


def test_manifest_matches_committed_canonical_golden() -> None:
    manifest = _manifest()
    golden = Path(__file__).parents[1] / "fixtures" / "run_manifest_v1.json"

    assert manifest.canonical_bytes() == golden.read_bytes().rstrip(b"\r\n")
    assert manifest.sha256 == "87a33af0a45b3240395de4f69638127f7df80946fb97addc3a7b6fa0a068e31a"


@pytest.mark.parametrize(
    "parameters",
    [
        {"api_key": "must-not-persist"},
        {"nested": {"refreshToken": "must-not-persist"}},
        {"value": float("nan")},
        {"value": float("inf")},
    ],
)
def test_manifest_rejects_secrets_and_nonfinite_parameters(
    parameters: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _manifest(parameters=parameters)


def test_manifest_rejects_invalid_time_order_and_duplicate_digest_names() -> None:
    with pytest.raises(ValidationError, match="finish"):
        _manifest(finished_at=STARTED - timedelta(seconds=1))
    duplicate = ContentDigest(name="same", sha256=DIGEST_A, size_bytes=1)
    with pytest.raises(ValidationError, match="unique"):
        _manifest(input_digests=(duplicate, duplicate))
    with pytest.raises(ValidationError, match="output digest names"):
        _manifest(output_digests=(duplicate, duplicate))
    with pytest.raises(ValidationError, match="offset"):
        _manifest(started_at=STARTED.replace(tzinfo=None))


def test_provenance_is_required_only_when_the_workflow_uses_it() -> None:
    exported = _manifest(
        operation=RunKind.EXPORT,
        espeak_version=None,
        phoible=None,
        dataset=None,
        model=None,
        target_source="none",
    )
    assert exported.espeak_version is None
    assert _manifest(espeak_version=None).espeak_version is None
    with pytest.raises(ValidationError, match="PHOIBLE"):
        _manifest(phoible=None)
    hf_selector = {
        "dataset": "demo/dataset",
        "config": "default",
        "split": "train",
        "text_column": "text",
        "revision": "a" * 40,
        "language": "en-us",
        "max_samples": 10,
        "trust_remote_code": False,
    }
    with pytest.raises(ValidationError, match="dataset provenance"):
        _manifest(
            operation=RunKind.GENERATE_REPOSITORY,
            parameters={"source": {"kind": "hugging_face", "spec": hf_selector}},
            dataset=None,
        )
    assert (
        _manifest(
            operation=RunKind.GENERATE_REPOSITORY,
            parameters={"source": {"kind": "hugging_face", "spec": hf_selector}},
            dataset=DatasetProvenance(
                name="demo/dataset",
                config="default",
                split="train",
                revision="a" * 40,
                selector_sha256=(
                    "ece309d6ab6f2cec7833e1b751bfeace5bbbcadd8c41238e94d1c94b88671d59"
                ),
            ),
        ).dataset
        is not None
    )
    with pytest.raises(ValidationError, match="model"):
        _manifest(operation=RunKind.GENERATE_LOCAL, model=None)


def test_exact_replay_requires_identical_recipe_and_outputs() -> None:
    expected = _manifest()

    matched = compare_replay(expected, _manifest(run_id=UUID(int=3)))
    changed_output = compare_replay(
        expected,
        _manifest(output_digests=(ContentDigest(name="selected", sha256=DIGEST_A, size_bytes=8),)),
    )
    changed_recipe = compare_replay(expected, _manifest(parameters={"algorithm": "stochastic"}))

    assert matched.verdict is ReplayVerdict.EXACT_MATCH
    assert changed_output.verdict is ReplayVerdict.MISMATCH
    assert changed_recipe.verdict is ReplayVerdict.MISMATCH
    assert changed_recipe.differences == ("parameters",)


def test_best_effort_and_nonreproducible_replays_are_disclosed() -> None:
    expected = _manifest(determinism=DeterminismClass.BEST_EFFORT)
    same = compare_replay(expected, _manifest(determinism=DeterminismClass.BEST_EFFORT))
    diverged = compare_replay(
        expected,
        _manifest(
            determinism=DeterminismClass.BEST_EFFORT,
            output_digests=(ContentDigest(name="selected", sha256=DIGEST_A, size_bytes=8),),
        ),
    )
    unavailable = compare_replay(
        _manifest(determinism=DeterminismClass.NONREPRODUCIBLE),
        _manifest(determinism=DeterminismClass.NONREPRODUCIBLE),
    )
    incompatible = compare_replay(
        expected,
        _manifest(
            determinism=DeterminismClass.NONREPRODUCIBLE,
            parameters={"algorithm": "changed"},
        ),
    )

    assert same.verdict is ReplayVerdict.BEST_EFFORT_MATCH
    assert diverged.verdict is ReplayVerdict.BEST_EFFORT_DIVERGENCE
    assert unavailable.verdict is ReplayVerdict.NONREPRODUCIBLE
    assert incompatible.verdict is ReplayVerdict.MISMATCH


@pytest.mark.parametrize("value", ["../secret.txt", "folder/file.txt", "bad\x00.txt", ".."])
def test_unsafe_filenames_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        safe_download_filename(value)


def test_download_filename_is_formula_safe_and_header_safe() -> None:
    assert safe_download_filename("=SUM(A1).csv") == "_SUM-A1-.csv"
    assert safe_download_filename("résumé output.json") == "resume-output.json"
    assert content_disposition("report.json") == (
        "attachment; filename=\"report.json\"; filename*=UTF-8''report.json"
    )
    assert safe_download_filename("\N{SNOWMAN}") == "artifact.bin"


def test_media_type_and_storage_key_are_allowlisted_and_filename_free() -> None:
    key = artifact_storage_key(
        organization_id=UUID(int=1),
        project_id=PROJECT_ID,
        run_id=RUN_ID,
        kind=ArtifactKind.RUN_MANIFEST,
        sha256=DIGEST_A,
    )

    assert normalize_media_type("Application/JSON; charset=utf-8") == "application/json"
    assert key.endswith(f"run-manifest/aa/{DIGEST_A}")
    assert "report" not in key
    with pytest.raises(ValueError, match="unsupported"):
        normalize_media_type("text/html")
    with pytest.raises(ValueError, match="digest"):
        artifact_storage_key(
            organization_id=UUID(int=1),
            project_id=PROJECT_ID,
            run_id=None,
            kind=ArtifactKind.EXPORT,
            sha256="not-a-digest",
        )


def test_staged_result_envelope_is_authority_free_and_content_addressed() -> None:
    reference = staged_artifact_reference(DIGEST_A)
    claim = StagedArtifactResult(
        staged_artifact_ref=reference,
        schema_id="corpuskit.local-generation-result.v1",
        artifact_type="run-result",
        media_type="application/json",
        size_bytes=10,
    )

    assert claim.sha256 == DIGEST_A
    assert staged_artifact_storage_key(DIGEST_A).endswith(f"/aa/{DIGEST_A}")
    assert not {"organization_id", "project_id", "run_id", "created_by"} & set(claim.model_dump())
    with pytest.raises(ValueError, match="digest"):
        staged_artifact_reference("../tenant")
    with pytest.raises(ValueError, match="digest"):
        staged_artifact_storage_key("../tenant")
    with pytest.raises(ValidationError):
        StagedArtifactResult.model_validate(
            {
                **claim.model_dump(mode="json"),
                "organization_id": str(UUID(int=1)),
            }
        )
