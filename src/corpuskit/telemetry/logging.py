"""Structured JSON logging with recursive credential and content redaction."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping, Sequence
from hashlib import sha256
from time import perf_counter
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token|api[_-]?key|prompt|"
    r"sentence|corpus|content|signed[_-]?url)|"
    r"(?:^|[_-])(?:text|body|payload|phonemes?|generated|hypothesis|reference)(?:$|[_-])",
    flags=re.IGNORECASE,
)
_REDACTED = "[REDACTED]"
_MAX_COLLECTION_ITEMS = 100
_MAX_STRING_CHARACTERS = 2_048
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", flags=re.ASCII)


def configure_structured_logging(level: str) -> None:
    """Configure deterministic JSON process logs once at application startup."""

    numeric_level = getattr(logging, level, logging.INFO)
    logging.basicConfig(level=numeric_level, format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            redact_event,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def redact_event(
    _: object,
    __: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor that bounds values and removes sensitive keyed data."""

    return {str(key): _sanitize(str(key), value, depth=0) for key, value in event_dict.items()}


class StructuredAccessLogMiddleware:
    """Emit one bounded event per HTTP request without paths, queries, or bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/internal/metrics":
            await self._app(scope, receive, send)
            return

        started = perf_counter()
        status_code = 500
        failed = False

        async def observe(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, observe)
        except Exception:
            failed = True
            raise
        finally:
            state = scope.get("state", {})
            candidate = state.get("request_id") if isinstance(state, Mapping) else None
            request_id = _safe_request_id(candidate)
            event = {
                "method": _safe_method(scope.get("method")),
                "route": _safe_route(scope),
                "status_class": f"{status_code // 100}xx",
                "status_code": status_code,
                "duration_ms": round(max(0.0, perf_counter() - started) * 1_000, 3),
                "request_id": request_id,
                "outcome": "unhandled_exception" if failed else "completed",
            }
            logger = structlog.get_logger("corpuskit.http")
            if failed:
                logger.error("http.request", **event)
            else:
                logger.info("http.request", **event)


def _sanitize(key: str, value: Any, *, depth: int) -> Any:
    if _SENSITIVE_KEY.search(key):
        return _REDACTED
    if depth >= 5:
        return "[MAX_DEPTH]"
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARACTERS]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        entries = list(value.items())[:_MAX_COLLECTION_ITEMS]
        return {
            str(child_key): _sanitize(str(child_key), child_value, depth=depth + 1)
            for child_key, child_value in entries
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [
            _sanitize(key, child_value, depth=depth + 1)
            for child_value in value[:_MAX_COLLECTION_ITEMS]
        ]
    return f"<{type(value).__name__}>"


def _safe_method(value: object) -> str:
    known = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
    return value if isinstance(value, str) and value in known else "OTHER"


def _safe_request_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_REQUEST_ID.fullmatch(value):
        return "unavailable"
    return f"sha256:{sha256(value.encode('ascii')).hexdigest()[:32]}"


def _safe_route(scope: Scope) -> str:
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


__all__ = ["StructuredAccessLogMiddleware", "configure_structured_logging", "redact_event"]
