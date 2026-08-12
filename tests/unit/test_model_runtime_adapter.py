"""Deterministic provider, offline loader and shared-model adapter acceptance."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import pickle
import sys
from collections import UserDict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from corpuskit.adapters.corpusgen.model_runtime import (
    CachedLocalModelLoader,
    CorpusgenModelRuntimeAdapter,
    EnvironmentSecretResolver,
    LiteLLMProviderClient,
    LoadedModelBundle,
    OfflineLocalSnapshotResolver,
    ProviderCallError,
    ProviderCompletion,
    SafetensorsPeftAdapterLoader,
    TransformersLocalModelLoader,
    _BoundedFluencyScorer,
    _CorpusgenModelRuntimeBindings,
    _DeduplicatingBackend,
    _GeneratedScorer,
    _resolve_local_snapshot,
    compute_snapshot_digest,
)
from corpuskit.domain.errors import (
    ApplicationError,
    DependencyUnavailableError,
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.generation import (
    CompositeScoringRequest,
    GenerationScoringOptions,
    GenerationStoppingCriteria,
    GenerationTarget,
    ReadabilityRange,
    RepositoryCandidate,
    ScoreWeights,
)
from corpuskit.domain.model_runtime import (
    DEFAULT_HOSTED_PROMPT_TEMPLATE,
    AnalysisText,
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedModelSelection,
    HostedPromptTemplatePolicy,
    HostedRunBudget,
    ImmutableModelPin,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
    LocalModelPolicy,
    LocalModelSelection,
    ModelDevice,
    ModelQuantization,
    PerplexitySentenceStatus,
    PhonRlAdapterSelection,
    ProviderRetryPolicy,
    ReproducibilityClass,
    SecretReference,
    WorkerModelProfile,
)
from corpuskit.domain.phon_rl import PhonRlCheckpointCompatibility

SECRET = SecretReference(reference="secret://env/MODEL_PROVIDER_KEY")
PROMPT_SECRET = SecretReference(reference="secret://env/MODEL_PROMPT_TEMPLATE")
RAW_SECRET = "raw-secret-value-never-serialize"
CUSTOM_PROMPT = "Use {target_units} for {k} lines in {language}."
PIN = ImmutableModelPin(model="acme/tiny-causal", revision="a" * 40)
ARTIFACT_DIGEST = "b" * 64


def peft_compatibility(**changes: object) -> PhonRlCheckpointCompatibility:
    values: dict[str, object] = {
        "base_model_id": PIN.model,
        "base_model_revision": PIN.revision,
        "base_model_snapshot_sha256": ARTIFACT_DIGEST,
        "tokenizer_id": PIN.model,
        "tokenizer_revision": PIN.revision,
        "tokenizer_snapshot_sha256": ARTIFACT_DIGEST,
        "corpusgen_version": importlib.metadata.version("corpusgen"),
        "torch_version": importlib.metadata.version("torch"),
        "transformers_version": importlib.metadata.version("transformers"),
        "peft_version": importlib.metadata.version("peft"),
        "peft_adapter": True,
    }
    values.update(changes)
    return PhonRlCheckpointCompatibility(**values)


def peft_root(tmp_path: Path) -> Path:
    root = tmp_path / "adapter"
    root.mkdir(parents=True)
    (root / "adapter_config.json").write_text(
        json.dumps(
            {
                "auto_mapping": None,
                "base_model_name_or_path": PIN.model,
                "peft_type": "LORA",
                "revision": PIN.revision,
                "task_type": "CAUSAL_LM",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (root / "adapter_model.safetensors").write_bytes(b"safe adapter bytes")
    for item in root.iterdir():
        item.chmod(0o400)
    root.chmod(0o500)
    return root


def _snapshot(root: Path) -> Path:
    snapshot = root / "snapshot"
    snapshot.mkdir()
    (snapshot / "model.safetensors").write_bytes(b"safe tensor bytes")
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    return snapshot


def hosted_policy(
    *,
    custom_prompt: bool = False,
    request_delay_seconds: float = 0.0,
) -> HostedModelPolicy:
    return HostedModelPolicy(
        provider="openai",
        model="openai/demo-model",
        connection_id="demo-provider",
        credential_ref=SECRET,
        input_cost_per_million_usd=Decimal("1"),
        output_cost_per_million_usd=Decimal("2"),
        max_output_tokens_per_request=128,
        request_delay_seconds=request_delay_seconds,
        prompt_templates=(
            HostedPromptTemplatePolicy(
                template_id="coverage-v1",
                template_ref=PROMPT_SECRET,
                sha256=hashlib.sha256(CUSTOM_PROMPT.encode()).hexdigest(),
                size_bytes=len(CUSTOM_PROMPT.encode()),
                max_rendered_bytes=1024,
            ),
        )
        if custom_prompt
        else (),
    )


def hosted_request(**changes: object) -> HostedGenerationRequest:
    values: dict[str, object] = {
        "selection": HostedModelSelection(
            provider="openai",
            model="openai/demo-model",
            connection_id="demo-provider",
        ),
        "target": GenerationTarget(phonemes=("p", "b")),
        "stopping": GenerationStoppingCriteria(
            max_sentences=2,
            max_iterations=2,
            timeout_seconds=2.0,
        ),
        "candidates_per_iteration": 2,
        "max_tokens_per_request": 32,
        "retry": ProviderRetryPolicy(
            max_retries=1,
            request_timeout_seconds=1.0,
            base_delay_seconds=0.25,
            max_delay_seconds=1.0,
        ),
        "budget": HostedRunBudget(
            max_requests=4,
            max_input_tokens=5_000,
            max_output_tokens=128,
            max_cost_usd=Decimal("1"),
        ),
        "activity_timeout_seconds": 4.0,
        "external_processing_confirmed": True,
    }
    values.update(changes)
    return HostedGenerationRequest(**values)


def local_policy(
    *,
    devices: tuple[ModelDevice, ...] = (ModelDevice.CPU,),
    quantizations: tuple[ModelQuantization, ...] = (ModelQuantization.NONE,),
) -> LocalModelPolicy:
    return LocalModelPolicy(
        pin=PIN,
        artifact_sha256=ARTIFACT_DIGEST,
        allowed_devices=devices,
        allowed_quantizations=quantizations,
    )


def local_request(**changes: object) -> LocalGenerationRequest:
    values: dict[str, object] = {
        "selection": LocalModelSelection(pin=PIN),
        "target": GenerationTarget(phonemes=("p", "b")),
        "stopping": GenerationStoppingCriteria(
            max_sentences=2,
            max_iterations=2,
            timeout_seconds=2,
        ),
        "seed": 1729,
        "activity_timeout_seconds": 4.0,
    }
    values.update(changes)
    return LocalGenerationRequest(**values)


def analysis_request(**changes: object) -> LanguageModelAnalysisRequest:
    values: dict[str, object] = {
        "selection": LocalModelSelection(pin=PIN),
        "texts": (
            AnalysisText(source_id="one", text="A fluent sentence."),
            AnalysisText(source_id="two", text="A second fluent sentence."),
        ),
        "batch_size": 2,
        "max_length": 64,
        "activity_timeout_seconds": 4,
    }
    values.update(changes)
    return LanguageModelAnalysisRequest(**values)


class FakeSecretResolver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.references: list[SecretReference] = []

    def resolve(self, reference: SecretReference) -> str:
        self.references.append(reference)
        if self.fail:
            raise RuntimeError(f"private {RAW_SECRET}")
        return CUSTOM_PROMPT if reference == PROMPT_SECRET else RAW_SECRET


class MappingSecretResolver:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt

    def resolve(self, reference: SecretReference) -> str:
        return self.prompt if reference == PROMPT_SECRET else RAW_SECRET


class UnexpectedAdapterError(Exception):
    pass


class RaisingSecretResolver:
    def __init__(self, error_type: type[Exception]) -> None:
        self.error_type = error_type

    def resolve(self, _: SecretReference) -> str:
        raise self.error_type("private adapter detail")


class FakeProvider:
    def __init__(self, outcomes: list[ProviderCompletion | ProviderCallError]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> ProviderCompletion:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ProviderCallError):
            raise outcome
        return outcome


class FakeLoader:
    def __init__(self) -> None:
        self.model = object()
        self.tokenizer = object()
        self.calls: list[tuple[ImmutableModelPin, ModelDevice, ModelQuantization, str]] = []

    def load(
        self,
        pin: ImmutableModelPin,
        *,
        device: ModelDevice,
        quantization: ModelQuantization,
        artifact_sha256: str,
    ) -> LoadedModelBundle:
        self.calls.append((pin, device, quantization, artifact_sha256))
        return LoadedModelBundle(self.model, self.tokenizer)


class FakeBackend:
    def __init__(
        self,
        name: str,
        completion: object | None = None,
        *,
        duplicate: bool = False,
    ) -> None:
        self._name = name
        self.completion = completion
        self.duplicate = duplicate

    @property
    def name(self) -> str:
        return self._name

    def generate(
        self,
        target_units: list[str],
        k: int = 5,
        **_: object,
    ) -> list[dict[str, object]]:
        text = "A generated sentence."
        if callable(self.completion):
            response = self.completion(f"Generate {k} examples with {', '.join(target_units)}")
            text = cast(ProviderCompletion, response).text.splitlines()[0]
        candidate: dict[str, object] = {"text": text, "phonemes": target_units}
        return [candidate, dict(candidate)] if self.duplicate else [candidate]


class FakeTarget:
    def __init__(self, phonemes: tuple[str, ...]) -> None:
        self.target = set(phonemes)
        self.covered: set[str] = set()

    @property
    def coverage(self) -> float:
        return len(self.covered) / len(self.target)

    @property
    def covered_units(self) -> set[str]:
        return set(self.covered)

    @property
    def missing(self) -> set[str]:
        return self.target - self.covered


@dataclass
class FakeScore:
    text: str | None
    phonemes: list[str]
    coverage_gain: int


class FakeScorer:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target

    def rank(
        self,
        candidates: list[dict[str, object]],
        top_k: int | None = None,
    ) -> list[FakeScore]:
        scores = [
            FakeScore(
                text=cast(str, item["text"]),
                phonemes=cast(list[str], item["phonemes"]),
                coverage_gain=len(set(cast(list[str], item["phonemes"])) - self.target.covered),
            )
            for item in candidates
        ]
        return scores if top_k is None else scores[:top_k]

    def score_and_commit(
        self,
        phonemes: list[str],
        sentence_index: int,
        text: str | None = None,
    ) -> FakeScore:
        del sentence_index
        gain = len(set(phonemes) - self.target.covered)
        self.target.covered.update(phonemes)
        return FakeScore(text=text, phonemes=phonemes, coverage_gain=gain)


class FakeLoop:
    def __init__(
        self,
        backend: FakeBackend,
        target: FakeTarget,
        scorer: FakeScorer,
        callback: object,
    ) -> None:
        self.backend = backend
        self.target = target
        self.scorer = scorer
        self.callback = callback

    def run(self) -> object:
        candidates = self.backend.generate(sorted(self.target.missing), k=2)
        ranked = self.scorer.rank(candidates)
        if not ranked:
            return self._result("backend_exhausted")
        best = ranked[0]
        self.scorer.score_and_commit(best.phonemes, 0, text=best.text)
        cast(object, self.callback)
        assert callable(self.callback)
        self.callback({"iteration": 1, "coverage": self.target.coverage})
        return self._result("target_coverage")

    def _result(self, stop_reason: str) -> object:
        return SimpleNamespace(
            coverage=self.target.coverage,
            covered_units=self.target.covered_units,
            missing_units=self.target.missing,
            unit="phoneme",
            backend=self.backend.name,
            elapsed_seconds=0.01,
            iterations=1,
            stop_reason=stop_reason,
        )


class FakeMetrics:
    def __init__(self, per_sentence: list[float] | None = None) -> None:
        self.per_sentence = per_sentence or [2.0, 4.0]
        self.corpus_perplexity = 2.5
        self.mean_perplexity = 3.0
        self.median_perplexity = 3.0
        self.std_perplexity = 1.0
        self.min_perplexity = 2.0
        self.max_perplexity = 4.0
        self.num_sentences = len(self.per_sentence)
        self.num_tokens = 8
        self.total_nll = 7.0


class FakeBindings:
    def __init__(
        self,
        *,
        duplicate: bool = False,
        wrong_backend: bool = False,
        scoreable: tuple[bool, ...] = (True, True),
    ) -> None:
        self.duplicate = duplicate
        self.wrong_backend = wrong_backend
        self.shared: list[LoadedModelBundle] = []
        self.scoreable = scoreable
        self.seeds: list[tuple[int, ModelDevice]] = []
        self.hosted_request_delays: list[float] = []

    def hosted_backend(
        self,
        request: object,
        prompt_template: str,
        completion: object,
        request_delay_seconds: float,
    ) -> FakeBackend:
        del request, prompt_template
        self.hosted_request_delays.append(request_delay_seconds)
        return FakeBackend(
            "wrong" if self.wrong_backend else "llm_api",
            completion,
            duplicate=self.duplicate,
        )

    def local_backend(self, request: object, bundle: LoadedModelBundle) -> FakeBackend:
        del request
        self.shared.append(bundle)
        return FakeBackend(
            "wrong" if self.wrong_backend else "local",
            duplicate=self.duplicate,
        )

    def phon_rl_backend(self, request: object, bundle: LoadedModelBundle) -> FakeBackend:
        return self.local_backend(request, bundle)

    def set_seed(self, seed: int, device: ModelDevice) -> None:
        self.seeds.append((seed, device))

    def targets(self, target: GenerationTarget) -> FakeTarget:
        return FakeTarget(target.phonemes)

    def scorer(
        self,
        targets: FakeTarget,
        options: object,
        fluency_scorer: object | None = None,
    ) -> FakeScorer:
        del options, fluency_scorer
        return FakeScorer(targets)

    def readability_filter(self, readability_range: object) -> object:
        del readability_range
        return lambda candidate: bool(candidate)

    def loop(
        self,
        backend: FakeBackend,
        targets: FakeTarget,
        scorer: FakeScorer,
        stopping: object,
        candidates_per_iteration: int,
        candidate_filter: object,
        on_progress: object,
    ) -> FakeLoop:
        del stopping, candidates_per_iteration, candidate_filter
        return FakeLoop(backend, targets, scorer, on_progress)

    def fluency_scorer(self, bundle: LoadedModelBundle) -> object:
        self.shared.append(bundle)
        return lambda text: 0.75 if text else 0.0

    def corpus_perplexity(
        self,
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        batch_size: int,
        max_length: int,
    ) -> FakeMetrics:
        assert len(texts) == batch_size
        assert max_length == 64
        self.shared.append(bundle)
        return FakeMetrics([2.0, 4.0][: sum(self.scoreable)])

    def scoreable_mask(
        self,
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        max_length: int,
    ) -> tuple[bool, ...]:
        del bundle
        assert max_length == 64
        assert len(texts) == len(self.scoreable)
        return self.scoreable


class RaisingLocalBindings(FakeBindings):
    def __init__(self, error_type: type[Exception]) -> None:
        super().__init__()
        self.error_type = error_type

    def set_seed(self, seed: int, device: ModelDevice) -> None:
        del seed, device
        raise self.error_type("private adapter detail")


class RaisingAnalysisBindings(FakeBindings):
    def __init__(self, error_type: type[Exception]) -> None:
        super().__init__()
        self.error_type = error_type

    def scoreable_mask(
        self,
        texts: list[str],
        bundle: LoadedModelBundle,
        *,
        max_length: int,
    ) -> tuple[bool, ...]:
        del texts, bundle, max_length
        raise self.error_type("private adapter detail")


class MissingFluencyBindings(FakeBindings):
    def fluency_scorer(self, bundle: LoadedModelBundle) -> object:
        del bundle
        raise ImportError("private optional dependency detail")


def completion(text: str = "A generated sentence.") -> ProviderCompletion:
    return ProviderCompletion(text=text, input_tokens=2, output_tokens=3)


def test_bounded_fluency_scorer_memoizes_and_validates_results() -> None:
    calls: list[str | None] = []
    scorer = _BoundedFluencyScorer(
        lambda text: calls.append(text) or 0.25,
        max_entries=1,
    )

    assert scorer("same") == scorer("same") == 0.25
    assert calls == ["same"]
    with pytest.raises(EngineContractError):
        scorer("second")
    with pytest.raises(EngineContractError):
        _BoundedFluencyScorer(lambda _: float("nan"))("invalid")


def test_hosted_generation_retries_with_counted_budget_and_redacted_manifest() -> None:
    provider = FakeProvider(
        [ProviderCallError(retryable=True, retry_after_seconds=0.5), completion()]
    )
    secret = FakeSecretResolver()
    slept: list[float] = []
    adapter = CorpusgenModelRuntimeAdapter(
        secret_resolver=secret,
        provider_client=provider,
        bindings=FakeBindings(),  # type: ignore[arg-type]
        sleeper=slept.append,
    )

    result = adapter.run_hosted(hosted_request(), hosted_policy())
    serialized = result.model_dump_json()

    assert result.coverage == 1.0
    assert result.usage.requests == 2
    assert result.usage.retries == 1
    assert result.usage.input_tokens == 2
    assert slept == [0.5]
    assert secret.references == [SECRET]
    assert RAW_SECRET not in serialized
    assert SECRET.reference not in serialized
    assert result.manifest.custom_prompt_template is False
    assert result.manifest.external_processing_confirmed is True
    assert result.manifest.processing_boundary == "external_provider"
    assert result.manifest.provider_seed_supported is False
    assert len(result.manifest.prompt_template_sha256) == 64
    assert provider.calls[0]["timeout_seconds"] <= 1.0  # type: ignore[operator]


def test_hosted_server_pacing_is_manifested_and_deadline_aware() -> None:
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def __call__(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.sleeps.append(delay)
            self.now += delay

    clock = Clock()
    provider = FakeProvider(
        [ProviderCallError(retryable=True, retry_after_seconds=0.5), completion()]
    )
    bindings = FakeBindings()
    result = CorpusgenModelRuntimeAdapter(
        secret_resolver=FakeSecretResolver(),
        provider_client=provider,
        bindings=bindings,  # type: ignore[arg-type]
        clock=clock,
        sleeper=clock.sleep,
    ).run_hosted(
        hosted_request(),
        hosted_policy(request_delay_seconds=0.25),
    )

    assert clock.sleeps == [0.25, 0.5, 0.25]
    assert bindings.hosted_request_delays == [0.25]
    assert result.manifest.request_delay_seconds == 0.25

    blocked_provider = FakeProvider([completion()])
    blocked_clock = Clock()
    with pytest.raises(EngineUnavailableError) as blocked:
        CorpusgenModelRuntimeAdapter(
            secret_resolver=FakeSecretResolver(),
            provider_client=blocked_provider,
            bindings=FakeBindings(),  # type: ignore[arg-type]
            clock=blocked_clock,
            sleeper=blocked_clock.sleep,
        ).run_hosted(
            hosted_request(activity_timeout_seconds=1.0),
            hosted_policy(request_delay_seconds=1.0),
        )
    assert blocked.value.operation == "model_runtime.hosted.deadline"
    assert blocked_provider.calls == []
    assert blocked_clock.sleeps == []


def test_hosted_custom_prompt_digest_and_generated_ids_are_stable() -> None:
    def execute() -> object:
        return CorpusgenModelRuntimeAdapter(
            secret_resolver=FakeSecretResolver(),
            provider_client=FakeProvider([completion()]),
            bindings=FakeBindings(duplicate=True),  # type: ignore[arg-type]
        ).run_hosted(
            hosted_request(prompt_template_id="coverage-v1"),
            hosted_policy(custom_prompt=True),
        )

    first = execute()
    second = execute()
    assert first.manifest.custom_prompt_template is True  # type: ignore[attr-defined]
    assert first.manifest.prompt_template_id == "coverage-v1"  # type: ignore[attr-defined]
    assert first.accepted[0].source_id == second.accepted[0].source_id  # type: ignore[attr-defined]
    assert len(first.accepted) == 1  # type: ignore[arg-type]
    serialized = first.model_dump_json()  # type: ignore[attr-defined]
    assert CUSTOM_PROMPT not in serialized
    assert PROMPT_SECRET.reference not in serialized


@pytest.mark.parametrize(
    ("prompt", "policy_change", "operation"),
    [
        (CUSTOM_PROMPT + "!", {}, "model_runtime.hosted.prompt_integrity"),
        (
            "Only {language}.",
            {
                "sha256": hashlib.sha256(b"Only {language}.").hexdigest(),
                "size_bytes": len(b"Only {language}."),
            },
            "model_runtime.hosted.prompt_schema",
        ),
        (
            "Use {target_units!r}.",
            {
                "sha256": hashlib.sha256(b"Use {target_units!r}.").hexdigest(),
                "size_bytes": len(b"Use {target_units!r}."),
            },
            "model_runtime.hosted.prompt_schema",
        ),
    ],
)
def test_hosted_prompt_secrets_fail_closed_on_integrity_and_schema(
    prompt: str,
    policy_change: dict[str, object],
    operation: str,
) -> None:
    policy = hosted_policy(custom_prompt=True)
    template = policy.prompt_templates[0].model_copy(update=policy_change)
    policy = policy.model_copy(update={"prompt_templates": (template,)})
    adapter = CorpusgenModelRuntimeAdapter(
        secret_resolver=MappingSecretResolver(prompt),
        provider_client=FakeProvider([completion()]),
        bindings=FakeBindings(),  # type: ignore[arg-type]
    )
    with pytest.raises(EngineUnavailableError) as caught:
        adapter.run_hosted(hosted_request(prompt_template_id="coverage-v1"), policy)
    assert caught.value.operation == operation


def test_unknown_prompt_id_is_rejected_before_any_secret_resolution() -> None:
    resolver = FakeSecretResolver()
    adapter = CorpusgenModelRuntimeAdapter(
        secret_resolver=resolver,
        provider_client=FakeProvider([completion()]),
        bindings=FakeBindings(),  # type: ignore[arg-type]
    )
    with pytest.raises(InvalidRequestError) as caught:
        adapter.run_hosted(
            hosted_request(prompt_template_id="unknown-template"),
            hosted_policy(custom_prompt=True),
        )
    assert caught.value.operation == "model_runtime.hosted.allowlist"
    assert resolver.references == []


@pytest.mark.parametrize(
    ("outcomes", "run_request", "error_type", "operation"),
    [
        (
            [ProviderCallError(retryable=False)],
            hosted_request(),
            EngineUnavailableError,
            "model_runtime.hosted.provider",
        ),
        (
            [completion("   ")],
            hosted_request(),
            EngineUnavailableError,
            "model_runtime.hosted.empty_response",
        ),
        (
            [ProviderCallError(retryable=True), completion()],
            hosted_request(
                budget=HostedRunBudget(
                    max_requests=1,
                    max_input_tokens=5_000,
                    max_output_tokens=32,
                    max_cost_usd=Decimal("1"),
                )
            ),
            InvalidRequestError,
            "model_runtime.hosted.budget_exhausted",
        ),
        (
            [ProviderCallError(retryable=True, retry_after_seconds=float("nan")), completion()],
            hosted_request(),
            EngineContractError,
            "model_runtime.hosted.retry_after",
        ),
    ],
)
def test_hosted_failures_never_become_empty_success(
    outcomes: list[ProviderCompletion | ProviderCallError],
    run_request: HostedGenerationRequest,
    error_type: type[Exception],
    operation: str,
) -> None:
    adapter = CorpusgenModelRuntimeAdapter(
        secret_resolver=FakeSecretResolver(),
        provider_client=FakeProvider(outcomes),
        bindings=FakeBindings(),  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    with pytest.raises(error_type) as caught:
        adapter.run_hosted(run_request, hosted_policy())
    assert cast(ApplicationError, caught.value).operation == operation
    assert RAW_SECRET not in str(caught.value)


def test_secret_and_engine_contract_failures_are_sanitized() -> None:
    with pytest.raises(EngineUnavailableError) as secret_error:
        CorpusgenModelRuntimeAdapter(
            secret_resolver=FakeSecretResolver(fail=True),
            provider_client=FakeProvider([completion()]),
            bindings=FakeBindings(),  # type: ignore[arg-type]
        ).run_hosted(hosted_request(), hosted_policy())
    assert RAW_SECRET not in str(secret_error.value)

    with pytest.raises(EngineContractError):
        CorpusgenModelRuntimeAdapter(
            secret_resolver=FakeSecretResolver(),
            provider_client=FakeProvider([completion()]),
            bindings=FakeBindings(wrong_backend=True),  # type: ignore[arg-type]
        ).run_hosted(hosted_request(), hosted_policy())


@pytest.mark.parametrize(
    ("error_type", "expected_type"),
    [
        (ImportError, DependencyUnavailableError),
        (ValueError, InvalidRequestError),
        (TypeError, EngineContractError),
        (RuntimeError, EngineUnavailableError),
        (UnexpectedAdapterError, EngineUnavailableError),
    ],
)
def test_adapter_exception_boundaries_are_sanitized_for_every_runtime(
    error_type: type[Exception],
    expected_type: type[ApplicationError],
) -> None:
    hosted = CorpusgenModelRuntimeAdapter(
        secret_resolver=RaisingSecretResolver(error_type),
        provider_client=FakeProvider([completion()]),
        bindings=FakeBindings(),  # type: ignore[arg-type]
    )
    local = CorpusgenModelRuntimeAdapter(
        model_loader=FakeLoader(),
        bindings=RaisingLocalBindings(error_type),  # type: ignore[arg-type]
    )
    analysis = CorpusgenModelRuntimeAdapter(
        model_loader=FakeLoader(),
        bindings=RaisingAnalysisBindings(error_type),  # type: ignore[arg-type]
    )

    for execute in (
        lambda: hosted.run_hosted(hosted_request(), hosted_policy()),
        lambda: local.run_local(
            local_request(),
            local_policy(),
            WorkerModelProfile.LOCAL_CPU,
        ),
        lambda: analysis.analyze_language_model(
            analysis_request(),
            local_policy(),
            WorkerModelProfile.LOCAL_CPU,
        ),
    ):
        with pytest.raises(expected_type) as caught:
            execute()
        assert "private adapter detail" not in str(caught.value)


def test_local_generation_is_offline_manifested_and_best_effort() -> None:
    loader = FakeLoader()
    bindings = FakeBindings(duplicate=True)
    adapter = CorpusgenModelRuntimeAdapter(
        model_loader=loader,
        bindings=bindings,  # type: ignore[arg-type]
    )

    result = adapter.run_local(
        local_request(do_sample=False),
        local_policy(),
        WorkerModelProfile.LOCAL_CPU,
    )

    assert result.coverage == 1.0
    assert result.reproducibility is ReproducibilityClass.BEST_EFFORT
    assert result.model.local_files_only is True
    assert result.model.trust_remote_code is False
    assert result.model.safetensors_only is True
    assert result.model.sampling_enabled is False
    assert result.model.deterministic_algorithms_enforced is False
    assert result.model.artifact_sha256 == ARTIFACT_DIGEST
    assert result.model.seed == 1729
    assert len(result.accepted) == 1
    assert loader.calls == [(PIN, ModelDevice.CPU, ModelQuantization.NONE, ARTIFACT_DIGEST)]
    assert bindings.shared[0].model is loader.model
    assert bindings.seeds == [(1729, ModelDevice.CPU)]


def test_local_generation_loads_fluency_only_when_weighted_and_fails_closed() -> None:
    zero_weight = CorpusgenModelRuntimeAdapter(
        model_loader=FakeLoader(),
        bindings=MissingFluencyBindings(),  # type: ignore[arg-type]
    ).run_local(
        local_request(),
        local_policy(),
        WorkerModelProfile.LOCAL_CPU,
    )
    assert zero_weight.model.fluency_scorer is None

    weighted = local_request(
        scoring=GenerationScoringOptions(weights=ScoreWeights(coverage=1, fluency=1))
    )
    with pytest.raises(DependencyUnavailableError) as unavailable:
        CorpusgenModelRuntimeAdapter(
            model_loader=FakeLoader(),
            bindings=MissingFluencyBindings(),  # type: ignore[arg-type]
        ).run_local(weighted, local_policy(), WorkerModelProfile.LOCAL_CPU)
    assert unavailable.value.operation == "model_runtime.local.run"


def test_local_gpu_quantization_requires_exact_policy_and_profile() -> None:
    request = local_request(
        selection=LocalModelSelection(
            pin=PIN,
            device=ModelDevice.CUDA,
            quantization=ModelQuantization.FOUR_BIT,
        )
    )
    policy = local_policy(
        devices=(ModelDevice.CUDA,),
        quantizations=(ModelQuantization.FOUR_BIT,),
    )
    adapter = CorpusgenModelRuntimeAdapter(
        model_loader=FakeLoader(),
        bindings=FakeBindings(),  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidRequestError) as profile_error:
        adapter.run_local(request, policy, WorkerModelProfile.LOCAL_CPU)
    assert profile_error.value.operation == "model_runtime.local.worker_profile"

    result = adapter.run_local(request, policy, WorkerModelProfile.LOCAL_GPU)
    assert result.model.device is ModelDevice.CUDA
    assert result.model.quantization is ModelQuantization.FOUR_BIT


def test_fluency_and_perplexity_share_one_application_owned_bundle() -> None:
    loader = FakeLoader()
    bindings = FakeBindings()
    composite = CompositeScoringRequest(
        target=GenerationTarget(phonemes=("p", "b")),
        candidates=(
            RepositoryCandidate(
                source_id="one",
                text="A fluent sentence.",
                phonemes=("p",),
            ),
            RepositoryCandidate(
                source_id="two",
                text="A second fluent sentence.",
                phonemes=("b",),
            ),
        ),
        options=GenerationScoringOptions(weights=ScoreWeights(coverage=0, fluency=2)),
    )
    result = CorpusgenModelRuntimeAdapter(
        model_loader=loader,
        bindings=bindings,  # type: ignore[arg-type]
    ).analyze_language_model(
        analysis_request(composite_scoring=composite),
        local_policy(),
        WorkerModelProfile.LOCAL_CPU,
    )

    assert len(loader.calls) == 1
    assert len(bindings.shared) == 2
    assert bindings.shared[0] is bindings.shared[1]
    assert bindings.shared[0].model is loader.model
    assert [item.source_id for item in result.fluency] == ["one", "two"]
    assert [item.score for item in result.fluency] == [0.75, 0.75]
    assert result.model.fluency_scorer == "perplexity"
    assert result.composite_scoring is not None
    assert [item.fluency_score for item in result.composite_scoring.ranked] == [0.75, 0.75]
    assert all(
        item.composite_score == pytest.approx(1.5) for item in result.composite_scoring.ranked
    )
    assert result.perplexity.per_sentence == (2.0, 4.0)
    assert result.scored_sentence_count == 2
    assert [item.source_id for item in result.sentence_perplexities] == ["one", "two"]
    assert all(
        item.status is PerplexitySentenceStatus.SCORED for item in result.sentence_perplexities
    )


def test_perplexity_preserves_scored_and_skipped_source_mapping() -> None:
    request = analysis_request()
    bindings = FakeBindings(scoreable=(False, True))
    result = CorpusgenModelRuntimeAdapter(
        model_loader=FakeLoader(),
        bindings=bindings,  # type: ignore[arg-type]
    ).analyze_language_model(
        request,
        local_policy(),
        WorkerModelProfile.LOCAL_CPU,
    )

    assert result.scored_sentence_count == 1
    assert result.perplexity.per_sentence == (2.0,)
    assert [item.source_id for item in result.sentence_perplexities] == ["one", "two"]
    assert [item.status for item in result.sentence_perplexities] == [
        PerplexitySentenceStatus.SKIPPED_TOO_SHORT,
        PerplexitySentenceStatus.SCORED,
    ]
    assert [item.perplexity for item in result.sentence_perplexities] == [None, 2.0]


class ClosableModel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class CountingLoader:
    def __init__(self) -> None:
        self.models: list[ClosableModel] = []

    def load(self, *_: object, **__: object) -> LoadedModelBundle:
        model = ClosableModel()
        self.models.append(model)
        return LoadedModelBundle(model, object())


def test_bounded_model_cache_reuses_evicts_and_cleans_up() -> None:
    delegate = CountingLoader()
    cache = CachedLocalModelLoader(delegate, max_entries=1)  # type: ignore[arg-type]
    first = cache.load(
        PIN,
        device=ModelDevice.CPU,
        quantization=ModelQuantization.NONE,
        artifact_sha256=ARTIFACT_DIGEST,
    )
    assert (
        cache.load(
            PIN,
            device=ModelDevice.CPU,
            quantization=ModelQuantization.NONE,
            artifact_sha256=ARTIFACT_DIGEST,
        )
        is first
    )

    second_pin = ImmutableModelPin(model="acme/other-causal", revision="c" * 40)
    second = cache.load(
        second_pin,
        device=ModelDevice.CPU,
        quantization=ModelQuantization.NONE,
        artifact_sha256="c" * 64,
    )
    assert cast(ClosableModel, first.model).closed is True
    assert cast(ClosableModel, second.model).closed is False
    cache.clear()
    assert cast(ClosableModel, second.model).closed is True
    with pytest.raises(ValueError, match="between one and four"):
        CachedLocalModelLoader(delegate, max_entries=0)  # type: ignore[arg-type]


def test_offline_transformers_loader_forces_revision_trust_and_safetensors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, dict[str, object]] = {}
    tokenizer = SimpleNamespace(pad_token=None, eos_token="<eos>")
    model = SimpleNamespace(eval=lambda: None)

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(name: str, **kwargs: object) -> object:
            calls["tokenizer"] = {"name": name, **kwargs}
            return tokenizer

    class AutoModel:
        @staticmethod
        def from_pretrained(name: str, **kwargs: object) -> object:
            calls["model"] = {"name": name, **kwargs}
            return model

    fake = ModuleType("transformers")
    fake.AutoTokenizer = AutoTokenizer  # type: ignore[attr-defined]
    fake.AutoModelForCausalLM = AutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", fake)
    snapshot = _snapshot(tmp_path)
    digest = compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)

    bundle = TransformersLocalModelLoader(
        lambda _: snapshot,
        approved_cache_root=tmp_path,
    ).load(
        PIN,
        device=ModelDevice.CPU,
        quantization=ModelQuantization.NONE,
        artifact_sha256=digest,
    )

    assert bundle.model is model
    assert calls["tokenizer"] == {
        "name": str(snapshot.resolve()),
        "revision": PIN.revision,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    assert calls["model"]["use_safetensors"] is True
    assert calls["model"]["device_map"] == "cpu"
    assert tokenizer.pad_token == "<eos>"


def test_default_transformers_loader_resolves_only_the_local_exact_hub_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "models--acme--tiny-causal"
    snapshot = repo_root / "snapshots" / PIN.revision
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"weights")
    calls: list[dict[str, object]] = []

    hub = ModuleType("huggingface_hub")

    def snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(snapshot)

    hub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = SimpleNamespace(  # type: ignore[attr-defined]
        from_pretrained=lambda *_args, **_kwargs: SimpleNamespace(
            pad_token="<pad>", eos_token="<eos>"
        )
    )
    transformers.AutoModelForCausalLM = SimpleNamespace(  # type: ignore[attr-defined]
        from_pretrained=lambda *_args, **_kwargs: object()
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    digest = compute_snapshot_digest(snapshot, approved_cache_root=repo_root)

    TransformersLocalModelLoader().load(
        PIN,
        device=ModelDevice.CPU,
        quantization=ModelQuantization.NONE,
        artifact_sha256=digest,
    )

    assert calls == [
        {
            "repo_id": PIN.model,
            "revision": PIN.revision,
            "local_files_only": True,
        }
    ]

    calls.clear()
    TransformersLocalModelLoader(approved_cache_root=repo_root).load(
        PIN,
        device=ModelDevice.CPU,
        quantization=ModelQuantization.NONE,
        artifact_sha256=digest,
    )
    assert calls == [
        {
            "repo_id": PIN.model,
            "revision": PIN.revision,
            "cache_dir": str(repo_root.absolute()),
            "local_files_only": True,
        }
    ]
    assert isinstance(
        TransformersLocalModelLoader(approved_cache_root=repo_root)._snapshot_resolver,
        OfflineLocalSnapshotResolver,
    )


def test_local_loader_dependency_and_snapshot_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(DependencyUnavailableError):
        TransformersLocalModelLoader(
            lambda _: tmp_path,
            approved_cache_root=tmp_path,
        ).load(
            PIN,
            device=ModelDevice.CPU,
            quantization=ModelQuantization.NONE,
            artifact_sha256=ARTIFACT_DIGEST,
        )

    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = object()  # type: ignore[attr-defined]
    transformers.AutoModelForCausalLM = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(DependencyUnavailableError):
        _resolve_local_snapshot(PIN)

    hub = ModuleType("huggingface_hub")

    def unavailable(**_: object) -> str:
        raise OSError("private cache path")

    hub.snapshot_download = unavailable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    with pytest.raises(EngineUnavailableError) as unavailable_error:
        _resolve_local_snapshot(PIN)
    assert "private cache path" not in str(unavailable_error.value)


def test_quantization_dependency_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    digest = compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = SimpleNamespace(  # type: ignore[attr-defined]
        from_pretrained=lambda *_args, **_kwargs: SimpleNamespace(
            pad_token="<pad>", eos_token="<eos>"
        )
    )
    transformers.AutoModelForCausalLM = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    with pytest.raises(DependencyUnavailableError) as missing:
        TransformersLocalModelLoader(
            lambda _: snapshot,
            approved_cache_root=tmp_path,
        ).load(
            PIN,
            device=ModelDevice.CUDA,
            quantization=ModelQuantization.FOUR_BIT,
            artifact_sha256=digest,
        )
    assert missing.value.operation == "model_runtime.local.quantization"


def test_litellm_client_disables_internal_retries_and_global_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    fake = ModuleType("litellm")
    fake.callbacks = []  # type: ignore[attr-defined]
    fake.success_callback = []  # type: ignore[attr-defined]
    fake.failure_callback = []  # type: ignore[attr-defined]
    fake.set_verbose = False  # type: ignore[attr-defined]

    def fake_completion(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="sentence"))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        )

    fake.completion = fake_completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    result = LiteLLMProviderClient().complete(
        provider="openai",
        model="openai/demo-model",
        prompt="prompt",
        temperature=0.5,
        max_tokens=8,
        api_key=RAW_SECRET,
        timeout_seconds=1,
    )
    assert result.output_tokens == 1
    assert calls[0]["num_retries"] == 0
    assert calls[0]["custom_llm_provider"] == "openai"

    fake.success_callback = [object()]  # type: ignore[attr-defined]
    with pytest.raises(EngineUnavailableError) as isolated:
        LiteLLMProviderClient().complete(
            provider="openai",
            model="openai/demo-model",
            prompt="prompt",
            temperature=0.5,
            max_tokens=8,
            api_key=RAW_SECRET,
            timeout_seconds=1,
        )
    assert isolated.value.operation == "model_runtime.hosted.callback_isolation"


def test_litellm_client_rejects_provider_model_mismatch_before_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    fake = ModuleType("litellm")
    fake.callbacks = []  # type: ignore[attr-defined]
    fake.success_callback = []  # type: ignore[attr-defined]
    fake.failure_callback = []  # type: ignore[attr-defined]
    fake.set_verbose = False  # type: ignore[attr-defined]
    fake.completion = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)

    with pytest.raises(EngineUnavailableError) as mismatch:
        LiteLLMProviderClient().complete(
            provider="openai",
            model="anthropic/demo-model",
            prompt="prompt",
            temperature=0.5,
            max_tokens=8,
            api_key=RAW_SECRET,
            timeout_seconds=1,
        )
    assert mismatch.value.operation == "model_runtime.hosted.provider_boundary"
    assert calls == []


def test_litellm_missing_dependency_and_non_string_content_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(DependencyUnavailableError):
        LiteLLMProviderClient().complete(
            provider="openai",
            model="openai/demo-model",
            prompt="prompt",
            temperature=0.5,
            max_tokens=8,
            api_key=RAW_SECRET,
            timeout_seconds=1,
        )

    fake = ModuleType("litellm")
    fake.callbacks = []  # type: ignore[attr-defined]
    fake.success_callback = []  # type: ignore[attr-defined]
    fake.failure_callback = []  # type: ignore[attr-defined]
    fake.set_verbose = False  # type: ignore[attr-defined]
    fake.completion = lambda **_: SimpleNamespace(  # type: ignore[attr-defined]
        choices=[SimpleNamespace(message=SimpleNamespace(content=7))],
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )
    monkeypatch.setitem(sys.modules, "litellm", fake)
    with pytest.raises(EngineContractError):
        LiteLLMProviderClient().complete(
            provider="openai",
            model="openai/demo-model",
            prompt="prompt",
            temperature=0.5,
            max_tokens=8,
            api_key=RAW_SECRET,
            timeout_seconds=1,
        )


def test_environment_secret_resolver_is_exact_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = EnvironmentSecretResolver()
    monkeypatch.setenv("MODEL_PROVIDER_KEY", RAW_SECRET)
    assert resolver.resolve(SECRET) == RAW_SECRET
    with pytest.raises(EngineUnavailableError):
        resolver.resolve(SecretReference(reference="secret://vault/provider"))
    monkeypatch.delenv("MODEL_PROVIDER_KEY")
    with pytest.raises(EngineUnavailableError):
        resolver.resolve(SECRET)
    for invalid_name in ("lowercase", "../MODEL_PROVIDER_KEY", "A" * 129):
        with pytest.raises(EngineUnavailableError) as unsafe:
            resolver.resolve(SecretReference(reference=f"secret://env/{invalid_name}"))
        assert unsafe.value.operation == "model_runtime.secret.resolve"


def test_snapshot_digest_detects_tamper_and_rejects_unsafe_weights(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = ModuleType("transformers")
    fake.AutoTokenizer = SimpleNamespace(from_pretrained=lambda *_args, **_kwargs: object())
    fake.AutoModelForCausalLM = SimpleNamespace(from_pretrained=lambda *_args, **_kwargs: object())
    monkeypatch.setitem(sys.modules, "transformers", fake)
    snapshot = _snapshot(tmp_path)
    digest = compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    (snapshot / "model.safetensors").write_bytes(b"tampered")

    with pytest.raises(EngineUnavailableError) as mismatch:
        TransformersLocalModelLoader(
            lambda _: snapshot,
            approved_cache_root=tmp_path,
        ).load(
            PIN,
            device=ModelDevice.CPU,
            quantization=ModelQuantization.NONE,
            artifact_sha256=digest,
        )
    assert mismatch.value.operation == "model_runtime.local.artifact_digest"

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "model.safetensors").write_bytes(b"safe")
    (unsafe / "pytorch_model.bin").write_bytes(b"pickle")
    with pytest.raises(EngineUnavailableError) as unsafe_error:
        compute_snapshot_digest(unsafe, approved_cache_root=tmp_path)
    assert unsafe_error.value.operation == "model_runtime.local.unsafe_weights"

    missing = tmp_path / "missing-safetensors"
    missing.mkdir()
    (missing / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EngineUnavailableError) as missing_error:
        compute_snapshot_digest(missing, approved_cache_root=tmp_path)
    assert missing_error.value.operation == "model_runtime.local.safetensors_required"


def test_snapshot_digest_allows_internal_hub_links_but_rejects_escape(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "models--acme--tiny-causal"
    snapshot = repo_root / "snapshots" / PIN.revision
    blob = repo_root / "blobs" / "model-content"
    snapshot.mkdir(parents=True)
    blob.parent.mkdir()
    blob.write_bytes(b"safe model")
    (snapshot / "model.safetensors").symlink_to(blob)
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")

    assert len(compute_snapshot_digest(snapshot, approved_cache_root=repo_root)) == 64

    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"unapproved")
    (snapshot / "escape.safetensors").symlink_to(outside)
    with pytest.raises(EngineUnavailableError) as boundary:
        compute_snapshot_digest(snapshot, approved_cache_root=repo_root)
    assert boundary.value.operation == "model_runtime.local.snapshot_boundary"


def test_custom_snapshot_resolver_requires_explicit_cache_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved cache root"):
        TransformersLocalModelLoader(lambda _: tmp_path)


def test_default_adapter_is_spawn_pickle_safe_and_cache_restarts_empty() -> None:
    adapter = CorpusgenModelRuntimeAdapter()
    restored = pickle.loads(pickle.dumps(adapter))  # noqa: S301 - locally created bytes

    assert isinstance(restored, CorpusgenModelRuntimeAdapter)


def test_safetensors_peft_loader_uses_exact_read_only_public_peft_seam(
    locked_local_runtime_metadata: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    root = peft_root(tmp_path)
    calls: list[tuple[object, str, bool, bool]] = []

    class Merged:
        evaluated = False

        def eval(self) -> None:
            self.evaluated = True

    merged = Merged()

    class LoadedAdapter:
        def merge_and_unload(self, *, safe_merge: bool) -> Merged:
            assert safe_merge is True
            return merged

    class PeftModel:
        @staticmethod
        def from_pretrained(
            model: object,
            path: str,
            *,
            is_trainable: bool,
            local_files_only: bool,
        ) -> LoadedAdapter:
            calls.append((model, path, is_trainable, local_files_only))
            return LoadedAdapter()

    module = ModuleType("peft")
    module.PeftModel = PeftModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "peft", module)
    base = LoadedModelBundle(model=object(), tokenizer=object())
    loaded = SafetensorsPeftAdapterLoader().load(
        base,
        adapter_root=root,
        compatibility=peft_compatibility(),
        policy=local_policy(),
    )

    assert loaded.model is merged
    assert loaded.tokenizer is base.tokenizer
    assert merged.evaluated is True
    assert calls == [(base.model, str(root.resolve()), False, True)]


def test_safetensors_peft_loader_rejects_layout_config_and_runtime_drift(
    locked_local_runtime_metadata: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    loader = SafetensorsPeftAdapterLoader()
    base = LoadedModelBundle(model=object(), tokenizer=object())
    policy = local_policy()

    with pytest.raises(InvalidRequestError) as incompatible:
        loader.load(
            base,
            adapter_root=tmp_path / "unused",
            compatibility=peft_compatibility(base_model_snapshot_sha256="0" * 64),
            policy=policy,
        )
    assert incompatible.value.operation == "model_runtime.local.phon_rl_adapter_compatibility"

    writable = peft_root(tmp_path / "writable")
    writable.chmod(0o700)
    with pytest.raises(EngineUnavailableError) as unsafe_root:
        loader.load(
            base,
            adapter_root=writable,
            compatibility=peft_compatibility(),
            policy=policy,
        )
    assert unsafe_root.value.operation == "model_runtime.local.phon_rl_adapter_layout"

    extra = peft_root(tmp_path / "extra")
    extra.chmod(0o700)
    (extra / "nested").mkdir()
    extra.chmod(0o500)
    with pytest.raises(EngineUnavailableError) as ambiguous:
        loader.load(
            base,
            adapter_root=extra,
            compatibility=peft_compatibility(),
            policy=policy,
        )
    assert ambiguous.value.operation == "model_runtime.local.phon_rl_adapter_layout"

    wrong_config = peft_root(tmp_path / "wrong-config")
    wrong_config.chmod(0o700)
    config_path = wrong_config / "adapter_config.json"
    config_path.chmod(0o600)
    config_path.write_text("{}", encoding="utf-8")
    config_path.chmod(0o400)
    wrong_config.chmod(0o500)
    with pytest.raises(EngineUnavailableError) as invalid_config:
        loader.load(
            base,
            adapter_root=wrong_config,
            compatibility=peft_compatibility(),
            policy=policy,
        )
    assert invalid_config.value.operation == "model_runtime.local.phon_rl_adapter_config"

    malformed = peft_root(tmp_path / "malformed")
    malformed.chmod(0o700)
    malformed_config = malformed / "adapter_config.json"
    malformed_config.chmod(0o600)
    malformed_config.write_text("{", encoding="utf-8")
    malformed_config.chmod(0o400)
    malformed.chmod(0o500)
    with pytest.raises(EngineUnavailableError) as unreadable:
        loader.load(
            base,
            adapter_root=malformed,
            compatibility=peft_compatibility(),
            policy=policy,
        )
    assert unreadable.value.operation == "model_runtime.local.phon_rl_adapter_load"

    runtime_failure = peft_root(tmp_path / "runtime")

    class PeftModel:
        @staticmethod
        def from_pretrained(*_: object, **__: object) -> object:
            raise RuntimeError("private adapter runtime detail")

    module = ModuleType("peft")
    module.PeftModel = PeftModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "peft", module)
    with pytest.raises(EngineUnavailableError) as unavailable:
        loader.load(
            base,
            adapter_root=runtime_failure,
            compatibility=peft_compatibility(),
            policy=policy,
        )
    assert unavailable.value.operation == "model_runtime.local.phon_rl_adapter_load"
    assert "private adapter runtime" not in str(unavailable.value)


def test_safetensors_peft_loader_fails_closed_when_peft_import_is_unavailable(
    locked_local_runtime_metadata: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    root = peft_root(tmp_path)
    real_import = importlib.import_module

    def unavailable(name: str, package: str | None = None) -> ModuleType:
        if name == "peft":
            raise ImportError("private import detail")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    with pytest.raises(DependencyUnavailableError) as missing:
        SafetensorsPeftAdapterLoader().load(
            LoadedModelBundle(object(), object()),
            adapter_root=root,
            compatibility=peft_compatibility(),
            policy=local_policy(),
        )
    assert missing.value.operation == "model_runtime.local.phon_rl_adapter_dependency"
    assert "private import" not in str(missing.value)


@pytest.mark.parametrize(
    ("error_type", "expected_type"),
    [
        (ImportError, DependencyUnavailableError),
        (ValueError, InvalidRequestError),
        (TypeError, EngineContractError),
        (RuntimeError, EngineUnavailableError),
        (UnexpectedAdapterError, EngineUnavailableError),
    ],
)
def test_phon_rl_inference_exception_boundaries_are_sanitized(
    error_type: type[Exception],
    expected_type: type[ApplicationError],
    locked_local_runtime_metadata: dict[str, str],
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata

    class RaisingPeftLoader:
        def load(self, *_: object, **__: object) -> LoadedModelBundle:
            raise error_type("private PEFT adapter detail")

    request = local_request(
        phon_rl_adapter=PhonRlAdapterSelection(
            artifact_id="123e4567-e89b-42d3-a456-426614174099",
            artifact_sha256="c" * 64,
            checkpoint_sha256="d" * 64,
        )
    )
    policy = local_policy().model_copy(update={"allow_phon_rl_adapters": True})
    adapter = CorpusgenModelRuntimeAdapter(
        model_loader=FakeLoader(),
        peft_adapter_loader=RaisingPeftLoader(),
        bindings=FakeBindings(),  # type: ignore[arg-type]
    )
    with pytest.raises(expected_type) as caught:
        adapter.run_local_phon_rl(
            request,
            policy,
            WorkerModelProfile.LOCAL_CPU,
            adapter_root=tmp_path,
            compatibility=peft_compatibility(),
        )
    assert "private PEFT adapter detail" not in str(caught.value)


def test_phon_rl_inference_is_explicitly_default_deny(
    locked_local_runtime_metadata: dict[str, str],
    tmp_path: Path,
) -> None:
    del locked_local_runtime_metadata
    with pytest.raises(InvalidRequestError) as denied:
        CorpusgenModelRuntimeAdapter(
            model_loader=FakeLoader(),
            bindings=FakeBindings(),  # type: ignore[arg-type]
        ).run_local_phon_rl(
            local_request(),
            local_policy(),
            WorkerModelProfile.LOCAL_CPU,
            adapter_root=tmp_path,
            compatibility=peft_compatibility(),
        )
    assert denied.value.operation == "model_runtime.local.phon_rl_adapter_policy"


def test_application_owned_phon_rl_backend_exercises_strategy_and_fail_closed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TokenIds:
        shape = (1, 2)

    class Inputs(UserDict[str, object]):
        input_ids = TokenIds()

        def to(self, device: object) -> Inputs:
            assert device == "cpu"
            return self

    class Output:
        def __getitem__(self, key: object) -> object:
            assert isinstance(key, tuple)
            return object()

    class Model:
        device = "cpu"

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, **kwargs: object) -> Output:
            self.calls.append(kwargs)
            processor = cast(list[object], kwargs["logits_processor"])[0]
            scores = object()
            assert callable(processor)
            assert processor(object(), scores) is scores
            return Output()

    class Tokenizer:
        eos_token_id = 0

        def __init__(self, texts: list[str]) -> None:
            self.texts = texts

        def __call__(self, prompt: str, **kwargs: object) -> Inputs:
            assert prompt
            assert kwargs == {"return_tensors": "pt", "padding": True}
            return Inputs({"input_ids": self.input_ids})

        @property
        def input_ids(self) -> TokenIds:
            return TokenIds()

        def batch_decode(self, output: object, **kwargs: object) -> list[str]:
            assert output is not None
            assert kwargs == {"skip_special_tokens": True}
            return self.texts

    model = Model()
    tokenizer = Tokenizer(["pea"])
    request = local_request(do_sample=True)
    backend = _CorpusgenModelRuntimeBindings.phon_rl_backend(
        request,
        LoadedModelBundle(model, tokenizer),
    )
    monkeypatch.setattr(
        "corpusgen.g2p.manager.G2PManager.phonemize_batch",
        lambda *_args, **_kwargs: [SimpleNamespace(phonemes=["p"])],
    )
    assert backend.generate([], k=1) == [{"text": "pea", "phonemes": ["p"]}]
    assert model.calls[-1]["temperature"] == request.temperature
    assert model.calls[-1]["top_p"] == request.top_p

    tokenizer.texts = ["pea", "bee"]
    greedy = _CorpusgenModelRuntimeBindings.phon_rl_backend(
        local_request(do_sample=False),
        LoadedModelBundle(model, tokenizer),
    )
    monkeypatch.setattr(
        "corpusgen.g2p.manager.G2PManager.phonemize_batch",
        lambda *_args, **_kwargs: [
            SimpleNamespace(phonemes=["p"]),
            SimpleNamespace(phonemes=["b"]),
        ],
    )
    assert len(greedy.generate(["p", "b"], k=2)) == 2
    assert model.calls[-1]["num_beams"] == 2

    tokenizer.texts = [""]
    with pytest.raises(EngineUnavailableError) as no_text:
        greedy.generate(["p"], k=1)
    assert no_text.value.operation == "model_runtime.local.empty_response"

    tokenizer.texts = ["pea"]
    monkeypatch.setattr(
        "corpusgen.g2p.manager.G2PManager.phonemize_batch",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(EngineContractError) as bad_g2p:
        greedy.generate(["p"], k=1)
    assert bad_g2p.value.operation == "model_runtime.local.phon_rl.g2p"

    monkeypatch.setattr(
        "corpusgen.g2p.manager.G2PManager.phonemize_batch",
        lambda *_args, **_kwargs: [SimpleNamespace(phonemes=[])],
    )
    with pytest.raises(EngineUnavailableError) as no_phonemes:
        greedy.generate(["p"], k=1)
    assert no_phonemes.value.operation == "model_runtime.local.empty_response"


def test_litellm_error_classification_and_response_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ModuleType("litellm")
    fake.callbacks = []  # type: ignore[attr-defined]
    fake.success_callback = []  # type: ignore[attr-defined]
    fake.failure_callback = []  # type: ignore[attr-defined]
    fake.set_verbose = False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)

    class LimitedError(RuntimeError):
        status_code = 429
        retry_after = 90

    def limited(**_: object) -> object:
        raise LimitedError("raw provider response")

    fake.completion = limited  # type: ignore[attr-defined]
    with pytest.raises(ProviderCallError) as classified:
        LiteLLMProviderClient().complete(
            provider="openai",
            model="openai/demo-model",
            prompt="prompt",
            temperature=0.5,
            max_tokens=8,
            api_key=RAW_SECRET,
            timeout_seconds=1,
        )
    assert classified.value.retryable is True
    assert classified.value.retry_after_seconds == 30
    assert "raw provider" not in str(classified.value)

    fake.completion = lambda **_: SimpleNamespace(choices=[], usage={})  # type: ignore[attr-defined]
    with pytest.raises(EngineContractError):
        LiteLLMProviderClient().complete(
            provider="openai",
            model="openai/demo-model",
            prompt="prompt",
            temperature=0.5,
            max_tokens=8,
            api_key=RAW_SECRET,
            timeout_seconds=1,
        )


def test_transformers_quantization_paths_remain_offline_and_safetensors_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(_: str, **__: object) -> object:
            return SimpleNamespace(pad_token="<pad>", eos_token="<eos>")

    class AutoModel:
        @staticmethod
        def from_pretrained(_: str, **kwargs: object) -> object:
            calls.append(kwargs)
            return object()

    class BitsAndBytesConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    fake = ModuleType("transformers")
    fake.AutoTokenizer = AutoTokenizer  # type: ignore[attr-defined]
    fake.AutoModelForCausalLM = AutoModel  # type: ignore[attr-defined]
    fake.BitsAndBytesConfig = BitsAndBytesConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", fake)
    snapshot = _snapshot(tmp_path)
    digest = compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)

    loader = TransformersLocalModelLoader(
        lambda _: snapshot,
        approved_cache_root=tmp_path,
    )
    loader.load(
        PIN,
        device=ModelDevice.CUDA,
        quantization=ModelQuantization.FOUR_BIT,
        artifact_sha256=digest,
    )
    loader.load(
        PIN,
        device=ModelDevice.CUDA,
        quantization=ModelQuantization.EIGHT_BIT,
        artifact_sha256=digest,
    )

    assert calls[0]["device_map"] == calls[1]["device_map"] == "auto"
    assert cast(BitsAndBytesConfig, calls[0]["quantization_config"]).kwargs == {
        "load_in_4bit": True
    }
    assert cast(BitsAndBytesConfig, calls[1]["quantization_config"]).kwargs == {
        "load_in_8bit": True
    }
    assert all(call["use_safetensors"] is True for call in calls)


class FakeG2P:
    def phonemize_batch(self, texts: list[str], language: str) -> list[object]:
        assert language == "en-us"
        return [SimpleNamespace(phonemes=["p"]) for _ in texts]


def test_real_corpusgen_hosted_backend_contract_with_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.g2p import manager

    monkeypatch.setattr(manager, "G2PManager", FakeG2P)
    prompts: list[str] = []

    def complete_prompt(prompt: str) -> ProviderCompletion:
        prompts.append(prompt)
        return completion("1. Pat packs.")

    backend = _CorpusgenModelRuntimeBindings.hosted_backend(
        hosted_request(),
        DEFAULT_HOSTED_PROMPT_TEMPLATE,
        complete_prompt,
        0.25,
    )
    candidates = backend.generate(["p"], k=1)
    assert candidates == [{"text": "Pat packs.", "phonemes": ["p"]}]
    assert "Target sounds: p" in prompts[0]
    assert cast(Any, backend).request_delay == 0.25

    call = cast(Any, backend)._call_with_retry
    with pytest.raises(EngineContractError):
        call(messages=[])
    with pytest.raises(EngineContractError):
        call(messages=[{"content": 7}])

    empty = _CorpusgenModelRuntimeBindings.hosted_backend(
        hosted_request(),
        DEFAULT_HOSTED_PROMPT_TEMPLATE,
        lambda _: completion("  "),
        0.0,
    )
    with pytest.raises(EngineUnavailableError):
        empty.generate(["p"], k=1)


def test_real_corpusgen_local_backend_uses_injected_bundle_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.generate.backends.local import LocalBackend

    generated: list[dict[str, object]] = [{"text": "Pat.", "phonemes": ["p"]}]

    def fake_generate(
        backend: object,
        target_units: list[str],
        k: int = 5,
        **_: object,
    ) -> list[dict[str, object]]:
        del target_units, k
        cast(Any, backend)._ensure_loaded()
        return list(generated)

    monkeypatch.setattr(LocalBackend, "generate", fake_generate)
    model = object()
    tokenizer = object()
    backend = _CorpusgenModelRuntimeBindings.local_backend(
        local_request(),
        LoadedModelBundle(model, tokenizer),
    )
    assert backend.generate(["p"], k=1) == generated
    assert cast(Any, backend).is_loaded is True
    cast(Any, backend)._ensure_loaded()
    generated.clear()
    with pytest.raises(EngineUnavailableError):
        backend.generate(["p"], k=1)


def test_real_corpusgen_generation_construction_and_public_shared_model_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.evaluate import perplexity

    target = _CorpusgenModelRuntimeBindings.targets(GenerationTarget(phonemes=("p", "b")))
    scorer = _CorpusgenModelRuntimeBindings.scorer(target, GenerationScoringOptions())
    readability_options = GenerationScoringOptions(
        weights=ScoreWeights(coverage=1, readability=1),
        readability_target=ReadabilityRange(minimum=40, maximum=80),
    )
    readable_scorer = _CorpusgenModelRuntimeBindings.scorer(target, readability_options)
    readable_filter = _CorpusgenModelRuntimeBindings.readability_filter(
        ReadabilityRange(minimum=0, maximum=100)
    )
    loop = _CorpusgenModelRuntimeBindings.loop(
        FakeBackend("local"),
        target,
        scorer,
        GenerationStoppingCriteria(max_sentences=1, max_iterations=1, timeout_seconds=1),
        1,
        readable_filter,
        lambda _: None,
    )
    assert loop.stopping_criteria.max_iterations == 1  # type: ignore[attr-defined]
    assert readable_scorer.readability_weight == 1  # type: ignore[attr-defined]

    bundle = LoadedModelBundle(object(), object())
    fluency = _CorpusgenModelRuntimeBindings.fluency_scorer(bundle)
    assert cast(Any, fluency).is_loaded is True

    seen: list[tuple[object, object]] = []

    def fake_perplexity(
        texts: list[str],
        **kwargs: object,
    ) -> FakeMetrics:
        assert texts == ["one", "two"]
        seen.append((kwargs["model"], kwargs["tokenizer"]))
        return FakeMetrics()

    monkeypatch.setattr(perplexity, "compute_corpus_perplexity", fake_perplexity)
    metrics = _CorpusgenModelRuntimeBindings.corpus_perplexity(
        ["one", "two"],
        bundle,
        batch_size=2,
        max_length=64,
    )
    assert metrics.corpus_perplexity == 2.5
    assert seen == [(bundle.model, bundle.tokenizer)]


def test_public_tokenizer_mapping_and_seed_hooks_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tokenizer(*_: object, **__: object) -> dict[str, object]:
        return {"input_ids": [[1], [1, 2]]}

    bundle = LoadedModelBundle(object(), tokenizer)
    assert _CorpusgenModelRuntimeBindings.scoreable_mask(
        ["short", "scoreable"],
        bundle,
        max_length=32,
    ) == (False, True)

    def batch_encoding_like(*_: object, **__: object) -> UserDict[str, object]:
        return UserDict({"input_ids": [[1], [1, 2]]})

    assert _CorpusgenModelRuntimeBindings.scoreable_mask(
        ["short", "scoreable"],
        LoadedModelBundle(object(), batch_encoding_like),
        max_length=32,
    ) == (False, True)

    class StaticTokenizer:
        def __init__(self, value: object) -> None:
            self.value = value

        def __call__(self, *_args: object, **_kwargs: object) -> object:
            return self.value

    for malformed in (
        {},
        {"input_ids": [[1]]},
        {"input_ids": ["not-a-row", [1, 2]]},
        {"input_ids": [[-1], [1, 2]]},
        {"input_ids": [[True], [1, 2]]},
        {"input_ids": [[1] * 33, [1, 2]]},
    ):
        malformed_bundle = LoadedModelBundle(
            object(),
            StaticTokenizer(malformed),
        )
        with pytest.raises(EngineContractError):
            _CorpusgenModelRuntimeBindings.scoreable_mask(
                ["one", "two"],
                malformed_bundle,
                max_length=32,
            )

    seeded: list[tuple[int, bool]] = []
    transformers = ModuleType("transformers")

    def set_seed(seed: int, *, deterministic: bool) -> None:
        seeded.append((seed, deterministic))

    transformers.set_seed = set_seed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    _CorpusgenModelRuntimeBindings.set_seed(1729, ModelDevice.CUDA)
    assert seeded == [(1729, False)]


def test_candidate_and_scorer_contract_mutations_fail_atomically() -> None:
    invalid_backend = FakeBackend("local")
    invalid_backend.generate = lambda *_args, **_kwargs: [{"text": "", "phonemes": []}]  # type: ignore[method-assign]
    with pytest.raises(EngineContractError):
        _DeduplicatingBackend(invalid_backend, "test").generate(["p"])

    empty_backend = FakeBackend("local")
    empty_backend.generate = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    with pytest.raises(EngineUnavailableError):
        _DeduplicatingBackend(empty_backend, "test").generate(["p"])

    target = FakeTarget(("p",))
    scorer = _GeneratedScorer(FakeScorer(target))
    with pytest.raises(EngineContractError):
        scorer.rank([{"text": "Pat", "phonemes": ["p"]}])
    with pytest.raises(EngineContractError):
        scorer.score_and_commit(["p"], 0, text="Pat")
