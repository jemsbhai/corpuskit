"""Authentication and tenant authorization boundary."""

from corpuskit.auth.dependencies import require_principal, require_roles
from corpuskit.auth.models import AuthRole, Principal
from corpuskit.auth.verifier import (
    AuthBoundaryError,
    AuthenticationError,
    AuthenticationUnavailableError,
    Authenticator,
    AuthorizationError,
    DemoAuthenticator,
    HttpOidcDocumentFetcher,
    OidcJwtVerifier,
    build_authenticator,
)

__all__ = [
    "AuthBoundaryError",
    "AuthRole",
    "AuthenticationError",
    "AuthenticationUnavailableError",
    "Authenticator",
    "AuthorizationError",
    "DemoAuthenticator",
    "HttpOidcDocumentFetcher",
    "OidcJwtVerifier",
    "Principal",
    "build_authenticator",
    "require_principal",
    "require_roles",
]
