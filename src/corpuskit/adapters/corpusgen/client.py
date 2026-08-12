"""Typed boundary adapter for CorpusGen's public evaluation APIs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, cast

from pydantic import ValidationError

from corpuskit.domain.corpus import (
    CorpusEvaluation,
    CoverageUnit,
    DistributionMetrics,
    EvaluationTarget,
    EvaluationTargetMode,
    G2PTranscription,
    Inventory,
    PhoneticFeature,
    Segment,
    SentenceCoverage,
    TextQualityMetrics,
    UnitCount,
    UnitSources,
)
from corpuskit.domain.errors import (
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
    InventoryDataUnavailableError,
    InventoryNotFoundError,
    LanguageNotSupportedError,
)
from corpuskit.domain.selection import (
    CorpusSelection,
    ParetoSolution,
    SelectionAlgorithm,
    SelectionMetadata,
    SelectionOptions,
    SelectionRequest,
)


class G2PResultLike(Protocol):
    """Public CorpusGen G2P result surface consumed by the adapter."""

    text: str
    ipa: str
    phonemes: list[str]
    language: str

    @property
    def diphones(self) -> list[str]: ...

    @property
    def triphones(self) -> list[str]: ...

    @property
    def phoneme_count(self) -> int: ...

    @property
    def unique_phonemes(self) -> set[str]: ...


class G2PManagerLike(Protocol):
    """Public CorpusGen G2P manager surface consumed by the adapter."""

    def phonemize(self, text: str, language: str = "en-us") -> G2PResultLike: ...

    def phonemize_batch(self, texts: list[str], language: str = "en-us") -> list[G2PResultLike]: ...


class DistributionLike(Protocol):
    entropy: float
    normalized_entropy: float
    jsd_uniform: float
    coefficient_of_variation: float
    min_count: int
    max_count: int
    count_ratio: float
    zero_count: int
    pcd_uniform: float
    jsd_reference: float | None
    pearson_correlation: float | None


class TextQualityLike(Protocol):
    sentence_length_words_mean: float
    sentence_length_words_median: float
    sentence_length_words_std: float
    sentence_length_words_min: int
    sentence_length_words_max: int
    sentence_length_phonemes_mean: float
    sentence_length_phonemes_median: float
    sentence_length_phonemes_std: float
    sentence_length_phonemes_min: int
    sentence_length_phonemes_max: int
    total_words: int
    unique_words: int
    type_token_ratio: float
    hapax_ratio: float
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None


class SentenceDetailLike(Protocol):
    index: int
    text: str
    phoneme_count: int
    new_phonemes: list[str]
    all_phonemes: list[str]


class EvaluationReportLike(Protocol):
    language: str
    unit: str
    target_phonemes: list[str]
    covered_phonemes: set[str]
    missing_phonemes: set[str]
    coverage: float
    phoneme_counts: dict[str, int]
    total_sentences: int
    sentence_details: list[SentenceDetailLike]
    phoneme_sources: dict[str, list[int]]
    distribution: DistributionLike | None
    text_quality: TextQualityLike | None


class SegmentLike(Protocol):
    phoneme: str
    segment_class: str
    marginal: bool
    allophones: list[str]
    features: dict[str, str]
    glyph_id: str


class InventoryLike(Protocol):
    inventory_id: int
    language_name: str
    iso639_3: str
    glottocode: str
    specific_dialect: str | None
    source: str
    segments: list[SegmentLike]
    phonemes: list[str]
    consonants: list[str]
    vowels: list[str]
    tones: list[str]
    marginal_phonemes: list[str]
    size: int
    consonant_count: int
    vowel_count: int
    tone_count: int


class Evaluator(Protocol):
    def __call__(
        self,
        sentences: list[str],
        language: str = "en-us",
        target_phonemes: list[str] | str | None = None,
        unit: str = "phoneme",
    ) -> EvaluationReportLike: ...


class InventoryLoader(Protocol):
    def __call__(self, language: str, source: str | None = None) -> InventoryLike: ...


class SelectionResultLike(Protocol):
    selected_indices: list[int]
    selected_sentences: list[str]
    coverage: float
    covered_units: set[str]
    missing_units: set[str]
    unit: str
    algorithm: str
    elapsed_seconds: float
    iterations: int
    metadata: dict[str, object]


class Selector(Protocol):
    def __call__(
        self,
        candidates: list[str],
        language: str = "en-us",
        target_phonemes: list[str] | str | None = None,
        unit: str = "phoneme",
        algorithm: str = "greedy",
        max_sentences: int | None = None,
        target_coverage: float = 1.0,
        candidate_phonemes: list[list[str]] | None = None,
        weights: dict[str, float] | None = None,
        **algorithm_kwargs: object,
    ) -> SelectionResultLike: ...


def _default_g2p_factory() -> G2PManagerLike:
    from corpusgen.g2p import G2PManager

    return cast(G2PManagerLike, G2PManager())


def _default_evaluator(
    sentences: list[str],
    language: str = "en-us",
    target_phonemes: list[str] | str | None = None,
    unit: str = "phoneme",
) -> EvaluationReportLike:
    from corpusgen import evaluate

    return cast(
        EvaluationReportLike,
        evaluate(
            sentences,
            language=language,
            target_phonemes=target_phonemes,
            unit=unit,
        ),
    )


def _default_inventory_loader(
    language: str,
    source: str | None = None,
) -> InventoryLike:
    from corpusgen import get_inventory

    return cast(InventoryLike, get_inventory(language, source=source))


def _default_selector(
    candidates: list[str],
    language: str = "en-us",
    target_phonemes: list[str] | str | None = None,
    unit: str = "phoneme",
    algorithm: str = "greedy",
    max_sentences: int | None = None,
    target_coverage: float = 1.0,
    candidate_phonemes: list[list[str]] | None = None,
    weights: dict[str, float] | None = None,
    **algorithm_kwargs: object,
) -> SelectionResultLike:
    from corpusgen import select_sentences

    return cast(
        SelectionResultLike,
        select_sentences(
            candidates,
            language=language,
            target_phonemes=target_phonemes,
            unit=unit,
            algorithm=algorithm,
            max_sentences=max_sentences,
            target_coverage=target_coverage,
            candidate_phonemes=candidate_phonemes,
            weights=weights,
            **algorithm_kwargs,
        ),
    )


class CorpusgenAdapter:
    """Normalize CorpusGen results and failures into stable domain contracts."""

    def __init__(
        self,
        *,
        g2p_factory: Callable[[], G2PManagerLike] | None = None,
        evaluator: Evaluator | None = None,
        inventory_loader: InventoryLoader | None = None,
        selector: Selector | None = None,
    ) -> None:
        self._g2p_factory = g2p_factory or _default_g2p_factory
        self._evaluator = evaluator or _default_evaluator
        self._inventory_loader = inventory_loader or _default_inventory_loader
        self._selector = selector or _default_selector
        self._g2p: G2PManagerLike | None = None

    def phonemize(self, text: str, *, language: str = "en-us") -> G2PTranscription:
        """Convert one text while preserving a safe error contract."""

        self._validate_language(language, "g2p.phonemize")
        result = self._invoke(
            lambda: self._g2p_manager().phonemize(text, language=language),
            operation="g2p.phonemize",
            key_error_is_inventory=False,
        )
        return self._normalize_g2p(result, operation="g2p.phonemize")

    def phonemize_batch(
        self,
        texts: Sequence[str],
        *,
        language: str = "en-us",
    ) -> tuple[G2PTranscription, ...]:
        """Convert an ordered text batch into immutable results."""

        self._validate_language(language, "g2p.phonemize_batch")
        text_list = list(texts)
        results = self._invoke(
            lambda: self._g2p_manager().phonemize_batch(text_list, language=language),
            operation="g2p.phonemize_batch",
            key_error_is_inventory=False,
        )
        if len(results) != len(text_list):
            raise EngineContractError("g2p.phonemize_batch")
        return tuple(
            self._normalize_g2p(result, operation="g2p.phonemize_batch") for result in results
        )

    def evaluate(
        self,
        sentences: Sequence[str],
        *,
        language: str = "en-us",
        unit: CoverageUnit = CoverageUnit.PHONEME,
        target: EvaluationTarget | None = None,
    ) -> CorpusEvaluation:
        """Evaluate text with an explicit, derived, or PHOIBLE target."""

        operation = "corpus.evaluate"
        self._validate_language(language, operation)
        sentence_list = list(sentences)
        if not sentence_list:
            raise InvalidRequestError(operation)
        resolved_target = target or EvaluationTarget()
        engine_target: list[str] | str | None
        if resolved_target.mode is EvaluationTargetMode.DERIVED:
            engine_target = None
        elif resolved_target.mode is EvaluationTargetMode.PHOIBLE:
            engine_target = "phoible"
        else:
            engine_target = list(resolved_target.phonemes)

        report = self._invoke(
            lambda: self._evaluator(
                sentence_list,
                language=language,
                target_phonemes=engine_target,
                unit=unit.value,
            ),
            operation=operation,
            key_error_is_inventory=resolved_target.mode is EvaluationTargetMode.PHOIBLE,
        )
        return self._normalize_evaluation(report, resolved_target.mode, unit, operation)

    def get_inventory(self, language: str, *, source: str | None = None) -> Inventory:
        """Resolve and normalize one PHOIBLE inventory."""

        operation = "inventory.get"
        self._validate_language(language, operation)
        if source is not None and not source.strip():
            raise InvalidRequestError(operation)
        result = self._invoke(
            lambda: self._inventory_loader(language, source=source),
            operation=operation,
            key_error_is_inventory=True,
        )
        return self._normalize_inventory(result, operation=operation)

    def select(self, request: SelectionRequest) -> CorpusSelection:
        """Select an optimized sentence subset with bounded typed options."""

        operation = "corpus.select"
        self._validate_language(request.language, operation)
        if not request.candidates:
            raise InvalidRequestError(operation)
        target: list[str] | str | None
        if request.target.mode is EvaluationTargetMode.DERIVED:
            target = None
        elif request.target.mode is EvaluationTargetMode.PHOIBLE:
            target = "phoible"
        else:
            target = list(request.target.phonemes)

        options = request.options
        weights = {item.unit: item.weight for item in options.weights} or None
        algorithm_kwargs = self._selection_algorithm_kwargs(options)
        selector = cast(Callable[..., SelectionResultLike], self._selector)
        result = self._invoke(
            lambda: selector(
                list(request.candidates),
                language=request.language,
                target_phonemes=target,
                unit=request.unit.value,
                algorithm=options.algorithm.value,
                max_sentences=options.max_sentences,
                target_coverage=options.target_coverage,
                weights=weights,
                **algorithm_kwargs,
            ),
            operation=operation,
            key_error_is_inventory=request.target.mode is EvaluationTargetMode.PHOIBLE,
        )
        return self._normalize_selection(result, request, operation)

    def _g2p_manager(self) -> G2PManagerLike:
        if self._g2p is None:
            self._g2p = self._invoke(
                self._g2p_factory,
                operation="g2p.initialize",
                key_error_is_inventory=False,
            )
        return self._g2p

    @staticmethod
    def _selection_algorithm_kwargs(options: SelectionOptions) -> dict[str, object]:
        distribution = {item.unit: item.weight for item in options.target_distribution}
        if options.algorithm is SelectionAlgorithm.STOCHASTIC:
            return {"epsilon": options.epsilon, "seed": options.seed}
        if options.algorithm is SelectionAlgorithm.DISTRIBUTION:
            return {"target_distribution": distribution}
        if options.algorithm is SelectionAlgorithm.ILP:
            return {"time_limit": options.ilp_time_limit_seconds}
        if options.algorithm is SelectionAlgorithm.NSGA2:
            return {
                "target_distribution": distribution or None,
                "population_size": options.population_size,
                "n_generations": options.generations,
                "seed": options.seed,
            }
        return {}

    @staticmethod
    def _validate_language(language: str, operation: str) -> None:
        if not language.strip():
            raise InvalidRequestError(operation)

    @staticmethod
    def _invoke[T](
        call: Callable[[], T],
        *,
        operation: str,
        key_error_is_inventory: bool,
    ) -> T:
        try:
            return call()
        except FileNotFoundError:
            raise InventoryDataUnavailableError(operation) from None
        except KeyError:
            error_type = (
                InventoryNotFoundError if key_error_is_inventory else LanguageNotSupportedError
            )
            raise error_type(operation) from None
        except ImportError:
            raise DependencyUnavailableError(operation) from None
        except ValueError:
            raise InvalidRequestError(operation) from None
        except (RuntimeError, OSError):
            raise EngineUnavailableError(operation) from None
        except Exception:
            raise EngineUnavailableError(operation) from None

    @staticmethod
    def _normalize_g2p(result: G2PResultLike, *, operation: str) -> G2PTranscription:
        try:
            return G2PTranscription(
                text=result.text,
                language=result.language,
                ipa=result.ipa,
                phonemes=tuple(result.phonemes),
                diphones=tuple(result.diphones),
                triphones=tuple(result.triphones),
                phoneme_count=result.phoneme_count,
                unique_phonemes=tuple(sorted(result.unique_phonemes)),
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _normalize_distribution(metrics: DistributionLike) -> DistributionMetrics:
        return DistributionMetrics(
            entropy=metrics.entropy,
            normalized_entropy=metrics.normalized_entropy,
            jsd_uniform=metrics.jsd_uniform,
            coefficient_of_variation=metrics.coefficient_of_variation,
            min_count=metrics.min_count,
            max_count=metrics.max_count,
            count_ratio=metrics.count_ratio,
            zero_count=metrics.zero_count,
            pcd_uniform=metrics.pcd_uniform,
            jsd_reference=metrics.jsd_reference,
            pearson_correlation=metrics.pearson_correlation,
        )

    @staticmethod
    def _normalize_text_quality(metrics: TextQualityLike) -> TextQualityMetrics:
        return TextQualityMetrics(
            sentence_length_words_mean=metrics.sentence_length_words_mean,
            sentence_length_words_median=metrics.sentence_length_words_median,
            sentence_length_words_std=metrics.sentence_length_words_std,
            sentence_length_words_min=metrics.sentence_length_words_min,
            sentence_length_words_max=metrics.sentence_length_words_max,
            sentence_length_phonemes_mean=metrics.sentence_length_phonemes_mean,
            sentence_length_phonemes_median=metrics.sentence_length_phonemes_median,
            sentence_length_phonemes_std=metrics.sentence_length_phonemes_std,
            sentence_length_phonemes_min=metrics.sentence_length_phonemes_min,
            sentence_length_phonemes_max=metrics.sentence_length_phonemes_max,
            total_words=metrics.total_words,
            unique_words=metrics.unique_words,
            type_token_ratio=metrics.type_token_ratio,
            hapax_ratio=metrics.hapax_ratio,
            flesch_reading_ease=metrics.flesch_reading_ease,
            flesch_kincaid_grade=metrics.flesch_kincaid_grade,
        )

    @classmethod
    def _normalize_evaluation(
        cls,
        report: EvaluationReportLike,
        target_mode: EvaluationTargetMode,
        requested_unit: CoverageUnit,
        operation: str,
    ) -> CorpusEvaluation:
        try:
            result_unit = CoverageUnit(report.unit)
            if result_unit is not requested_unit:
                raise ValueError("engine returned a different coverage unit")
            distribution = (
                cls._normalize_distribution(report.distribution)
                if report.distribution is not None
                else None
            )
            text_quality = (
                cls._normalize_text_quality(report.text_quality)
                if report.text_quality is not None
                else None
            )
            return CorpusEvaluation(
                language=report.language,
                unit=result_unit,
                target_mode=target_mode,
                target_units=tuple(
                    sorted(set(report.covered_phonemes) | set(report.missing_phonemes))
                ),
                covered_units=tuple(sorted(report.covered_phonemes)),
                missing_units=tuple(sorted(report.missing_phonemes)),
                coverage=report.coverage,
                total_sentences=report.total_sentences,
                unit_counts=tuple(
                    UnitCount(unit=unit, count=count)
                    for unit, count in sorted(report.phoneme_counts.items())
                ),
                sentence_details=tuple(
                    SentenceCoverage(
                        index=detail.index,
                        text=detail.text,
                        phoneme_count=detail.phoneme_count,
                        new_units=tuple(detail.new_phonemes),
                        all_phonemes=tuple(detail.all_phonemes),
                    )
                    for detail in report.sentence_details
                ),
                unit_sources=tuple(
                    UnitSources(unit=unit, sentence_indices=tuple(indices))
                    for unit, indices in sorted(report.phoneme_sources.items())
                ),
                distribution=distribution,
                text_quality=text_quality,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @staticmethod
    def _normalize_inventory(result: InventoryLike, *, operation: str) -> Inventory:
        try:
            segments = tuple(
                Segment(
                    phoneme=segment.phoneme,
                    segment_class=segment.segment_class,
                    marginal=segment.marginal,
                    allophones=tuple(segment.allophones),
                    features=tuple(
                        PhoneticFeature(name=name, value=value)
                        for name, value in sorted(segment.features.items())
                    ),
                    glyph_id=segment.glyph_id,
                )
                for segment in result.segments
            )
            return Inventory(
                inventory_id=result.inventory_id,
                language_name=result.language_name,
                iso639_3=result.iso639_3,
                glottocode=result.glottocode,
                specific_dialect=result.specific_dialect,
                source=result.source,
                segments=segments,
                phonemes=tuple(result.phonemes),
                consonants=tuple(result.consonants),
                vowels=tuple(result.vowels),
                tones=tuple(result.tones),
                marginal_phonemes=tuple(result.marginal_phonemes),
                size=result.size,
                consonant_count=result.consonant_count,
                vowel_count=result.vowel_count,
                tone_count=result.tone_count,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @classmethod
    def _normalize_selection(
        cls,
        result: SelectionResultLike,
        request: SelectionRequest,
        operation: str,
    ) -> CorpusSelection:
        try:
            unit = CoverageUnit(result.unit)
            algorithm = SelectionAlgorithm(result.algorithm)
            if unit is not request.unit or algorithm is not request.options.algorithm:
                raise ValueError("engine returned a different selection contract")
            indices = tuple(result.selected_indices)
            sentences = tuple(result.selected_sentences)
            if len(indices) != len(sentences) or len(indices) != len(set(indices)):
                raise ValueError("engine returned inconsistent selected items")
            if any(index < 0 or index >= len(request.candidates) for index in indices):
                raise ValueError("engine returned an out-of-range candidate index")
            if sentences != tuple(request.candidates[index] for index in indices):
                raise ValueError("engine returned sentences that do not match selected indices")
            if set(result.covered_units) & set(result.missing_units):
                raise ValueError("engine returned overlapping coverage sets")
            return CorpusSelection(
                selected_indices=indices,
                selected_sentences=sentences,
                coverage=result.coverage,
                covered_units=tuple(sorted(result.covered_units)),
                missing_units=tuple(sorted(result.missing_units)),
                unit=unit,
                target_mode=request.target.mode,
                algorithm=algorithm,
                elapsed_seconds=result.elapsed_seconds,
                iterations=result.iterations,
                metadata=cls._normalize_selection_metadata(result.metadata),
            )
        except (AttributeError, KeyError, TypeError, ValidationError, ValueError):
            raise EngineContractError(operation) from None

    @classmethod
    def _normalize_selection_metadata(
        cls,
        metadata: dict[str, object],
    ) -> SelectionMetadata:
        pareto_raw = metadata.get("pareto_front", [])
        if not isinstance(pareto_raw, list):
            raise TypeError("pareto_front must be a list")
        pareto = tuple(cls._normalize_pareto_solution(item) for item in pareto_raw)
        return SelectionMetadata(
            evaluations=cls._optional_int(metadata.get("evaluations")),
            epsilon=cls._optional_float(metadata.get("epsilon")),
            seed=cls._optional_int(metadata.get("seed")),
            sample_size=cls._optional_int(metadata.get("sample_size")),
            kl_divergence=cls._optional_float(metadata.get("kl_divergence")),
            solver_status=cls._optional_string(metadata.get("solver_status")),
            pareto_front=pareto,
        )

    @classmethod
    def _normalize_pareto_solution(cls, value: object) -> ParetoSolution:
        if not isinstance(value, dict):
            raise TypeError("pareto solution must be a mapping")
        indices = value.get("selected_indices")
        if not isinstance(indices, list) or not all(
            isinstance(index, int) and not isinstance(index, bool) for index in indices
        ):
            raise TypeError("selected_indices must be integers")
        coverage = cls._required_float(value.get("coverage"))
        sentence_count = cls._required_int(value.get("n_sentences"))
        return ParetoSolution(
            coverage=coverage,
            sentence_count=sentence_count,
            selected_indices=tuple(indices),
            kl_divergence=cls._optional_float(value.get("kl_divergence")),
        )

    @staticmethod
    def _required_int(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("expected integer metadata")
        return value

    @classmethod
    def _optional_int(cls, value: object) -> int | None:
        return None if value is None else cls._required_int(value)

    @staticmethod
    def _required_float(value: object) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("expected numeric metadata")
        return float(value)

    @classmethod
    def _optional_float(cls, value: object) -> float | None:
        return None if value is None else cls._required_float(value)

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("expected string metadata")
        return value
