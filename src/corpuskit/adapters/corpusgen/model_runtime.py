"""Secure worker adapters for CorpusGen hosted/local model capabilities."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import re
import stat
import string
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast

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
    GenerationScoringOptions,
    GenerationStoppingCriteria,
    GenerationStopReason,
    GenerationTarget,
    ReadabilityRange,
)
from corpuskit.domain.model_runtime import (
    DEFAULT_HOSTED_PROMPT_TEMPLATE,
    MAX_FLUENCY_SCORING_EVALUATIONS,
    CorpusPerplexity,
    FluencyScore,
    HostedExecutionManifest,
    HostedGenerationRequest,
    HostedGenerationResult,
    HostedModelPolicy,
    HostedModelSelection,
    HostedPromptTemplatePolicy,
    HostedUsage,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LanguageModelAnalysisResult,
    LocalGenerationRequest,
    LocalGenerationResult,
    LocalModelPolicy,
    ModelDevice,
    ModelExecutionManifest,
    ModelQuantization,
    PerplexitySentenceStatus,
    ReproducibilityClass,
    SecretReference,
    SentencePerplexity,
    WorkerModelProfile,
    quantization_value,
)
from corpuskit.domain.phon_rl import MAX_RL_CHECKPOINT_BYTES, PhonRlCheckpointCompatibility

_MILLION = Decimal(1_000_000)
_DIGEST_CHUNK_BYTES = 4 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 10_000
_MAX_PEFT_CONFIG_BYTES = 256 * 1024
_UNSAFE_WEIGHT_SUFFIXES = frozenset({".bin", ".pkl", ".pickle", ".pt", ".pth"})


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """Provider-neutral response; it contains usage but never credentials."""

    text: str
    input_tokens: int
    output_tokens: int


class ProviderCallError(RuntimeError):
    """Classified provider failure with a deliberately non-sensitive message."""

    def __init__(
        self,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__("The configured model provider did not complete the request.")


class HostedProviderClient(Protocol):
    def complete(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
        timeout_seconds: float,
    ) -> ProviderCompletion: ...


class SecretResolver(Protocol):
    def resolve(self, reference: SecretReference) -> str: ...


class EnvironmentSecretResolver:
    """Resolve only explicit ``secret://env/NAME`` references inside a worker."""

    _PREFIX = "secret://env/"
    _NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$", re.ASCII)

    def resolve(self, reference: SecretReference) -> str:
        if not reference.reference.startswith(self._PREFIX):
            raise EngineUnavailableError("model_runtime.secret.resolve")
        name = reference.reference.removeprefix(self._PREFIX)
        if self._NAME.fullmatch(name) is None:
            raise EngineUnavailableError("model_runtime.secret.resolve")
        value = os.environ.get(name)
        if value is None or not value.strip():
            raise EngineUnavailableError("model_runtime.secret.resolve")
        return value


