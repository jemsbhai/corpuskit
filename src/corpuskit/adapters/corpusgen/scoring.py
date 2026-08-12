"""CorpusGen adapter for composite scoring and versioned n-gram artifacts."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast

from pydantic import JsonValue, ValidationError

from corpuskit.domain.errors import (
    ApplicationError,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    CandidateScore,
    CompositeScoringRequest,
    CompositeScoringResult,
    NgramConstraintTrainingRequest,
    NgramScorerMode,
    NgramScorerTrainingRequest,
    PhonotacticArtifact,
    PhonotacticArtifactType,
    PhonotacticScoreRequest,
    PhonotacticScoreResult,
    ReadabilityBatchResult,
    ReadabilityRequest,
    ReadabilityResult,
    ReadabilityStatus,
    RepositoryCandidate,
    ScoringState,
)


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


class TargetLike(Protocol):
    @property
    def covered_units(self) -> set[str]: ...

    def update(self, phonemes: list[str], sentence_index: int) -> None: ...


class ReadabilityLike(Protocol):
    def compute_fre(self, text: str | None) -> float | None: ...

    def __call__(self, text: str | None) -> float: ...

    def as_filter(
        self,
        min_fre: float,
        max_fre: float,
    ) -> Callable[[dict[str, object]], bool]: ...


class NgramScorerLike(Protocol):
    def __call__(self, phonemes: list[str]) -> float: ...

    def save(self, path: str | Path) -> None: ...


class NgramConstraintLike(Protocol):
    def fit(self, phoneme_sequences: list[list[str]]) -> None: ...

    def fit_from_text(self, texts: list[str], language: str = "en-us") -> None: ...

    def score(self, phonemes: list[str]) -> float: ...

    def to_dict(self) -> dict[str, object]: ...


class CorpusgenScoringAdapter:
    """Expose JSON-safe scorer workflows without leaking upstream failures."""

    def __init__(
        self,
        *,
        authorized_fluency_scorer: Callable[[str | None], float] | None = None,
    ) -> None:
        """Bind worker-authorized fluency without giving HTTP a model-loading seam."""

        self._authorized_fluency_scorer = authorized_fluency_scorer

    def composite(self, request: CompositeScoringRequest) -> CompositeScoringResult:
        operation = "scoring.composite"
        try:
            targets = self._targets(request)
            covered_before = tuple(sorted(targets.covered_units))
            readability = self._readability(request)
            scorer = self._composite_scorer(request, targets, readability)
            engine_candidates: list[dict[str, object]] = [
                {"text": item.text, "phonemes": list(item.phonemes)} for item in request.candidates
            ]
            ranked_raw = scorer.rank(engine_candidates, top_k=request.top_k)
            ranked = self._normalize_ranked(
                ranked_raw,
                request.candidates,
                readability,
            )
            committed: CandidateScore | None = None
            state_after = request.state
            if request.commit_source_id is not None:
                candidate = next(
                    item
                    for item in request.candidates
                    if item.source_id == request.commit_source_id
                )
                raw_commit = scorer.score_and_commit(
                    list(candidate.phonemes),
                    len(request.state.covered_sequences),
                    text=candidate.text,
                )
                committed = self._normalize_score(raw_commit, candidate, readability)
                preview = next(
                    (item for item in ranked if item.source_id == request.commit_source_id),
                    None,
                )
                if preview is not None and preview != committed:
                    raise EngineContractError(operation)
                state_after = ScoringState(
                    covered_sequences=(*request.state.covered_sequences, candidate),
                    accepted_source_ids=(
                        *request.state.accepted_source_ids,
                        candidate.source_id,
                    ),
                )
            return CompositeScoringResult(
                ranked=ranked,
                committed=committed,
                state_before=request.state,
                state_after=state_after,
                covered_units_before=covered_before,
                covered_units_after=tuple(sorted(targets.covered_units)),
            )
        except ApplicationError:
            raise
        except ValueError:
            raise InvalidRequestError(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except (ValidationError, AttributeError, TypeError, KeyError, StopIteration):
            raise EngineContractError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    def train_ngram_scorer(
        self,
        request: NgramScorerTrainingRequest,
    ) -> PhonotacticArtifact:
        operation = "scoring.ngram_scorer.train"
        try:
            from corpusgen.generate.scorers.phonotactic import NgramPhonotacticScorer

            if request.mode is NgramScorerMode.INVENTORY_DERIVED:
                scorer = NgramPhonotacticScorer(list(request.phonemes), n=request.n)
            else:
                scorer = NgramPhonotacticScorer.from_corpus(
                    [list(item.phonemes) for item in request.sequences],
                    n=request.n,
                )
            payload = self._save_ngram_scorer(cast(NgramScorerLike, scorer))
            payload["corpuskit_training_mode"] = request.mode.value
            return PhonotacticArtifact.build(
                PhonotacticArtifactType.NGRAM_SCORER,
                payload,
            )
        except ApplicationError:
            raise
        except ValueError:
            raise InvalidRequestError(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except (ValidationError, AttributeError, TypeError, KeyError, json.JSONDecodeError):
            raise EngineContractError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    def train_ngram_constraint(
        self,
        request: NgramConstraintTrainingRequest,
    ) -> PhonotacticArtifact:
        operation = "scoring.ngram_constraint.train"
        try:
            from corpusgen.generate.phon_ctg.constraints import NgramPhonotacticModel

            model = cast(
                NgramConstraintLike,
                NgramPhonotacticModel(
                    order=request.order,
                    smoothing=request.smoothing,
                ),
            )
            if request.sequences:
                model.fit([list(item.phonemes) for item in request.sequences])
            else:
                model.fit_from_text(list(request.texts), language=request.language)
            payload = cast(dict[str, JsonValue], model.to_dict())
            return PhonotacticArtifact.build(
                PhonotacticArtifactType.NGRAM_CONSTRAINT,
                payload,
            )
        except ApplicationError:
            raise
        except ValueError:
            raise InvalidRequestError(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except (ValidationError, AttributeError, TypeError, KeyError):
            raise EngineContractError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    def score_phonotactics(self, request: PhonotacticScoreRequest) -> PhonotacticScoreResult:
        operation = "scoring.phonotactic.score"
        try:
            scorer = self.scorer_callable(request.artifact)
            scores = tuple(scorer(list(item.phonemes)) for item in request.sequences)
            return PhonotacticScoreResult(
                artifact_type=request.artifact.artifact_type,
                scores=scores,
            )
        except ApplicationError:
            raise
        except ValueError:
            raise InvalidRequestError(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except (ValidationError, AttributeError, TypeError, KeyError, json.JSONDecodeError):
            raise EngineContractError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    def readability(self, request: ReadabilityRequest) -> ReadabilityBatchResult:
        operation = "scoring.readability"
        try:
            from corpusgen.generate.scorers.readability import ReadabilityScorer

            target = request.target_range
            scorer = cast(
                ReadabilityLike,
                ReadabilityScorer(
                    target_range=(target.minimum, target.maximum) if target else None
                ),
            )
            result: list[ReadabilityResult] = []
            for text in request.texts:
                fre = scorer.compute_fre(text)
                if fre is None:
                    result.append(
                        ReadabilityResult(
                            text=text,
                            status=ReadabilityStatus.UNAVAILABLE,
                        )
                    )
                    continue
                accepted = None
                if request.filter_range is not None:
                    accepted = scorer.as_filter(
                        request.filter_range.minimum,
                        request.filter_range.maximum,
                    )({"text": text, "phonemes": []})
                result.append(
                    ReadabilityResult(
                        text=text,
                        status=ReadabilityStatus.AVAILABLE,
                        flesch_reading_ease=fre,
                        score=scorer(text),
                        accepted_by_filter=accepted,
                    )
                )
            return ReadabilityBatchResult(results=tuple(result))
        except ApplicationError:
            raise
        except ValueError:
            raise InvalidRequestError(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except (ValidationError, AttributeError, TypeError, KeyError):
            raise EngineContractError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    def scorer_callable(
        self,
        artifact: PhonotacticArtifact,
    ) -> Callable[[list[str]], float]:
        """Restore either artifact kind to its matching CorpusGen scoring callable."""

        if artifact.artifact_type is PhonotacticArtifactType.NGRAM_SCORER:
            return self._load_ngram_scorer(artifact.payload)
        if artifact.artifact_type is PhonotacticArtifactType.NGRAM_CONSTRAINT:
            from corpusgen.generate.phon_ctg.constraints import NgramPhonotacticModel

            model = cast(
                NgramConstraintLike,
                NgramPhonotacticModel.from_dict(cast(dict[str, object], artifact.payload)),
            )
            return model.score
        raise EngineContractError("scoring.artifact.restore")

    @staticmethod
    def _save_ngram_scorer(scorer: NgramScorerLike) -> dict[str, JsonValue]:
        with TemporaryDirectory(prefix="corpuskit-ngram-") as temporary:
            path = Path(temporary) / "artifact.json"
            scorer.save(path)
            decoded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise EngineContractError("scoring.ngram_scorer.serialize")
        return cast(dict[str, JsonValue], decoded)

    @staticmethod
    def _load_ngram_scorer(payload: dict[str, JsonValue]) -> Callable[[list[str]], float]:
        from corpusgen.generate.scorers.phonotactic import NgramPhonotacticScorer

        engine_payload = dict(payload)
        engine_payload.pop("corpuskit_training_mode", None)
        with TemporaryDirectory(prefix="corpuskit-ngram-") as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text(
                json.dumps(engine_payload, ensure_ascii=False, allow_nan=False),
                encoding="utf-8",
            )
            scorer = NgramPhonotacticScorer.load(path)
        return cast(Callable[[list[str]], float], scorer)

    @staticmethod
    def _targets(request: CompositeScoringRequest) -> TargetLike:
        from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory

        targets = cast(
            TargetLike,
            PhoneticTargetInventory(
                target_phonemes=list(request.target.phonemes),
                unit=request.target.unit.value,
            ),
        )
        for index, sequence in enumerate(request.state.covered_sequences):
            targets.update(list(sequence.phonemes), index)
        return targets

    def _composite_scorer(
        self,
        request: CompositeScoringRequest,
        targets: TargetLike,
        readability: ReadabilityLike | None,
    ) -> ScorerLike:
        from corpusgen.generate.phon_ctg.scorer import PhoneticScorer

        phonotactic = None
        if request.options.phonotactic_artifact is not None:
            phonotactic = self.scorer_callable(request.options.phonotactic_artifact)
        weights = request.options.weights
        fluency = None
        if weights.fluency > 0:
            fluency = self._authorized_fluency_scorer
            if fluency is None:
                raise InvalidRequestError("scoring.composite.fluency_worker_only")
        return cast(
            ScorerLike,
            PhoneticScorer(
                targets=targets,
                phonotactic_scorer=phonotactic,
                fluency_scorer=fluency,
                readability_scorer=readability,
                coverage_weight=weights.coverage,
                phonotactic_weight=weights.phonotactic,
                fluency_weight=weights.fluency,
                readability_weight=weights.readability,
            ),
        )

    @staticmethod
    def _readability(request: CompositeScoringRequest) -> ReadabilityLike | None:
        if request.options.weights.readability == 0:
            return None
        from corpusgen.generate.scorers.readability import ReadabilityScorer

        target = request.options.readability_target
        return cast(
            ReadabilityLike,
            ReadabilityScorer(target_range=(target.minimum, target.maximum) if target else None),
        )

    def _normalize_ranked(
        self,
        raw_results: list[ScoreResultLike],
        candidates: tuple[RepositoryCandidate, ...],
        readability: ReadabilityLike | None,
    ) -> tuple[CandidateScore, ...]:
        lookup: dict[tuple[str, tuple[str, ...]], list[RepositoryCandidate]] = {}
        for candidate in candidates:
            lookup.setdefault((candidate.text, candidate.phonemes), []).append(candidate)
        normalized: list[CandidateScore] = []
        for raw in raw_results:
            matches = lookup.get((raw.text or "", tuple(raw.phonemes)))
            if not matches:
                raise EngineContractError("scoring.composite.rank")
            normalized.append(self._normalize_score(raw, matches.pop(0), readability))
        return tuple(normalized)

    @staticmethod
    def _normalize_score(
        raw: ScoreResultLike,
        candidate: RepositoryCandidate,
        readability: ReadabilityLike | None,
    ) -> CandidateScore:
        fre = readability.compute_fre(candidate.text) if readability is not None else None
        readability_status = (
            ReadabilityStatus.AVAILABLE
            if readability is not None and fre is not None
            else ReadabilityStatus.UNAVAILABLE
        )
        try:
            return CandidateScore(
                source_id=candidate.source_id,
                text=candidate.text,
                phonemes=tuple(raw.phonemes),
                coverage_gain=raw.coverage_gain,
                weighted_coverage_gain=raw.weighted_coverage_gain,
                phonotactic_score=raw.phonotactic_score,
                fluency_score=raw.fluency_score,
                readability_status=readability_status,
                readability_score=(
                    raw.readability_score
                    if readability_status is ReadabilityStatus.AVAILABLE
                    else None
                ),
                composite_score=raw.composite_score,
                new_units=tuple(sorted(raw.new_units)),
            )
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise EngineContractError("scoring.composite.result") from None


__all__ = ["CorpusgenScoringAdapter"]
