"""Golden vectors, roundtrips, readability, and scoring atomicity."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.adapters.corpusgen.scoring import CorpusgenScoringAdapter
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    CompositeScoringRequest,
    GenerationScoringOptions,
    GenerationTarget,
    NgramConstraintTrainingRequest,
    NgramScorerMode,
    NgramScorerTrainingRequest,
    PhonemeSequence,
    PhonotacticArtifact,
    PhonotacticArtifactType,
    PhonotacticScoreRequest,
    ReadabilityRange,
    ReadabilityRequest,
    ReadabilityStatus,
    RepositoryCandidate,
    ScoreWeights,
    ScoringState,
)


def _sequences() -> tuple[PhonemeSequence, ...]:
    return (
        PhonemeSequence(phonemes=("p", "a")),
        PhonemeSequence(phonemes=("p", "a")),
        PhonemeSequence(phonemes=("b", "a")),
    )


def test_composite_golden_vector_and_preview_are_non_mutating() -> None:
    state = ScoringState(
        covered_sequences=(PhonemeSequence(phonemes=("k",)),),
        accepted_source_ids=("earlier",),
    )
    request = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p", "b", "k")),
        candidates=(
            RepositoryCandidate(
                source_id="p-row",
                text="Reading simple stories can help students learn new ideas.",
                phonemes=("p",),
            ),
            RepositoryCandidate(
                source_id="b-row",
                text="The quick brown fox jumps over the lazy dog.",
                phonemes=("b",),
            ),
        ),
        state=state,
        options=GenerationScoringOptions(
            weights=ScoreWeights(coverage=2.0, readability=0.5),
            readability_target=ReadabilityRange(minimum=60, maximum=80),
        ),
    )

    result = CorpusgenScoringAdapter().composite(request)

    assert [item.source_id for item in result.ranked] == ["p-row", "b-row"]
    assert result.ranked[0].readability_score == 1.0
    assert result.ranked[0].composite_score == 2.5
    assert result.ranked[1].readability_score == pytest.approx(0.285)
    assert result.ranked[1].composite_score == pytest.approx(2.1425)
    assert result.committed is None
    assert result.state_before == result.state_after == state
    assert result.covered_units_before == result.covered_units_after == ("k",)


def test_atomic_commit_matches_preview_and_returns_next_immutable_state() -> None:
    candidate = RepositoryCandidate(
        source_id="p-row",
        text="Pat.",
        phonemes=("p",),
    )
    result = CorpusgenScoringAdapter().composite(
        CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p", "b")),
            candidates=(candidate,),
            commit_source_id=candidate.source_id,
        )
    )

    assert result.committed == result.ranked[0]
    assert result.covered_units_before == ()
    assert result.covered_units_after == ("p",)
    assert result.state_before.accepted_source_ids == ()
    assert result.state_after.accepted_source_ids == ("p-row",)
    assert result.state_after.covered_sequences[0].phonemes == ("p",)


def test_duplicate_acceptance_and_ambiguous_candidates_are_rejected() -> None:
    candidate = RepositoryCandidate(source_id="same", text="Pat.", phonemes=("p",))
    with pytest.raises(ValidationError):
        CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p",)),
            candidates=(candidate, candidate),
        )
    with pytest.raises(ValidationError):
        CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p",)),
            candidates=(candidate,),
            state=ScoringState(accepted_source_ids=("same",)),
            commit_source_id="same",
        )
    with pytest.raises(ValidationError):
        CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p",)),
            candidates=(candidate,),
            commit_source_id="missing",
        )


def test_commit_failure_does_not_mutate_caller_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.generate.phon_ctg.scorer import PhoneticScorer

    state = ScoringState()

    def fail_commit(*_: object, **__: object) -> object:
        raise RuntimeError("private failure")

    monkeypatch.setattr(PhoneticScorer, "score_and_commit", fail_commit)
    request = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p",)),
        candidates=(RepositoryCandidate(source_id="p", text="Pat.", phonemes=("p",)),),
        state=state,
        commit_source_id="p",
    )

    with pytest.raises(EngineUnavailableError) as caught:
        CorpusgenScoringAdapter().composite(request)

    assert request.state == state
    assert request.state.covered_sequences == ()
    assert "private failure" not in str(caught.value)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (NgramScorerMode.INVENTORY_DERIVED, (8 / 15, 8 / 15)),
        (NgramScorerMode.CORPUS_TRAINED, (2 / 3, 0.5)),
    ],
)
def test_ngram_scorer_artifact_roundtrip_and_golden_scores(
    mode: NgramScorerMode,
    expected: tuple[float, float],
) -> None:
    adapter = CorpusgenScoringAdapter()
    request = (
        NgramScorerTrainingRequest(mode=mode, n=2, phonemes=("p", "a", "b"))
        if mode is NgramScorerMode.INVENTORY_DERIVED
        else NgramScorerTrainingRequest(mode=mode, n=2, sequences=_sequences())
    )
    artifact = adapter.train_ngram_scorer(request)
    restored = PhonotacticArtifact.model_validate_json(artifact.model_dump_json())
    scores = adapter.score_phonotactics(
        PhonotacticScoreRequest(
            artifact=restored,
            sequences=(
                PhonemeSequence(phonemes=("p", "a")),
                PhonemeSequence(phonemes=("a", "p")),
            ),
        )
    )

    assert restored.artifact_type is PhonotacticArtifactType.NGRAM_SCORER
    assert scores.scores == pytest.approx(expected)
    json.loads(restored.model_dump_json())


def test_ngram_constraint_roundtrip_golden_scores() -> None:
    adapter = CorpusgenScoringAdapter()
    artifact = adapter.train_ngram_constraint(
        NgramConstraintTrainingRequest(
            order=2,
            smoothing=0.01,
            sequences=_sequences(),
        )
    )
    restored = PhonotacticArtifact.model_validate(artifact.model_dump(mode="json"))
    result = adapter.score_phonotactics(
        PhonotacticScoreRequest(
            artifact=restored,
            sequences=(
                PhonemeSequence(phonemes=("p", "a")),
                PhonemeSequence(phonemes=("a", "p")),
            ),
        )
    )

    assert artifact.artifact_type is PhonotacticArtifactType.NGRAM_CONSTRAINT
    assert result.scores == pytest.approx((0.4625773266221808, 0.003729005065959349))


@pytest.mark.integration
def test_ngram_constraint_can_train_from_raw_text_with_real_espeak() -> None:
    adapter = CorpusgenScoringAdapter()
    artifact = adapter.train_ngram_constraint(
        NgramConstraintTrainingRequest(
            texts=("Hello world.", "A clear voice."),
            language="en-us",
        )
    )

    assert artifact.payload["is_fitted"] is True
    assert artifact.payload["vocabulary"]


def test_invalid_artifact_payload_is_sanitized() -> None:
    artifact = PhonotacticArtifact.build(
        PhonotacticArtifactType.NGRAM_SCORER,
        {"unexpected": "private-path"},
    )
    request = PhonotacticScoreRequest(
        artifact=artifact,
        sequences=(PhonemeSequence(phonemes=("p", "a")),),
    )

    with pytest.raises(EngineContractError) as caught:
        CorpusgenScoringAdapter().score_phonotactics(request)

    assert "private-path" not in str(caught.value)


def test_readability_exposes_unavailable_instead_of_a_false_low_score() -> None:
    result = CorpusgenScoringAdapter().readability(
        ReadabilityRequest(
            texts=("Reading simple stories can help students learn new ideas.", "你好世界", ""),
            filter_range=ReadabilityRange(minimum=60, maximum=80),
        )
    )

    assert result.results[0].status is ReadabilityStatus.AVAILABLE
    assert result.results[0].flesch_reading_ease == pytest.approx(66.1)
    assert result.results[0].accepted_by_filter is True
    for unavailable in result.results[1:]:
        assert unavailable.status is ReadabilityStatus.UNAVAILABLE
        assert unavailable.flesch_reading_ease is None
        assert unavailable.score is None
        assert unavailable.accepted_by_filter is None


def test_readability_filter_rejection_and_target_range_validation() -> None:
    result = CorpusgenScoringAdapter().readability(
        ReadabilityRequest(
            texts=("The cat sat on the mat.",),
            target_range=ReadabilityRange(minimum=60, maximum=80),
            filter_range=ReadabilityRange(minimum=60, maximum=80),
        )
    )
    assert result.results[0].status is ReadabilityStatus.AVAILABLE
    assert result.results[0].score == 0.0
    assert result.results[0].accepted_by_filter is False

    with pytest.raises(ValidationError):
        ReadabilityRange(minimum=90, maximum=50)


def test_scoring_options_require_artifacts_and_supported_components() -> None:
    with pytest.raises(ValidationError):
        GenerationScoringOptions(weights=ScoreWeights(coverage=0, phonotactic=1))
    with pytest.raises(ValidationError):
        GenerationScoringOptions(
            weights=ScoreWeights(),
            readability_target=ReadabilityRange(minimum=40, maximum=60),
        )
    with pytest.raises(ValidationError):
        ScoreWeights(coverage=0, phonotactic=0, readability=0)
    assert ScoreWeights(coverage=0, fluency=1).fluency == 1
    with pytest.raises(ValidationError):
        NgramScorerTrainingRequest(
            mode=NgramScorerMode.INVENTORY_DERIVED,
            phonemes=("p", "p", "a"),
        )


def test_composite_fluency_is_injected_worker_only_and_changes_ranking() -> None:
    request = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p", "b")),
        candidates=(
            RepositoryCandidate(source_id="p", text="less fluent", phonemes=("p",)),
            RepositoryCandidate(source_id="b", text="more fluent", phonemes=("b",)),
        ),
        options=GenerationScoringOptions(weights=ScoreWeights(coverage=1, fluency=2)),
    )

    with pytest.raises(InvalidRequestError) as denied:
        CorpusgenScoringAdapter().composite(request)
    assert denied.value.operation == "scoring.composite.fluency_worker_only"

    result = CorpusgenScoringAdapter(
        authorized_fluency_scorer=lambda text: 0.9 if text == "more fluent" else 0.2
    ).composite(request)

    assert [item.source_id for item in result.ranked] == ["b", "p"]
    assert [item.fluency_score for item in result.ranked] == [0.9, 0.2]
    assert result.ranked[0].composite_score == pytest.approx(2.8)
    assert result.ranked[1].composite_score == pytest.approx(1.4)


def test_zero_fluency_weight_never_invokes_an_injected_model_scorer() -> None:
    def unexpected(_: str | None) -> float:
        raise AssertionError("model scorer must stay cold")

    result = CorpusgenScoringAdapter(authorized_fluency_scorer=unexpected).composite(
        CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p",)),
            candidates=(RepositoryCandidate(source_id="p", text="Pat.", phonemes=("p",)),),
        )
    )

    assert result.ranked[0].fluency_score == 0


def test_composite_uses_restored_phonotactic_artifact() -> None:
    adapter = CorpusgenScoringAdapter()
    artifact = adapter.train_ngram_scorer(
        NgramScorerTrainingRequest(
            mode=NgramScorerMode.CORPUS_TRAINED,
            sequences=_sequences(),
        )
    )
    result = adapter.composite(
        CompositeScoringRequest(
            target=GenerationTarget(phonemes=("p",)),
            candidates=(
                RepositoryCandidate(
                    source_id="p",
                    text="Pat.",
                    phonemes=("p", "a"),
                ),
            ),
            options=GenerationScoringOptions(
                weights=ScoreWeights(coverage=1, phonotactic=1),
                phonotactic_artifact=artifact,
            ),
        )
    )
    assert result.ranked[0].phonotactic_score == pytest.approx(2 / 3)
    assert result.ranked[0].composite_score == pytest.approx(5 / 3)


def test_composite_rejects_commit_or_rank_contract_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.generate.phon_ctg.scorer import PhoneticScorer

    original_commit = PhoneticScorer.score_and_commit

    def changed_commit(self: Any, *args: Any, **kwargs: Any) -> object:
        result = original_commit(self, *args, **kwargs)
        return replace(result, composite_score=result.composite_score + 1)

    monkeypatch.setattr(PhoneticScorer, "score_and_commit", changed_commit)
    request = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p",)),
        candidates=(RepositoryCandidate(source_id="p", text="Pat.", phonemes=("p",)),),
        commit_source_id="p",
    )
    with pytest.raises(EngineContractError):
        CorpusgenScoringAdapter().composite(request)

    monkeypatch.undo()
    original_rank = PhoneticScorer.rank

    def changed_rank(self: Any, *args: Any, **kwargs: Any) -> list[object]:
        result = original_rank(self, *args, **kwargs)
        return [replace(result[0], text="not-a-candidate")]

    monkeypatch.setattr(PhoneticScorer, "rank", changed_rank)
    with pytest.raises(EngineContractError):
        CorpusgenScoringAdapter().composite(request.model_copy(update={"commit_source_id": None}))


def test_composite_rejects_nonfinite_engine_score(monkeypatch: pytest.MonkeyPatch) -> None:
    from corpusgen.generate.phon_ctg.scorer import PhoneticScorer

    original_rank = PhoneticScorer.rank

    def invalid_rank(self: Any, *args: Any, **kwargs: Any) -> list[object]:
        result = original_rank(self, *args, **kwargs)
        return [replace(result[0], composite_score=float("nan"))]

    monkeypatch.setattr(PhoneticScorer, "rank", invalid_rank)
    request = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p",)),
        candidates=(RepositoryCandidate(source_id="p", text="Pat.", phonemes=("p",)),),
    )
    with pytest.raises(EngineContractError):
        CorpusgenScoringAdapter().composite(request)


class InvalidSavingScorer:
    def save(self, path: str | Path) -> None:
        Path(path).write_text("[]", encoding="utf-8")

    def __call__(self, phonemes: list[str]) -> float:
        del phonemes
        return 0.0


def test_ngram_serializer_rejects_non_object_payload() -> None:
    with pytest.raises(EngineContractError):
        CorpusgenScoringAdapter._save_ngram_scorer(InvalidSavingScorer())


@pytest.mark.parametrize(
    ("error_factory", "expected_type"),
    [
        (lambda: InvalidRequestError("safe.operation"), InvalidRequestError),
        (lambda: ValueError("private"), InvalidRequestError),
        (lambda: ImportError("private"), DependencyUnavailableError),
        (lambda: TypeError("private"), EngineContractError),
        (lambda: RuntimeError("private"), EngineUnavailableError),
        (lambda: LookupError("private"), EngineUnavailableError),
    ],
)
@pytest.mark.parametrize(
    "operation",
    ["scorer_train", "constraint_train", "artifact_score", "readability"],
)
def test_all_scoring_operations_sanitize_engine_error_categories(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error_factory: Any,
    expected_type: type[Exception],
) -> None:
    adapter = CorpusgenScoringAdapter()
    error = error_factory()

    def fail(*_: object, **__: object) -> object:
        raise error

    if operation == "scorer_train":
        monkeypatch.setattr(adapter, "_save_ngram_scorer", fail)
        invoke = partial(
            adapter.train_ngram_scorer,
            NgramScorerTrainingRequest(
                mode=NgramScorerMode.INVENTORY_DERIVED,
                phonemes=("p", "a"),
            ),
        )
    elif operation == "constraint_train":
        from corpusgen.generate.phon_ctg.constraints import NgramPhonotacticModel

        monkeypatch.setattr(NgramPhonotacticModel, "fit", fail)
        invoke = partial(
            adapter.train_ngram_constraint,
            NgramConstraintTrainingRequest(sequences=_sequences()),
        )
    elif operation == "artifact_score":
        monkeypatch.setattr(adapter, "scorer_callable", fail)
        artifact = PhonotacticArtifact.build(
            PhonotacticArtifactType.NGRAM_SCORER,
            {"phonemes": ["p", "a"]},
        )
        invoke = partial(
            adapter.score_phonotactics,
            PhonotacticScoreRequest(
                artifact=artifact,
                sequences=(PhonemeSequence(phonemes=("p", "a")),),
            ),
        )
    else:
        from corpusgen.generate.scorers.readability import ReadabilityScorer

        monkeypatch.setattr(ReadabilityScorer, "compute_fre", fail)
        invoke = partial(adapter.readability, ReadabilityRequest(texts=("A sentence.",)))

    with pytest.raises(expected_type) as caught:
        invoke()
    if not isinstance(error, InvalidRequestError):
        assert "private" not in str(caught.value)
