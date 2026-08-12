"""Contracts for shell-safe, non-executing CorpusGen CLI previews."""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from corpuskit.api.cli_parity import cli_parity_router
from corpuskit.domain.cli_parity import (
    CliCommandPreview,
    CliEvaluateRequest,
    CliGenerateRequest,
    CliGenerationBackend,
    CliGuidance,
    CliInventoryRequest,
    CliPreviewRequest,
    CliQuantization,
    CliReproducibility,
    CliSelectRequest,
    CliTargetMode,
    CliWeight,
)
from corpuskit.domain.selection import SelectionAlgorithm, UnitWeight
from corpuskit.services.cli_parity import CliParityService


def test_inventory_preview_has_exact_argv_and_utf8_shell_commands() -> None:
    preview = CliParityService().preview(CliInventoryRequest(language="en-us", source="spa"))

    assert preview.argv == (
        "corpusgen",
        "inventory",
        "--language",
        "en-us",
        "--format",
        "json",
        "--source",
        "spa",
    )
    assert shlex.split(preview.posix_command)[1:] == list(preview.argv)
    assert preview.posix_command.startswith("PYTHONUTF8=1 ")
    assert preview.powershell_command.startswith("$env:PYTHONUTF8 = '1'; & ")
    assert preview.reproducibility is CliReproducibility.EXACT_INPUTS_REQUIRED


def test_evaluate_preview_quotes_hostile_text_without_changing_argv() -> None:
    sentence = "A user's $(Remove-Item *) corpus; echo 'still data'"
    preview = CliParityService().preview(
        CliEvaluateRequest(language="en-us", sentences=(sentence,))
    )

    assert preview.argv[2] == sentence
    assert shlex.split(preview.posix_command)[1:] == list(preview.argv)
    assert "user''s" in preview.powershell_command
    assert "echo ''still data''" in preview.powershell_command
    assert "explicit target list" in " ".join(preview.warnings)


def test_distribution_selection_uses_canonical_json_and_discloses_cli_limits() -> None:
    preview = CliParityService().preview(
        CliSelectRequest(
            language="en-us",
            file_path="inputs/candidates.txt",
            algorithm=SelectionAlgorithm.DISTRIBUTION,
            target_distribution=(
                UnitWeight(unit="t", weight=0.4),
                UnitWeight(unit="p", weight=0.6),
            ),
            max_sentences=10,
            output_path="outputs/selected.txt",
        )
    )

    position = preview.argv.index("--target-distribution")
    assert preview.argv[position + 1] == '{"p":0.6,"t":0.4}'
    assert "--max-sentences" in preview.argv
    assert "--output" in preview.argv
    assert "stochastic seeds" in " ".join(preview.warnings)


def test_file_phoible_and_stochastic_previews_cover_optional_cli_flags() -> None:
    service = CliParityService()
    evaluation = service.preview(
        CliEvaluateRequest(
            language="fr-fr",
            file_path="inputs/corpus.txt",
            target=CliTargetMode.PHOIBLE,
        )
    )
    selection = service.preview(
        CliSelectRequest(
            language="fr-fr",
            file_path="inputs/corpus.txt",
            target=CliTargetMode.PHOIBLE,
            algorithm=SelectionAlgorithm.STOCHASTIC,
        )
    )

    assert evaluation.argv[2:4] == ("--file", "inputs/corpus.txt")
    assert "--target" in evaluation.argv
    assert "--target" in selection.argv
    assert selection.reproducibility is CliReproducibility.BEST_EFFORT


def test_generation_preview_covers_repository_guidance_scoring_and_safety_stops() -> None:
    preview = CliParityService().preview(
        CliGenerateRequest(
            backend=CliGenerationBackend.LOCAL,
            language="en-us",
            model="org/model",
            phonemes=("p", "t"),
            weights=(CliWeight(unit="p", weight=2.0),),
            max_sentences=12,
            max_iterations=20,
            timeout_seconds=60,
            device="cuda",
            quantization=CliQuantization.FOUR_BIT,
            prompt_template="Use {target_units} safely.",
            guidance=CliGuidance.DATG,
            phonotactic_weight=0.5,
            phonotactic_scorer="ngram",
            phonotactic_corpus_path="inputs/reference.txt",
            fluency_weight=0.2,
            fluency_scorer="perplexity",
            fluency_model="org/fluency",
            output_path="outputs/generated.txt",
        )
    )

    joined = " ".join(preview.argv)
    for option in (
        "--phonemes",
        "--weights",
        "--max-sentences",
        "--max-iterations",
        "--timeout",
        "--quantization",
        "--prompt-template",
        "--datg-boost",
        "--phonotactic-corpus",
        "--fluency-model",
    ):
        assert option in preview.argv
    assert "p,t" in preview.argv
    assert "p:2.0" in preview.argv
    warnings = " ".join(preview.warnings)
    assert "verified offline revision and snapshot digest" in joined + warnings
    assert "seed, top-p, or sampling-mode" in warnings
    assert "readability scorer" in warnings
    assert preview.reproducibility is CliReproducibility.BEST_EFFORT


