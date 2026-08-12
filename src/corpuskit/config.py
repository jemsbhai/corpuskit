"""Validated application configuration."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from corpuskit.domain.datg import DatgRuntimePolicyEntry
from corpuskit.domain.generation import HuggingFaceRepositorySpec
from corpuskit.domain.model_runtime import HostedModelPolicy, LocalModelPolicy
from corpuskit.domain.phon_rl import PhonRlRuntimePolicyEntry


class RuntimeRole(StrEnum):
    """Exact process role used to scope production-only configuration and secrets."""

    API = "api"
    DISPATCHER = "dispatcher"
    WORKER = "worker"
    MAINTENANCE = "maintenance"


class Settings(BaseSettings):
    """CorpusKit settings loaded from ``CORPUSKIT_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="CORPUSKIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    runtime_role: RuntimeRole = RuntimeRole.API
    auth_mode: Literal["demo", "oidc"] = "demo"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    metrics_enabled: bool = True
    metrics_bearer_token: SecretStr | None = Field(default=None, repr=False)
    database_url: str = Field(default="sqlite+aiosqlite:///./data/corpuskit.db", repr=False)
    adoption_database_url: SecretStr | None = Field(default=None, repr=False)
    artifact_root: Path = Path("artifacts")
    artifact_backend: Literal["filesystem", "s3"] = "filesystem"
    artifact_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    artifact_retention_days: int = Field(default=30, ge=30, le=3_650)
    artifact_orphan_grace_seconds: int = Field(default=3_600, ge=60, le=86_400)
    artifact_download_chunk_bytes: int = Field(default=256 * 1024, ge=16 * 1024, le=8 * 1024 * 1024)
    artifact_presign_seconds: int = Field(default=300, ge=30, le=900)
    artifact_s3_endpoint: str | None = None
    artifact_s3_bucket: str = Field(default="corpuskit-artifacts", min_length=3, max_length=63)
    artifact_s3_region: str = Field(default="us-east-1", min_length=1, max_length=64)
    artifact_s3_access_key_id: SecretStr | None = None
    artifact_s3_secret_access_key: SecretStr | None = None
    artifact_s3_session_token: SecretStr | None = None
    artifact_s3_path_style: bool = False
    artifact_s3_sse: Literal["AES256", "aws:kms"] | None = None
    artifact_s3_kms_key_id: SecretStr | None = None
    artifact_s3_connect_timeout_seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    artifact_s3_read_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    artifact_s3_max_attempts: int = Field(default=3, ge=1, le=5)
    allowed_origins: tuple[str, ...] = Field(default_factory=lambda: ("http://localhost:3000",))
    job_backend: Literal["inline", "temporal"] = "inline"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = Field(default="batch-cpu", min_length=1, max_length=128)
    temporal_tls: bool = False
    temporal_api_key: SecretStr | None = None
    temporal_connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    temporal_activity_heartbeat_seconds: float = Field(default=5.0, ge=0.5, le=10.0)
    temporal_max_concurrent_activities: int = Field(default=8, ge=1, le=128)
    worker_profile: Literal[
        "interactive-cpu",
        "batch-cpu",
        "external-provider",
        "gpu-inference",
        "gpu-training",
    ] = "batch-cpu"
    worker_graceful_shutdown_seconds: int = Field(default=30, ge=1, le=300)
    worker_activity_deadline_cap_seconds: float = Field(
        default=300.0,
        gt=0.0,
        le=86_400.0,
    )
    worker_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    worker_hosted_model_policies: tuple[HostedModelPolicy, ...] = Field(
        default=(),
        max_length=64,
        repr=False,
    )
    worker_huggingface_repository_policies: tuple[HuggingFaceRepositorySpec, ...] = Field(
        default=(),
        max_length=64,
        repr=False,
    )
    worker_local_model_policies: tuple[LocalModelPolicy, ...] = Field(
        default=(),
        max_length=64,
        repr=False,
    )
    worker_model_cache_root: Path | None = None
    worker_model_cache_mount_read_only: bool = False
    worker_datg_runtime_policies: tuple[DatgRuntimePolicyEntry, ...] = Field(
        default=(),
        max_length=64,
        repr=False,
    )
    worker_datg_model_cache_root: Path | None = None
    worker_datg_index_cache_root: Path | None = None
    worker_datg_index_publish_root: Path | None = None
    worker_datg_cache_mount_read_only: bool = False
    worker_phon_rl_runtime_policies: tuple[PhonRlRuntimePolicyEntry, ...] = Field(
        default=(),
        max_length=64,
        repr=False,
    )
    worker_phon_rl_cache_roots: dict[str, Path] = Field(
        default_factory=dict,
        max_length=16,
        repr=False,
    )
    dispatcher_id: str | None = Field(default=None, min_length=1, max_length=80)
    dispatcher_batch_size: int = Field(default=20, ge=1, le=200)
    dispatcher_poll_seconds: float = Field(default=1.0, ge=0.05, le=30.0)
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_algorithms: tuple[Literal["RS256"], ...] = Field(
        default=("RS256",), min_length=1, max_length=1
    )
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=3600)
    oidc_refresh_cooldown_seconds: int = Field(default=10, ge=1, le=300)
    oidc_http_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30.0)
    oidc_clock_skew_seconds: int = Field(default=0, ge=0, le=60)
    oidc_organization_claim: str = Field(default="org_id", min_length=1, max_length=128)
    oidc_role_claim: str = Field(default="role", min_length=1, max_length=128)
    required_capabilities: frozenset[str] = Field(
        default_factory=lambda: frozenset({"corpusgen-core"})
    )
    capability_cache_seconds: int = Field(default=60, ge=0, le=3600)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_sentences_per_import: int = Field(default=10_000, ge=1, le=250_000)
    max_sentence_characters: int = Field(default=2_000, ge=1, le=100_000)
    api_rate_limit_enabled: bool = False
    api_rate_limit_window_seconds: int = Field(default=60, ge=10, le=3_600)
    api_rate_limit_read_requests: int = Field(default=600, ge=10, le=100_000)
    api_rate_limit_write_requests: int = Field(default=120, ge=1, le=10_000)
    api_rate_limit_retention_windows: int = Field(default=3, ge=2, le=100)
    api_bind_host: str = "127.0.0.1"
    api_docs_enabled: bool = True

    @model_validator(mode="after")
    def validate_security_posture(self) -> Settings:
        """Reject insecure production combinations at startup."""

        if self.temporal_task_queue != self.worker_profile:
            raise ValueError("Temporal task queue must match the server-controlled worker profile")
        if "corpusgen-core" not in self.required_capabilities:
            raise ValueError("Required capabilities must include corpusgen-core")
        repository_policy_ids = tuple(
            (
                item.dataset,
                item.config,
                item.split,
                item.text_column,
                item.revision,
                item.language,
            )
            for item in self.worker_huggingface_repository_policies
        )
        if len(repository_policy_ids) != len(set(repository_policy_ids)):
            raise ValueError("Hugging Face repository policies must have unique exact selectors")
        if not _is_canonical_ip_literal(self.api_bind_host):
            raise ValueError("API bind host must be a canonical IPv4 or IPv6 address")
        if self.temporal_api_key is not None and not self.temporal_tls:
            raise ValueError("Temporal API-key authentication requires TLS")
        if self.adoption_database_url is not None:
            adoption_url = self.adoption_database_url.get_secret_value()
            if not _is_credentialed_postgres_url(adoption_url):
                raise ValueError(
                    "Adoption database URL must be a credential-bearing PostgreSQL asyncpg URL"
                )
            if self.environment in {"staging", "production"} and _database_credentials(
                adoption_url
            ) == _database_credentials(self.database_url):
                raise ValueError(
                    "Adoption and worker database URLs must use distinct service credentials"
                )
        if not _is_s3_bucket(self.artifact_s3_bucket):
            raise ValueError("Artifact S3 bucket must be a conservative DNS-compatible name")
        if not _is_s3_region(self.artifact_s3_region):
            raise ValueError("Artifact S3 region contains unsupported characters")
        access_key_configured = self.artifact_s3_access_key_id is not None
        secret_key_configured = self.artifact_s3_secret_access_key is not None
        if access_key_configured != secret_key_configured:
            raise ValueError("Artifact S3 access and secret keys must be configured together")
        if self.artifact_s3_session_token is not None and not access_key_configured:
            raise ValueError("Artifact S3 session token requires access and secret keys")
        if self.artifact_s3_sse == "aws:kms" and self.artifact_s3_kms_key_id is None:
            raise ValueError("Artifact SSE-KMS requires a configured KMS key ID")
        if self.artifact_s3_sse != "aws:kms" and self.artifact_s3_kms_key_id is not None:
            raise ValueError("Artifact KMS key ID is only valid with aws:kms encryption")
        if self.artifact_backend == "s3" and (
            self.artifact_s3_endpoint is None
            or not _is_fixed_s3_endpoint(
                self.artifact_s3_endpoint,
                require_tls=self.environment in {"staging", "production"},
            )
        ):
            raise ValueError(
                "Artifact S3 requires a fixed HTTP(S) endpoint; TLS is mandatory outside "
                "local environments"
            )
        if (
            self.runtime_role is RuntimeRole.API
            and self.auth_mode == "demo"
            and self.environment not in {"development", "test"}
        ):
            raise ValueError(
                "Demo authentication is limited to development and test; production requires oidc"
            )
        if (
            self.runtime_role is RuntimeRole.API
            and self.environment == "production"
            and self.api_docs_enabled
        ):
            raise ValueError("Production API docs must be explicitly disabled")
        if self.metrics_bearer_token is not None and not _is_metrics_bearer_token(
            self.metrics_bearer_token.get_secret_value()
        ):
            raise ValueError("Metrics bearer token must be 32-512 visible ASCII characters")
        if self.environment in {"staging", "production"}:
            if not _is_credentialed_postgres_url(self.database_url):
                raise ValueError(
                    "Staging and production require a credential-bearing PostgreSQL asyncpg "
                    "database URL"
                )
            if self.runtime_role in {
                RuntimeRole.API,
                RuntimeRole.DISPATCHER,
                RuntimeRole.WORKER,
            }:
                if self.job_backend != "temporal":
                    raise ValueError(
                        "Staging and production API, dispatcher, and worker roles require "
                        "CORPUSKIT_JOB_BACKEND=temporal"
                    )
                if not self.temporal_tls:
                    raise ValueError(
                        "Staging and production API, dispatcher, and worker Temporal "
                        "connections require TLS"
                    )
            if self.runtime_role in {
                RuntimeRole.API,
                RuntimeRole.WORKER,
                RuntimeRole.MAINTENANCE,
            }:
                if self.artifact_backend != "s3":
                    raise ValueError(
                        "Staging and production API, worker, and maintenance roles require "
                        "S3-compatible artifact storage"
                    )
                if self.artifact_s3_sse is None:
                    raise ValueError(
                        "Staging and production artifact storage requires SSE-S3 or SSE-KMS"
                    )
            if self.runtime_role is RuntimeRole.WORKER and self.adoption_database_url is None:
                raise ValueError(
                    "Staging and production worker roles require a distinct adoption database URL"
                )
            if self.runtime_role is RuntimeRole.API:
                if not self.api_rate_limit_enabled:
                    raise ValueError(
                        "Staging and production API roles require centralized request rate limiting"
                    )
                if not self.metrics_enabled or self.metrics_bearer_token is None:
                    raise ValueError("Staging and production require protected Prometheus metrics")
                if self.auth_mode != "oidc":
                    raise ValueError("Staging and production require CORPUSKIT_AUTH_MODE=oidc")
                if not self.oidc_issuer or not self.oidc_audience:
                    raise ValueError("Staging and production OIDC require issuer and audience")
                if not self.allowed_origins:
                    raise ValueError(
                        "Staging and production require at least one exact HTTPS origin"
                    )
                if any(not _is_exact_https_origin(origin) for origin in self.allowed_origins):
                    raise ValueError(
                        "Staging and production CORS origins must be exact HTTPS origins without "
                        "credentials, paths, queries, fragments, or wildcards"
                    )
                if len(set(self.allowed_origins)) != len(self.allowed_origins):
                    raise ValueError("Staging and production CORS origins must be unique")
        return self


