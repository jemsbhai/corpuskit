"""CorpusGen adapter for bounded, source-aware repository generation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol, cast

from pydantic import ValidationError

from corpuskit.domain.errors import (
    ApplicationError,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    AcceptedCandidate,
    GenerationExecutionMode,
    GenerationScoringOptions,
    GenerationStopReason,
    HuggingFaceRepository,
    PrephonemizedRepository,
    RawTextRepository,
    ReadabilityRange,
    RepositoryGenerationRequest,
    RepositoryGenerationResult,
    RepositorySource,
)


class RepositoryLike(Protocol):
    """CorpusGen repository surface used by the source-aware wrapper."""

    @property
    def name(self) -> str: ...

    @property
    def pool(self) -> list[dict[str, object]]: ...

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **kwargs: object,
    ) -> list[dict[str, object]]: ...

    def mark_used(self, pool_index: int) -> None: ...


class TargetLike(Protocol):
    @property
    def coverage(self) -> float: ...

    @property
    def covered_units(self) -> set[str]: ...

    @property
    def missing(self) -> set[str]: ...


class ScoreResultLike(Protocol):
    text: str | None
    phonemes: list[str]
    coverage_gain: int
    weighted_coverage_gain: float
    phonotactic_score: float
    fluency_score: float
    readability_score: float
    composite_score: float
    new_units: set[str]


class ScorerLike(Protocol):
    def rank(
        self,
        candidates: list[dict[str, object]],
        top_k: int | None = None,
    ) -> list[ScoreResultLike]: ...

    def score_and_commit(
        self,
        phonemes: list[str],
        sentence_index: int,
        text: str | None = None,
    ) -> ScoreResultLike: ...


class LoopResultLike(Protocol):
    coverage: float
    covered_units: set[str]
    missing_units: set[str]
    unit: str
    backend: str
    elapsed_seconds: float
    iterations: int
    stop_reason: str


class LoopLike(Protocol):
    def run(self) -> LoopResultLike: ...


class GenerationBindings(Protocol):
    """Injectable construction contract used by fake-engine tests."""

    def repository(self, pool: list[dict[str, object]]) -> RepositoryLike: ...

    def repository_from_texts(self, texts: list[str], language: str) -> RepositoryLike: ...

    def repository_from_huggingface(
        self,
        source: HuggingFaceRepository,
    ) -> RepositoryLike: ...

    def targets(self, request: RepositoryGenerationRequest) -> TargetLike: ...

    def scorer(
        self,
        targets: TargetLike,
        options: GenerationScoringOptions,
    ) -> ScorerLike: ...

    def readability_filter(
        self,
        readability_range: ReadabilityRange,
    ) -> Callable[[dict[str, object]], bool]: ...

    def loop(
        self,
        request: RepositoryGenerationRequest,
        backend: RepositoryLike,
        targets: TargetLike,
        scorer: ScorerLike,
        candidate_filter: Callable[[dict[str, object]], bool] | None,
        on_progress: Callable[[dict[str, object]], None],
    ) -> LoopLike: ...


class _CorpusgenBindings:
    """Lazy imports keep optional repository dependencies out of API startup."""

    @staticmethod
    def repository(pool: list[dict[str, object]]) -> RepositoryLike:
        from corpusgen.generate.backends.repository import RepositoryBackend

        return cast(RepositoryLike, RepositoryBackend(pool=pool))

    @staticmethod
    def repository_from_texts(texts: list[str], language: str) -> RepositoryLike:
        from corpusgen.generate.backends.repository import RepositoryBackend

        return cast(RepositoryLike, RepositoryBackend.from_texts(texts, language=language))

    @staticmethod
    def repository_from_huggingface(source: HuggingFaceRepository) -> RepositoryLike:
        from corpusgen.generate.backends.repository import RepositoryBackend

        spec = source.spec
        return cast(
            RepositoryLike,
            RepositoryBackend.from_huggingface(
                dataset_name=spec.dataset,
                text_column=spec.text_column,
                split=spec.split,
                language=spec.language,
                max_samples=spec.max_samples,
                name=spec.config,
                revision=spec.revision,
                trust_remote_code=False,
            ),
        )

    @staticmethod
    def targets(request: RepositoryGenerationRequest) -> TargetLike:
        from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory

        return cast(
            TargetLike,
            PhoneticTargetInventory(
                target_phonemes=list(request.target.phonemes),
                unit=request.target.unit.value,
            ),
        )

    @staticmethod
    def scorer(targets: TargetLike, options: GenerationScoringOptions) -> ScorerLike:
        from corpusgen.generate.phon_ctg.scorer import PhoneticScorer
        from corpusgen.generate.scorers.readability import ReadabilityScorer

        from corpuskit.adapters.corpusgen.scoring import CorpusgenScoringAdapter

        phonotactic = None
        if options.phonotactic_artifact is not None:
            phonotactic = CorpusgenScoringAdapter().scorer_callable(options.phonotactic_artifact)
        readability = None
        if options.weights.readability > 0:
            target_range = options.readability_target
            engine_range = (
                (target_range.minimum, target_range.maximum) if target_range is not None else None
            )
            readability = ReadabilityScorer(target_range=engine_range)
        return cast(
            ScorerLike,
            PhoneticScorer(
                targets=targets,
                phonotactic_scorer=phonotactic,
                readability_scorer=readability,
                coverage_weight=options.weights.coverage,
                phonotactic_weight=options.weights.phonotactic,
                fluency_weight=0.0,
                readability_weight=options.weights.readability,
            ),
        )

    @staticmethod
    def readability_filter(
        readability_range: ReadabilityRange,
    ) -> Callable[[dict[str, object]], bool]:
        from corpusgen.generate.scorers.readability import ReadabilityScorer

        return cast(
            Callable[[dict[str, object]], bool],
            ReadabilityScorer().as_filter(
                min_fre=readability_range.minimum,
                max_fre=readability_range.maximum,
            ),
        )

    @staticmethod
    def loop(
        request: RepositoryGenerationRequest,
        backend: RepositoryLike,
        targets: TargetLike,
        scorer: ScorerLike,
        candidate_filter: Callable[[dict[str, object]], bool] | None,
        on_progress: Callable[[dict[str, object]], None],
    ) -> LoopLike:
        from corpusgen.generate.phon_ctg.loop import GenerationLoop, StoppingCriteria

        stopping = request.stopping
        return cast(
            LoopLike,
            GenerationLoop(
                backend=backend,
                targets=targets,
                scorer=scorer,
                stopping_criteria=StoppingCriteria(
                    target_coverage=stopping.target_coverage,
                    max_sentences=stopping.max_sentences,
                    max_iterations=stopping.max_iterations,
                    timeout_seconds=stopping.timeout_seconds,
                ),
                candidates_per_iteration=request.candidates_per_iteration,
                candidate_filter=candidate_filter,
                on_progress=on_progress,
            ),
        )


class _SourceRepository:
    """Retain stable source IDs and remove an accepted source exactly once."""

    def __init__(self, repository: RepositoryLike, source_ids: list[str]) -> None:
        if len(repository.pool) != len(source_ids):
            raise EngineContractError("generation.repository.prepare")
        self._repository = repository
        self._source_ids = list(source_ids)

    @property
    def name(self) -> str:
        return self._repository.name

    @property
    def pool(self) -> list[dict[str, object]]:
        return self._repository.pool

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        candidates = self._repository.generate(target_units, k=k, **kwargs)
        normalized: list[dict[str, object]] = []
        for candidate in candidates:
            pool_index = candidate.get("_pool_index")
            if not isinstance(pool_index, int) or not 0 <= pool_index < len(self._source_ids):
                raise EngineContractError("generation.repository.generate")
            item = dict(candidate)
            item["_source_id"] = self._source_ids[pool_index]
            normalized.append(item)
        return normalized

    def mark_used(self, pool_index: int) -> None:
        self._repository.mark_used(pool_index)
        self._source_ids.pop(pool_index)

    def mark_source_used(self, source_id: str) -> None:
        try:
            pool_index = self._source_ids.index(source_id)
        except ValueError:
            raise EngineContractError("generation.repository.commit") from None
        self.mark_used(pool_index)


class _SourceAwareScorer:
    """Bridge CorpusGen scores back to repository identity and deduplicate commits."""

    def __init__(self, scorer: ScorerLike, repository: _SourceRepository) -> None:
        self._scorer = scorer
        self._repository = repository
        self._ranked_sources: list[tuple[tuple[str, tuple[str, ...]], str]] = []
        self._accepted_ids: set[str] = set()
        self.accepted: list[tuple[str, str, tuple[str, ...], int]] = []

    def rank(
        self,
        candidates: list[dict[str, object]],
        top_k: int | None = None,
    ) -> list[ScoreResultLike]:
        lookup: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for candidate in candidates:
            key = self._candidate_key(candidate)
            source_id = candidate.get("_source_id")
            if not isinstance(source_id, str) or source_id in self._accepted_ids:
                raise EngineContractError("generation.score.rank")
            lookup.setdefault(key, []).append(source_id)
        ranked = self._scorer.rank(candidates, top_k=top_k)
        self._ranked_sources = []
        for result in ranked:
            key = (result.text or "", tuple(result.phonemes))
            matching = lookup.get(key)
            if not matching:
                raise EngineContractError("generation.score.rank")
            self._ranked_sources.append((key, matching.pop(0)))
        return ranked

    def score_and_commit(
        self,
        phonemes: list[str],
        sentence_index: int,
        text: str | None = None,
    ) -> ScoreResultLike:
        key = (text or "", tuple(phonemes))
        source_id = next(
            (
                candidate_id
                for candidate_key, candidate_id in self._ranked_sources
                if candidate_key == key and candidate_id not in self._accepted_ids
            ),
            None,
        )
        if source_id is None:
            raise EngineContractError("generation.score.commit")
        result = self._scorer.score_and_commit(phonemes, sentence_index, text=text)
        if result.coverage_gain <= 0:
            raise EngineContractError("generation.score.commit")
        self._repository.mark_source_used(source_id)
        self._accepted_ids.add(source_id)
        self.accepted.append((source_id, text or "", tuple(phonemes), result.coverage_gain))
        return result

    @staticmethod
    def _candidate_key(candidate: dict[str, object]) -> tuple[str, tuple[str, ...]]:
        text = candidate.get("text", "")
        phonemes = candidate.get("phonemes")
        if not isinstance(text, str) or not isinstance(phonemes, list):
            raise EngineContractError("generation.repository.candidate")
        if any(not isinstance(value, str) for value in phonemes):
            raise EngineContractError("generation.repository.candidate")
        return text, tuple(cast(list[str], phonemes))


AcceptedProgressSink = Callable[[AcceptedCandidate, float], None]


class CorpusgenGenerationAdapter:
    """Run CorpusGen's state machine behind a bounded, sanitized contract."""

    def __init__(self, bindings: GenerationBindings | None = None) -> None:
        self._bindings = bindings or _CorpusgenBindings()

    def run_repository(
        self,
        request: RepositoryGenerationRequest,
        *,
        execution_mode: GenerationExecutionMode,
        on_accepted: AcceptedProgressSink | None = None,
    ) -> RepositoryGenerationResult:
        operation = "generation.repository.run"
        try:
            repository, source_ids = self._prepare_repository(request.source)
            source_repository = _SourceRepository(repository, source_ids)
            targets = self._bindings.targets(request)
            scorer = _SourceAwareScorer(
                self._bindings.scorer(targets, request.scoring),
                source_repository,
            )
            candidate_filter = None
            if request.scoring.readability_filter is not None:
                candidate_filter = self._bindings.readability_filter(
                    request.scoring.readability_filter
                )

            def progress_callback(data: dict[str, object]) -> None:
                if not scorer.accepted:
                    raise EngineContractError("generation.progress")
                iteration = data.get("iteration")
                coverage = data.get("coverage")
                if not isinstance(iteration, int) or not isinstance(coverage, (int, float)):
                    raise EngineContractError("generation.progress")
                source_id, text, phonemes, coverage_gain = scorer.accepted[-1]
                accepted = AcceptedCandidate(
                    source_id=source_id,
                    text=text,
                    phonemes=phonemes,
                    iteration=iteration,
                    coverage_gain=coverage_gain,
                )
                if on_accepted is not None:
                    on_accepted(accepted, float(coverage))

            loop = self._bindings.loop(
                request,
                cast(RepositoryLike, source_repository),
                targets,
                cast(ScorerLike, scorer),
                candidate_filter,
                progress_callback,
            )
            raw = loop.run()
            return self._normalize_result(
                raw,
                request,
                execution_mode,
                scorer.accepted,
            )
        except ApplicationError:
            raise
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except ValueError:
            raise InvalidRequestError(operation) from None
        except (ValidationError, AttributeError, TypeError, KeyError, IndexError):
            raise EngineContractError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    def _prepare_repository(self, source: RepositorySource) -> tuple[RepositoryLike, list[str]]:
        if isinstance(source, PrephonemizedRepository):
            pool: list[dict[str, object]] = [
                {"text": item.text, "phonemes": list(item.phonemes)} for item in source.entries
            ]
            return self._bindings.repository(pool), [item.source_id for item in source.entries]
        if isinstance(source, RawTextRepository):
            repository = self._bindings.repository_from_texts(
                [item.text for item in source.entries], source.language
            )
            return repository, [item.source_id for item in source.entries]
        if isinstance(source, HuggingFaceRepository):
            repository = self._bindings.repository_from_huggingface(source)
            source_ids = self._huggingface_source_ids(source, len(repository.pool))
            return repository, source_ids
        raise EngineContractError("generation.repository.prepare")

    @staticmethod
    def _huggingface_source_ids(source: HuggingFaceRepository, count: int) -> list[str]:
        spec = source.spec
        identity = "|".join((spec.dataset, spec.config, spec.split, spec.revision))
        prefix = hashlib.sha256(identity.encode()).hexdigest()[:20]
        return [f"hf:{prefix}:{index}" for index in range(count)]

    @staticmethod
    def _normalize_result(
        raw: LoopResultLike,
        request: RepositoryGenerationRequest,
        execution_mode: GenerationExecutionMode,
        accepted_rows: list[tuple[str, str, tuple[str, ...], int]],
    ) -> RepositoryGenerationResult:
        operation = "generation.repository.result"
        try:
            stop_reason = GenerationStopReason(raw.stop_reason)
            accepted = tuple(
                AcceptedCandidate(
                    source_id=source_id,
                    text=text,
                    phonemes=phonemes,
                    iteration=index,
                    coverage_gain=gain,
                )
                for index, (source_id, text, phonemes, gain) in enumerate(accepted_rows, start=1)
            )
            if raw.backend != "repository" or raw.unit != request.target.unit.value:
                raise EngineContractError(operation)
            if len({item.source_id for item in accepted}) != len(accepted):
                raise EngineContractError(operation)
            return RepositoryGenerationResult(
                execution_mode=execution_mode,
                source_kind=request.source.kind,
                unit=request.target.unit,
                accepted=accepted,
                coverage=raw.coverage,
                covered_units=tuple(sorted(raw.covered_units)),
                missing_units=tuple(sorted(raw.missing_units)),
                iterations=raw.iterations,
                elapsed_seconds=raw.elapsed_seconds,
                stop_reason=stop_reason,
            )
        except EngineContractError:
            raise
        except (ValidationError, ValueError, AttributeError, TypeError):
            raise EngineContractError(operation) from None


__all__ = [
    "AcceptedProgressSink",
    "CorpusgenGenerationAdapter",
    "GenerationBindings",
]