class LiteLLMProviderClient:
    """Thin live client; retry classification stays provider-neutral and sanitized."""

    def complete(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
        timeout_seconds: float,
    ) -> ProviderCompletion:
        try:
            HostedModelSelection(
                provider=provider,
                model=model,
                connection_id="provider-client",
            )
        except ValidationError:
            raise EngineUnavailableError("model_runtime.hosted.provider_boundary") from None
        try:
            litellm = importlib.import_module("litellm")
        except ImportError:
            raise DependencyUnavailableError("model_runtime.hosted.dependency") from None
        callback_fields = (
            "callbacks",
            "success_callback",
            "failure_callback",
            "_async_success_callback",
            "_async_failure_callback",
        )
        if any(getattr(litellm, name, None) for name in callback_fields) or bool(
            getattr(litellm, "set_verbose", False)
        ):
            raise EngineUnavailableError("model_runtime.hosted.callback_isolation")
        try:
            response = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=api_key,
                timeout=timeout_seconds,
                num_retries=0,
                custom_llm_provider=provider,
            )
        except Exception as error:
            status = getattr(error, "status_code", None)
            retryable = status in {408, 409, 425, 429} or (
                isinstance(status, int) and 500 <= status <= 599
            )
            retry_after = getattr(error, "retry_after", None)
            safe_retry_after = (
                min(float(retry_after), 30.0)
                if isinstance(retry_after, (int, float))
                and not isinstance(retry_after, bool)
                and math.isfinite(float(retry_after))
                and retry_after >= 0
                else None
            )
            raise ProviderCallError(
                retryable=retryable,
                retry_after_seconds=safe_retry_after,
            ) from None
        try:
            text = response.choices[0].message.content
            usage = response.usage
            input_tokens = _integer_usage(usage, "prompt_tokens", "input_tokens")
            output_tokens = _integer_usage(usage, "completion_tokens", "output_tokens")
            if not isinstance(text, str):
                raise TypeError
            return ProviderCompletion(
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise EngineContractError("model_runtime.hosted.response") from None


@dataclass(frozen=True, slots=True)
class LoadedModelBundle:
    """Application-owned object identity shared through public CorpusGen constructors."""

    model: object
    tokenizer: object


class LocalModelLoader(Protocol):
    def load(
        self,
        pin: ImmutableModelPin,
        *,
        device: ModelDevice,
        quantization: ModelQuantization,
        artifact_sha256: str,
    ) -> LoadedModelBundle: ...


class PeftAdapterLoader(Protocol):
    def load(
        self,
        base: LoadedModelBundle,
        *,
        adapter_root: Path,
        compatibility: PhonRlCheckpointCompatibility,
        policy: LocalModelPolicy,
    ) -> LoadedModelBundle: ...


@dataclass(frozen=True, slots=True)
class OfflineLocalSnapshotResolver:
    """Pickle-safe exact-pin resolver confined to one operator cache root."""

    approved_cache_root: Path

    def __call__(self, pin: ImmutableModelPin) -> Path:
        root = self.approved_cache_root.absolute()
        try:
            hub = importlib.import_module("huggingface_hub")
            snapshot_download = cast(Callable[..., str], hub.snapshot_download)
        except (ImportError, AttributeError):
            raise DependencyUnavailableError("model_runtime.local.dependency") from None
        try:
            snapshot = Path(
                snapshot_download(
                    repo_id=pin.model,
                    revision=pin.revision,
                    cache_dir=str(root),
                    local_files_only=True,
                )
            ).absolute()
            resolved_root = root.resolve(strict=True)
            resolved_snapshot = snapshot.resolve(strict=True)
            if (
                resolved_snapshot.name != pin.revision
                or resolved_snapshot.parent.name != "snapshots"
                or not resolved_snapshot.is_relative_to(resolved_root)
            ):
                raise EngineUnavailableError("model_runtime.local.snapshot_layout")
            return resolved_snapshot
        except ApplicationError:
            raise
        except Exception:
            raise EngineUnavailableError("model_runtime.local.snapshot") from None


class TransformersLocalModelLoader:
    """Offline-only exact-revision causal-LM loader."""

    def __init__(
        self,
        snapshot_resolver: Callable[[ImmutableModelPin], Path] | None = None,
        *,
        approved_cache_root: Path | None = None,
    ) -> None:
        self._snapshot_resolver = snapshot_resolver or (
            OfflineLocalSnapshotResolver(approved_cache_root)
            if approved_cache_root is not None
            else _resolve_local_snapshot
        )
        if snapshot_resolver is not None and approved_cache_root is None:
            raise ValueError("Custom snapshot resolvers require an approved cache root.")
        self._approved_cache_root = approved_cache_root

    def load(
        self,
        pin: ImmutableModelPin,
        *,
        device: ModelDevice,
        quantization: ModelQuantization,
        artifact_sha256: str,
    ) -> LoadedModelBundle:
        try:
            transformers = importlib.import_module("transformers")
            auto_model = cast(Any, transformers.AutoModelForCausalLM)
            auto_tokenizer = cast(Any, transformers.AutoTokenizer)
        except (ImportError, AttributeError):
            raise DependencyUnavailableError("model_runtime.local.dependency") from None

        snapshot = self._snapshot_resolver(pin).absolute()
        approved_root = self._approved_cache_root or _default_repo_cache_root(snapshot, pin)
        if compute_snapshot_digest(snapshot, approved_cache_root=approved_root) != artifact_sha256:
            raise EngineUnavailableError("model_runtime.local.artifact_digest")
        snapshot = snapshot.resolve(strict=True)
        common: dict[str, object] = {
            "revision": pin.revision,
            "local_files_only": True,
            "trust_remote_code": False,
        }
        tokenizer = auto_tokenizer.from_pretrained(str(snapshot), **common)
        model_kwargs = dict(common)
        model_kwargs["use_safetensors"] = True
        if quantization is not ModelQuantization.NONE:
            try:
                quantization_config = cast(
                    Callable[..., object],
                    transformers.BitsAndBytesConfig,
                )
            except AttributeError:
                raise DependencyUnavailableError("model_runtime.local.quantization") from None
            if quantization is ModelQuantization.FOUR_BIT:
                model_kwargs["quantization_config"] = quantization_config(load_in_4bit=True)
            else:
                model_kwargs["quantization_config"] = quantization_config(load_in_8bit=True)
            model_kwargs["device_map"] = "auto"
        else:
            model_kwargs["device_map"] = device.value
        model = auto_model.from_pretrained(str(snapshot), **model_kwargs)
        if getattr(tokenizer, "pad_token", None) is None:
            tokenizer.pad_token = tokenizer.eos_token
        eval_method = getattr(model, "eval", None)
        if callable(eval_method):
            eval_method()
        return LoadedModelBundle(model=model, tokenizer=tokenizer)


class SafetensorsPeftAdapterLoader:
    """Merge a verified LoRA adapter without CorpusGen private-field assignment."""

    def load(
        self,
        base: LoadedModelBundle,
        *,
        adapter_root: Path,
        compatibility: PhonRlCheckpointCompatibility,
        policy: LocalModelPolicy,
    ) -> LoadedModelBundle:
        _validate_peft_compatibility(compatibility, policy)
        expected = {"adapter_config.json", "adapter_model.safetensors"}
        try:
            root = adapter_root.resolve(strict=True)
            files = tuple(root.iterdir())
            config_path = root / "adapter_config.json"
            weights_path = root / "adapter_model.safetensors"
            if (
                {item.name for item in files} != expected
                or any(
                    not item.is_file()
                    or item.is_symlink()
                    or item.resolve(strict=True).parent != root
                    for item in files
                )
                or root.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                or any(
                    item.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                    for item in files
                )
                or not 0 < config_path.stat().st_size <= _MAX_PEFT_CONFIG_BYTES
                or not 0 < weights_path.stat().st_size <= MAX_RL_CHECKPOINT_BYTES
            ):
                raise EngineUnavailableError("model_runtime.local.phon_rl_adapter_layout")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if (
                not isinstance(config, dict)
                or config.get("base_model_name_or_path") != policy.pin.model
                or config.get("revision") != policy.pin.revision
                or config.get("auto_mapping") is not None
                or str(config.get("peft_type", "")).upper() != "LORA"
                or str(config.get("task_type", "")).upper() != "CAUSAL_LM"
            ):
                raise EngineUnavailableError("model_runtime.local.phon_rl_adapter_config")
            peft = importlib.import_module("peft")
            peft_model = cast(Any, peft.PeftModel).from_pretrained(
                base.model,
                str(root),
                is_trainable=False,
                local_files_only=True,
            )
            merged = peft_model.merge_and_unload(safe_merge=True)
            eval_method = getattr(merged, "eval", None)
            if callable(eval_method):
                eval_method()
            return LoadedModelBundle(model=merged, tokenizer=base.tokenizer)
        except ApplicationError:
            raise
        except ImportError:
            raise DependencyUnavailableError(
                "model_runtime.local.phon_rl_adapter_dependency"
            ) from None
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            json.JSONDecodeError,
        ):
            raise EngineUnavailableError("model_runtime.local.phon_rl_adapter_load") from None


def _resolve_local_snapshot(pin: ImmutableModelPin) -> Path:
    try:
        hub = importlib.import_module("huggingface_hub")
        snapshot_download = cast(Callable[..., str], hub.snapshot_download)
    except (ImportError, AttributeError):
        raise DependencyUnavailableError("model_runtime.local.dependency") from None
    try:
        resolved = snapshot_download(
            repo_id=pin.model,
            revision=pin.revision,
            local_files_only=True,
        )
    except Exception:
        raise EngineUnavailableError("model_runtime.local.snapshot") from None
    return Path(resolved)


def _default_repo_cache_root(snapshot: Path, pin: ImmutableModelPin) -> Path:
    if snapshot.name != pin.revision or snapshot.parent.name != "snapshots":
        raise EngineUnavailableError("model_runtime.local.snapshot_layout")
    return snapshot.parent.parent


def compute_snapshot_digest(snapshot: Path, *, approved_cache_root: Path) -> str:
    """Hash a provisioned snapshot manifest and every file byte using the v1 algorithm."""

    try:
        approved_root = approved_cache_root.resolve(strict=True)
        snapshot = snapshot.resolve(strict=True)
        if not snapshot.is_dir() or not snapshot.is_relative_to(approved_root):
            raise EngineUnavailableError("model_runtime.local.snapshot")
        files = tuple(sorted(item for item in snapshot.rglob("*") if item.is_file()))
        if not files or len(files) > _MAX_SNAPSHOT_FILES:
            raise EngineUnavailableError("model_runtime.local.snapshot")
        if any(item.suffix.casefold() in _UNSAFE_WEIGHT_SUFFIXES for item in files):
            raise EngineUnavailableError("model_runtime.local.unsafe_weights")
        if not any(item.suffix.casefold() == ".safetensors" for item in files):
            raise EngineUnavailableError("model_runtime.local.safetensors_required")
        manifest = hashlib.sha256(b"corpuskit.snapshot.v1\0")
        for item in files:
            relative = item.relative_to(snapshot).as_posix().encode()
            resolved_item = item.resolve(strict=True)
            if not resolved_item.is_file() or not resolved_item.is_relative_to(approved_root):
                raise EngineUnavailableError("model_runtime.local.snapshot_boundary")
            file_digest = hashlib.sha256()
            size = 0
            with resolved_item.open("rb") as stream:
                while chunk := stream.read(_DIGEST_CHUNK_BYTES):
                    size += len(chunk)
                    file_digest.update(chunk)
            manifest.update(len(relative).to_bytes(4, "big"))
            manifest.update(relative)
            manifest.update(size.to_bytes(8, "big"))
            manifest.update(file_digest.digest())
        return manifest.hexdigest()
    except ApplicationError:
        raise
    except (OSError, ValueError, OverflowError):
        raise EngineUnavailableError("model_runtime.local.snapshot") from None


