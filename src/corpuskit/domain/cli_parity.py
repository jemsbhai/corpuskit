"""Typed contracts for safe, copyable CorpusGen CLI previews."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from corpuskit.domain.corpus import CoverageUnit, FrozenDomainModel
from corpuskit.domain.selection import SelectionAlgorithm, UnitWeight

MAX_CLI_SENTENCES = 100
MAX_CLI_TEXT_CHARACTERS = 4_000
MAX_CLI_PATH_CHARACTERS = 512
MAX_CLI_PROMPT_CHARACTERS = 4_096

_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$", re.ASCII)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,191}$", re.ASCII)
_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", re.ASCII)


class CliDomainModel(FrozenDomainModel):
    """Forbid non-finite values in every command-preview contract."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CliWorkflow(StrEnum):
    INVENTORY = "inventory"
    EVALUATE = "evaluate"
    SELECT = "select"
    GENERATE = "generate"


class CliReproducibility(StrEnum):
    EXACT_INPUTS_REQUIRED = "exact_inputs_required"
    BEST_EFFORT = "best_effort"
    EXTERNAL_DEPENDENCY = "external_dependency"


class CliEvaluationFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    JSON_LD = "jsonld"


class CliBasicFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class CliVerbosity(StrEnum):
    MINIMAL = "minimal"
    NORMAL = "normal"
    VERBOSE = "verbose"


class CliTargetMode(StrEnum):
    DERIVED = "derived"
    PHOIBLE = "phoible"


class CliGenerationBackend(StrEnum):
    REPOSITORY = "repository"
    LLM_API = "llm_api"
    LOCAL = "local"


class CliGuidance(StrEnum):
    NONE = "none"
    DATG = "datg"
    RL = "rl"


