"""FastAPI authentication and organization-role dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from corpuskit.auth.models import AuthRole, Principal
from corpuskit.auth.verifier import AuthenticationError, Authenticator, AuthorizationError

_bearer = HTTPBearer(auto_error=False)


async def require_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Authenticate once and attach the verified tenant context to the request."""

    authenticator: Authenticator = request.app.state.authenticator
    token = credentials.credentials if credentials is not None else None
    if credentials is not None and credentials.scheme.lower() != "bearer":
        raise AuthenticationError
    principal = await authenticator.authenticate(token)
    request.state.principal = principal
    route = getattr(request.scope.get("route"), "path", None)
    limiter = request.app.state.rate_limiter
    await limiter.enforce(
        principal,
        method=request.method,
        route=route if isinstance(route, str) else "",
    )
    return principal


def require_roles(*allowed: AuthRole) -> Callable[..., object]:
    """Create an organization-role authorization dependency."""

    allowed_roles = frozenset(allowed)

    async def authorize(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        if principal.role not in allowed_roles:
            raise AuthorizationError
        return principal

    return authorize


__all__ = ["require_principal", "require_roles"]
