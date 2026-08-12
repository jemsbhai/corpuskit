"""Immutable public-Hub acceptance for the real CorpusGen repository loader."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from corpuskit.domain.generation import (
    GenerationExecutionMode,
    GenerationStoppingCriteria,
    GenerationTarget,
    HuggingFaceRepository,
    HuggingFaceRepositorySpec,
    RepositoryGenerationRequest,
)

_DATASET = "lhoestq/demo1"
_REVISION = "87ecf163bedca9d80598b528940a9c4f99e14c11"
_EXPECTED_ACCEPTED_TEXT_SHA256 = "fbc740541bedc12708ae4b09ce968a45e1e7988cda4ec3cf4bde7f0a457f0394"


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("CORPUSKIT_RUN_HUGGINGFACE_ACCEPTANCE") != "1",
    reason="set CORPUSKIT_RUN_HUGGINGFACE_ACCEPTANCE=1 for immutable Hub acceptance",
)
def test_real_immutable_huggingface_dataset_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Download the exact public revision and cross the real datasets/eSpeak boundary."""

    cache_root = tmp_path / "huggingface"
    monkeypatch.setenv("HF_HOME", str(cache_root))
    monkeypatch.setenv("HF_DATASETS_CACHE", str(cache_root / "datasets"))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache_root / "hub"))
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")

    from corpuskit.adapters.corpusgen.generation import CorpusgenGenerationAdapter

    source = HuggingFaceRepository(
        spec=HuggingFaceRepositorySpec(
            dataset=_DATASET,
            config="default",
            split="train",
            text_column="review",
            revision=_REVISION,
            language="en-us",
            max_samples=2,
        )
    )

    result = CorpusgenGenerationAdapter().run_repository(
        RepositoryGenerationRequest(
            source=source,
            target=GenerationTarget(phonemes=("\u0261",)),
            stopping=GenerationStoppingCriteria(
                max_sentences=1,
                max_iterations=2,
                timeout_seconds=10,
            ),
        ),
        execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
    )

    assert cache_root.is_dir()
    assert result.backend == "repository"
    assert result.coverage == 1.0
    assert result.covered_units == ("\u0261",)
    assert result.missing_units == ()
    assert result.stop_reason.value == "target_coverage"
    assert len(result.accepted) == 1
    assert result.accepted[0].source_id == "hf:d405d130fda72fd21986:0"
    assert result.accepted[0].phonemes
    assert (
        hashlib.sha256(result.accepted[0].text.encode("utf-8")).hexdigest()
        == _EXPECTED_ACCEPTED_TEXT_SHA256
    )