def test_dataset_and_llm_generation_previews_disclose_external_reproducibility() -> None:
    service = CliParityService()
    dataset = service.preview(
        CliGenerateRequest(
            backend="repository",
            language="en-us",
            dataset="org/dataset",
            split="train",
            max_samples=100,
        )
    )
    llm = service.preview(
        CliGenerateRequest(
            backend="llm_api",
            language="en-us",
            model="provider/model",
            prompt_template="Include {target_units}.",
            max_sentences=None,
            max_iterations=10,
            timeout_seconds=None,
        )
    )

    assert "--dataset" in dataset.argv
    assert "--split" in dataset.argv
    assert "--max-samples" in dataset.argv
    assert "cannot pin a dataset" in " ".join(dataset.warnings)
    assert "--llm-temperature" in llm.argv
    assert "--llm-max-tokens" in llm.argv
    assert "--prompt-template" in llm.argv
    assert llm.reproducibility is CliReproducibility.EXTERNAL_DEPENDENCY
    llm_warnings = " ".join(llm.warnings)
    assert "credentials" in llm_warnings
    assert "separate provider allowlist" in llm_warnings
    assert "per-candidate phonemes" in llm_warnings


def test_rl_guidance_config_serializes_without_flat_datg_controls() -> None:
    preview = CliParityService().preview(
        CliGenerateRequest(
            backend="local",
            language="en-us",
            model="org/model",
            guidance="rl",
            guidance_config_path="config/guidance.json",
            rl_adapter_path="models/adapter",
            max_sentences=5,
            max_iterations=None,
            timeout_seconds=20,
        )
    )

    assert "--guidance-config" in preview.argv
    assert "--rl-adapter-path" in preview.argv
    assert "--datg-boost" not in preview.argv


@pytest.mark.parametrize(
    "payload",
    [
        {"workflow": "inventory", "language": "bad language"},
        {"workflow": "evaluate", "language": "en-us"},
        {
            "workflow": "evaluate",
            "language": "en-us",
            "sentences": ["one"],
            "file_path": "corpus.txt",
        },
        {
            "workflow": "select",
            "language": "en-us",
            "file_path": "pool.txt\n--output=stolen",
        },
        {
            "workflow": "select",
            "language": "en-us",
            "file_path": "pool.txt",
            "algorithm": "distribution",
        },
        {
            "workflow": "select",
            "language": "en-us",
            "file_path": "pool.txt",
            "algorithm": "greedy",
            "target_distribution": [{"unit": "p", "weight": 1}],
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "guidance": "rl",
        },
        {
            "workflow": "generate",
            "backend": "llm_api",
            "language": "en-us",
            "model": "provider/model",
            "guidance": "datg",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "max_sentences": None,
            "max_iterations": None,
            "timeout_seconds": None,
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "device": "cpu",
            "quantization": "4bit",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "api_key": "must-not-be-accepted",
        },
    ],
)
def test_preview_domain_rejects_ambiguous_unsafe_or_secret_input(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(CliPreviewRequest).validate_python(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"workflow": "inventory", "language": "en-us", "source": "bad source"},
        {"workflow": "evaluate", "language": "en-us", "sentences": [" "]},
        {
            "workflow": "select",
            "language": "en-us",
            "file_path": "pool.txt",
            "algorithm": "distribution",
            "target_distribution": [
                {"unit": "p", "weight": 1},
                {"unit": "p", "weight": 2},
            ],
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "model": "org/model",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "file_path": "pool.txt",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "dataset": "bad dataset",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "dataset": "org/dataset",
            "text_column": "bad column",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model\nunsafe",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "rl_adapter_path": "adapter",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "prompt_template": "missing placeholder",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "prompt_template": "Use {target_units}",
        },
        {
            "workflow": "generate",
            "backend": "local",
            "language": "en-us",
            "model": "org/model",
            "guidance_config_path": "guidance.json",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "phonotactic_weight": 1,
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "fluency_scorer": "perplexity",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "phonotactic_corpus_path": "reference.txt",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "fluency_model": "org/model",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "target_source": "bad source",
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "phonemes": ["p", "p"],
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "phonemes": ["p,t"],
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "weights": [
                {"unit": "p", "weight": 1},
                {"unit": "p", "weight": 2},
            ],
        },
        {
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": " pool.txt",
        },
    ],
)
def test_preview_domain_hardening_rejects_invalid_combinations(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(CliPreviewRequest).validate_python(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 0.0])
def test_cli_weights_require_finite_positive_values(value: float) -> None:
    with pytest.raises(ValidationError):
        CliWeight(unit="p", weight=value)


def test_cli_weight_rejects_ambiguous_serialization_delimiters() -> None:
    with pytest.raises(ValidationError):
        CliWeight(unit="p:t", weight=1)


def test_http_route_parses_discriminated_requests_and_never_executes_shell() -> None:
    app = FastAPI()
    app.include_router(cli_parity_router(CliParityService()), prefix="/api/v1")
    client = TestClient(app)

    response = client.post(
        "/api/v1/labs/cli/preview",
        json={
            "workflow": "generate",
            "backend": "repository",
            "language": "en-us",
            "file_path": "pool.txt",
            "max_sentences": 5,
            "max_iterations": 10,
            "timeout_seconds": 30,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow"] == "generate"
    assert body["argv"][:4] == ["corpusgen", "generate", "--backend", "repository"]
    assert body["environment"] == [["PYTHONUTF8", "1"]]
    assert "--api-key" not in body["argv"]


def test_http_route_returns_schema_error_for_unknown_fields() -> None:
    app = FastAPI()
    app.include_router(cli_parity_router(CliParityService()), prefix="/api/v1")

    response = TestClient(app).post(
        "/api/v1/labs/cli/preview",
        json={
            "workflow": "inventory",
            "language": "en-us",
            "command": "whoami",
        },
    )

    assert response.status_code == 422
    assert "extra_forbidden" in json.dumps(response.json())


def test_preview_response_rejects_non_argv_contracts() -> None:
    with pytest.raises(ValidationError):
        CliCommandPreview(
            workflow="inventory",
            argv=("corpusgen",),
            posix_command="x",
            powershell_command="x",
            reproducibility="best_effort",
        )