class CachedLocalModelLoader:
    """Bounded process-local LRU with explicit model eviction and cleanup."""

    def __init__(self, loader: LocalModelLoader, *, max_entries: int = 2) -> None:
        if not 1 <= max_entries <= 4:
            raise ValueError("The local model cache must contain between one and four entries.")
        self._loader = loader
        self._max_entries = max_entries
        self._entries: OrderedDict[
            tuple[str, str, ModelDevice, ModelQuantization, str],
            LoadedModelBundle,
        ] = OrderedDict()
        self._lock = RLock()

    def __getstate__(self) -> tuple[LocalModelLoader, int]:
        """Spawned activities start empty instead of serializing live model objects."""

        return self._loader, self._max_entries

    def __setstate__(self, state: tuple[LocalModelLoader, int]) -> None:
        loader, max_entries = state
        self._loader = loader
        self._max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = RLock()

    def load(
        self,
        pin: ImmutableModelPin,
        *,
        device: ModelDevice,
        quantization: ModelQuantization,
        artifact_sha256: str,
    ) -> LoadedModelBundle:
        key = (pin.model, pin.revision, device, quantization, artifact_sha256)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
            loaded = self._loader.load(
                pin,
                device=device,
                quantization=quantization,
                artifact_sha256=artifact_sha256,
            )
            self._entries[key] = loaded
            while len(self._entries) > self._max_entries:
                _, evicted = self._entries.popitem(last=False)
                self._cleanup(evicted)
            return loaded

    def clear(self) -> None:
        with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self._cleanup(entry)

    @staticmethod
    def _cleanup(bundle: LoadedModelBundle) -> None:
        close = getattr(bundle.model, "close", None)
        if callable(close):
            with suppress(Exception):
                close()


class BackendLike(Protocol):
    @property
    def name(self) -> str: ...

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **kwargs: object,
    ) -> list[dict[str, object]]: ...


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


class PerplexityMetricsLike(Protocol):
    per_sentence: list[float]
    corpus_perplexity: float
    mean_perplexity: float
    median_perplexity: float
    std_perplexity: float
    min_perplexity: float
    max_perplexity: float
    num_sentences: int
    num_tokens: int
    total_nll: float


class ModelRuntimeBindings(Protocol):
    """Injectable CorpusGen construction surface for deterministic acceptance tests."""

    def hosted_backend(
        self,
        request: HostedGenerationRequest,
        prompt_template: str,
        completion: Callable[[str], ProviderCompletion],
        request_delay_seconds: float,
    ) -> BackendLike: ...

    def local_backend(
        self,
        request: LocalGenerationRequest,
        bundle: LoadedModelBundle,
    ) -> BackendLike: ...

    def phon_rl_backend(
        self,
        request: LocalGenerationRequest,
        bundle: LoadedModelBundle,
    ) -> BackendLike: ...

    def set_seed(self, seed: int, device: ModelDevice) -> None: ...

    def targets(self, target: GenerationTarget) -> TargetLike: ...

    def scorer(
        self,
        targets: TargetLike,
        options: GenerationScoringOptions,
        fluency_scorer: Callable[[str | None], float] | None = None,
    ) -> ScorerLike: ...

    def readability_filter(
        self,
        readability_range: ReadabilityRange,
    ) -> Callable[[dict[str, object]], bool]: ...

    def loop(
        self,
        backend: BackendLike,
        targets: TargetLike,
        scorer: ScorerLike,
        stopping: GenerationStoppingCriteria,
        candidates_per_iteration: int,
        candidate_filter: Callable[[dict[str, object]], bool] | None,
        on_progress: Callable[[dict[str, object]], None],
    ) -> LoopLike: ...

    def fluency_scorer(self, bundle: LoadedModelBundle) -> Callable[[str | None], float]: ...

    def scoreable_mask(
        self,
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        max_length: int,
    ) -> tuple[bool, ...]: ...

    def corpus_perplexity(
        self,
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        batch_size: int,
        max_length: int,
    ) -> PerplexityMetricsLike: ...