class CliDevice(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"


class CliQuantization(StrEnum):
    NONE = "none"
    FOUR_BIT = "4bit"
    EIGHT_BIT = "8bit"


class CliPhonotacticScorer(StrEnum):
    NONE = "none"
    NGRAM = "ngram"


class CliFluencyScorer(StrEnum):
    NONE = "none"
    PERPLEXITY = "perplexity"


class CliWeight(CliDomainModel):
    """One CLI-serializable positive unit weight."""

    unit: str = Field(min_length=1, max_length=64)
    weight: float = Field(gt=0.0, le=1_000_000.0)

    @model_validator(mode="after")
    def validate_cli_symbol(self) -> Self:
        if any(character in self.unit for character in (",", ":", "\r", "\n", "\x00")):
            raise ValueError("CLI weight units cannot contain comma, colon, NUL, or a newline.")
        return self


class CliInventoryRequest(CliDomainModel):
    workflow: Literal[CliWorkflow.INVENTORY] = CliWorkflow.INVENTORY
    language: str = Field(min_length=2, max_length=32)
    source: str | None = Field(default=None, min_length=1, max_length=64)
    output_format: CliBasicFormat = CliBasicFormat.JSON

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        _validate_language(self.language)
        if self.source is not None and _SAFE_SOURCE.fullmatch(self.source) is None:
            raise ValueError("PHOIBLE source must be a safe identifier.")
        return self


class CliEvaluateRequest(CliDomainModel):
    workflow: Literal[CliWorkflow.EVALUATE] = CliWorkflow.EVALUATE
    language: str = Field(min_length=2, max_length=32)
    sentences: tuple[str, ...] = Field(default=(), max_length=MAX_CLI_SENTENCES)
    file_path: str | None = Field(default=None, min_length=1, max_length=MAX_CLI_PATH_CHARACTERS)
    target: CliTargetMode = CliTargetMode.DERIVED
    unit: CoverageUnit = CoverageUnit.PHONEME
    output_format: CliEvaluationFormat = CliEvaluationFormat.JSON
    verbosity: CliVerbosity = CliVerbosity.NORMAL

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        _validate_language(self.language)
        if bool(self.sentences) == bool(self.file_path):
            raise ValueError("Provide either inline sentences or one file path.")
        _validate_sentences(self.sentences)
        if self.file_path is not None:
            _validate_path(self.file_path)
        return self


class CliSelectRequest(CliDomainModel):
    workflow: Literal[CliWorkflow.SELECT] = CliWorkflow.SELECT
    language: str = Field(min_length=2, max_length=32)
    file_path: str = Field(min_length=1, max_length=MAX_CLI_PATH_CHARACTERS)
    target: CliTargetMode = CliTargetMode.DERIVED
    unit: CoverageUnit = CoverageUnit.PHONEME
    algorithm: SelectionAlgorithm = SelectionAlgorithm.GREEDY
    target_distribution: tuple[UnitWeight, ...] = Field(default=(), max_length=10_000)
    max_sentences: int | None = Field(default=None, ge=1, le=2_000)
    target_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    output_format: CliBasicFormat = CliBasicFormat.JSON
    output_path: str | None = Field(default=None, min_length=1, max_length=MAX_CLI_PATH_CHARACTERS)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        _validate_language(self.language)
        _validate_path(self.file_path)
        if self.output_path is not None:
            _validate_path(self.output_path)
        units = tuple(item.unit for item in self.target_distribution)
        if len(units) != len(set(units)):
            raise ValueError("Target-distribution units must be unique.")
        requires_distribution = self.algorithm is SelectionAlgorithm.DISTRIBUTION
        if requires_distribution != bool(self.target_distribution):
            raise ValueError("Only the distribution selector accepts a target distribution.")
        return self


class CliGenerateRequest(CliDomainModel):
    workflow: Literal[CliWorkflow.GENERATE] = CliWorkflow.GENERATE
    backend: CliGenerationBackend
    language: str = Field(min_length=2, max_length=32)
    file_path: str | None = Field(default=None, min_length=1, max_length=MAX_CLI_PATH_CHARACTERS)
    dataset: str | None = Field(default=None, min_length=1, max_length=192)
    text_column: str = Field(default="text", min_length=1, max_length=128)
    split: str | None = Field(default=None, min_length=1, max_length=128)
    max_samples: int | None = Field(default=None, ge=1, le=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=192)
    target_source: str = Field(default="phoible", min_length=1, max_length=64)
    phonemes: tuple[str, ...] = Field(default=(), max_length=64)
    weights: tuple[CliWeight, ...] = Field(default=(), max_length=4_096)
    unit: CoverageUnit = CoverageUnit.PHONEME
    target_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    max_sentences: int | None = Field(default=50, ge=1, le=10_000)
    max_iterations: int | None = Field(default=100, ge=1, le=10_000)
    timeout_seconds: float | None = Field(default=300.0, gt=0.0, le=86_400.0)
    candidates_per_iteration: int = Field(default=5, ge=1, le=128)
    llm_temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1_024, ge=1, le=131_072)
    local_temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    local_max_tokens: int = Field(default=256, ge=1, le=16_384)
    device: CliDevice = CliDevice.AUTO
    quantization: CliQuantization = CliQuantization.NONE
    prompt_template: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CLI_PROMPT_CHARACTERS,
    )
    guidance: CliGuidance = CliGuidance.NONE
    guidance_config_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CLI_PATH_CHARACTERS,
    )
    datg_boost: float = Field(default=5.0, ge=0.0, le=100.0)
    datg_penalty: float = Field(default=-5.0, ge=-100.0, le=0.0)
    datg_anti_mode: Literal["covered", "frequency"] = "covered"
    datg_frequency_threshold: int = Field(default=10, ge=0, le=1_000_000)
    datg_batch_size: int = Field(default=512, ge=1, le=8_192)
    rl_adapter_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CLI_PATH_CHARACTERS,
    )
    coverage_weight: float = Field(default=1.0, ge=0.0, le=1_000.0)
    phonotactic_weight: float = Field(default=0.0, ge=0.0, le=1_000.0)
    phonotactic_scorer: CliPhonotacticScorer = CliPhonotacticScorer.NONE
    phonotactic_corpus_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CLI_PATH_CHARACTERS,
    )
    phonotactic_n: int = Field(default=2, ge=1, le=8)
    fluency_weight: float = Field(default=0.0, ge=0.0, le=1_000.0)
    fluency_scorer: CliFluencyScorer = CliFluencyScorer.NONE
    fluency_model: str | None = Field(default=None, min_length=1, max_length=192)
    fluency_device: CliDevice = CliDevice.AUTO
    output_format: CliBasicFormat = CliBasicFormat.JSON
    output_path: str | None = Field(default=None, min_length=1, max_length=MAX_CLI_PATH_CHARACTERS)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        _validate_language(self.language)
        self._validate_sources()
        self._validate_generation_options()
        self._validate_paths()
        self._validate_symbols()
        return self

    def _validate_sources(self) -> None:
        has_file = self.file_path is not None
        has_dataset = self.dataset is not None
        if self.backend is CliGenerationBackend.REPOSITORY:
            if has_file == has_dataset:
                raise ValueError("Repository generation requires exactly one file or dataset.")
            if self.model is not None:
                raise ValueError("Repository generation does not accept a model.")
        elif self.model is None or has_file or has_dataset:
            raise ValueError("LLM and local generation require a model and no repository source.")
        if self.dataset is not None and _SAFE_IDENTIFIER.fullmatch(self.dataset) is None:
            raise ValueError("Dataset must be a safe identifier.")
        for value in (self.text_column, self.split):
            if value is not None and _SAFE_IDENTIFIER.fullmatch(value) is None:
                raise ValueError("Dataset fields must be safe identifiers.")
        for value in (self.model, self.fluency_model):
            if value is not None and any(character in value for character in ("\x00", "\r", "\n")):
                raise ValueError("Model identifiers cannot contain NUL or a newline.")

    def _validate_generation_options(self) -> None:
        if all(
            value is None
            for value in (self.max_sentences, self.max_iterations, self.timeout_seconds)
        ):
            raise ValueError("At least one finite stopping limit is required.")
        if self.guidance is not CliGuidance.NONE and self.backend is not CliGenerationBackend.LOCAL:
            raise ValueError("Guidance is available only for the local backend.")
        if self.guidance is CliGuidance.RL and self.rl_adapter_path is None:
            raise ValueError("RL guidance requires an adapter path.")
        if self.guidance is not CliGuidance.RL and self.rl_adapter_path is not None:
            raise ValueError("An RL adapter path requires RL guidance.")
        if self.quantization is not CliQuantization.NONE and (
            self.backend is not CliGenerationBackend.LOCAL or self.device is not CliDevice.CUDA
        ):
            raise ValueError("Quantization requires local generation on CUDA.")
        if self.prompt_template is not None and "{target_units}" not in self.prompt_template:
            raise ValueError("A prompt template must contain {target_units}.")
        if self.prompt_template is not None and (
            self.backend is CliGenerationBackend.REPOSITORY or "\x00" in self.prompt_template
        ):
            raise ValueError("Prompt templates require a model backend and cannot contain NUL.")
        if self.guidance_config_path is not None and (
            self.backend is not CliGenerationBackend.LOCAL or self.guidance is CliGuidance.NONE
        ):
            raise ValueError("Guidance config requires an enabled local guidance strategy.")
        if (self.phonotactic_weight > 0) != (self.phonotactic_scorer is CliPhonotacticScorer.NGRAM):
            raise ValueError("Phonotactic weight and scorer must be enabled together.")
        if (self.fluency_weight > 0) != (self.fluency_scorer is CliFluencyScorer.PERPLEXITY):
            raise ValueError("Fluency weight and scorer must be enabled together.")
        if self.phonotactic_corpus_path is not None and (
            self.phonotactic_scorer is not CliPhonotacticScorer.NGRAM
        ):
            raise ValueError("A phonotactic corpus requires the n-gram scorer.")
        if self.fluency_model is not None and (
            self.fluency_scorer is not CliFluencyScorer.PERPLEXITY
        ):
            raise ValueError("A fluency model requires the perplexity scorer.")

    def _validate_paths(self) -> None:
        for value in (
            self.file_path,
            self.guidance_config_path,
            self.rl_adapter_path,
            self.phonotactic_corpus_path,
            self.output_path,
        ):
            if value is not None:
                _validate_path(value)

    def _validate_symbols(self) -> None:
        if _SAFE_SOURCE.fullmatch(self.target_source) is None:
            raise ValueError("Target source must be phoible or a safe PHOIBLE source identifier.")
        if len(set(self.phonemes)) != len(self.phonemes):
            raise ValueError("Additional phonemes must be unique.")
        if any(
            not value.strip()
            or len(value) > 64
            or any(character in value for character in (",", "\r", "\n", "\x00"))
            for value in self.phonemes
        ):
            raise ValueError("Additional phonemes must be CLI-safe symbols.")
        units = tuple(item.unit for item in self.weights)
        if len(units) != len(set(units)):
            raise ValueError("Generation weight units must be unique.")