def _is_exact_https_origin(value: str) -> bool:
    """Return whether a value is a credential-free serialized HTTPS origin."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or not _is_valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "*" in value
    ):
        return False
    expected_netloc = parsed.hostname
    if ":" in expected_netloc and not expected_netloc.startswith("["):
        expected_netloc = f"[{expected_netloc}]"
    if port is not None:
        expected_netloc = f"{expected_netloc}:{port}"
    return value == f"https://{expected_netloc}"


def _is_valid_hostname(value: str) -> bool:
    """Accept canonical IP literals or conservative ASCII DNS names."""

    try:
        ip_address(value)
        return True
    except ValueError:
        pass
    if len(value) > 253:
        return False
    labels = value.split(".")
    return all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(
            character.isascii() and (character.isalnum() or character == "-") for character in label
        )
        for label in labels
    )


def _is_canonical_ip_literal(value: str) -> bool:
    """Accept only an unambiguous canonical address, never a hostname or host:port pair."""

    try:
        return str(ip_address(value)) == value
    except ValueError:
        return False


def _is_credentialed_postgres_url(value: str) -> bool:
    """Accept only explicit PostgreSQL asyncpg credentials without echoing them."""

    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "postgresql+asyncpg"
        and parsed.username
        and parsed.password
        and parsed.hostname
        and parsed.path not in {"", "/"}
        and parsed.fragment == ""
    )


def _database_credentials(value: str) -> tuple[str | None, str | None]:
    """Extract an in-memory equality key without rendering either credential."""

    parsed = urlsplit(value)
    return parsed.username, parsed.password


def _is_fixed_s3_endpoint(value: str, *, require_tls: bool) -> bool:
    """Validate a fixed credential-free S3 endpoint with no request-controlled components."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or (require_tls and parsed.scheme != "https")
        or parsed.hostname is None
        or not _is_valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in value
    ):
        return False
    expected_host = parsed.hostname
    if ":" in expected_host and not expected_host.startswith("["):
        expected_host = f"[{expected_host}]"
    if port is not None:
        expected_host = f"{expected_host}:{port}"
    return value.rstrip("/") == f"{parsed.scheme}://{expected_host}"


def _is_s3_bucket(value: str) -> bool:
    """Accept portable S3-compatible bucket names and reject IP-like names."""

    if value != value.lower() or ".." in value or ".-" in value or "-." in value:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value, flags=re.ASCII):
        return False
    try:
        ip_address(value)
    except ValueError:
        return True
    return False


def _is_s3_region(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", value, flags=re.ASCII) is not None


def _is_metrics_bearer_token(value: str) -> bool:
    """Accept bounded opaque bearer material without whitespace or control bytes."""

    return 32 <= len(value) <= 512 and all(33 <= ord(character) <= 126 for character in value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide validated settings instance."""

    return Settings()