class _CorpusgenModelRuntimeBindings:
    @staticmethod
    def hosted_backend(
        request: HostedGenerationRequest,
        prompt_template: str,
        completion: Callable[[str], ProviderCompletion],
        request_delay_seconds: float,
    ) -> BackendLike:
        from corpusgen.generate.backends.llm_api import LLMBackend

        class _AuthorizedLLMBackend(LLMBackend):  # type: ignore[misc]
            def _call_with_retry(self, **kwargs: Any) -> object:
                messages = kwargs.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise EngineContractError("model_runtime.hosted.prompt")
                prompt = messages[0].get("content")
                if not isinstance(prompt, str):
                    raise EngineContractError("model_runtime.hosted.prompt")
                response = completion(prompt)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=response.text))]
                )

            def generate(
                self,
                target_units: list[str],
                k: int = 5,
                **kwargs: Any,
            ) -> list[dict[str, object]]:
                candidates = cast(
                    list[dict[str, object]],
                    super().generate(target_units, k=k, **kwargs),
                )
                if not candidates:
                    raise EngineUnavailableError("model_runtime.hosted.empty_response")
                return candidates

        return cast(
            BackendLike,
            _AuthorizedLLMBackend(
                model=request.selection.model,
                language=request.language,
                api_key=None,
                prompt_template=prompt_template,
                temperature=request.temperature,
                max_tokens=request.max_tokens_per_request,
                max_retries=0,
                retry_delay=0.0,
                request_delay=request_delay_seconds,
            ),
        )

    @staticmethod
    def local_backend(
        request: LocalGenerationRequest,
        bundle: LoadedModelBundle,
    ) -> BackendLike:
        from corpusgen.generate.backends.local import LocalBackend

        class _PinnedLocalBackend(LocalBackend):  # type: ignore[misc]
            def _ensure_loaded(self) -> None:
                if self.is_loaded:
                    return
                self._model = bundle.model
                self._tokenizer = bundle.tokenizer

            def generate(
                self,
                target_units: list[str],
                k: int = 5,
                **kwargs: Any,
            ) -> list[dict[str, object]]:
                candidates = cast(
                    list[dict[str, object]],
                    super().generate(target_units, k=k, **kwargs),
                )
                if not candidates:
                    raise EngineUnavailableError("model_runtime.local.empty_response")
                return candidates

        return cast(
            BackendLike,
            _PinnedLocalBackend(
                model_name=request.selection.pin.model,
                language=request.language,
                device=request.selection.device.value,
                quantization=quantization_value(request.selection.quantization),
                max_new_tokens=request.max_new_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                do_sample=request.do_sample,
                model_kwargs={
                    "revision": request.selection.pin.revision,
                    "local_files_only": True,
                    "trust_remote_code": False,
                },
            ),
        )

    @staticmethod
    def phon_rl_backend(
        request: LocalGenerationRequest,
        bundle: LoadedModelBundle,
    ) -> BackendLike:
        """Application-owned backend passed through CorpusGen's public generation loop seam."""

        from corpusgen.generate.backends.local import DEFAULT_PROMPT_TEMPLATE
        from corpusgen.generate.phon_rl.policy import PhonRLStrategy

        strategy = PhonRLStrategy(adapter_path=None)

        class _PhonRlLogitsProcessor:
            def __call__(self, input_ids: object, scores: object) -> object:
                return strategy.modify_logits(input_ids, scores)

        class _PreparedPhonRlBackend:
            @property
            def name(self) -> str:
                return "local"

            def generate(
                self,
                target_units: list[str],
                k: int = 5,
                **kwargs: Any,
            ) -> list[dict[str, object]]:
                # Reimplement the short public algorithm because LocalBackend 0.1.7 keeps
                # its loaded objects private and offers no constructor injection seam.
                prompt = DEFAULT_PROMPT_TEMPLATE.format(
                    target_units=", ".join(target_units) if target_units else "(any)",
                    language=request.language,
                    k=k,
                )
                model = cast(Any, bundle.model)
                tokenizer = cast(Any, bundle.tokenizer)
                inputs = tokenizer(prompt, return_tensors="pt", padding=True)
                inputs = inputs.to(model.device)
                strategy.prepare(target_units, model, tokenizer)
                generate_kwargs: dict[str, object] = {
                    "max_new_tokens": request.max_new_tokens,
                    "do_sample": request.do_sample,
                    "num_return_sequences": k,
                    "pad_token_id": tokenizer.eos_token_id,
                    "logits_processor": [_PhonRlLogitsProcessor()],
                }
                if request.do_sample:
                    generate_kwargs.update(temperature=request.temperature, top_p=request.top_p)
                elif k > 1:
                    generate_kwargs["num_beams"] = k
                output = model.generate(**inputs, **generate_kwargs)
                prompt_length = inputs.input_ids.shape[-1]
                texts = tokenizer.batch_decode(
                    output[:, prompt_length:],
                    skip_special_tokens=True,
                )
                sentences = [
                    line.strip() for text in texts for line in text.splitlines() if line.strip()
                ]
                if not sentences:
                    raise EngineUnavailableError("model_runtime.local.empty_response")
                from corpusgen.g2p.manager import G2PManager

                selected = sentences[:k]
                phonemized = G2PManager().phonemize_batch(selected, language=request.language)
                if len(phonemized) != len(selected):
                    raise EngineContractError("model_runtime.local.phon_rl.g2p")
                candidates = [
                    {"text": text, "phonemes": result.phonemes}
                    for text, result in zip(selected, phonemized, strict=True)
                    if result.phonemes
                ]
                if not candidates:
                    raise EngineUnavailableError("model_runtime.local.empty_response")
                return candidates

        return cast(
            BackendLike,
            _PreparedPhonRlBackend(),
        )

    @staticmethod
    def set_seed(seed: int, device: ModelDevice) -> None:
        del device
        set_seed = cast(
            Callable[..., None],
            importlib.import_module("transformers").set_seed,
        )
        set_seed(seed, deterministic=False)

    @staticmethod
    def targets(target: GenerationTarget) -> TargetLike:
        from corpusgen.generate.phon_ctg.targets import PhoneticTargetInventory

        return cast(
            TargetLike,
            PhoneticTargetInventory(
                target_phonemes=list(target.phonemes),
                unit=target.unit.value,
            ),
        )

    @staticmethod
    def scorer(
        targets: TargetLike,
        options: GenerationScoringOptions,
        fluency_scorer: Callable[[str | None], float] | None = None,
    ) -> ScorerLike:
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
                fluency_scorer=fluency_scorer,
                readability_scorer=readability,
                coverage_weight=options.weights.coverage,
                phonotactic_weight=options.weights.phonotactic,
                fluency_weight=options.weights.fluency,
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
        backend: BackendLike,
        targets: TargetLike,
        scorer: ScorerLike,
        stopping: GenerationStoppingCriteria,
        candidates_per_iteration: int,
        candidate_filter: Callable[[dict[str, object]], bool] | None,
        on_progress: Callable[[dict[str, object]], None],
    ) -> LoopLike:
        from corpusgen.generate.phon_ctg.loop import GenerationLoop, StoppingCriteria

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
                candidates_per_iteration=candidates_per_iteration,
                candidate_filter=candidate_filter,
                on_progress=on_progress,
            ),
        )

    @staticmethod
    def fluency_scorer(bundle: LoadedModelBundle) -> Callable[[str | None], float]:
        from corpusgen.generate.scorers.fluency import PerplexityFluencyScorer

        return cast(
            Callable[[str | None], float],
            PerplexityFluencyScorer.from_model(bundle.model, bundle.tokenizer),
        )

    @staticmethod
    def scoreable_mask(
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        max_length: int,
    ) -> tuple[bool, ...]:
        tokenizer = cast(Callable[..., object], bundle.tokenizer)
        encoded = tokenizer(
            texts,
            padding=False,
            truncation=True,
            max_length=max_length,
        )
        if not isinstance(encoded, Mapping) or not isinstance(encoded.get("input_ids"), list):
            raise EngineContractError("model_runtime.analysis.tokenizer")
        rows = encoded["input_ids"]
        if len(rows) != len(texts) or any(
            not isinstance(row, list)
            or len(row) > max_length
            or any(
                not isinstance(token_id, int)
                or isinstance(token_id, bool)
                or token_id < 0
                or token_id > 10_000_000
                for token_id in row
            )
            for row in rows
        ):
            raise EngineContractError("model_runtime.analysis.tokenizer")
        return tuple(len(cast(list[object], row)) >= 2 for row in rows)

    @staticmethod
    def corpus_perplexity(
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        batch_size: int,
        max_length: int,
    ) -> PerplexityMetricsLike:
        from corpusgen.evaluate.perplexity import compute_corpus_perplexity

        return cast(
            PerplexityMetricsLike,
            compute_corpus_perplexity(
                texts,
                batch_size=batch_size,
                max_length=max_length,
                model=bundle.model,
                tokenizer=bundle.tokenizer,
            ),
        )


