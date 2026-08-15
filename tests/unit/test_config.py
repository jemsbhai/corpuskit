"""Configuration safety tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from corpuskit.config import RuntimeRole, Settings

ROOT = Path(__file__).parents[2]


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "auth_mode": "oidc",
        "oidc_issuer": "https://id.example.test",
        "oidc_audience": "corpuskit",
        "job_backend": "temporal",
        "temporal_tls": True,
        "api_docs_enabled": False,
        "database_url": "postgresql+asyncpg://corpuskit:secret@db.example.test/corpuskit",
        "allowed_origins": ["https://app.example.test"],
        "artifact_backend": "s3",
        "artifact_s3_endpoint": "https://objects.example.test",
        "artifact_s3_sse": "AES256",
        "metrics_bearer_token": "m" * 32,
        "api_rate_limit_enabled": True,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_development_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.max_sentences_per_import == 10_000
    assert settings.max_sentence_characters == 2_000
    assert settings.artifact_max_bytes == 10 * 1024 * 1024
    assert settings.artifact_retention_days == 30

    with pytest.raises(ValidationError, match="Instance is frozen"):
        settings.log_level = "DEBUG"


def test_committed_development_env_example_is_loadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in tuple(os.environ):
        if variable.startswith(("CORPUSKIT_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(variable)

    settings = Settings(_env_file=ROOT / ".env.example")

    assert settings.environment == "development"
    assert settings.database_url == "sqlite+aiosqlite:///./data/corpuskit.db"
    assert settings.job_backend == "inline"


def test_database_credentials_are_excluded_from_settings_repr() -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+asyncpg://user:canary-secret@db.test/corpuskit",
        _env_file=None,
    )

    assert "canary-secret" not in repr(settings)


def test_hosted_provider_policy_parses_bounded_server_pacing_from_json_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_reference = "secret://env/CORPUSKIT_PROVIDER_TEST_KEY"
    monkeypatch.setenv(
        "CORPUSKIT_WORKER_HOSTED_MODEL_POLICIES",
        json.dumps(
            [
                {
                    "provider": "openai",
                    "model": "openai/demo-model",
                    "connection_id": "demo-provider",
                    "credential_ref": {"reference": secret_reference},
                    "input_cost_per_million_usd": 1,
                    "output_cost_per_million_usd": 2,
                    "max_output_tokens_per_request": 128,
                    "request_delay_seconds": 0.25,
                }
            ]
        ),
    )

    settings = Settings(environment="test", _env_file=None)

    assert settings.worker_hosted_model_policies[0].request_delay_seconds == 0.25
    assert secret_reference not in repr(settings)


def test_adoption_database_credentials_are_strict_and_excluded_from_repr() -> None:
    settings = Settings(
        environment="test",
        adoption_database_url=(
            "postgresql+asyncpg://adoption-user:adoption-canary@db.test/corpuskit"
        ),
        _env_file=None,
    )

    assert "adoption-canary" not in repr(settings)
    assert settings.adoption_database_url is not None
    assert settings.adoption_database_url.get_secret_value().startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///adoption.db",
        "postgresql+asyncpg://adoption-user@db.test/corpuskit",
        "postgresql+asyncpg://:secret@db.test/corpuskit",
        "postgresql+asyncpg://adoption-user:secret@db.test/",
        "postgresql+asyncpg://adoption-user:secret@db.test/corpuskit#fragment",
        "postgresql+asyncpg://adoption-user:secret@db.test:invalid/corpuskit",
    ],
)
def test_adoption_database_rejects_uncredentialed_or_unsafe_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="credential-bearing PostgreSQL"):
        Settings(environment="test", adoption_database_url=url, _env_file=None)


def test_deployed_adoption_database_credentials_must_be_distinct() -> None:
    shared = "postgresql+asyncpg://corpuskit:secret@db.example.test/corpuskit"
    with pytest.raises(ValidationError, match="distinct service credentials"):
        _production_settings(database_url=shared, adoption_database_url=shared)
    with pytest.raises(ValidationError, match="distinct service credentials"):
        _production_settings(
            database_url=f"{shared}?application_name=worker",
            adoption_database_url=f"{shared}?application_name=adoption",
        )


def test_object_store_credentials_are_excluded_from_settings_repr() -> None:
    settings = Settings(
        environment="test",
        artifact_backend="s3",
        artifact_s3_endpoint="http://127.0.0.1:9000",
        artifact_s3_access_key_id="access-canary",
        artifact_s3_secret_access_key="secret-canary",
        artifact_s3_session_token="token-canary",
        _env_file=None,
    )

    displayed = repr(settings)
    assert "access-canary" not in displayed
    assert "secret-canary" not in displayed
    assert "token-canary" not in displayed


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auth_mode": "demo", "job_backend": "temporal", "api_docs_enabled": False}, "oidc"),
        (
            {
                "auth_mode": "oidc",
                "oidc_issuer": None,
                "oidc_audience": None,
                "job_backend": "temporal",
                "api_docs_enabled": False,
            },
            "issuer and audience",
        ),
        (
            {
                "auth_mode": "oidc",
                "oidc_issuer": "https://id.example.test",
                "oidc_audience": "corpuskit",
                "job_backend": "inline",
                "api_docs_enabled": False,
            },
            "temporal",
        ),
        (
            {
                "auth_mode": "oidc",
                "oidc_issuer": "https://id.example.test",
                "oidc_audience": "corpuskit",
                "job_backend": "temporal",
                "api_docs_enabled": True,
            },
            "docs",
        ),
    ],
)
def test_production_rejects_insecure_combinations(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _production_settings(**overrides)


def test_production_accepts_hardened_configuration() -> None:
    settings = _production_settings()

    assert settings.environment == "production"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"metrics_enabled": False}, "protected Prometheus metrics"),
        ({"metrics_bearer_token": None}, "protected Prometheus metrics"),
        ({"metrics_bearer_token": "too-short"}, "32-512 visible ASCII"),
        ({"metrics_bearer_token": "m" * 31 + "\n"}, "32-512 visible ASCII"),
        ({"metrics_bearer_token": "é" * 32}, "32-512 visible ASCII"),
        ({"metrics_bearer_token": "m" * 513}, "32-512 visible ASCII"),
    ],
)
def test_deployed_metrics_require_a_strong_opaque_token(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _production_settings(**overrides)


def test_metrics_token_is_excluded_from_settings_repr() -> None:
    settings = Settings(
        environment="test",
        metrics_bearer_token="metrics-canary-secret-that-is-long-enough",
        _env_file=None,
    )

    assert "metrics-canary" not in repr(settings)


def test_invalid_metrics_token_is_rejected_in_local_environments_too() -> None:
    with pytest.raises(ValidationError, match="32-512 visible ASCII"):
        Settings(environment="test", metrics_bearer_token="short", _env_file=None)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database_url": "sqlite+aiosqlite:///production.db"}, "PostgreSQL"),
        ({"temporal_tls": False}, "Temporal connections require TLS"),
        ({"artifact_backend": "filesystem"}, "S3-compatible artifact storage"),
        ({"artifact_s3_sse": None}, "SSE-S3 or SSE-KMS"),
        ({"artifact_s3_endpoint": "http://objects.example.test"}, "fixed HTTP"),
        ({"artifact_s3_endpoint": "https://user@objects.example.test"}, "fixed HTTP"),
        ({"artifact_s3_endpoint": "https://objects.example.test/path"}, "fixed HTTP"),
        ({"artifact_s3_endpoint": "https://objects.example.test?bucket=other"}, "fixed HTTP"),
        ({"allowed_origins": []}, "at least one exact HTTPS origin"),
        ({"allowed_origins": ["*"]}, "exact HTTPS origins"),
        ({"allowed_origins": ["http://app.example.test"]}, "exact HTTPS origins"),
        ({"allowed_origins": ["https://user@app.example.test"]}, "exact HTTPS origins"),
        ({"allowed_origins": ["https://app example.test"]}, "exact HTTPS origins"),
        ({"allowed_origins": ["https://app.example.test:invalid"]}, "exact HTTPS origins"),
        ({"allowed_origins": ["https://app.example.test/path"]}, "exact HTTPS origins"),
        ({"allowed_origins": ["https://app.example.test?query=1"]}, "exact HTTPS origins"),
        (
            {"allowed_origins": ["https://app.example.test", "https://app.example.test"]},
            "must be unique",
        ),
    ],
)
def test_production_rejects_unsafe_platform_configuration(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _production_settings(**overrides)


def test_temporal_api_key_requires_tls_in_every_environment() -> None:
    with pytest.raises(ValidationError, match="API-key authentication requires TLS"):
        Settings(environment="test", temporal_api_key="secret", _env_file=None)


def test_core_capability_cannot_be_removed_from_readiness() -> None:
    with pytest.raises(ValidationError, match="include corpusgen-core"):
        Settings(environment="test", required_capabilities={"phoible"}, _env_file=None)


def test_upload_budget_has_a_defensible_hard_ceiling() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 104857600"):
        Settings(environment="test", max_upload_bytes=100 * 1024 * 1024 + 1, _env_file=None)


@pytest.mark.parametrize("bucket", ["UPPERCASE", "127.0.0.1", "bad..bucket", "-bad"])
def test_artifact_bucket_rejects_nonportable_names(bucket: str) -> None:
    with pytest.raises(ValidationError, match="bucket"):
        Settings(environment="test", artifact_s3_bucket=bucket, _env_file=None)


def test_s3_credentials_and_kms_policy_are_coherent() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        Settings(environment="test", artifact_s3_access_key_id="only-one", _env_file=None)
    with pytest.raises(ValidationError, match="session token requires"):
        Settings(environment="test", artifact_s3_session_token="orphan", _env_file=None)
    with pytest.raises(ValidationError, match="requires a configured KMS"):
        Settings(environment="test", artifact_s3_sse="aws:kms", _env_file=None)
    with pytest.raises(ValidationError, match="only valid"):
        Settings(environment="test", artifact_s3_kms_key_id="key", _env_file=None)


def test_local_s3_endpoint_may_use_explicit_path_style_http() -> None:
    settings = Settings(
        environment="test",
        artifact_backend="s3",
        artifact_s3_endpoint="http://127.0.0.1:9000",
        artifact_s3_path_style=True,
        _env_file=None,
    )

    assert settings.artifact_s3_path_style is True


def test_staging_rejects_demo_authentication() -> None:
    with pytest.raises(ValidationError, match="development and test"):
        Settings(environment="staging", auth_mode="demo", _env_file=None)


@pytest.mark.parametrize(
    "host",
    ["localhost", "0.0.0.0:8000", "127.000.000.001", "[::1]", "", "example.test"],
)
def test_api_bind_host_rejects_ambiguous_or_non_ip_values(host: str) -> None:
    with pytest.raises(ValidationError, match="canonical IPv4 or IPv6"):
        Settings(environment="test", api_bind_host=host, _env_file=None)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "::1", "0.0.0.0", "::"],  # noqa: S104 - explicit opt-in fixtures
)
def test_api_bind_host_accepts_canonical_loopback_and_explicit_container_values(host: str) -> None:
    settings = Settings(environment="test", api_bind_host=host, _env_file=None)

    assert settings.api_bind_host == host


def test_staging_uses_the_deployed_security_posture_but_may_expose_docs() -> None:
    settings = _production_settings(environment="staging", api_docs_enabled=True)

    assert settings.environment == "staging"
    assert settings.api_docs_enabled is True


def _deployed_role_settings(role: RuntimeRole, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "runtime_role": role,
        "database_url": "postgresql+asyncpg://role-user:secret@db.example.test/corpuskit",
        "_env_file": None,
    }
    if role in {RuntimeRole.API, RuntimeRole.DISPATCHER, RuntimeRole.WORKER}:
        values.update(job_backend="temporal", temporal_tls=True)
    if role in {RuntimeRole.API, RuntimeRole.WORKER, RuntimeRole.MAINTENANCE}:
        values.update(
            artifact_backend="s3",
            artifact_s3_endpoint="https://objects.example.test",
            artifact_s3_sse="AES256",
        )
    if role is RuntimeRole.API:
        values.update(
            auth_mode="oidc",
            oidc_issuer="https://id.example.test",
            oidc_audience="corpuskit",
            api_docs_enabled=False,
            metrics_bearer_token="m" * 32,
            allowed_origins=["https://app.example.test"],
            api_rate_limit_enabled=True,
        )
    if role is RuntimeRole.WORKER:
        values["adoption_database_url"] = (
            "postgresql+asyncpg://adoption-user:other-secret@db.example.test/corpuskit"
        )
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.mark.parametrize("role", list(RuntimeRole))
def test_each_deployed_runtime_role_accepts_only_its_required_posture(
    role: RuntimeRole,
) -> None:
    settings = _deployed_role_settings(role)

    assert settings.runtime_role is role


@pytest.mark.parametrize("role", list(RuntimeRole))
def test_every_deployed_runtime_role_requires_credentialed_postgres(
    role: RuntimeRole,
) -> None:
    with pytest.raises(ValidationError, match="credential-bearing PostgreSQL"):
        _deployed_role_settings(role, database_url="sqlite+aiosqlite:///unsafe.db")
    with pytest.raises(ValidationError, match="credential-bearing PostgreSQL"):
        _deployed_role_settings(
            role,
            database_url="postgresql+asyncpg://db.example.test/corpuskit",
        )


@pytest.mark.parametrize("role", [RuntimeRole.API, RuntimeRole.DISPATCHER, RuntimeRole.WORKER])
def test_temporal_roles_cannot_disable_tls_or_durable_jobs(role: RuntimeRole) -> None:
    with pytest.raises(ValidationError, match="Temporal connections require TLS"):
        _deployed_role_settings(role, temporal_tls=False)
    with pytest.raises(ValidationError, match="JOB_BACKEND=temporal"):
        _deployed_role_settings(role, job_backend="inline")


@pytest.mark.parametrize("role", [RuntimeRole.API, RuntimeRole.WORKER, RuntimeRole.MAINTENANCE])
def test_artifact_roles_cannot_disable_encrypted_object_storage(role: RuntimeRole) -> None:
    with pytest.raises(ValidationError, match="S3-compatible artifact storage"):
        _deployed_role_settings(role, artifact_backend="filesystem")
    with pytest.raises(ValidationError, match="SSE-S3 or SSE-KMS"):
        _deployed_role_settings(role, artifact_s3_sse=None)


def test_worker_requires_distinct_adoption_credentials() -> None:
    with pytest.raises(ValidationError, match="distinct adoption database URL"):
        _deployed_role_settings(RuntimeRole.WORKER, adoption_database_url=None)
    shared = "postgresql+asyncpg://role-user:secret@db.example.test/corpuskit"
    with pytest.raises(ValidationError, match="distinct service credentials"):
        _deployed_role_settings(
            RuntimeRole.WORKER,
            database_url=shared,
            adoption_database_url=shared,
        )


def test_non_api_roles_do_not_require_or_receive_api_security_secrets() -> None:
    for role in (RuntimeRole.DISPATCHER, RuntimeRole.WORKER, RuntimeRole.MAINTENANCE):
        settings = _deployed_role_settings(role)
        assert settings.metrics_bearer_token is None
        assert settings.oidc_issuer is None
        assert settings.oidc_audience is None


def test_missing_or_unknown_runtime_role_fails_closed_for_non_api_postures() -> None:
    with pytest.raises(ValidationError, match="protected Prometheus metrics"):
        Settings(
            environment="production",
            auth_mode="oidc",
            oidc_issuer="https://id.example.test",
            oidc_audience="corpuskit",
            api_docs_enabled=False,
            database_url="postgresql+asyncpg://dispatcher:secret@db.example.test/corpuskit",
            job_backend="temporal",
            temporal_tls=True,
            artifact_backend="s3",
            artifact_s3_endpoint="https://objects.example.test",
            artifact_s3_sse="AES256",
            api_rate_limit_enabled=True,
            _env_file=None,
        )
    with pytest.raises(ValidationError, match="runtime_role"):
        Settings(environment="test", runtime_role="unknown", _env_file=None)