CliPreviewRequest = Annotated[
    CliInventoryRequest | CliEvaluateRequest | CliSelectRequest | CliGenerateRequest,
    Field(discriminator="workflow"),
]


class CliCommandPreview(CliDomainModel):
    """A shell-independent argv plus correctly quoted display commands."""

    workflow: CliWorkflow
    argv: tuple[str, ...] = Field(min_length=2, max_length=256)
    posix_command: str = Field(min_length=1, max_length=65_536)
    powershell_command: str = Field(min_length=1, max_length=65_536)
    environment: tuple[tuple[str, str], ...] = (("PYTHONUTF8", "1"),)
    reproducibility: CliReproducibility
    warnings: tuple[str, ...] = ()


def _validate_language(language: str) -> None:
    if _LANGUAGE.fullmatch(language) is None:
        raise ValueError("Language must be an eSpeak-style language or voice identifier.")


def _validate_path(path: str) -> None:
    if path != path.strip() or any(character in path for character in ("\x00", "\r", "\n")):
        raise ValueError("CLI paths cannot contain surrounding whitespace, NUL, or a newline.")


def _validate_sentences(sentences: tuple[str, ...]) -> None:
    if any(
        not sentence.strip() or len(sentence) > MAX_CLI_TEXT_CHARACTERS or "\x00" in sentence
        for sentence in sentences
    ):
        raise ValueError("Inline sentences must be non-empty, bounded, and NUL-free.")


__all__ = [
    "CliBasicFormat",
    "CliCommandPreview",
    "CliDevice",
    "CliEvaluateRequest",
    "CliEvaluationFormat",
    "CliFluencyScorer",
    "CliGenerateRequest",
    "CliGenerationBackend",
    "CliGuidance",
    "CliInventoryRequest",
    "CliPhonotacticScorer",
    "CliPreviewRequest",
    "CliQuantization",
    "CliReproducibility",
    "CliSelectRequest",
    "CliTargetMode",
    "CliVerbosity",
    "CliWeight",
    "CliWorkflow",
]
