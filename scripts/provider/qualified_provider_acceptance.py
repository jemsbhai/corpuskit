"""Run and verify the bounded manual live-provider qualification.

The live command intentionally has no API-key argument. The production
``EnvironmentSecretResolver`` reads ``QUALIFIED_PROVIDER_API_KEY`` through a fixed
``secret://env`` reference. Retained evidence contains neither the rendered prompt nor
provider output; it records only the immutable source/runtime identity, fixed fixture
digests, public provider/model selection, budget limits, and bounded observations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from corpuskit.adapters.corpusgen.model_runtime import (
    CorpusgenModelRuntimeAdapter,
    HostedProviderClient,
)
from corpuskit.domain.generation import GenerationStoppingCriteria, GenerationTarget
from corpuskit.domain.model_runtime import (
    DEFAULT_HOSTED_PROMPT_TEMPLATE,
    HostedGenerationRequest,
    HostedModelPolicy,
    HostedModelSelection,
    HostedRunBudget,
    ProviderRetryPolicy,
    SecretReference,
)
from corpuskit.services.model_runtime import ModelRuntimePolicy

EVIDENCE_SCHEMA = "corpuskit.qualified-provider-acceptance.v1"
FIXTURE_ID = "corpuskit.provider-fixed-phoneme-p.v1"
NETWORK_POLICY = "operator-allowlisted-provider-egress"
SECRET_ENVIRONMENT_VARIABLE = "QUALIFIED_PROVIDER_API_KEY"  # noqa: S105 - variable name only
CONNECTION_ID = "qualified-provider"
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_REQUESTS = 2
MAX_INPUT_TOKENS = 2_048
MAX_OUTPUT_TOKENS = 96
MAX_TOKENS_PER_REQUEST = 48
MAX_COST_USD = Decimal("0.05")
MAX_ITERATIONS = 2
MAX_SENTENCES = 1
LOOP_TIMEOUT_SECONDS = 20.0
ACTIVITY_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 12.0
TARGET = ("p",)
LANGUAGE = "en-us"
TEMPERATURE = 0.0

_SHA40 = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_MILLION = Decimal(1_000_000)


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RuntimeEvidence(_EvidenceModel):
    worker_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    python_version: str = Field(min_length=3, max_length=32)
    corpuskit_version: str = Field(min_length=1, max_length=64)
    corpusgen_version: str = Field(min_length=1, max_length=64)
    litellm_version: str = Field(min_length=1, max_length=64)
    adapter: Literal["CorpusgenModelRuntimeAdapter"] = "CorpusgenModelRuntimeAdapter"
    provider_client: Literal["LiteLLMProviderClient"] = "LiteLLMProviderClient"
    network_policy: Literal["operator-allowlisted-provider-egress"] = (
        "operator-allowlisted-provider-egress"
    )


class FixtureEvidence(_EvidenceModel):
    fixture_id: Literal["corpuskit.provider-fixed-phoneme-p.v1"] = (
        "corpuskit.provider-fixed-phoneme-p.v1"
    )
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_text_retained: Literal[False] = False
    generated_text_retained: Literal[False] = False


class SelectionEvidence(_EvidenceModel):
    provider: str = Field(min_length=2, max_length=32)
    model: str = Field(min_length=3, max_length=192)
    namespace_bound: Literal[True] = True
    input_cost_per_million_usd: Decimal = Field(gt=Decimal("0"), le=Decimal("1000"))
    output_cost_per_million_usd: Decimal = Field(gt=Decimal("0"), le=Decimal("1000"))


class BoundsEvidence(_EvidenceModel):
    max_requests: Literal[2] = 2
    max_input_tokens: Literal[2048] = 2048
    max_output_tokens: Literal[96] = 96
    max_tokens_per_request: Literal[48] = 48
    max_cost_usd: Decimal = Field(default=MAX_COST_USD, gt=Decimal("0"))
    max_iterations: Literal[2] = 2
    max_sentences: Literal[1] = 1
    loop_timeout_seconds: float = Field(default=LOOP_TIMEOUT_SECONDS, ge=20.0, le=20.0)
    activity_timeout_seconds: float = Field(
        default=ACTIVITY_TIMEOUT_SECONDS,
        ge=30.0,
        le=30.0,
    )
    request_timeout_seconds: float = Field(
        default=REQUEST_TIMEOUT_SECONDS,
        ge=12.0,
        le=12.0,
    )
    max_retries: Literal[0] = 0


class ObservationEvidence(_EvidenceModel):
    requests: int = Field(ge=1, le=MAX_REQUESTS)
    retries: Literal[0] = 0
    input_tokens: int = Field(ge=1, le=MAX_INPUT_TOKENS)
    output_tokens: int = Field(ge=1, le=MAX_OUTPUT_TOKENS)
    reserved_input_tokens: int = Field(ge=1, le=MAX_INPUT_TOKENS)
    reserved_output_tokens: int = Field(ge=1, le=MAX_OUTPUT_TOKENS)
    actual_cost_usd: Decimal = Field(gt=Decimal("0"), le=MAX_COST_USD)
    reserved_cost_usd: Decimal = Field(gt=Decimal("0"), le=MAX_COST_USD)
    accepted_count: Literal[1] = 1
    coverage: float = Field(default=1.0, ge=1.0, le=1.0)
    covered_target_count: Literal[1] = 1
    missing_target_count: Literal[0] = 0
    iterations: int = Field(ge=1, le=MAX_ITERATIONS)
    elapsed_seconds: float = Field(ge=0.0, le=LOOP_TIMEOUT_SECONDS)
    stop_reason: Literal["target_coverage"] = "target_coverage"
    manifest_verified: Literal[True] = True


class PrivacyEvidence(_EvidenceModel):
    credential_value_retained: Literal[False] = False
    credential_reference_retained: Literal[False] = False
    prompt_text_retained: Literal[False] = False
    generated_text_retained: Literal[False] = False
    provider_callbacks_disabled: Literal[True] = True


class QualifiedProviderEvidence(_EvidenceModel):
    schema_version: Literal["corpuskit.qualified-provider-acceptance.v1"] = (
        "corpuskit.qualified-provider-acceptance.v1"
    )
    status: Literal["passed"] = "passed"
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    completed_at: AwareDatetime
    runtime: RuntimeEvidence
    fixture: FixtureEvidence
    selection: SelectionEvidence
    bounds: BoundsEvidence
    observation: ObservationEvidence
    privacy: PrivacyEvidence = PrivacyEvidence()


@dataclass(frozen=True, slots=True)
class QualificationConfig:
    source_revision: str
    worker_image_digest: str
    provider: str
    model: str
    input_cost_per_million_usd: Decimal
    output_cost_per_million_usd: Decimal


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fixture_contract() -> dict[str, object]:
    """Return the fixed non-sensitive fixture identity without the prompt text."""

    return {
        "schema": "corpuskit.provider-fixture-contract.v1",
        "prompt_template_sha256": hashlib.sha256(
            DEFAULT_HOSTED_PROMPT_TEMPLATE.encode("utf-8")
        ).hexdigest(),
        "target_sha256": _canonical_sha256({"phonemes": TARGET, "unit": "phoneme"}),
        "language": LANGUAGE,
        "temperature": TEMPERATURE,
        "candidates_per_iteration": 1,
        "stopping": {
            "target_coverage": 1.0,
            "max_sentences": MAX_SENTENCES,
            "max_iterations": MAX_ITERATIONS,
            "timeout_seconds": LOOP_TIMEOUT_SECONDS,
        },
        "max_tokens_per_request": MAX_TOKENS_PER_REQUEST,
        "retry": {"max_retries": 0, "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS},
        "budget": {
            "max_requests": MAX_REQUESTS,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_cost_usd": str(MAX_COST_USD),
        },
        "activity_timeout_seconds": ACTIVITY_TIMEOUT_SECONDS,
        "external_processing_confirmed": True,
    }


def _validate_config(config: QualificationConfig) -> HostedModelSelection:
    if (
        _SHA40.fullmatch(config.source_revision) is None
        or config.source_revision == "0" * 40
        or _IMAGE_DIGEST.fullmatch(config.worker_image_digest) is None
        or config.worker_image_digest == f"sha256:{'0' * 64}"
    ):
        raise RuntimeError("qualified provider source identity is invalid")
    for price in (
        config.input_cost_per_million_usd,
        config.output_cost_per_million_usd,
    ):
        if not price.is_finite() or price <= 0 or price > Decimal("1000"):
            raise RuntimeError("qualified provider pricing is invalid")
    try:
        return HostedModelSelection(
            provider=config.provider,
            model=config.model,
            connection_id=CONNECTION_ID,
        )
    except ValidationError:
        raise RuntimeError("qualified provider/model binding is invalid") from None


def _request(selection: HostedModelSelection) -> HostedGenerationRequest:
    return HostedGenerationRequest(
        selection=selection,
        target=GenerationTarget(phonemes=TARGET),
        language=LANGUAGE,
        stopping=GenerationStoppingCriteria(
            target_coverage=1.0,
            max_sentences=MAX_SENTENCES,
            max_iterations=MAX_ITERATIONS,
            timeout_seconds=LOOP_TIMEOUT_SECONDS,
        ),
        candidates_per_iteration=1,
        temperature=TEMPERATURE,
        max_tokens_per_request=MAX_TOKENS_PER_REQUEST,
        retry=ProviderRetryPolicy(
            max_retries=0,
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            base_delay_seconds=0.0,
            max_delay_seconds=0.0,
        ),
        budget=HostedRunBudget(
            max_requests=MAX_REQUESTS,
            max_input_tokens=MAX_INPUT_TOKENS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_cost_usd=MAX_COST_USD,
        ),
        activity_timeout_seconds=ACTIVITY_TIMEOUT_SECONDS,
        external_processing_confirmed=True,
    )


def _policy(config: QualificationConfig) -> HostedModelPolicy:
    return HostedModelPolicy(
        provider=config.provider,
        model=config.model,
        connection_id=CONNECTION_ID,
        credential_ref=SecretReference(reference=f"secret://env/{SECRET_ENVIRONMENT_VARIABLE}"),
        input_cost_per_million_usd=config.input_cost_per_million_usd,
        output_cost_per_million_usd=config.output_cost_per_million_usd,
        max_output_tokens_per_request=MAX_TOKENS_PER_REQUEST,
        request_delay_seconds=0.0,
    )


def _runtime_evidence(config: QualificationConfig) -> RuntimeEvidence:
    return RuntimeEvidence(
        worker_image_digest=config.worker_image_digest,
        python_version=sys.version.split()[0],
        corpuskit_version=importlib.metadata.version("corpuskit-app"),
        corpusgen_version=importlib.metadata.version("corpusgen"),
        litellm_version=importlib.metadata.version("litellm"),
    )


def run_qualification(
    config: QualificationConfig,
    output: Path,
    *,
    provider_client: HostedProviderClient | None = None,
    completed_at: datetime | None = None,
) -> QualifiedProviderEvidence:
    """Call the public adapter exactly within the fixed qualification contract."""

    _prepare_new_output(output)
    selection = _validate_config(config)
    if os.environ.get("CORPUSKIT_ACCEPTANCE_NETWORK") != NETWORK_POLICY:
        raise RuntimeError("qualified provider network policy is unavailable")
    request = _request(selection)
    policy = _policy(config)
    admission = ModelRuntimePolicy(hosted_models=(policy,))
    validation = admission.validate_hosted(request)
    estimate = admission.estimate_hosted(request)
    if (
        validation.maximum_requests != MAX_REQUESTS
        or validation.maximum_authorized_cost_usd != MAX_COST_USD
        or estimate.maximum_requests != MAX_REQUESTS
        or estimate.estimated_ceiling_usd > MAX_COST_USD
    ):
        raise RuntimeError("qualified provider admission bounds are invalid")

    adapter = CorpusgenModelRuntimeAdapter(provider_client=provider_client)
    result = adapter.run_hosted(request, policy)
    if (
        result.manifest.provider != config.provider
        or result.manifest.model != config.model
        or result.manifest.max_tokens_per_request != MAX_TOKENS_PER_REQUEST
        or result.manifest.budget != request.budget
        or result.manifest.retry != request.retry
        or result.manifest.whole_activity_timeout_seconds != ACTIVITY_TIMEOUT_SECONDS
        or result.manifest.prompt_template_sha256
        != hashlib.sha256(DEFAULT_HOSTED_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
        or result.manifest.custom_prompt_template
        or not result.manifest.external_processing_confirmed
        or result.manifest.provider_seed_supported
    ):
        raise RuntimeError("qualified provider execution manifest is invalid")
    if (
        len(result.accepted) != 1
        or result.coverage != 1.0
        or len(result.covered_units) != 1
        or result.missing_units
        or result.iterations < 1
        or result.iterations > MAX_ITERATIONS
        or result.stop_reason.value != "target_coverage"
    ):
        raise RuntimeError("qualified provider output quality gate failed")

    contract = fixture_contract()
    try:
        evidence = QualifiedProviderEvidence(
            source_revision=config.source_revision,
            completed_at=completed_at or datetime.now(UTC),
            runtime=_runtime_evidence(config),
            fixture=FixtureEvidence(
                contract_sha256=_canonical_sha256(contract),
                prompt_template_sha256=str(contract["prompt_template_sha256"]),
                target_sha256=str(contract["target_sha256"]),
            ),
            selection=SelectionEvidence(
                provider=config.provider,
                model=config.model,
                input_cost_per_million_usd=config.input_cost_per_million_usd,
                output_cost_per_million_usd=config.output_cost_per_million_usd,
            ),
            bounds=BoundsEvidence(),
            observation=ObservationEvidence(
                requests=result.usage.requests,
                retries=result.usage.retries,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                reserved_input_tokens=result.usage.reserved_input_tokens,
                reserved_output_tokens=result.usage.reserved_output_tokens,
                actual_cost_usd=result.usage.actual_cost_usd,
                reserved_cost_usd=result.usage.reserved_cost_usd,
                accepted_count=len(result.accepted),
                coverage=result.coverage,
                covered_target_count=len(result.covered_units),
                missing_target_count=len(result.missing_units),
                iterations=result.iterations,
                elapsed_seconds=result.elapsed_seconds,
                stop_reason=result.stop_reason.value,
                manifest_verified=True,
            ),
        )
    except ValidationError:
        raise RuntimeError("qualified provider observation gate failed") from None
    validated = validate_qualified_provider_evidence(
        evidence,
        expected_source_revision=config.source_revision,
        expected_worker_image_digest=config.worker_image_digest,
        expected_provider=config.provider,
        expected_model=config.model,
        expected_input_cost_per_million_usd=config.input_cost_per_million_usd,
        expected_output_cost_per_million_usd=config.output_cost_per_million_usd,
    )
    _write_evidence(output, validated)
    return validated


def validate_qualified_provider_evidence(
    value: QualifiedProviderEvidence | dict[str, object] | str | bytes,
    *,
    expected_source_revision: str,
    expected_worker_image_digest: str,
    expected_provider: str,
    expected_model: str,
    expected_input_cost_per_million_usd: Decimal,
    expected_output_cost_per_million_usd: Decimal,
) -> QualifiedProviderEvidence:
    """Schema- and identity-validate a retained provider artifact, failing closed."""

    try:
        evidence = (
            QualifiedProviderEvidence.model_validate_json(value)
            if isinstance(value, (str, bytes))
            else QualifiedProviderEvidence.model_validate(value)
        )
    except (ValidationError, ValueError, TypeError):
        raise RuntimeError("qualified provider evidence validation failed") from None

    fixture = fixture_contract()
    expected_actual_cost = (
        Decimal(evidence.observation.input_tokens) * evidence.selection.input_cost_per_million_usd
        + Decimal(evidence.observation.output_tokens)
        * evidence.selection.output_cost_per_million_usd
    ) / _MILLION
    expected_reserved_cost = (
        Decimal(evidence.observation.reserved_input_tokens)
        * evidence.selection.input_cost_per_million_usd
        + Decimal(evidence.observation.reserved_output_tokens)
        * evidence.selection.output_cost_per_million_usd
    ) / _MILLION
    if (
        evidence.source_revision != expected_source_revision
        or evidence.runtime.worker_image_digest != expected_worker_image_digest
        or evidence.selection.provider != expected_provider
        or evidence.selection.model != expected_model
        or evidence.selection.input_cost_per_million_usd != expected_input_cost_per_million_usd
        or evidence.selection.output_cost_per_million_usd != expected_output_cost_per_million_usd
        or evidence.selection.model.partition("/")[0] != evidence.selection.provider
        or evidence.fixture.contract_sha256 != _canonical_sha256(fixture)
        or evidence.fixture.prompt_template_sha256 != fixture["prompt_template_sha256"]
        or evidence.fixture.target_sha256 != fixture["target_sha256"]
        or evidence.bounds.max_cost_usd != MAX_COST_USD
        or evidence.observation.actual_cost_usd != expected_actual_cost
        or evidence.observation.reserved_cost_usd != expected_reserved_cost
        or evidence.observation.input_tokens > evidence.observation.reserved_input_tokens
        or evidence.observation.output_tokens > evidence.observation.reserved_output_tokens
        or evidence.observation.reserved_output_tokens
        != evidence.observation.requests * MAX_TOKENS_PER_REQUEST
    ):
        raise RuntimeError("qualified provider evidence contract failed")
    _validate_config(
        QualificationConfig(
            source_revision=expected_source_revision,
            worker_image_digest=expected_worker_image_digest,
            provider=expected_provider,
            model=expected_model,
            input_cost_per_million_usd=expected_input_cost_per_million_usd,
            output_cost_per_million_usd=expected_output_cost_per_million_usd,
        )
    )
    return evidence


def _prepare_new_output(output: Path) -> None:
    if output.suffix != ".json" or output.exists() or output.is_symlink():
        raise RuntimeError("qualified provider output path is invalid")
    parent = output.parent
    if not parent.is_dir() or parent.is_symlink():
        raise RuntimeError("qualified provider output directory is invalid")


def _write_evidence(output: Path, evidence: QualifiedProviderEvidence) -> None:
    payload = (
        json.dumps(
            evidence.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise RuntimeError("qualified provider evidence exceeded its size limit")
    # Validate exactly the serialized representation before making it visible.
    validate_qualified_provider_evidence(
        payload,
        expected_source_revision=evidence.source_revision,
        expected_worker_image_digest=evidence.runtime.worker_image_digest,
        expected_provider=evidence.selection.provider,
        expected_model=evidence.selection.model,
        expected_input_cost_per_million_usd=evidence.selection.input_cost_per_million_usd,
        expected_output_cost_per_million_usd=evidence.selection.output_cost_per_million_usd,
    )
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-link publication is atomic and, unlike replace(), fails if a
            # competing writer creates the destination after the preflight.
            os.link(temporary, output)
        except FileExistsError:
            raise RuntimeError("qualified provider output path became unavailable") from None
        output.chmod(0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def read_and_validate_evidence(
    path: Path,
    *,
    expected_source_revision: str,
    expected_worker_image_digest: str,
    expected_provider: str,
    expected_model: str,
    expected_input_cost_per_million_usd: Decimal,
    expected_output_cost_per_million_usd: Decimal,
) -> QualifiedProviderEvidence:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise RuntimeError("qualified provider evidence file is invalid")
    return validate_qualified_provider_evidence(
        path.read_bytes(),
        expected_source_revision=expected_source_revision,
        expected_worker_image_digest=expected_worker_image_digest,
        expected_provider=expected_provider,
        expected_model=expected_model,
        expected_input_cost_per_million_usd=expected_input_cost_per_million_usd,
        expected_output_cost_per_million_usd=expected_output_cost_per_million_usd,
    )


def _config_from_args(arguments: argparse.Namespace) -> QualificationConfig:
    try:
        input_price = Decimal(arguments.input_cost_per_million_usd)
        output_price = Decimal(arguments.output_cost_per_million_usd)
    except Exception:
        raise RuntimeError("qualified provider pricing is invalid") from None
    return QualificationConfig(
        source_revision=arguments.candidate_sha,
        worker_image_digest=arguments.worker_image_digest,
        provider=arguments.provider,
        model=arguments.model,
        input_cost_per_million_usd=input_price,
        output_cost_per_million_usd=output_price,
    )


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--worker-image-digest", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="perform one bounded live-provider qualification")
    _add_identity_arguments(run)
    run.add_argument("--input-cost-per-million-usd", required=True)
    run.add_argument("--output-cost-per-million-usd", required=True)
    run.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify one retained JSON artifact offline")
    _add_identity_arguments(verify)
    verify.add_argument("--input-cost-per-million-usd", required=True)
    verify.add_argument("--output-cost-per-million-usd", required=True)
    verify.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "run":
            config = _config_from_args(arguments)
            run_qualification(config, arguments.output)
        else:
            verify_config = _config_from_args(arguments)
            read_and_validate_evidence(
                arguments.input,
                expected_source_revision=verify_config.source_revision,
                expected_worker_image_digest=verify_config.worker_image_digest,
                expected_provider=verify_config.provider,
                expected_model=verify_config.model,
                expected_input_cost_per_million_usd=(verify_config.input_cost_per_million_usd),
                expected_output_cost_per_million_usd=(verify_config.output_cost_per_million_usd),
            )
    except Exception:
        # Never stringify an external exception: provider SDKs may embed request data.
        sys.stderr.write("qualified provider acceptance failed\n")
        return 2
    sys.stdout.write("qualified provider acceptance passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
