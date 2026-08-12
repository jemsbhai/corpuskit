"""Development principal and production OIDC JWT verification."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from jwt.api_jwk import PyJWK

from corpuskit.auth.models import DEMO_PRINCIPAL, AuthRole, Principal
from corpuskit.config import Settings

MAX_TOKEN_BYTES = 16_384
MAX_OIDC_DOCUMENT_BYTES = 1_048_576
MAX_JWKS_KEYS = 100


class AuthBoundaryError(Exception):
    """Base class for stable authentication and authorization failures."""

    code = "authentication_error"
    public_message = "Authentication failed."
    status_code = 401


class AuthenticationError(AuthBoundaryError):
    """Stable, non-sensitive invalid-credential result."""

    code = "invalid_authentication"
    public_message = "Authentication credentials are missing or invalid."
    status_code = 401


class AuthorizationError(AuthBoundaryError):
    """Stable, non-sensitive insufficient-role result."""

    code = "forbidden"
    public_message = "The authenticated identity is not permitted to perform this operation."
    status_code = 403


class AuthenticationUnavailableError(AuthBoundaryError):
    """Stable result when fresh identity-provider keys cannot be obtained."""

    code = "authentication_unavailable"
    public_message = "Authentication is temporarily unavailable."
    status_code = 503


class Authenticator(Protocol):
    """Narrow authentication interface consumed by the FastAPI layer."""

    async def authenticate(self, token: str | None) -> Principal: ...


class OidcDocumentFetcher(Protocol):
    """Fetch trusted OIDC JSON documents without exposing transport details."""

    async def fetch_json(self, url: str) -> Mapping[str, Any]: ...


class HttpOidcDocumentFetcher:
    """Bounded HTTP fetcher for discovery and JWKS documents."""

    def __init__(self, *, timeout_seconds: float) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("OIDC HTTP timeout must be finite and between 0 and 30 seconds")
        self._timeout = httpx.Timeout(timeout_seconds)

    async def fetch_json(self, url: str) -> Mapping[str, Any]:
        try:
            trusted_url = _validate_https_url(url, label="document URL")
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "GET",
                    trusted_url,
                    headers={"Accept": "application/json"},
                ) as response,
            ):
                response.raise_for_status()
                declared_size = response.headers.get("content-length")
                if declared_size is not None and int(declared_size) > MAX_OIDC_DOCUMENT_BYTES:
                    raise AuthenticationUnavailableError
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_OIDC_DOCUMENT_BYTES:
                        raise AuthenticationUnavailableError
                    chunks.append(chunk)
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthenticationUnavailableError from exc
        try:
            document = json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthenticationUnavailableError from exc
        if not isinstance(document, dict):
            raise AuthenticationUnavailableError
        return document


class DemoAuthenticator:
    """Return one deterministic principal in development and tests only."""

    async def authenticate(self, token: str | None) -> Principal:
        del token
        return DEMO_PRINCIPAL


class OidcJwtVerifier:
    """Verify bearer JWTs against bounded, rotation-aware OIDC key caches."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithms: tuple[str, ...] = ("RS256",),
        organization_claim: str = "org_id",
        role_claim: str = "role",
        cache_seconds: int = 300,
        refresh_cooldown_seconds: int = 10,
        clock_skew_seconds: int = 0,
        fetcher: OidcDocumentFetcher,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = _validate_https_url(issuer, label="issuer")
        if not audience or len(audience) > 512:
            raise ValueError("OIDC audience must be non-empty and bounded")
        if not algorithms or any(item != "RS256" for item in algorithms):
            raise ValueError("Only RS256 is supported")
        if cache_seconds <= 0:
            raise ValueError("OIDC key-cache duration must be positive")
        if refresh_cooldown_seconds <= 0:
            raise ValueError("OIDC refresh cooldown must be positive")
        if clock_skew_seconds < 0:
            raise ValueError("OIDC clock skew must not be negative")
        self._audience = audience
        self._algorithms = algorithms
        self._organization_claim = organization_claim
        self._role_claim = role_claim
        self._cache_seconds = cache_seconds
        self._refresh_cooldown_seconds = refresh_cooldown_seconds
        self._clock_skew_seconds = clock_skew_seconds
        self._fetcher = fetcher
        self._monotonic = monotonic
        self._keys: dict[str, PyJWK] = {}
        self._refreshed_at = float("-inf")
        self._last_refresh_attempt = float("-inf")
        self._last_refresh_failed = False
        self._refresh_lock = asyncio.Lock()

    async def authenticate(self, token: str | None) -> Principal:
        if token is None or not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise AuthenticationError
        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, OverflowError, TypeError, ValueError) as exc:
            raise AuthenticationError from exc
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in self._algorithms or not isinstance(key_id, str) or not key_id:
            raise AuthenticationError
        if len(key_id) > 128:
            raise AuthenticationError

        key = await self._get_key(key_id)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._clock_skew_seconds,
                options={
                    "require": ["aud", "exp", "iss", "sub"],
                    "verify_aud": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_nbf": True,
                    "verify_signature": True,
                },
            )
        except (jwt.PyJWTError, OverflowError, TypeError, ValueError) as exc:
            raise AuthenticationError from exc
        return self._principal_from_claims(claims)

    async def _get_key(self, key_id: str) -> PyJWK:
        now = self._monotonic()
        cached = self._keys.get(key_id)
        if cached is not None and now - self._refreshed_at <= self._cache_seconds:
            return cached
        async with self._refresh_lock:
            now = self._monotonic()
            cached = self._keys.get(key_id)
            if cached is not None and now - self._refreshed_at <= self._cache_seconds:
                return cached
            cache_is_fresh = now - self._refreshed_at <= self._cache_seconds
            cooldown_active = now - self._last_refresh_attempt < self._refresh_cooldown_seconds
            if cooldown_active and (self._last_refresh_failed or cache_is_fresh):
                if cached is not None or self._last_refresh_failed:
                    raise AuthenticationUnavailableError
                raise AuthenticationError
            self._last_refresh_attempt = now
            try:
                await self._refresh_keys(now)
            except AuthenticationUnavailableError:
                self._last_refresh_failed = True
                raise
            self._last_refresh_failed = False
            try:
                return self._keys[key_id]
            except KeyError as exc:
                raise AuthenticationError from exc

    async def _refresh_keys(self, now: float) -> None:
        discovery_url = f"{self._issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            metadata = await self._fetcher.fetch_json(discovery_url)
            if metadata.get("issuer") != self._issuer:
                raise AuthenticationUnavailableError
            jwks_uri_value = metadata.get("jwks_uri")
            if not isinstance(jwks_uri_value, str):
                raise AuthenticationUnavailableError
            jwks_uri = _validate_https_url(jwks_uri_value, label="JWKS URI")
            if _url_origin(jwks_uri) != _url_origin(self._issuer):
                raise AuthenticationUnavailableError
            document = await self._fetcher.fetch_json(jwks_uri)
            raw_keys = document.get("keys")
            if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > MAX_JWKS_KEYS:
                raise AuthenticationUnavailableError
            parsed: dict[str, PyJWK] = {}
            for raw_key in raw_keys:
                if not isinstance(raw_key, dict):
                    continue
                key_id = raw_key.get("kid")
                if not isinstance(key_id, str) or not key_id or len(key_id) > 128:
                    continue
                if raw_key.get("alg") not in {None, "RS256"} or key_id in parsed:
                    continue
                key_operations = raw_key.get("key_ops")
                if key_operations is not None and (
                    not isinstance(key_operations, list) or "verify" not in key_operations
                ):
                    continue
                key = PyJWK.from_dict(raw_key, algorithm="RS256")
                if key.key_type == "RSA" and key.public_key_use in {None, "sig"}:
                    parsed[key_id] = key
            if not parsed:
                raise AuthenticationUnavailableError
        except AuthenticationUnavailableError:
            raise
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise AuthenticationUnavailableError from exc
        self._keys = parsed
        self._refreshed_at = now

    def _principal_from_claims(self, claims: Mapping[str, Any]) -> Principal:
        subject = claims.get("sub")
        organization = claims.get(self._organization_claim)
        role = claims.get(self._role_claim)
        display_name = claims.get("name")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise AuthenticationError
        if not isinstance(organization, str) or not isinstance(role, str):
            raise AuthenticationError
        if display_name is not None and not isinstance(display_name, str):
            raise AuthenticationError
        for numeric_date_claim in ("exp", "nbf"):
            value = claims.get(numeric_date_claim)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise AuthenticationError
        try:
            return Principal(
                subject=subject,
                organization_id=UUID(organization),
                role=AuthRole(role),
                display_name=display_name,
            )
        except (TypeError, ValueError) as exc:
            raise AuthenticationError from exc


