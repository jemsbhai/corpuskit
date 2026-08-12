"""Operator-only Prometheus scrape endpoint."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from corpuskit.telemetry import PROMETHEUS_CONTENT_TYPE, ApiMetrics


def metrics_router(metrics: ApiMetrics, bearer_token: SecretStr | None) -> APIRouter:
    """Build a scrape router protected by a server-configured opaque token when set."""

    router = APIRouter()
    expected = bearer_token.get_secret_value().encode("utf-8") if bearer_token else None

    @router.get(
        "/internal/metrics",
        include_in_schema=False,
        response_class=Response,
    )
    async def prometheus_metrics(request: Request) -> Response:
        if expected is not None and not _authorized(request, expected):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "code": "metrics_authentication_required",
                    "message": "Valid operator credentials are required.",
                    "operation": "observability.metrics",
                    "request_id": getattr(request.state, "request_id", "unavailable"),
                },
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
        return Response(
            content=metrics.render(),
            headers={
                "Content-Type": PROMETHEUS_CONTENT_TYPE,
                "Cache-Control": "no-store",
            },
        )

    return router


def _authorized(request: Request, expected: bytes) -> bool:
    authorization_values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"authorization"
    ]
    if len(authorization_values) != 1:
        return False
    authorization = authorization_values[0]
    if not authorization.startswith(b"Bearer "):
        return False
    candidate = authorization.removeprefix(b"Bearer ")
    if not 1 <= len(candidate) <= 512 or candidate != candidate.strip():
        return False
    return secrets.compare_digest(candidate, expected)


__all__ = ["metrics_router"]
