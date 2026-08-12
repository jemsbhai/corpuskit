"""Real CorpusGen CLI parity acceptance for copyable CorpusKit previews."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pytest
from click.testing import CliRunner, Result
from corpusgen.cli import main as corpusgen_cli

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.adapters.corpusgen.generation import CorpusgenGenerationAdapter
from corpuskit.adapters.corpusgen.model_runtime import (
    CorpusgenModelRuntimeAdapter,
    ProviderCompletion,
)
from corpuskit.domain.cli_parity import (
    CliEvaluateRequest,
    CliGenerateRequest,
    CliInventoryRequest,
    CliSelectRequest,
)
from corpuskit.domain.corpus import EvaluationTarget
from corpuskit.domain.generation import (
    GenerationExecutionMode,
    GenerationStoppingCriteria,
    GenerationTarget,
    RawTextCandidate,
    RawTextRepository,
    RepositoryGenerationRequest,
)
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedModelSelection,
    HostedRunBudget,
    ProviderRetryPolicy,
    SecretReference,
)
from corpuskit.domain.selection import SelectionOptions, SelectionRequest
from corpuskit.services.cli_parity import CliParityService

pytestmark = pytest.mark.integration

_FAKE_PROVIDER_TOKEN = "deterministic-test-boundary"


class _AcceptedLike(Protocol):
    text: str


class _AdapterGenerationLike(Protocol):
    accepted: tuple[_AcceptedLike, ...]
    coverage: float
    covered_units: tuple[str, ...]
    missing_units: tuple[str, ...]
    iterations: int
    stop_reason: object


class _FakeSecretResolver:
    def resolve(self, reference: SecretReference) -> str:
        assert reference.reference == "secret://env/CLI_PARITY_PROVIDER_KEY"
        return _FAKE_PROVIDER_TOKEN


class _FakeHostedProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, object]] = []

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
        assert api_key == _FAKE_PROVIDER_TOKEN
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        return ProviderCompletion(text=self._text, input_tokens=32, output_tokens=4)


def _invoke(argv: tuple[str, ...]) -> Result:
    assert argv[0] == "corpusgen"
    result = CliRunner().invoke(
        corpusgen_cli,
        list(argv[1:]),
        env={"PYTHONUTF8": "1"},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    return result


def _normalize_cli_generation(result: dict[str, object]) -> dict[str, object]:
    texts = result["generated_sentences"]
    covered = result["covered_units"]
    missing = result["missing_units"]
    assert isinstance(texts, list)
    assert all(isinstance(item, str) for item in texts)
    assert isinstance(covered, list)
    assert all(isinstance(item, str) for item in covered)
    assert isinstance(missing, list)
    assert all(isinstance(item, str) for item in missing)
    return {
        "texts": tuple(texts),
        "accepted_count": result["num_generated"],
        "coverage": result["coverage"],
        "covered_units": tuple(sorted(covered)),
        "missing_units": tuple(sorted(missing)),
        "unit": result["unit"],
        "backend": result["backend"],
        "iterations": result["iterations"],
        "stop_reason": result["stop_reason"],
    }


def _normalize_adapter_generation(
    result: _AdapterGenerationLike,
    *,
    backend: str,
    unit: str,
) -> dict[str, object]:
    stop_reason = getattr(result.stop_reason, "value", result.stop_reason)
    return {
        "texts": tuple(item.text for item in result.accepted),
        "accepted_count": len(result.accepted),
        "coverage": result.coverage,
        "covered_units": tuple(sorted(result.covered_units)),
        "missing_units": tuple(sorted(result.missing_units)),
        "unit": unit,
        "backend": backend,
        "iterations": result.iterations,
        "stop_reason": stop_reason,
    }


def test_inventory_preview_executes_with_json_matching_adapter_contract() -> None:
    preview = CliParityService().preview(CliInventoryRequest(language="en-us"))

    cli_result = json.loads(_invoke(preview.argv).stdout)
    api_result = CorpusgenAdapter().get_inventory("en-us")

    assert cli_result["iso639_3"] == api_result.iso639_3
    assert cli_result["language_name"] == api_result.language_name
    assert cli_result["source"] == api_result.source
    assert cli_result["total"] == api_result.size
    assert set(cli_result["phonemes"]) == {segment.phoneme for segment in api_result.segments}


def test_evaluation_preview_executes_with_json_matching_adapter_contract() -> None:
    sentences = ("Pat taps.", "Bob kicks.")
    preview = CliParityService().preview(CliEvaluateRequest(language="en-us", sentences=sentences))

    cli_result = json.loads(_invoke(preview.argv).stdout)
    api_result = CorpusgenAdapter().evaluate(sentences, target=EvaluationTarget())

    assert cli_result["language"] == api_result.language
    assert cli_result["unit"] == api_result.unit.value
    assert cli_result["coverage"] == api_result.coverage
    assert cli_result["total_sentences"] == api_result.total_sentences
    assert set(cli_result["covered_phonemes"]) == set(api_result.covered_units)
    assert set(cli_result["missing_phonemes"]) == set(api_result.missing_units)


def test_selection_preview_executes_with_json_matching_adapter_contract(tmp_path: Path) -> None:
    candidates = ("Pat taps.", "Bob kicks.", "A sad cat.")
    source = tmp_path / "candidate pool.txt"
    source.write_text("\n".join(candidates) + "\n", encoding="utf-8")
    preview = CliParityService().preview(CliSelectRequest(language="en-us", file_path=str(source)))

    cli_result = json.loads(_invoke(preview.argv).stdout)
    api_result = CorpusgenAdapter().select(
        SelectionRequest(
            candidates=candidates,
            language="en-us",
            target=EvaluationTarget(),
            options=SelectionOptions(),
        )
    )

    assert cli_result["algorithm"] == api_result.algorithm.value
    assert cli_result["unit"] == api_result.unit.value
    assert cli_result["coverage"] == api_result.coverage
    assert tuple(cli_result["selected_indices"]) == api_result.selected_indices
    assert tuple(cli_result["selected_sentences"]) == api_result.selected_sentences
    assert set(cli_result["covered_units"]) == set(api_result.covered_units)


def test_repository_generation_preview_is_accepted_by_real_cli(tmp_path: Path) -> None:
    candidates = ("Pat taps.", "Bob kicks.", "A sad cat.")
    source = tmp_path / "repository.txt"
    source.write_text("\n".join(candidates) + "\n", encoding="utf-8")
    preview = CliParityService().preview(
        CliGenerateRequest(
            backend="repository",
            language="en-us",
            file_path=str(source),
            max_sentences=2,
            max_iterations=3,
            timeout_seconds=10,
        )
    )

    cli_result = json.loads(_invoke(preview.argv).stdout)
    inventory = CorpusgenAdapter().get_inventory("en-us")
    adapter_result = CorpusgenGenerationAdapter().run_repository(
        RepositoryGenerationRequest(
            source=RawTextRepository(
                entries=tuple(
                    RawTextCandidate(source_id=f"candidate-{index}", text=text)
                    for index, text in enumerate(candidates)
                ),
                language="en-us",
            ),
            target=GenerationTarget(phonemes=inventory.phonemes),
            stopping=GenerationStoppingCriteria(
                max_sentences=2,
                max_iterations=3,
                timeout_seconds=10,
            ),
            candidates_per_iteration=5,
        ),
        execution_mode=GenerationExecutionMode.SYNCHRONOUS_PREVIEW,
    )

    assert _normalize_cli_generation(cli_result) == _normalize_adapter_generation(
        adapter_result,
        backend="repository",
        unit="phoneme",
    )
    assert set(cli_result["generated_sentences"]).issubset(candidates)


def test_hosted_generation_preview_executes_with_fake_provider_matching_adapter_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real locked CLI and CorpusKit adapter cross the same deterministic provider seam."""

    from corpusgen.generate.backends import llm_api as corpusgen_llm

    model = "openai/cli-parity-model"
    response_text = "1. Bob kicks."
    cli_calls: list[dict[str, object]] = []

    def fake_completion(**kwargs: object) -> SimpleNamespace:
        cli_calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
        )

    monkeypatch.setattr(corpusgen_llm, "_call_llm", fake_completion)
    preview = CliParityService().preview(
        CliGenerateRequest(
            backend="llm_api",
            language="en-us",
            model=model,
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=10,
            candidates_per_iteration=1,
            llm_temperature=0.3,
            llm_max_tokens=32,
        )
    )
    cli_result = json.loads(_invoke(preview.argv).stdout)

    target = GenerationTarget(phonemes=CorpusgenAdapter().get_inventory("en-us").phonemes)
    request = HostedGenerationRequest(
        selection=HostedModelSelection(
            provider="openai",
            model=model,
            connection_id="cli-parity-provider",
        ),
        target=target,
        stopping=GenerationStoppingCriteria(
            max_sentences=1,
            max_iterations=1,
            timeout_seconds=10,
        ),
        candidates_per_iteration=1,
        temperature=0.3,
        max_tokens_per_request=32,
        retry=ProviderRetryPolicy(
            max_retries=0,
            request_timeout_seconds=5,
            base_delay_seconds=0,
            max_delay_seconds=0,
        ),
        budget=HostedRunBudget(
            max_requests=1,
            max_input_tokens=2_000,
            max_output_tokens=32,
            max_cost_usd=Decimal("1"),
        ),
        activity_timeout_seconds=20,
        external_processing_confirmed=True,
    )
    policy = HostedModelPolicy(
        provider="openai",
        model=model,
        connection_id="cli-parity-provider",
        credential_ref=SecretReference(reference="secret://env/CLI_PARITY_PROVIDER_KEY"),
        input_cost_per_million_usd=Decimal("0"),
        output_cost_per_million_usd=Decimal("0"),
        max_output_tokens_per_request=32,
    )
    provider = _FakeHostedProvider(response_text)
    adapter_result = CorpusgenModelRuntimeAdapter(
        secret_resolver=_FakeSecretResolver(),
        provider_client=provider,
    ).run_hosted(request, policy)

    assert _normalize_cli_generation(cli_result) == _normalize_adapter_generation(
        adapter_result,
        backend="llm_api",
        unit="phoneme",
    )
    assert len(cli_calls) == len(provider.calls) == adapter_result.usage.requests == 1
    cli_call = cli_calls[0]
    messages = cli_call["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, dict)
    assert cli_call["model"] == provider.calls[0]["model"] == model
    assert message["content"] == provider.calls[0]["prompt"]
    assert cli_call["temperature"] == provider.calls[0]["temperature"] == 0.3
    assert cli_call["max_tokens"] == provider.calls[0]["max_tokens"] == 32
    assert "api_key" not in cli_call
    assert {"provider", "model", "usage", "manifest"}.isdisjoint(cli_result)