class _HostedBudgetRunner:
    def __init__(
        self,
        request: HostedGenerationRequest,
        policy: HostedModelPolicy,
        client: HostedProviderClient,
        *,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self._request = request
        self._policy = policy
        self._client = client
        self._clock = clock
        self._sleeper = sleeper
        self._deadline = clock() + request.activity_timeout_seconds
        self._requests = 0
        self._retries = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._reserved_input = 0
        self._reserved_output = 0
        self._actual_cost = Decimal(0)
        self._reserved_cost = Decimal(0)

    def complete(self, prompt: str, api_key: str) -> ProviderCompletion:
        input_reservation = max(1, len(prompt.encode("utf-8")))
        last_failure = False
        for attempt in range(self._request.retry.max_retries + 1):
            self._sleep_before_request()
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                raise EngineUnavailableError("model_runtime.hosted.deadline")
            self._reserve(input_reservation, is_retry=attempt > 0)
            try:
                response = self._client.complete(
                    provider=self._request.selection.provider,
                    model=self._request.selection.model,
                    prompt=prompt,
                    temperature=self._request.temperature,
                    max_tokens=self._request.max_tokens_per_request,
                    api_key=api_key,
                    timeout_seconds=min(
                        self._request.retry.request_timeout_seconds,
                        remaining,
                    ),
                )
            except ProviderCallError as error:
                last_failure = True
                if not error.retryable or attempt >= self._request.retry.max_retries:
                    raise EngineUnavailableError("model_runtime.hosted.provider") from None
                self._sleep_before_retry(attempt, error.retry_after_seconds)
                continue
            self._record(response, input_reservation)
            if not response.text.strip():
                raise EngineUnavailableError("model_runtime.hosted.empty_response")
            return response
        if last_failure:
            raise EngineUnavailableError("model_runtime.hosted.provider")
        raise EngineUnavailableError("model_runtime.hosted.deadline")

    def _sleep_before_request(self) -> None:
        delay = self._policy.request_delay_seconds
        if delay <= 0:
            return
        if self._clock() + delay >= self._deadline:
            raise EngineUnavailableError("model_runtime.hosted.deadline")
        self._sleeper(delay)

    def usage(self) -> HostedUsage:
        return HostedUsage(
            requests=self._requests,
            retries=self._retries,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            reserved_input_tokens=self._reserved_input,
            reserved_output_tokens=self._reserved_output,
            actual_cost_usd=self._actual_cost,
            reserved_cost_usd=self._reserved_cost,
        )

    def _reserve(self, input_tokens: int, *, is_retry: bool) -> None:
        output_tokens = self._request.max_tokens_per_request
        next_requests = self._requests + 1
        next_input = self._reserved_input + input_tokens
        next_output = self._reserved_output + output_tokens
        next_cost = _price(self._policy, next_input, next_output)
        if (
            next_requests > self._request.budget.max_requests
            or next_input > self._request.budget.max_input_tokens
            or next_output > self._request.budget.max_output_tokens
            or next_cost > self._request.budget.max_cost_usd
        ):
            raise InvalidRequestError("model_runtime.hosted.budget_exhausted")
        self._requests = next_requests
        self._reserved_input = next_input
        self._reserved_output = next_output
        self._reserved_cost = next_cost
        if is_retry:
            self._retries += 1

    def _record(self, response: ProviderCompletion, input_reservation: int) -> None:
        if (
            response.input_tokens < 0
            or response.output_tokens < 0
            or response.input_tokens > input_reservation
            or response.output_tokens > self._request.max_tokens_per_request
        ):
            raise EngineContractError("model_runtime.hosted.usage")
        self._input_tokens += response.input_tokens
        self._output_tokens += response.output_tokens
        self._actual_cost = _price(
            self._policy,
            self._input_tokens,
            self._output_tokens,
        )
        if self._actual_cost > self._request.budget.max_cost_usd:
            raise EngineContractError("model_runtime.hosted.usage")

    def _sleep_before_retry(self, attempt: int, retry_after_seconds: float | None) -> None:
        if retry_after_seconds is not None and (
            not math.isfinite(retry_after_seconds) or retry_after_seconds < 0
        ):
            raise EngineContractError("model_runtime.hosted.retry_after")
        exponential = min(
            self._request.retry.base_delay_seconds * (2**attempt),
            self._request.retry.max_delay_seconds,
        )
        delay = retry_after_seconds if retry_after_seconds is not None else exponential
        delay = min(delay, self._request.retry.max_delay_seconds)
        if delay <= 0:
            return
        if self._clock() + delay >= self._deadline:
            raise EngineUnavailableError("model_runtime.hosted.deadline")
        self._sleeper(delay)


@dataclass(slots=True)
class _AcceptedRow:
    source_id: str
    text: str
    phonemes: tuple[str, ...]
    coverage_gain: int
    iteration: int = 0


class _DeduplicatingBackend:
    def __init__(self, backend: BackendLike, namespace: str) -> None:
        self._backend = backend
        self._namespace = namespace
        self._seen: set[str] = set()

    @property
    def name(self) -> str:
        return self._backend.name

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        candidates = self._backend.generate(target_units, k=k, **kwargs)
        if not isinstance(candidates, list) or not candidates:
            raise EngineUnavailableError("model_runtime.generation.empty_response")
        unique: list[dict[str, object]] = []
        for candidate in candidates:
            text = candidate.get("text")
            phonemes = candidate.get("phonemes")
            if (
                not isinstance(text, str)
                or not text.strip()
                or not isinstance(phonemes, list)
                or not phonemes
                or any(not isinstance(item, str) or not item for item in phonemes)
            ):
                raise EngineContractError("model_runtime.generation.candidate")
            identity = _candidate_id(self._namespace, text, cast(list[str], phonemes))
            if identity in self._seen:
                continue
            self._seen.add(identity)
            item = dict(candidate)
            item["_source_id"] = identity
            unique.append(item)
        return unique


class _BoundedFluencyScorer:
    """Validate and memoize model scores so rank/commit never infer twice."""

    def __init__(
        self,
        scorer: Callable[[str | None], float],
        *,
        max_entries: int = MAX_FLUENCY_SCORING_EVALUATIONS,
    ) -> None:
        self._scorer = scorer
        self._max_entries = max_entries
        self._scores: dict[str, float] = {}

    def __call__(self, text: str | None) -> float:
        if text is None or not text.strip():
            raise EngineContractError("model_runtime.fluency.text")
        cached = self._scores.get(text)
        if cached is not None:
            return cached
        if len(self._scores) >= self._max_entries:
            raise EngineContractError("model_runtime.fluency.bound")
        score = self._scorer(text)
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
            or not 0.0 <= float(score) <= 1.0
        ):
            raise EngineContractError("model_runtime.fluency.result")
        normalized = float(score)
        self._scores[text] = normalized
        return normalized


class _GeneratedScorer:
    def __init__(self, scorer: ScorerLike) -> None:
        self._scorer = scorer
        self._ranked: list[tuple[tuple[str, tuple[str, ...]], str]] = []
        self._accepted_ids: set[str] = set()
        self.accepted: list[_AcceptedRow] = []

    def rank(
        self,
        candidates: list[dict[str, object]],
        top_k: int | None = None,
    ) -> list[ScoreResultLike]:
        lookup: dict[tuple[str, tuple[str, ...]], str] = {}
        for candidate in candidates:
            text = candidate.get("text")
            phonemes = candidate.get("phonemes")
            source_id = candidate.get("_source_id")
            if not isinstance(text, str) or not isinstance(phonemes, list):
                raise EngineContractError("model_runtime.generation.rank")
            if not isinstance(source_id, str) or source_id in self._accepted_ids:
                raise EngineContractError("model_runtime.generation.rank")
            lookup[(text, tuple(cast(list[str], phonemes)))] = source_id
        ranked = self._scorer.rank(candidates, top_k=top_k)
        self._ranked = []
        for result in ranked:
            key = (result.text or "", tuple(result.phonemes))
            source_id = lookup.get(key)
            if source_id is None:
                raise EngineContractError("model_runtime.generation.rank")
            self._ranked.append((key, source_id))
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
                for candidate_key, candidate_id in self._ranked
                if candidate_key == key and candidate_id not in self._accepted_ids
            ),
            None,
        )
        if source_id is None:
            raise EngineContractError("model_runtime.generation.commit")
        result = self._scorer.score_and_commit(phonemes, sentence_index, text=text)
        if result.coverage_gain <= 0:
            raise EngineContractError("model_runtime.generation.commit")
        self._accepted_ids.add(source_id)
        self.accepted.append(
            _AcceptedRow(
                source_id=source_id,
                text=text or "",
                phonemes=tuple(phonemes),
                coverage_gain=result.coverage_gain,
            )
        )
        return result