def _validate_https_url(value: str, *, label: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            f"OIDC {label} must be an HTTPS URL without credentials or fragments"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError(f"OIDC {label} must be an HTTPS URL without credentials or fragments")
    return value


def _url_origin(value: str) -> tuple[str, str, int]:
    """Return a canonical HTTPS origin for a URL already validated above."""

    parsed = urlsplit(value)
    assert parsed.hostname is not None
    return parsed.scheme, parsed.hostname.casefold(), parsed.port or 443


def build_authenticator(settings: Settings) -> Authenticator:
    """Build the configured authentication boundary after settings validation."""

    if settings.auth_mode == "demo":
        if settings.environment not in {"development", "test"}:
            raise ValueError("Demo authentication is limited to development and test")
        return DemoAuthenticator()
    if settings.oidc_issuer is None or settings.oidc_audience is None:
        raise ValueError("OIDC issuer and audience are required")
    return OidcJwtVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        algorithms=tuple(settings.oidc_algorithms),
        organization_claim=settings.oidc_organization_claim,
        role_claim=settings.oidc_role_claim,
        cache_seconds=settings.oidc_jwks_cache_seconds,
        refresh_cooldown_seconds=settings.oidc_refresh_cooldown_seconds,
        clock_skew_seconds=settings.oidc_clock_skew_seconds,
        fetcher=HttpOidcDocumentFetcher(timeout_seconds=settings.oidc_http_timeout_seconds),
    )


__all__ = [
    "AuthBoundaryError",
    "AuthenticationError",
    "AuthenticationUnavailableError",
    "Authenticator",
    "AuthorizationError",
    "DemoAuthenticator",
    "HttpOidcDocumentFetcher",
    "OidcDocumentFetcher",
    "OidcJwtVerifier",
    "build_authenticator",
]
