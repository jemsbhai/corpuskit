"""Coverage-trajectory parity with real selection and generation results."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import BaseModel

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.adapters.corpusgen.analysis import CorpusgenAnalysisAdapter
from corpuskit.adapters.corpusgen.generation import CorpusgenGenerationAdapter
from corpuskit.domain import (
    CorpusSelection,
    CoverageTrajectoryRequest,
    CoverageUnit,
    EvaluationTarget,
    EvaluationTargetMode,
    SelectionAlgorithm,
    SelectionOptions,
    SelectionRequest,
)
from corpuskit.domain.generation import (
    GenerationExecutionMode,
    GenerationStoppingCriteria,
    GenerationTarget,
    PrephonemizedRepository,
    RepositoryCandidate,
    RepositoryGenerationRequest,
    RepositoryGenerationResult,
)
from corpuskit.persistence.artifact_store import InMemoryObjectStore

pytestmark = pytest.mark.integration


async def _persisted_roundtrip[ResultModel: BaseModel](
    value: ResultModel,
    model: type[ResultModel],
    key: str,
) -> ResultModel:
    content = value.model_dump_json().encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    store = InMemoryObjectStore()

    created = await store.put(
        key=f"artifacts/v1/trajectory/{key}/{digest}",
        content=content,
        sha256=digest,
        media_type="application/json",
    )
    stream = await store.open(created.descriptor.key, chunk_bytes=17)
    restored = b"".join([chunk async for chunk in stream.chunks])

    assert hashlib.sha256(restored).hexdigest() == created.descriptor.sha256
    return model.model_validate_json(restored, strict=True)


@pytest.mark.asyncio
async def test_selection_result_reconstructs_the_same_ordered_coverage_trajectory() -> None:
    targets = ("p", "b", "t", "k")
    adapter = CorpusgenAdapter()
    selection = adapter.select(
        SelectionRequest(
            candidates=("Pea.", "Bee.", "Tea.", "Key.", "Pea bee tea key."),
            language="en-us",
            target=EvaluationTarget(
                mode=EvaluationTargetMode.EXPLICIT,
                phonemes=targets,
            ),
            options=SelectionOptions(
                algorithm=SelectionAlgorithm.GREEDY,
                max_sentences=2,
            ),
        )
    )
    persisted = await _persisted_roundtrip(selection, CorpusSelection, "selection")
    transcriptions = adapter.phonemize_batch(persisted.selected_sentences, language="en-us")
    trajectory = CorpusgenAnalysisAdapter().trajectory(
        CoverageTrajectoryRequest(
            phoneme_sequences=tuple(item.phonemes for item in transcriptions),
            target_units=targets,
            unit=CoverageUnit.PHONEME,
        )
    )

    assert trajectory.coverages[-1] == persisted.coverage == 1.0
    assert trajectory.snapshots[-1].covered_count == len(persisted.covered_units)
    assert set().union(*(set(item.new_units) for item in trajectory.snapshots)) == set(
        persisted.covered_units
    )


@pytest.mark.asyncio
async def test_generation_result_reconstructs_the_same_ordered_coverage_trajectory() -> None:
    request = RepositoryGenerationRequest(
        source=PrephonemizedRepository(
            entries=(
                RepositoryCandidate(source_id="row-p", text="Pat.", phonemes=("p", "a", "t")),
                RepositoryCandidate(source_id="row-b", text="Bat.", phonemes=("b", "a", "t")),
            )
        ),
        target=GenerationTarget(phonemes=("p", "b")),
        stopping=GenerationStoppingCriteria(
            max_sentences=2,
            max_iterations=5,
            timeout_seconds=2.0,
        ),
    )
    generated = CorpusgenGenerationAdapter().run_repository(
        request,
        execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
    )
    persisted = await _persisted_roundtrip(
        generated,
        RepositoryGenerationResult,
        "generation",
    )
    trajectory = CorpusgenAnalysisAdapter().trajectory(
        CoverageTrajectoryRequest(
            phoneme_sequences=tuple(item.phonemes for item in persisted.accepted),
            target_units=request.target.phonemes,
            unit=request.target.unit,
        )
    )

    assert trajectory.coverages[-1] == persisted.coverage == 1.0
    assert trajectory.gains == tuple(item.coverage_gain for item in persisted.accepted)
    assert set().union(*(set(item.new_units) for item in trajectory.snapshots)) == set(
        persisted.covered_units
    )
