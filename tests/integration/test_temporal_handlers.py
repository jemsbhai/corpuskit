"""Acceptance for every core durable handler against pinned CorpusGen."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from corpuskit.config import Settings
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.selection import CorpusSelectionArtifactV1
from corpuskit.workflows.handlers import build_core_handler_registry
from corpuskit.workflows.policies import SUPPORTED_CORE_KINDS


class MemoryStager:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def stage_model_result(
        self,
        *,
        kind: RunKind,
        payload: bytes,
        content_sha256: str,
    ) -> str:
        assert kind is RunKind.SELECT
        assert hashlib.sha256(payload).hexdigest() == content_sha256
        self.payloads.append(payload)
        return f"staged-artifact://sha256/{content_sha256}"


@pytest.mark.integration
def test_all_six_core_handlers_validate_execute_and_return_bounded_summaries() -> None:
    settings = Settings(environment="test", _env_file=None)
    stager = MemoryStager()
    registry = build_core_handler_registry(settings, stager=stager)
    specs: dict[RunKind, dict[str, Any]] = {
        RunKind.PHONEMIZE: {"language": "en-us", "text": "hello world"},
        RunKind.EVALUATE: {
            "language": "en-us",
            "sentences": ["hello world", "durable speech corpus"],
            "unit": "phoneme",
            "target": {"mode": "derived", "phonemes": []},
        },
        RunKind.DISTRIBUTION: {
            "counts": [{"unit": "h", "count": 2}, {"unit": "w", "count": 1}],
            "target_units": ["h", "w"],
            "reference_distribution": None,
        },
        RunKind.TRAJECTORY: {
            "phoneme_sequences": [["h", "e"], ["w", "e"]],
            "target_units": ["h", "e", "w"],
            "unit": "phoneme",
        },
        RunKind.ERROR_RATES: {
            "references": ["hello world"],
            "hypotheses": ["hello word"],
            "reference_phonemes": [["h", "e", "l", "o"]],
            "hypothesis_phonemes": [["h", "e", "l"]],
            "case_sensitive": False,
        },
        RunKind.SELECT: {
            "candidates": ["hello world", "durable speech corpus"],
            "language": "en-us",
            "unit": "phoneme",
            "target": {"mode": "derived", "phonemes": []},
            "options": {"algorithm": "greedy", "max_sentences": 1},
        },
    }

    summaries = {kind: registry.execute(kind, spec) for kind, spec in specs.items()}

    assert {kind.value for kind in registry.kinds} == SUPPORTED_CORE_KINDS
    assert summaries[RunKind.PHONEMIZE]["item_count"] == 1
    assert summaries[RunKind.EVALUATE]["total_sentences"] == 2
    assert summaries[RunKind.DISTRIBUTION]["target_unit_count"] == 2
    assert summaries[RunKind.TRAJECTORY]["sentence_count"] == 2
    assert summaries[RunKind.ERROR_RATES]["sentence_count"] == 1
    selection_claim = summaries[RunKind.SELECT]
    assert selection_claim["schema_id"] == "corpuskit.corpus-selection.v1"
    assert set(selection_claim) == {
        "artifact_type",
        "contract",
        "media_type",
        "schema_id",
        "size_bytes",
        "staged_artifact_ref",
    }
    assert all(
        len(summary["result_sha256"]) == 64
        for kind, summary in summaries.items()
        if kind is not RunKind.SELECT
    )
    assert "hello world" not in repr(summaries)
    first = CorpusSelectionArtifactV1.model_validate_json(stager.payloads[0], strict=True)
    assert len(first.selected_sentences) == 1
    assert first.selected_sentences == tuple(
        specs[RunKind.SELECT]["candidates"][index] for index in first.selected_indices
    )

    reordered = dict(specs[RunKind.SELECT])
    reordered["candidates"] = list(reversed(reordered["candidates"]))
    reordered_summary = registry.execute(RunKind.SELECT, reordered)
    second = CorpusSelectionArtifactV1.model_validate_json(stager.payloads[1], strict=True)
    assert second.coverage == first.coverage
    assert len(second.selected_indices) == len(first.selected_indices)
    assert (
        reordered_summary["staged_artifact_ref"] != summaries[RunKind.SELECT]["staged_artifact_ref"]
    )
