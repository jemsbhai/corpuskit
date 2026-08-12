"""Fail-closed contracts for the manual credentialed provider qualifier."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.provider.qualified_provider_acceptance import (  # noqa: E402
    EVIDENCE_SCHEMA,
    MAX_COST_USD,
    MAX_EVIDENCE_BYTES,
    NETWORK_POLICY,
    QualificationConfig,
    QualifiedProviderEvidence,
    _parser,
    fixture_contract,
    main,
    read_and_validate_evidence,
    run_qualification,
    validate_qualified_provider_evidence,
)

from corpuskit.adapters.corpusgen.model_runtime import (  # noqa: E402
    ProviderCallError,
    ProviderCompletion,
)
from corpuskit.domain.errors import (  # noqa: E402
    EngineContractError,
    EngineUnavailableError,
    InvalidRequestError,
)
from corpuskit.domain.model_runtime import DEFAULT_HOSTED_PROMPT_TEMPLATE  # noqa: E402

SOURCE_SHA = "a" * 40
IMAGE_DIGEST = f"sha256:{'b' * 64}"
PROVIDER = "openai"
MODEL = "openai/demo-model"
INPUT_PRICE = Decimal("1")
OUTPUT_PRICE = Decimal("2")
API_KEY = "provider-credential-marker"
OUTPUT_MARKER = "generated-output-marker"
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/qualified-provider.yml"


class FakeProvider:
    def __init__(self, outcomes: list[ProviderCompletion | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> ProviderCompletion:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeG2P:
    phonemes: ClassVar[tuple[str, ...]] = ("p",)

    def phonemize_batch(self, texts: list[str], language: str) -> list[object]:
        assert language == "en-us"
        return [SimpleNamespace(phonemes=list(self.phonemes)) for _ in texts]


def config(**changes: object) -> QualificationConfig:
    values: dict[str, object] = {
        "source_revision": SOURCE_SHA,
        "worker_image_digest": IMAGE_DIGEST,
        "provider": PROVIDER,
        "model": MODEL,
        "input_cost_per_million_usd": INPUT_PRICE,
        "output_cost_per_million_usd": OUTPUT_PRICE,
    }
    values.update(changes)
    return QualificationConfig(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def qualified_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from corpusgen.g2p import manager

    monkeypatch.setattr(manager, "G2PManager", FakeG2P)
    monkeypatch.setenv("QUALIFIED_PROVIDER_API_KEY", API_KEY)
    monkeypatch.setenv("CORPUSKIT_ACCEPTANCE_NETWORK", NETWORK_POLICY)


def _successful_evidence(tmp_path: Path) -> tuple[Path, FakeProvider]:
    output = tmp_path / "provider.json"
    provider = FakeProvider(
        [
            ProviderCompletion(
                text=f"Pat packs the {OUTPUT_MARKER}.",
                input_tokens=10,
                output_tokens=3,
            )
        ]
    )
    run_qualification(
        config(),
        output,
        provider_client=provider,
        completed_at=datetime(2026, 8, 12, 6, 30, tzinfo=UTC),
    )
    return output, provider


def test_fake_provider_runs_through_public_adapter_and_writes_redacted_evidence(
    tmp_path: Path,
) -> None:
    output, provider = _successful_evidence(tmp_path)
    payload = output.read_text(encoding="utf-8")
    evidence = read_and_validate_evidence(
        output,
        expected_source_revision=SOURCE_SHA,
        expected_worker_image_digest=IMAGE_DIGEST,
        expected_provider=PROVIDER,
        expected_model=MODEL,
        expected_input_cost_per_million_usd=INPUT_PRICE,
        expected_output_cost_per_million_usd=OUTPUT_PRICE,
    )

    assert evidence.schema_version == EVIDENCE_SCHEMA
    assert evidence.status == "passed"
    assert evidence.observation.requests == 1
    assert evidence.observation.retries == 0
    assert evidence.observation.coverage == 1.0
    assert evidence.observation.actual_cost_usd == Decimal("0.000016")
    assert evidence.observation.reserved_cost_usd <= MAX_COST_USD
    assert len(payload.encode("utf-8")) < MAX_EVIDENCE_BYTES
    assert payload.endswith("\n")
    for forbidden in (
        API_KEY,
        OUTPUT_MARKER,
        DEFAULT_HOSTED_PROMPT_TEMPLATE,
        "secret://",
        "api_key",
        '"accepted":',
        '"source_id":',
    ):
        assert forbidden not in payload

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["provider"] == PROVIDER
    assert call["model"] == MODEL
    assert call["api_key"] == API_KEY
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 48
    assert 0 < call["timeout_seconds"] <= 12.0  # type: ignore[operator]


def test_committed_pydantic_schema_is_strict_and_fixture_is_digest_only() -> None:
    schema = QualifiedProviderEvidence.model_json_schema()
    contract = fixture_contract()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {
        "source_revision",
        "completed_at",
        "runtime",
        "fixture",
        "selection",
        "bounds",
        "observation",
    }
    assert "prompt_template_sha256" in contract
    assert "target_sha256" in contract
    assert DEFAULT_HOSTED_PROMPT_TEMPLATE not in json.dumps(contract)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), "corpuskit.qualified-provider-acceptance.v0"),
        (("source_revision",), "c" * 40),
        (("runtime", "worker_image_digest"), f"sha256:{'c' * 64}"),
        (("fixture", "contract_sha256"), "0" * 64),
        (("fixture", "generated_text_retained"), True),
        (("selection", "provider"), "anthropic"),
        (("selection", "model"), "anthropic/demo-model"),
        (("bounds", "max_cost_usd"), "0.06"),
        (("observation", "actual_cost_usd"), "0.01"),
        (("observation", "reserved_output_tokens"), 47),
        (("observation", "requests"), 2),
        (("privacy", "credential_value_retained"), True),
    ],
)
def test_evidence_validator_rejects_identity_privacy_and_budget_mutations(
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: object,
) -> None:
    output, _ = _successful_evidence(tmp_path)
    payload = json.loads(output.read_text(encoding="utf-8"))
    target: dict[str, Any] = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement

    with pytest.raises(RuntimeError, match="qualified provider evidence"):
        validate_qualified_provider_evidence(
            payload,
            expected_source_revision=SOURCE_SHA,
            expected_worker_image_digest=IMAGE_DIGEST,
            expected_provider=PROVIDER,
            expected_model=MODEL,
            expected_input_cost_per_million_usd=INPUT_PRICE,
            expected_output_cost_per_million_usd=OUTPUT_PRICE,
        )


def test_evidence_validator_rejects_unknown_raw_output_field(tmp_path: Path) -> None:
    output, _ = _successful_evidence(tmp_path)
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["raw_output"] = API_KEY

    with pytest.raises(RuntimeError, match="qualified provider evidence validation failed"):
        validate_qualified_provider_evidence(
            payload,
            expected_source_revision=SOURCE_SHA,
            expected_worker_image_digest=IMAGE_DIGEST,
            expected_provider=PROVIDER,
            expected_model=MODEL,
            expected_input_cost_per_million_usd=INPUT_PRICE,
            expected_output_cost_per_million_usd=OUTPUT_PRICE,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_revision": "0" * 40},
        {"source_revision": "A" * 40},
        {"worker_image_digest": f"sha256:{'0' * 64}"},
        {"model": "anthropic/demo-model"},
        {"input_cost_per_million_usd": Decimal("NaN")},
        {"output_cost_per_million_usd": Decimal("0")},
    ],
)
def test_invalid_identity_namespace_or_pricing_fails_before_provider_contact(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    provider = FakeProvider([ProviderCompletion(text="Pat.", input_tokens=1, output_tokens=1)])
    output = tmp_path / "provider.json"

    with pytest.raises(RuntimeError):
        run_qualification(config(**changes), output, provider_client=provider)

    assert provider.calls == []
    assert not output.exists()


def test_price_reservation_over_ceiling_fails_before_provider_contact(tmp_path: Path) -> None:
    provider = FakeProvider([ProviderCompletion(text="Pat.", input_tokens=1, output_tokens=1)])
    output = tmp_path / "provider.json"

    with pytest.raises(InvalidRequestError) as caught:
        run_qualification(
            config(
                input_cost_per_million_usd=Decimal("1000"),
                output_cost_per_million_usd=Decimal("1000"),
            ),
            output,
            provider_client=provider,
        )

    assert caught.value.operation == "model_runtime.hosted.budget"
    assert provider.calls == []
    assert not output.exists()


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    [
        (
            ProviderCompletion(text="Pat.", input_tokens=10_000, output_tokens=1),
            EngineContractError,
        ),
        (ProviderCompletion(text="   ", input_tokens=1, output_tokens=1), EngineUnavailableError),
        (ProviderCallError(retryable=False), EngineUnavailableError),
        (ProviderCallError(retryable=True), EngineUnavailableError),
    ],
)
def test_provider_usage_empty_output_and_failures_leave_no_artifact(
    tmp_path: Path,
    outcome: ProviderCompletion | Exception,
    error_type: type[Exception],
) -> None:
    provider = FakeProvider([outcome])
    output = tmp_path / "provider.json"

    with pytest.raises(error_type):
        run_qualification(config(), output, provider_client=provider)

    assert len(provider.calls) == 1
    assert not output.exists()


def test_missing_target_coverage_cannot_become_qualified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingTargetG2P(FakeG2P):
        phonemes: ClassVar[tuple[str, ...]] = ("b",)

    from corpusgen.g2p import manager

    monkeypatch.setattr(manager, "G2PManager", MissingTargetG2P)
    provider = FakeProvider(
        [ProviderCompletion(text="A bland line.", input_tokens=3, output_tokens=3)]
    )
    output = tmp_path / "provider.json"

    with pytest.raises(RuntimeError, match="output quality gate"):
        run_qualification(config(), output, provider_client=provider)

    assert not output.exists()


def test_missing_secret_or_network_policy_fails_without_provider_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider([ProviderCompletion(text="Pat.", input_tokens=1, output_tokens=1)])
    monkeypatch.delenv("QUALIFIED_PROVIDER_API_KEY")
    with pytest.raises(EngineUnavailableError) as caught:
        run_qualification(config(), tmp_path / "missing-secret.json", provider_client=provider)
    assert caught.value.operation == "model_runtime.secret.resolve"
    assert provider.calls == []

    monkeypatch.setenv("QUALIFIED_PROVIDER_API_KEY", API_KEY)
    monkeypatch.delenv("CORPUSKIT_ACCEPTANCE_NETWORK")
    with pytest.raises(RuntimeError, match="network policy"):
        run_qualification(config(), tmp_path / "missing-network.json", provider_client=provider)
    assert provider.calls == []


def test_existing_evidence_cannot_be_overwritten_or_mistaken_for_a_new_run(
    tmp_path: Path,
) -> None:
    output = tmp_path / "provider.json"
    output.write_text("stale", encoding="utf-8")
    provider = FakeProvider([ProviderCompletion(text="Pat.", input_tokens=1, output_tokens=1)])

    with pytest.raises(RuntimeError, match="output path"):
        run_qualification(config(), output, provider_client=provider)

    assert output.read_text(encoding="utf-8") == "stale"
    assert provider.calls == []


def test_evidence_publication_does_not_overwrite_a_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "provider.json"
    real_link = os.link

    def racing_link(source: str | Path, destination: str | Path) -> None:
        Path(destination).write_text("competing-writer", encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(os, "link", racing_link)
    provider = FakeProvider([ProviderCompletion(text="Pat.", input_tokens=1, output_tokens=1)])

    with pytest.raises(RuntimeError, match="became unavailable"):
        run_qualification(config(), output, provider_client=provider)

    assert output.read_text(encoding="utf-8") == "competing-writer"
    assert not list(tmp_path.glob(".*.tmp"))


def test_verify_rejects_oversized_or_wrong_expected_identity(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
    with pytest.raises(RuntimeError, match="evidence file"):
        read_and_validate_evidence(
            oversized,
            expected_source_revision=SOURCE_SHA,
            expected_worker_image_digest=IMAGE_DIGEST,
            expected_provider=PROVIDER,
            expected_model=MODEL,
            expected_input_cost_per_million_usd=INPUT_PRICE,
            expected_output_cost_per_million_usd=OUTPUT_PRICE,
        )

    output, _ = _successful_evidence(tmp_path)
    with pytest.raises(RuntimeError, match="evidence contract"):
        read_and_validate_evidence(
            output,
            expected_source_revision="c" * 40,
            expected_worker_image_digest=IMAGE_DIGEST,
            expected_provider=PROVIDER,
            expected_model=MODEL,
            expected_input_cost_per_million_usd=INPUT_PRICE,
            expected_output_cost_per_million_usd=OUTPUT_PRICE,
        )


def test_cli_has_no_credential_or_budget_override_argument() -> None:
    parser = _parser()
    help_text = parser.format_help()
    assert "api-key" not in help_text
    subparsers = next(action for action in parser._actions if action.dest == "command")
    choices = cast(dict[str, argparse.ArgumentParser], subparsers.choices)
    run_help = choices["run"].format_help()
    assert "api-key" not in run_help
    assert "max-requests" not in run_help
    assert "max-cost" not in run_help
    assert "max-retries" not in run_help

    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "run",
                "--api-key",
                API_KEY,
            ]
        )


def test_cli_never_prints_an_external_exception_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_: object, **__: object) -> object:
        raise RuntimeError(API_KEY)

    monkeypatch.setattr(
        "scripts.provider.qualified_provider_acceptance.run_qualification",
        fail,
    )
    arguments = [
        "run",
        "--candidate-sha",
        SOURCE_SHA,
        "--worker-image-digest",
        IMAGE_DIGEST,
        "--provider",
        PROVIDER,
        "--model",
        MODEL,
        "--input-cost-per-million-usd",
        "1",
        "--output-cost-per-million-usd",
        "2",
        "--output",
        "provider.json",
    ]

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "qualified provider acceptance failed\n"
    assert API_KEY not in captured.err


def test_manual_workflow_is_protected_pinned_and_spend_is_never_pr_automatic() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "pull_request_target:" not in workflow
    assert "schedule:" not in workflow
    assert "environment: qualified-provider" in workflow
    assert "runs-on: [self-hosted, linux, x64, corpuskit-qualified, provider-egress]" in workflow
    assert re.search(r"permissions:\s+contents: read", workflow)
    assert action_references
    assert all(re.fullmatch(r"[^/@\s]+/[^@\s]+@[0-9a-f]{40}", item) for item in action_references)
    assert "continue-on-error" not in workflow


def test_workflow_checks_exact_sha_and_hardens_live_and_offline_boundaries() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    live_step = workflow.split(
        "- name: Run one budget-capped live provider qualification",
        maxsplit=1,
    )[1].split("- name: Revalidate the retained artifact", maxsplit=1)[0]
    verify_step = workflow.split(
        "- name: Revalidate the retained artifact without network or credentials",
        maxsplit=1,
    )[1].split("- name: Upload the redacted exact-candidate evidence", maxsplit=1)[0]

    assert 'actual_sha="$(git rev-parse HEAD)"' in workflow
    assert 'test "${actual_sha}" = "${REQUESTED_CANDIDATE_SHA}"' in workflow
    assert "git diff --exit-code" in workflow
    assert "git diff --cached --exit-code" in workflow
    assert "corpuskit.egress-policy" in workflow
    assert "timeout --signal=TERM --kill-after=5s 40s" in live_step
    for hardening in (
        "--user 10001:10001",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--pids-limit 256",
        "--memory 2g",
        "--cpus 2",
    ):
        assert hardening in live_step
    assert '--network "${EGRESS_NETWORK}"' in live_step
    assert "--network none" in verify_step
    assert "QUALIFIED_PROVIDER_API_KEY" not in verify_step
    cleanup_step = workflow.split("- name: Remove ephemeral qualification state", maxsplit=1)[1]
    assert 'docker container rm --force "${container_id}"' in cleanup_step
    assert 'rm -f -- "${cidfile}"' in cleanup_step
    assert "path: ${{ steps.evidence.outputs.path }}/provider.json" in workflow
    assert "path: ${{ steps.evidence.outputs.path }}\n" not in workflow


def test_workflow_passes_secret_only_as_environment_and_never_as_an_argument() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    live_step = workflow.split(
        "- name: Run one budget-capped live provider qualification",
        maxsplit=1,
    )[1].split("- name: Revalidate the retained artifact", maxsplit=1)[0]

    assert workflow.count("${{ secrets.QUALIFIED_PROVIDER_API_KEY }}") == 1
    secret_line = next(line for line in live_step.splitlines() if "secrets." in line)
    assert secret_line.strip().startswith("QUALIFIED_PROVIDER_API_KEY:")
    assert "--env QUALIFIED_PROVIDER_API_KEY" in live_step
    assert "--api-key" not in workflow
    assert "secret://" not in workflow
    assert "*.json" not in workflow