class CorpusgenModelRuntimeAdapter:
    """Worker-only hosted generation, pinned local generation and shared LM analysis."""

    def __init__(
        self,
        *,
        secret_resolver: SecretResolver | None = None,
        provider_client: HostedProviderClient | None = None,
        model_loader: LocalModelLoader | None = None,
        peft_adapter_loader: PeftAdapterLoader | None = None,
        bindings: ModelRuntimeBindings | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self._provider_client = provider_client or LiteLLMProviderClient()
        self._model_loader = model_loader or CachedLocalModelLoader(TransformersLocalModelLoader())
        self._peft_adapter_loader = peft_adapter_loader or SafetensorsPeftAdapterLoader()
        self._bindings = bindings or _CorpusgenModelRuntimeBindings()
        self._clock = clock
        self._sleeper = sleeper

    def run_hosted(
        self,
        request: HostedGenerationRequest,
        policy: HostedModelPolicy,
    ) -> HostedGenerationResult:
        operation = "model_runtime.hosted.run"
        try:
            _require_hosted_policy(request, policy)
            api_key = self._secret_resolver.resolve(policy.credential_ref)
            if not isinstance(api_key, str) or not api_key.strip():
                raise EngineUnavailableError("model_runtime.secret.resolve")
            prompt_template, prompt_policy = _resolve_hosted_prompt_template(
                request,
                policy,
                self._secret_resolver,
            )
            runner = _HostedBudgetRunner(
                request,
                policy,
                self._provider_client,
                clock=self._clock,
                sleeper=self._sleeper,
            )
            backend = self._bindings.hosted_backend(
                request,
                prompt_template,
                lambda prompt: runner.complete(prompt, api_key),
                policy.request_delay_seconds,
            )
            common = self._run_generation(
                backend,
                target=request.target,
                stopping=request.stopping,
                scoring=request.scoring,
                candidates_per_iteration=request.candidates_per_iteration,
                namespace=f"hosted|{request.selection.provider}|{request.selection.model}",
                expected_backend="llm_api",
            )
            return HostedGenerationResult(
                manifest=HostedExecutionManifest(
                    provider=request.selection.provider,
                    model=request.selection.model,
                    temperature=request.temperature,
                    max_tokens_per_request=request.max_tokens_per_request,
                    prompt_template_sha256=hashlib.sha256(
                        prompt_template.encode("utf-8")
                    ).hexdigest(),
                    prompt_template_id=(
                        prompt_policy.template_id if prompt_policy is not None else None
                    ),
                    custom_prompt_template=prompt_policy is not None,
                    request_delay_seconds=policy.request_delay_seconds,
                    retry=request.retry,
                    budget=request.budget,
                    whole_activity_timeout_seconds=request.activity_timeout_seconds,
                ),
                usage=runner.usage(),
                **common,
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

    def run_local(
        self,
        request: LocalGenerationRequest,
        policy: LocalModelPolicy,
        profile: WorkerModelProfile,
    ) -> LocalGenerationResult:
        operation = "model_runtime.local.run"
        try:
            _require_local_policy(request, policy, profile)
            bundle = self._model_loader.load(
                request.selection.pin,
                device=request.selection.device,
                quantization=request.selection.quantization,
                artifact_sha256=policy.artifact_sha256,
            )
            if request.phon_rl_adapter is not None:
                raise InvalidRequestError("model_runtime.local.phon_rl_materialization_required")
            backend = self._bindings.local_backend(request, bundle)
            fluency_scorer = (
                _BoundedFluencyScorer(self._bindings.fluency_scorer(bundle))
                if request.scoring.weights.fluency > 0
                else None
            )
            self._bindings.set_seed(request.seed, request.selection.device)
            common = self._run_generation(
                backend,
                target=request.target,
                stopping=request.stopping,
                scoring=request.scoring,
                candidates_per_iteration=request.candidates_per_iteration,
                namespace=(f"local|{request.selection.pin.model}|{request.selection.pin.revision}"),
                expected_backend="local",
                fluency_scorer=fluency_scorer,
            )
            return LocalGenerationResult(
                model=_manifest(
                    request.selection.pin,
                    request.selection.device,
                    request.selection.quantization,
                    artifact_sha256=policy.artifact_sha256,
                    sampling_enabled=request.do_sample,
                    seed=request.seed,
                    fluency_scorer=("perplexity" if request.scoring.weights.fluency > 0 else None),
                ),
                reproducibility=ReproducibilityClass.BEST_EFFORT,
                **common,
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

    def run_local_phon_rl(
        self,
        request: LocalGenerationRequest,
        policy: LocalModelPolicy,
        profile: WorkerModelProfile,
        *,
        adapter_root: Path,
        compatibility: PhonRlCheckpointCompatibility,
    ) -> LocalGenerationResult:
        operation = "model_runtime.local.phon_rl.run"
        try:
            _require_local_policy(request, policy, profile)
            if request.phon_rl_adapter is None or not policy.allow_phon_rl_adapters:
                raise InvalidRequestError("model_runtime.local.phon_rl_adapter_policy")
            base = self._model_loader.load(
                request.selection.pin,
                device=request.selection.device,
                quantization=request.selection.quantization,
                artifact_sha256=policy.artifact_sha256,
            )
            bundle = self._peft_adapter_loader.load(
                base,
                adapter_root=adapter_root,
                compatibility=compatibility,
                policy=policy,
            )
            backend = self._bindings.phon_rl_backend(request, bundle)
            fluency_scorer = (
                _BoundedFluencyScorer(self._bindings.fluency_scorer(bundle))
                if request.scoring.weights.fluency > 0
                else None
            )
            self._bindings.set_seed(request.seed, request.selection.device)
            common = self._run_generation(
                backend,
                target=request.target,
                stopping=request.stopping,
                scoring=request.scoring,
                candidates_per_iteration=request.candidates_per_iteration,
                namespace=(
                    f"local|phon_rl|{request.selection.pin.model}|"
                    f"{request.selection.pin.revision}|"
                    f"{request.phon_rl_adapter.checkpoint_sha256}"
                ),
                expected_backend="local",
                fluency_scorer=fluency_scorer,
            )
            return LocalGenerationResult(
                model=_manifest(
                    request.selection.pin,
                    request.selection.device,
                    request.selection.quantization,
                    artifact_sha256=policy.artifact_sha256,
                    sampling_enabled=request.do_sample,
                    seed=request.seed,
                    fluency_scorer=("perplexity" if request.scoring.weights.fluency > 0 else None),
                    guidance_strategy="phon_rl",
                    adapter_artifact_sha256=request.phon_rl_adapter.artifact_sha256,
                    adapter_checkpoint_sha256=request.phon_rl_adapter.checkpoint_sha256,
                ),
                reproducibility=ReproducibilityClass.BEST_EFFORT,
                **common,
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

    def analyze_language_model(
        self,
        request: LanguageModelAnalysisRequest,
        policy: LocalModelPolicy,
        profile: WorkerModelProfile,
    ) -> LanguageModelAnalysisResult:
        operation = "model_runtime.analysis.run"
        try:
            _require_analysis_policy(request, policy, profile)
            bundle = self._model_loader.load(
                request.selection.pin,
                device=request.selection.device,
                quantization=request.selection.quantization,
                artifact_sha256=policy.artifact_sha256,
            )
            fluency_scorer = _BoundedFluencyScorer(
                self._bindings.fluency_scorer(bundle),
                max_entries=len(request.texts),
            )
            fluency_values = {item.text: fluency_scorer(item.text) for item in request.texts}
            fluency = tuple(
                FluencyScore(source_id=item.source_id, score=fluency_values[item.text])
                for item in request.texts
            )
            composite_scoring = None
            if request.composite_scoring is not None:
                from corpuskit.adapters.corpusgen.scoring import CorpusgenScoringAdapter

                def cached_fluency(text: str | None) -> float:
                    if text is None or text not in fluency_values:
                        raise EngineContractError("model_runtime.analysis.composite_mapping")
                    return fluency_values[text]

                composite_scoring = CorpusgenScoringAdapter(
                    authorized_fluency_scorer=cached_fluency
                ).composite(request.composite_scoring)
            scoreable = self._bindings.scoreable_mask(
                [item.text for item in request.texts],
                bundle,
                max_length=request.max_length,
            )
            raw = self._bindings.corpus_perplexity(
                [item.text for item in request.texts],
                bundle,
                batch_size=request.batch_size,
                max_length=request.max_length,
            )
            perplexity = CorpusPerplexity(
                per_sentence=tuple(raw.per_sentence),
                corpus_perplexity=raw.corpus_perplexity,
                mean_perplexity=raw.mean_perplexity,
                median_perplexity=raw.median_perplexity,
                std_perplexity=raw.std_perplexity,
                min_perplexity=raw.min_perplexity,
                max_perplexity=raw.max_perplexity,
                num_sentences=raw.num_sentences,
                num_tokens=raw.num_tokens,
                total_nll=raw.total_nll,
            )
            if len(scoreable) != len(request.texts) or sum(scoreable) != perplexity.num_sentences:
                raise EngineContractError("model_runtime.analysis.source_mapping")
            values = iter(perplexity.per_sentence)
            sentence_perplexities = tuple(
                SentencePerplexity(
                    source_id=item.source_id,
                    status=(
                        PerplexitySentenceStatus.SCORED
                        if is_scoreable
                        else PerplexitySentenceStatus.SKIPPED_TOO_SHORT
                    ),
                    perplexity=next(values) if is_scoreable else None,
                )
                for item, is_scoreable in zip(request.texts, scoreable, strict=True)
            )
            return LanguageModelAnalysisResult(
                model=_manifest(
                    request.selection.pin,
                    request.selection.device,
                    request.selection.quantization,
                    artifact_sha256=policy.artifact_sha256,
                    fluency_scorer="perplexity",
                ),
                fluency=fluency,
                perplexity=perplexity,
                sentence_perplexities=sentence_perplexities,
                input_sentence_count=len(request.texts),
                scored_sentence_count=perplexity.num_sentences,
                composite_scoring=composite_scoring,
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

    def _run_generation(
        self,
        backend: BackendLike,
        *,
        target: GenerationTarget,
        stopping: GenerationStoppingCriteria,
        scoring: GenerationScoringOptions,
        candidates_per_iteration: int,
        namespace: str,
        expected_backend: str,
        fluency_scorer: Callable[[str | None], float] | None = None,
    ) -> dict[str, object]:
        targets = self._bindings.targets(target)
        scorer = _GeneratedScorer(self._bindings.scorer(targets, scoring, fluency_scorer))
        deduplicated = _DeduplicatingBackend(backend, namespace)
        candidate_filter = None
        if scoring.readability_filter is not None:
            candidate_filter = self._bindings.readability_filter(scoring.readability_filter)

        def progress(data: dict[str, object]) -> None:
            if not scorer.accepted:
                raise EngineContractError("model_runtime.generation.progress")
            iteration = data.get("iteration")
            if not isinstance(iteration, int) or iteration < 1:
                raise EngineContractError("model_runtime.generation.progress")
            scorer.accepted[-1].iteration = iteration

        loop = self._bindings.loop(
            cast(BackendLike, deduplicated),
            targets,
            cast(ScorerLike, scorer),
            stopping,
            candidates_per_iteration,
            candidate_filter,
            progress,
        )
        raw = loop.run()
        if raw.backend != expected_backend or raw.unit != target.unit.value:
            raise EngineContractError("model_runtime.generation.result")
        try:
            stop_reason = GenerationStopReason(raw.stop_reason)
            accepted = tuple(
                AcceptedCandidate(
                    source_id=item.source_id,
                    text=item.text,
                    phonemes=item.phonemes,
                    iteration=item.iteration,
                    coverage_gain=item.coverage_gain,
                )
                for item in scorer.accepted
            )
            if any(item.iteration == 0 for item in accepted):
                raise EngineContractError("model_runtime.generation.result")
            return {
                "accepted": accepted,
                "coverage": raw.coverage,
                "covered_units": tuple(sorted(raw.covered_units)),
                "missing_units": tuple(sorted(raw.missing_units)),
                "iterations": raw.iterations,
                "elapsed_seconds": raw.elapsed_seconds,
                "stop_reason": stop_reason,
            }
        except EngineContractError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise EngineContractError("model_runtime.generation.result") from None


def _resolve_hosted_prompt_template(
    request: HostedGenerationRequest,
    policy: HostedModelPolicy,
    resolver: SecretResolver,
) -> tuple[str, HostedPromptTemplatePolicy | None]:
    if request.prompt_template_id is None:
        return DEFAULT_HOSTED_PROMPT_TEMPLATE, None
    prompt_policy = next(
        (
            item
            for item in policy.prompt_templates
            if item.template_id == request.prompt_template_id
        ),
        None,
    )
    if prompt_policy is None:
        raise InvalidRequestError("model_runtime.hosted.prompt_allowlist")
    template = _load_hosted_prompt_template(prompt_policy, resolver)
    try:
        rendered = template.format(
            target_units=", ".join(request.target.phonemes),
            language=request.language,
            k=request.candidates_per_iteration,
        )
    except (KeyError, ValueError, AttributeError):
        raise EngineUnavailableError("model_runtime.hosted.prompt_schema") from None
    if len(rendered.encode("utf-8")) > prompt_policy.max_rendered_bytes:
        raise InvalidRequestError("model_runtime.hosted.prompt_rendered_limit")
    return template, prompt_policy


def _load_hosted_prompt_template(
    prompt_policy: HostedPromptTemplatePolicy,
    resolver: SecretResolver,
) -> str:
    template = resolver.resolve(prompt_policy.template_ref)
    if not isinstance(template, str) or not template or "\x00" in template:
        raise EngineUnavailableError("model_runtime.hosted.prompt_secret")
    encoded = template.encode("utf-8")
    if (
        len(encoded) != prompt_policy.size_bytes
        or hashlib.sha256(encoded).hexdigest() != prompt_policy.sha256
    ):
        raise EngineUnavailableError("model_runtime.hosted.prompt_integrity")
    try:
        parsed = tuple(string.Formatter().parse(template))
    except ValueError:
        raise EngineUnavailableError("model_runtime.hosted.prompt_schema") from None
    fields = {field_name for _, field_name, _, _ in parsed if field_name is not None}
    if "target_units" not in fields or not fields <= {"target_units", "language", "k"}:
        raise EngineUnavailableError("model_runtime.hosted.prompt_schema")
    if any(format_spec or conversion is not None for _, _, format_spec, conversion in parsed):
        raise EngineUnavailableError("model_runtime.hosted.prompt_schema")
    return template


def validate_hosted_policy_secrets(
    policy: HostedModelPolicy,
    resolver: SecretResolver,
) -> None:
    """Fail worker startup if a hosted credential or prompt secret is unavailable or invalid."""

    credential = resolver.resolve(policy.credential_ref)
    if not isinstance(credential, str) or not credential.strip():
        raise EngineUnavailableError("model_runtime.secret.resolve")
    for prompt_policy in policy.prompt_templates:
        _load_hosted_prompt_template(prompt_policy, resolver)


def _require_hosted_policy(
    request: HostedGenerationRequest,
    policy: HostedModelPolicy,
) -> None:
    if (
        policy.provider != request.selection.provider
        or policy.model != request.selection.model
        or policy.connection_id != request.selection.connection_id
        or request.max_tokens_per_request > policy.max_output_tokens_per_request
        or (
            request.prompt_template_id is not None
            and request.prompt_template_id
            not in {item.template_id for item in policy.prompt_templates}
        )
    ):
        raise InvalidRequestError("model_runtime.hosted.allowlist")


def _require_local_policy(
    request: LocalGenerationRequest,
    policy: LocalModelPolicy,
    profile: WorkerModelProfile,
) -> None:
    _require_pin_and_execution(
        request.selection.pin,
        request.selection.device,
        request.selection.quantization,
        policy,
        profile,
    )


def _require_analysis_policy(
    request: LanguageModelAnalysisRequest,
    policy: LocalModelPolicy,
    profile: WorkerModelProfile,
) -> None:
    _require_pin_and_execution(
        request.selection.pin,
        request.selection.device,
        request.selection.quantization,
        policy,
        profile,
    )


def _require_pin_and_execution(
    pin: ImmutableModelPin,
    device: ModelDevice,
    quantization: ModelQuantization,
    policy: LocalModelPolicy,
    profile: WorkerModelProfile,
) -> None:
    if (
        pin != policy.pin
        or device not in policy.allowed_devices
        or quantization not in policy.allowed_quantizations
    ):
        raise InvalidRequestError("model_runtime.local.allowlist")
    if device is ModelDevice.CUDA and profile is not WorkerModelProfile.LOCAL_GPU:
        raise InvalidRequestError("model_runtime.local.worker_profile")
    if quantization is not ModelQuantization.NONE and device is not ModelDevice.CUDA:
        raise InvalidRequestError("model_runtime.local.quantization")


def _manifest(
    pin: ImmutableModelPin,
    device: ModelDevice,
    quantization: ModelQuantization,
    *,
    artifact_sha256: str,
    sampling_enabled: bool | None = None,
    seed: int | None = None,
    fluency_scorer: Literal["perplexity"] | None = None,
    guidance_strategy: Literal["phon_rl"] | None = None,
    adapter_artifact_sha256: str | None = None,
    adapter_checkpoint_sha256: str | None = None,
) -> ModelExecutionManifest:
    return ModelExecutionManifest(
        model=pin.model,
        revision=pin.revision,
        artifact_sha256=artifact_sha256,
        device=device,
        quantization=quantization,
        fluency_scorer=fluency_scorer,
        sampling_enabled=sampling_enabled,
        seed=seed,
        guidance_strategy=guidance_strategy,
        adapter_artifact_sha256=adapter_artifact_sha256,
        adapter_checkpoint_sha256=adapter_checkpoint_sha256,
    )


def _validate_peft_compatibility(
    compatibility: PhonRlCheckpointCompatibility,
    policy: LocalModelPolicy,
) -> None:
    expected = {
        "corpusgen": _installed_version("corpusgen"),
        "torch": _installed_version("torch"),
        "transformers": _installed_version("transformers"),
        "peft": _installed_version("peft"),
    }
    if (
        not compatibility.peft_adapter
        or compatibility.base_model_id != policy.pin.model
        or compatibility.base_model_revision != policy.pin.revision
        or compatibility.base_model_snapshot_sha256 != policy.artifact_sha256
        or compatibility.tokenizer_id != policy.pin.model
        or compatibility.tokenizer_revision != policy.pin.revision
        or compatibility.tokenizer_snapshot_sha256 != policy.artifact_sha256
        or compatibility.corpusgen_version != expected["corpusgen"]
        or compatibility.torch_version != expected["torch"]
        or compatibility.transformers_version != expected["transformers"]
        or compatibility.peft_version != expected["peft"]
    ):
        raise InvalidRequestError("model_runtime.local.phon_rl_adapter_compatibility")


def _installed_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        raise DependencyUnavailableError("model_runtime.local.phon_rl_adapter_dependency") from None


def _price(policy: HostedModelPolicy, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * policy.input_cost_per_million_usd
        + Decimal(output_tokens) * policy.output_cost_per_million_usd
    ) / _MILLION


def _candidate_id(namespace: str, text: str, phonemes: list[str]) -> str:
    digest = hashlib.sha256(f"{namespace}\0{text}\0{'\0'.join(phonemes)}".encode()).hexdigest()
    return f"generated:{digest[:48]}"


def _integer_usage(usage: object, *names: str) -> int:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    raise ValueError("Provider usage is unavailable.")


__all__ = [
    "CachedLocalModelLoader",
    "CorpusgenModelRuntimeAdapter",
    "EnvironmentSecretResolver",
    "HostedProviderClient",
    "LiteLLMProviderClient",
    "LoadedModelBundle",
    "LocalModelLoader",
    "ModelRuntimeBindings",
    "OfflineLocalSnapshotResolver",
    "ProviderCallError",
    "ProviderCompletion",
    "SecretResolver",
    "TransformersLocalModelLoader",
    "compute_snapshot_digest",
    "validate_hosted_policy_secrets",
]
