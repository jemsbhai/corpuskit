"""Linux acceptance for native-script G2P, PHOIBLE, evaluation, and selection."""

from __future__ import annotations

import sys

import pytest

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.domain.multilingual_demo import DemoCaseId, MultilingualDemoRequest, WritingSystem
from corpuskit.services.multilingual_demo import MultilingualDemoService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "The supported Windows eSpeak package does not accept these native scripts through "
            "its narrow-character API; deployed Linux images are the acceptance runtime."
        ),
    ),
]


def test_curated_suite_exercises_five_writing_system_and_inventory_cases() -> None:
    result = MultilingualDemoService(CorpusgenAdapter()).run(MultilingualDemoRequest())

    assert result.passed is True
    assert result.passed_count == result.case_count == 5
    assert {case.case for case in result.cases} == set(DemoCaseId)
    assert {case.writing_system for case in result.cases} == set(WritingSystem)
    for case in result.cases:
        assert case.inventory is not None
        assert case.evaluation is not None
        assert case.selection is not None
        assert case.transcriptions
        assert case.unique_phonemes
        assert case.evaluation.coverage == 1.0
        assert case.selection.coverage == 1.0
        assert case.failure_code is None
    tonal = {
        case.case: case.inventory.tone_count for case in result.cases if case.inventory is not None
    }
    assert tonal[DemoCaseId.CJK_MANDARIN] > 0
    assert tonal[DemoCaseId.TONAL_VIETNAMESE] > 0
