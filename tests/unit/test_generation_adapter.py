"""CorpusGen generation boundary and repository safety tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from corpuskit.adapters.corpusgen.generation import (
    CorpusgenGenerationAdapter,
    _CorpusgenBindings,
    _SourceAwareScorer,
    _SourceRepository,
)
from corpuskit.adapters.corpusgen.scoring import CorpusgenScoringAdapter
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    GenerationExecutionMode,
    GenerationScoringOptions,
    GenerationStoppingCriteria,
    GenerationTarget,
    HuggingFaceRepository,
    HuggingFaceRepositorySpec,
    NgramScorerMode,
    NgramScorerTrainingRequest,
    PhonemeSequence,
    PrephonemizedRepository,
    RawTextCandidate,
    RawTextRepository,
    ReadabilityRange,
    RepositoryCandidate,
    RepositoryGenerationRequest,
    ScoreWeights,
)


def _prephonemized_request(
    *,
    target: tuple[str, ...] = ("p", "b"),
    max_sentences: int = 5,
) -> RepositoryGenerationRequest:
    return RepositoryGenerationRequest(
        source=PrephonemizedRepository(
            entries=(
                RepositoryCandidate(
                    source_id="row-p",
                    text="Pat.",
                    phonemes=("p", "a", "t"),
                ),
                RepositoryCandidate(
                    source_id="row-b",
                    text="Bat.",
                    phonemes=("b", "a", "t"),
                ),
            )
        ),
        target=GenerationTarget(phonemes=target),
        stopping=GenerationStoppingCriteria(
            max_sentences=max_sentences,
            max_iterations=5,
            timeout_seconds=2.0,
        ),
    )


def test_real_repository_loop_preserves_ids_and_prevents_duplicate_acceptance() -> None:
    progress: list[tuple[str, float]] = []
    result = CorpusgenGenerationAdapter().run_repository(
        _prephonemized_request(),
        execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
        on_accepted=lambda item, coverage: progress.append((item.source_id, coverage)),
    )

    assert [item.source_id for item in result.accepted] == ["row-p", "row-b"]
    assert len({item.source_id for item in result.accepted}) == len(result.accepted)
    assert result.coverage == 1.0
    assert result.stop_reason == "target_coverage"
    assert progress == [("row-p", 0.5), ("row-b", 1.0)]


def test_real_repository_honors_max_sentence_stop() -> None:
    result = CorpusgenGenerationAdapter().run_repository(
        _prephonemized_request(target=("p", "b", "k"), max_sentences=1),
        execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
    )

    assert len(result.accepted) == 1
    assert result.stop_reason == "max_sentences"
    assert result.coverage == pytest.approx(1 / 3)


@pytest.mark.integration
def test_raw_text_repository_runs_real_espeak_g2p() -> None:
    request = RepositoryGenerationRequest(
        source=RawTextRepository(
            entries=(RawTextCandidate(source_id="hello", text="Hello world."),),
            language="en-us",
        ),
        target=GenerationTarget(phonemes=("h",)),
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=2,
            timeout_seconds=2.0,
        ),
    )

    result = CorpusgenGenerationAdapter().run_repository(
        request,
        execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
    )

    assert result.accepted[0].source_id == "hello"
    assert result.accepted[0].phonemes
    assert result.coverage == 1.0


class FakeRepository:
    def __init__(self, pool: list[dict[str, object]]) -> None:
        self._pool = pool

    @property
    def name(self) -> str:
        return "repository"

    @property
    def pool(self) -> list[dict[str, object]]:
        return list(self._pool)

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        del target_units, k, kwargs
        return []

    def mark_used(self, pool_index: int) -> None:
        self._pool.pop(pool_index)


class FakeTargets:
    coverage = 0.0
    covered_units: ClassVar[set[str]] = set()
    missing: ClassVar[set[str]] = {"p"}


class FakeScorer:
    def rank(
        self,
        candidates: list[dict[str, object]],
        top_k: int | None = None,
    ) -> list[Any]:
        del candidates, top_k
        return []

    def score_and_commit(
        self,
        phonemes: list[str],
        sentence_index: int,
        text: str | None = None,
    ) -> Any:
        del phonemes, sentence_index, text
        raise AssertionError("not called")


class FakeLoopResult:
    coverage = 0.0
    covered_units: ClassVar[set[str]] = set()
    missing_units: ClassVar[set[str]] = {"p"}
    unit = "phoneme"
    backend = "repository"
    elapsed_seconds = 0.01
    iterations = 1
    stop_reason = "new-upstream-reason"


class FakeLoop:
    def run(self) -> FakeLoopResult:
        return FakeLoopResult()


class ContractBindings:
    def __init__(
        self,
        *,
        empty_pool: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.empty_pool = empty_pool
        self.error = error

    def repository(self, pool: list[dict[str, object]]) -> FakeRepository:
        if self.error is not None:
            raise self.error
        return FakeRepository([] if self.empty_pool else pool)

    def repository_from_texts(self, texts: list[str], language: str) -> FakeRepository:
        del texts, language
        return FakeRepository([])

    def repository_from_huggingface(self, source: HuggingFaceRepository) -> FakeRepository:
        del source
        return FakeRepository([])

    def targets(self, request: RepositoryGenerationRequest) -> FakeTargets:
        del request
        return FakeTargets()

    def scorer(self, targets: FakeTargets, options: Any) -> FakeScorer:
        del targets, options
        return FakeScorer()

    def readability_filter(self, readability_range: Any) -> Callable[[dict[str, object]], bool]:
        del readability_range
        return lambda _: True

    def loop(
        self,
        request: RepositoryGenerationRequest,
        backend: FakeRepository,
        targets: FakeTargets,
        scorer: FakeScorer,
        candidate_filter: Callable[[dict[str, object]], bool] | None,
        on_progress: Callable[[dict[str, object]], None],
    ) -> FakeLoop:
        del request, backend, targets, scorer, candidate_filter, on_progress
        return FakeLoop()


def test_fake_binding_pool_mismatch_is_a_contract_error() -> None:
    adapter = CorpusgenGenerationAdapter(
        bindings=ContractBindings(empty_pool=True)  # type: ignore[arg-type]
    )
    with pytest.raises(EngineContractError):
        adapter.run_repository(
            _prephonemized_request(),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )


def test_unknown_stop_reason_and_runtime_errors_are_sanitized() -> None:
    adapter = CorpusgenGenerationAdapter(
        bindings=ContractBindings()  # type: ignore[arg-type]
    )
    with pytest.raises(EngineContractError):
        adapter.run_repository(
            _prephonemized_request(),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )

    adapter = CorpusgenGenerationAdapter(
        bindings=ContractBindings(error=RuntimeError("sensitive engine path"))  # type: ignore[arg-type]
    )
    with pytest.raises(EngineUnavailableError) as caught:
        adapter.run_repository(
            _prephonemized_request(),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
    assert "sensitive engine path" not in str(caught.value)


def test_huggingface_binding_forces_remote_code_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from corpusgen.generate.backends.repository import RepositoryBackend

    captured: dict[str, object] = {}

    def fake_loader(**kwargs: object) -> FakeRepository:
        captured.update(kwargs)
        return FakeRepository([{"text": "one", "phonemes": ["w", "n"]}])

    monkeypatch.setattr(RepositoryBackend, "from_huggingface", fake_loader)
    source = HuggingFaceRepository(
        spec=HuggingFaceRepositorySpec(
            dataset="owner/corpus",
            config="clean",
            split="train",
            text_column="sentence",
            revision="e" * 40,
            max_samples=17,
        )
    )

    repository = _CorpusgenBindings.repository_from_huggingface(source)

    assert repository.pool
    assert captured == {
        "dataset_name": "owner/corpus",
        "text_column": "sentence",
        "split": "train",
        "language": "en-us",
        "max_samples": 17,
        "name": "clean",
        "revision": "e" * 40,
        "trust_remote_code": False,
    }


def test_generation_can_use_phonotactic_readability_score_and_filter() -> None:
    artifact = CorpusgenScoringAdapter().train_ngram_scorer(
        NgramScorerTrainingRequest(
            mode=NgramScorerMode.CORPUS_TRAINED,
            sequences=(PhonemeSequence(phonemes=("p", "a")),),
        )
    )
    request = RepositoryGenerationRequest(
        source=PrephonemizedRepository(
            entries=(
                RepositoryCandidate(
                    source_id="readable",
                    text="Reading simple stories can help students learn new ideas.",
                    phonemes=("p", "a"),
                ),
            )
        ),
        target=GenerationTarget(phonemes=("p",)),
        scoring=GenerationScoringOptions(
            weights=ScoreWeights(coverage=1, phonotactic=1, readability=1),
            phonotactic_artifact=artifact,
            readability_target=ReadabilityRange(minimum=60, maximum=80),
            readability_filter=ReadabilityRange(minimum=60, maximum=80),
        ),
    )

    result = CorpusgenGenerationAdapter().run_repository(
        request,
        execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
    )

    assert result.coverage == 1.0
    assert result.accepted[0].source_id == "readable"


@dataclass
class FakeScore:
    text: str | None = "Pat."
    phonemes: list[str] | None = None
    coverage_gain: int = 1
    weighted_coverage_gain: float = 1.0
    phonotactic_score: float = 0.0
    fluency_score: float = 0.0
    readability_score: float = 0.0
    composite_score: float = 1.0
    new_units: ClassVar[set[str]] = {"p"}

    def __post_init__(self) -> None:
        if self.phonemes is None:
            self.phonemes = ["p"]


class ResultScorer:
    def __init__(self, result: FakeScore) -> None:
        self.result = result

    def rank(
        self,
        candidates: list[dict[str, object]],
        top_k: int | None = None,
    ) -> list[FakeScore]:
        del candidates, top_k
        return [self.result]

    def score_and_commit(
        self,
        phonemes: list[str],
        sentence_index: int,
        text: str | None = None,
    ) -> FakeScore:
        del phonemes, sentence_index, text
        return self.result


class CandidateRepository(FakeRepository):
    def __init__(
        self,
        pool: list[dict[str, object]],
        returned: list[dict[str, object]],
    ) -> None:
        super().__init__(pool)
        self.returned = returned

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        del target_units, k, kwargs
        return self.returned


def test_source_wrappers_reject_malformed_or_untracked_candidates() -> None:
    pool = [{"text": "Pat.", "phonemes": ["p"]}]
    repository = _SourceRepository(FakeRepository(pool), ["source"])
    assert repository.name == "repository"
    assert repository.pool == pool
    with pytest.raises(EngineContractError):
        repository.mark_source_used("missing")

    malformed = _SourceRepository(
        CandidateRepository(pool, [{"text": "Pat.", "phonemes": ["p"], "_pool_index": 9}]),
        ["source"],
    )
    with pytest.raises(EngineContractError):
        malformed.generate(["p"])

    aware = _SourceAwareScorer(ResultScorer(FakeScore()), repository)  # type: ignore[arg-type]
    with pytest.raises(EngineContractError):
        aware.score_and_commit(["p"], 0, "Pat.")
    for candidate in (
        {"text": 7, "phonemes": ["p"], "_source_id": "source"},
        {"text": "Pat.", "phonemes": [7], "_source_id": "source"},
    ):
        with pytest.raises(EngineContractError):
            aware.rank([candidate])


def test_source_aware_scorer_rejects_rank_mismatch_and_nonpositive_commit() -> None:
    pool = [{"text": "Pat.", "phonemes": ["p"]}]
    repository = _SourceRepository(FakeRepository(pool), ["source"])
    mismatch = _SourceAwareScorer(
        ResultScorer(FakeScore(text="Other.")),  # type: ignore[arg-type]
        repository,
    )
    with pytest.raises(EngineContractError):
        mismatch.rank([{"text": "Pat.", "phonemes": ["p"], "_source_id": "source"}])

    zero = _SourceAwareScorer(
        ResultScorer(FakeScore(coverage_gain=0)),  # type: ignore[arg-type]
        repository,
    )
    candidates = [{"text": "Pat.", "phonemes": ["p"], "_source_id": "source"}]
    zero.rank(candidates)
    with pytest.raises(EngineContractError):
        zero.score_and_commit(["p"], 0, "Pat.")


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (ImportError("private"), DependencyUnavailableError),
        (ValueError("private"), InvalidRequestError),
        (TypeError("private"), EngineContractError),
        (LookupError("private"), EngineUnavailableError),
    ],
)
def test_generation_exception_categories_are_sanitized(
    error: Exception,
    expected_type: type[Exception],
) -> None:
    adapter = CorpusgenGenerationAdapter(
        bindings=ContractBindings(error=error)  # type: ignore[arg-type]
    )
    with pytest.raises(expected_type) as caught:
        adapter.run_repository(
            _prephonemized_request(),
            execution_mode=GenerationExecutionMode.WORKER_ACTIVITY,
        )
    assert "private" not in str(caught.value)


def test_huggingface_source_ids_are_deterministic_and_bounded() -> None:
    source = HuggingFaceRepository(
        spec=HuggingFaceRepositorySpec(
            dataset="owner/corpus",
            config="clean",
            split="train",
            text_column="text",
            revision="1" * 40,
        )
    )
    first = CorpusgenGenerationAdapter._huggingface_source_ids(source, 2)
    second = CorpusgenGenerationAdapter._huggingface_source_ids(source, 2)
    assert first == second
    assert len(set(first)) == 2
    assert all(item.startswith("hf:") and len(item) < 192 for item in first)


def test_result_normalizer_rejects_backend_duplicate_and_invalid_values() -> None:
    request = _prephonemized_request()
    raw = FakeLoopResult()
    raw.stop_reason = "backend_exhausted"
    raw.backend = "other"
    with pytest.raises(EngineContractError):
        CorpusgenGenerationAdapter._normalize_result(
            raw,
            request,
            GenerationExecutionMode.WORKER_ACTIVITY,
            [],
        )

    raw.backend = "repository"
    duplicates = [
        ("same", "Pat.", ("p",), 1),
        ("same", "Bat.", ("b",), 1),
    ]
    with pytest.raises(EngineContractError):
        CorpusgenGenerationAdapter._normalize_result(
            raw,
            request,
            GenerationExecutionMode.WORKER_ACTIVITY,
            duplicates,
        )

    raw.elapsed_seconds = -1
    with pytest.raises(EngineContractError):
        CorpusgenGenerationAdapter._normalize_result(
            raw,
            request,
            GenerationExecutionMode.WORKER_ACTIVITY,
            [],
        )
