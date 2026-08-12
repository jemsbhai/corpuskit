"""Low-cardinality Prometheus metrics for the CorpusKit API process."""

from __future__ import annotations

from time import perf_counter
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROMETHEUS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)


class ApiMetrics:
    """Own an isolated registry so app factories and tests never share collectors."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "corpuskit_http_requests_total",
            "Completed HTTP requests by bounded route template and status class.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "corpuskit_http_request_duration_seconds",
            "HTTP request duration through the final response body.",
            ("method", "route"),
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.in_progress = Gauge(
            "corpuskit_http_requests_in_progress",
            "HTTP requests currently executing.",
            registry=self.registry,
        )
        self.unhandled = Counter(
            "corpuskit_http_unhandled_exceptions_total",
            "Unhandled exceptions that escaped the application boundary.",
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


class ApiMetricsMiddleware:
    """Measure HTTP traffic without using raw paths, queries, tenants, or users as labels."""

    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/internal/metrics":
            await self._app(scope, receive, send)
            return

        started = perf_counter()
        status_code = 500
        self._metrics.in_progress.inc()

        async def observe(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, observe)
        except Exception:
            self._metrics.unhandled.inc()
            raise
        finally:
            method = _bounded_method(scope.get("method"))
            route = _bounded_route(scope)
            self._metrics.requests.labels(
                method=method,
                route=route,
                status_class=f"{status_code // 100}xx",
            ).inc()
            self._metrics.duration.labels(method=method, route=route).observe(
                max(0.0, perf_counter() - started)
            )
            self._metrics.in_progress.dec()


def _bounded_method(value: object) -> str:
    if isinstance(value, str) and value in _KNOWN_METHODS:
        return value
    return "OTHER"


def _bounded_route(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if (
        not isinstance(template, str)
        or not template.startswith("/")
        or len(template) > 256
        or any(ord(character) < 32 or ord(character) > 126 for character in template)
    ):
        return "unmatched"
    return template


__all__ = ["PROMETHEUS_CONTENT_TYPE", "ApiMetrics", "ApiMetricsMiddleware"]
