"""Adversarial authentication and tenant-context tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from corpuskit.auth import (
    AuthenticationError,
    AuthenticationUnavailableError,
    AuthRole,
    DemoAuthenticator,
    HttpOidcDocumentFetcher,
    OidcJwtVerifier,
    Principal,
    build_authenticator,
)
from corpuskit.auth.dependencies import require_principal, require_roles
from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings

ISSUER = "https://identity.example.test/realms/corpuskit"
AUDIENCE = "corpuskit-api"
ORGANIZATION_ID = "c9e9bc55-a985-4a5c-ae71-c987667e1c22"


class FakeFetcher:
    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.fail = False
        self.calls: list[str] = []
        self.metadata_issuer = ISSUER
        self.jwks_uri = f"{ISSUER}/protocol/openid-connect/certs"

    async def fetch_json(self, url: str) -> Mapping[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise AuthenticationUnavailableError
        if url.endswith("openid-configuration"):
            return {"issuer": self.metadata_issuer, "jwks_uri": self.jwks_uri}
        return {"keys": self.keys}


class BlockingFetcher(FakeFetcher):
    def __init__(self, keys: list[dict[str, Any]]) -> None:
        super().__init__(keys)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_json(self, url: str) -> Mapping[str, Any]:
        if url.endswith("openid-configuration") and not self.started.is_set():
            self.started.set()
            await self.release.wait()
        return await super().fetch_json(url)


class RecordingAuthenticator:
    def __init__(self) -> None:
        self.tokens: list[str | None] = []

    async def authenticate(self, token: str | None) -> Principal:
        self.tokens.append(token)
        return DEMO_PRINCIPAL


class RecordingRateLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[Principal, str, str]] = []

    async def enforce(self, principal: Principal, *, method: str, route: str) -> None:
        self.calls.append((principal, method, route))


def _key(key_id: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    assert isinstance(public_jwk, dict)
    public_jwk.update({"kid": key_id, "use": "sig", "alg": "RS256"})
    return private, public_jwk


def _token(
    private: rsa.RSAPrivateKey,
    key_id: str,
    *,
    omit: tuple[str, ...] = (),
    **overrides: Any,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "oidc-user-42",
        "org_id": ORGANIZATION_ID,
        "role": "editor",
        "name": "Ada User",
        "iat": now,
        "nbf": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    for claim in omit:
        claims.pop(claim, None)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": key_id})


def _verifier(
    fetcher: FakeFetcher,
    *,
    monotonic: Any = lambda: 100.0,
) -> OidcJwtVerifier:
    return OidcJwtVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        fetcher=fetcher,
        cache_seconds=300,
        refresh_cooldown_seconds=10,
        monotonic=monotonic,
    )


@pytest.mark.asyncio
async def test_demo_authenticator_is_stable_and_does_not_require_a_token() -> None:
    assert await DemoAuthenticator().authenticate(None) == DEMO_PRINCIPAL


@pytest.mark.asyncio
async def test_valid_oidc_token_builds_verified_tenant_context() -> None:
    private, jwk = _key("key-1")
    principal = await _verifier(FakeFetcher([jwk])).authenticate(_token(private, "key-1"))

    assert principal.subject == "oidc-user-42"
    assert principal.organization_id == UUID(ORGANIZATION_ID)
    assert principal.role is AuthRole.EDITOR
    assert principal.display_name == "Ada User"


@pytest.mark.asyncio
async def test_algorithm_none_is_rejected_before_key_fetch() -> None:
    _, jwk = _key("key-1")
    fetcher = FakeFetcher([jwk])
    token = jwt.encode(
        {"sub": "attacker"},
        key="",
        algorithm="none",
        headers={"kid": "key-1"},
    )

    with pytest.raises(AuthenticationError):
        await _verifier(fetcher).authenticate(token)
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_oversized_key_identifier_is_rejected_before_key_fetch() -> None:
    private, jwk = _key("trusted")
    fetcher = FakeFetcher([jwk])

    with pytest.raises(AuthenticationError):
        await _verifier(fetcher).authenticate(_token(private, "k" * 129))

    assert fetcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "x" * 16_385])
async def test_missing_malformed_and_oversized_tokens_are_rejected(token: str | None) -> None:
    with pytest.raises(AuthenticationError):
        await _verifier(FakeFetcher([])).authenticate(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_override",
    [
        {"iss": "https://attacker.example.test"},
        {"aud": "some-other-api"},
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},
        {"nbf": datetime.now(UTC) + timedelta(minutes=5)},
    ],
    ids=["wrong-issuer", "wrong-audience", "expired", "not-yet-valid"],
)
async def test_strict_registered_claim_validation(claim_override: dict[str, Any]) -> None:
    private, jwk = _key("key-1")

    with pytest.raises(AuthenticationError):
        await _verifier(FakeFetcher([jwk])).authenticate(_token(private, "key-1", **claim_override))


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["exp", "nbf"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_numeric_dates_are_stable_authentication_failures(
    claim: str,
    value: float,
) -> None:
    private, jwk = _key("key-1")

    with pytest.raises(AuthenticationError):
        await _verifier(FakeFetcher([jwk])).authenticate(_token(private, "key-1", **{claim: value}))


@pytest.mark.asyncio
async def test_missing_expiration_and_wrong_signature_are_rejected() -> None:
    trusted_private, trusted_jwk = _key("key-1")
    attacker_private, _ = _key("attacker-key")
    verifier = _verifier(FakeFetcher([trusted_jwk]))

    with pytest.raises(AuthenticationError):
        await verifier.authenticate(_token(trusted_private, "key-1", omit=("exp",)))
    with pytest.raises(AuthenticationError):
        await verifier.authenticate(_token(attacker_private, "key-1"))


@pytest.mark.asyncio
async def test_signing_key_rotation_refreshes_after_bounded_cooldown() -> None:
    clock = [100.0]
    old_private, old_jwk = _key("old-key")
    new_private, new_jwk = _key("new-key")
    fetcher = FakeFetcher([old_jwk])
    verifier = _verifier(fetcher, monotonic=lambda: clock[0])

    assert (await verifier.authenticate(_token(old_private, "old-key"))).role is AuthRole.EDITOR
    clock[0] += 11
    fetcher.keys = [new_jwk]
    assert (await verifier.authenticate(_token(new_private, "new-key"))).role is AuthRole.EDITOR
    assert len([url for url in fetcher.calls if url.endswith("openid-configuration")]) == 2


@pytest.mark.asyncio
async def test_concurrent_first_use_shares_one_key_refresh() -> None:
    private, jwk = _key("key-1")
    fetcher = BlockingFetcher([jwk])
    verifier = _verifier(fetcher)
    token = _token(private, "key-1")

    first = asyncio.create_task(verifier.authenticate(token))
    await fetcher.started.wait()
    second = asyncio.create_task(verifier.authenticate(token))
    await asyncio.sleep(0)
    fetcher.release.set()

    principals = await asyncio.gather(first, second)

    assert [principal.subject for principal in principals] == ["oidc-user-42"] * 2
    assert len([url for url in fetcher.calls if url.endswith("openid-configuration")]) == 1


@pytest.mark.asyncio
async def test_unknown_key_ids_are_rejected_and_refresh_is_rate_limited() -> None:
    trusted_private, trusted_jwk = _key("trusted")
    unknown_private, _ = _key("unknown")
    fetcher = FakeFetcher([trusted_jwk])
    verifier = _verifier(fetcher)

    await verifier.authenticate(_token(trusted_private, "trusted"))
    with pytest.raises(AuthenticationError):
        await verifier.authenticate(_token(unknown_private, "unknown"))
    assert len(fetcher.calls) == 2


@pytest.mark.asyncio
async def test_unknown_initial_key_is_rejected_after_one_refresh() -> None:
    _, trusted_jwk = _key("trusted")
    unknown_private, _ = _key("unknown")

    with pytest.raises(AuthenticationError):
        await _verifier(FakeFetcher([trusted_jwk])).authenticate(_token(unknown_private, "unknown"))


@pytest.mark.asyncio
async def test_network_failure_fails_closed_after_key_cache_expires() -> None:
    clock = [100.0]
    private, jwk = _key("key-1")
    fetcher = FakeFetcher([jwk])
    verifier = _verifier(fetcher, monotonic=lambda: clock[0])
    token = _token(private, "key-1")

    await verifier.authenticate(token)
    clock[0] += 301
    fetcher.fail = True
    with pytest.raises(AuthenticationUnavailableError):
        await verifier.authenticate(token)
    with pytest.raises(AuthenticationUnavailableError):
        await verifier.authenticate(token)


@pytest.mark.asyncio
async def test_valid_cached_key_survives_brief_provider_failure() -> None:
    private, jwk = _key("key-1")
    fetcher = FakeFetcher([jwk])
    verifier = _verifier(fetcher)
    token = _token(private, "key-1")

    await verifier.authenticate(token)
    fetcher.fail = True
    assert (await verifier.authenticate(token)).subject == "oidc-user-42"


@pytest.mark.asyncio
async def test_discovery_issuer_mismatch_and_unsafe_jwks_uri_fail_closed() -> None:
    private, jwk = _key("key-1")
    fetcher = FakeFetcher([jwk])
    fetcher.metadata_issuer = "https://attacker.example.test"
    with pytest.raises(AuthenticationUnavailableError):
        await _verifier(fetcher).authenticate(_token(private, "key-1"))

    fetcher = FakeFetcher([jwk])
    fetcher.jwks_uri = "file:///private/signing-keys.json"
    with pytest.raises(AuthenticationUnavailableError):
        await _verifier(fetcher).authenticate(_token(private, "key-1"))

    fetcher = FakeFetcher([jwk])
    fetcher.jwks_uri = "https://attacker.example.test/signing-keys.json"
    with pytest.raises(AuthenticationUnavailableError):
        await _verifier(fetcher).authenticate(_token(private, "key-1"))

    fetcher = FakeFetcher([jwk])
    fetcher.jwks_uri = None  # type: ignore[assignment]
    with pytest.raises(AuthenticationUnavailableError):
        await _verifier(fetcher).authenticate(_token(private, "key-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keys",
    [
        [],
        ["not-a-key"],
        [{"kid": "", "kty": "RSA", "alg": "RS256"}],
        [{"kid": "key-1", "kty": "RSA", "alg": "HS256"}],
        [{"kid": "key-1", "kty": "RSA", "alg": "RS256"}],
        [
            {
                "kid": "key-1",
                "kty": "RSA",
                "alg": "RS256",
                "key_ops": ["encrypt"],
            }
        ],
    ],
)
async def test_empty_or_malformed_jwks_fails_closed(keys: list[Any]) -> None:
    private, _ = _key("key-1")
    with pytest.raises(AuthenticationUnavailableError):
        await _verifier(FakeFetcher(keys)).authenticate(_token(private, "key-1"))


@pytest.mark.asyncio
async def test_jwk_use_and_duplicate_key_ids_are_enforced() -> None:
    private, jwk = _key("key-1")
    encryption_key = dict(jwk, use="enc")
    with pytest.raises(AuthenticationUnavailableError):
        await _verifier(FakeFetcher([encryption_key])).authenticate(_token(private, "key-1"))

    principal = await _verifier(FakeFetcher([jwk, jwk])).authenticate(_token(private, "key-1"))
    assert principal.subject == "oidc-user-42"


@respx.mock
@pytest.mark.asyncio
async def test_http_document_fetcher_bounds_responses_and_does_not_follow_redirects() -> None:
    url = "https://identity.example.test/.well-known/openid-configuration"
    fetcher = HttpOidcDocumentFetcher(timeout_seconds=1)
    route = respx.get(url).mock(return_value=httpx.Response(200, json={"issuer": ISSUER}))
    assert (await fetcher.fetch_json(url))["issuer"] == ISSUER
    assert route.called

    route.mock(return_value=httpx.Response(302, headers={"Location": "https://attacker.test"}))
    with pytest.raises(AuthenticationUnavailableError):
        await fetcher.fetch_json(url)

    route.mock(return_value=httpx.Response(200, content=b"not-json"))
    with pytest.raises(AuthenticationUnavailableError):
        await fetcher.fetch_json(url)

    route.mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(AuthenticationUnavailableError):
        await fetcher.fetch_json(url)

    route.mock(return_value=httpx.Response(200, headers={"Content-Length": "1048577"}))
    with pytest.raises(AuthenticationUnavailableError):
        await fetcher.fetch_json(url)

    route.mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(b"x" * (1_048_576 + 1)),
        )
    )
    with pytest.raises(AuthenticationUnavailableError):
        await fetcher.fetch_json(url)

    with pytest.raises(AuthenticationUnavailableError):
        await fetcher.fetch_json("http://127.0.0.1/private")


@pytest.mark.asyncio
async def test_unknown_role_and_invalid_organization_are_rejected() -> None:
    private, jwk = _key("key-1")
    verifier = _verifier(FakeFetcher([jwk]))

    with pytest.raises(AuthenticationError):
        await verifier.authenticate(_token(private, "key-1", role="superuser"))
    with pytest.raises(AuthenticationError):
        await verifier.authenticate(_token(private, "key-1", org_id="../../tenant"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"sub": ""},
        {"org_id": 7},
        {"role": 7},
        {"name": 7},
        {"exp": "9999999999"},
    ],
)
async def test_malformed_identity_claim_types_are_rejected(claims: dict[str, Any]) -> None:
    private, jwk = _key("key-1")

    with pytest.raises(AuthenticationError):
        await _verifier(FakeFetcher([jwk])).authenticate(_token(private, "key-1", **claims))


@pytest.mark.parametrize(
    "url",
    [
        "http://identity.example.test",
        "https://user:password@identity.example.test",
        "https://identity.example.test?token=secret",
        "https://identity.example.test:8443",
        "https://identity.example.test:not-a-port",
        "https://identity.example.test/line\nbreak",
        "file:///private/keys.json",
    ],
)
def test_oidc_issuer_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL"):
        OidcJwtVerifier(
            issuer=url,
            audience=AUDIENCE,
            fetcher=FakeFetcher([]),
        )


@pytest.mark.parametrize("audience", ["", "a" * 513])
def test_oidc_audience_is_bounded(audience: str) -> None:
    with pytest.raises(ValueError, match="audience"):
        OidcJwtVerifier(issuer=ISSUER, audience=audience, fetcher=FakeFetcher([]))


@pytest.mark.parametrize("algorithms", [(), ("HS256",), ("RS256", "HS256")])
def test_oidc_algorithm_allowlist_cannot_be_weakened(algorithms: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="RS256"):
        OidcJwtVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=algorithms,
            fetcher=FakeFetcher([]),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cache_seconds": 0}, "cache"),
        ({"refresh_cooldown_seconds": 0}, "cooldown"),
        ({"clock_skew_seconds": -1}, "clock skew"),
    ],
)
def test_oidc_cache_controls_reject_unbounded_values(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        OidcJwtVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            fetcher=FakeFetcher([]),
            **kwargs,
        )


@pytest.mark.parametrize("timeout", [0.0, float("nan"), float("inf"), 31.0])
def test_oidc_http_timeout_is_finite_and_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        HttpOidcDocumentFetcher(timeout_seconds=timeout)


def test_authenticator_factory_rejects_incomplete_oidc_and_builds_each_mode() -> None:
    assert isinstance(
        build_authenticator(Settings(environment="test", auth_mode="demo", _env_file=None)),
        DemoAuthenticator,
    )
    with pytest.raises(ValueError, match="issuer and audience"):
        build_authenticator(Settings(environment="test", auth_mode="oidc", _env_file=None))
    assert isinstance(
        build_authenticator(
            Settings(
                environment="test",
                auth_mode="oidc",
                oidc_issuer=ISSUER,
                oidc_audience=AUDIENCE,
                _env_file=None,
            )
        ),
        OidcJwtVerifier,
    )

    invalid_production_demo = Settings.model_construct(
        environment="production",
        auth_mode="demo",
    )
    with pytest.raises(ValueError, match="limited"):
        build_authenticator(invalid_production_demo)


@pytest.mark.asyncio
async def test_auth_dependencies_reject_non_bearer_and_allow_matching_role() -> None:
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(authenticator=DemoAuthenticator()),
            ),
        }
    )
    credentials = HTTPAuthorizationCredentials(scheme="Basic", credentials="not-a-bearer")

    with pytest.raises(AuthenticationError):
        await require_principal(request, credentials)

    authorize = cast(
        Callable[[Principal], Awaitable[Principal]],
        require_roles(AuthRole.OWNER),
    )
    assert await authorize(DEMO_PRINCIPAL) == DEMO_PRINCIPAL


@pytest.mark.asyncio
async def test_principal_dependency_forwards_mixed_case_bearer_and_attaches_identity() -> None:
    authenticator = RecordingAuthenticator()
    limiter = RecordingRateLimiter()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "route": SimpleNamespace(path="/api/v1/projects/{project_id}"),
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    authenticator=authenticator,
                    rate_limiter=limiter,
                )
            ),
        }
    )
    credentials = HTTPAuthorizationCredentials(scheme="BeArEr", credentials="signed-token")

    principal = await require_principal(request, credentials)

    assert authenticator.tokens == ["signed-token"]
    assert principal is DEMO_PRINCIPAL
    assert request.state.principal is DEMO_PRINCIPAL
    assert limiter.calls == [
        (DEMO_PRINCIPAL, "GET", "/api/v1/projects/{project_id}"),
    ]
