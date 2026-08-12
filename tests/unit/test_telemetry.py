"""Security and cardinality tests for process telemetry."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from corpuskit.api.app import create_app
from corpuskit.config import Settings
from corpuskit.telemetry import (
    ApiMetrics,
    ApiMetricsMiddleware,
    StructuredAccessLogMiddleware,
    configure_structured_logging,
)
from corpuskit.telemetry.logging import redact_event


@pytest.mark.asyncio
async def test_metrics_report_bounded_templates_and_exclude_the_scrape_route() -> None:
    application = create_app(Settings(environment="test", _env_file=None))
    secret_path = "/private/corpus-canary"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        live = await client.get("/api/v1/health/live?token=query-canary")
        missing = await client.get(secret_path)
        scrape = await client.get("/internal/metrics")
        second_scrape = await client.get("/internal/metrics")

    assert live.status_code == 200
    assert missing.status_code == 404
    assert scrape.status_code == 200
    assert scrape.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert scrape.headers["cache-control"] == "no-store"
    body = second_scrape.text
    assert 'route="/health/live"' in body
    assert 'route="unmatched"' in body
    assert "/internal/metrics" not in body
    assert secret_path not in body
    assert "query-canary" not in body


@pytest.mark.asyncio
async def test_metrics_authentication_is_constant_contract_and_never_reflects_token() -> None:
    token = "metrics-canary-secret-value-1234567890"
    settings = Settings(
        environment="test",
        metrics_bearer_token=token,
        _env_file=None,
    )
    application = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        missing = await client.get("/internal/metrics")
        wrong = await client.get(
            "/internal/metrics",
            headers={"Authorization": "Bearer wrong-canary-secret"},
        )
        wrong_scheme = await client.get(
            "/internal/metrics",
            headers={"Authorization": f"Basic {token}"},
        )
        malformed = await client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token} "},
        )
        oversized = await client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {'x' * 513}"},
        )
        duplicated = await client.get(
            "/internal/metrics",
            headers=[
                ("Authorization", f"Bearer {token}"),
                ("Authorization", f"Bearer {token}"),
            ],
        )
        valid = await client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {token}"},
        )

    for response in (missing, wrong, wrong_scheme, malformed, oversized, duplicated):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["code"] == "metrics_authentication_required"
        assert token not in response.text
        assert "wrong-canary" not in response.text
    assert valid.status_code == 200
    assert token not in valid.text


@pytest.mark.asyncio
async def test_disabled_metrics_are_not_mounted_or_collected() -> None:
    application = create_app(Settings(environment="test", metrics_enabled=False, _env_file=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/internal/metrics")

    assert response.status_code == 404
    assert application.state.metrics is None


@pytest.mark.asyncio
async def test_metrics_middleware_balances_gauge_when_application_raises() -> None:
    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        scope["route"] = SimpleNamespace(path="/bounded/{item_id}")
        raise RuntimeError("private failure text")

    metrics = ApiMetrics()
    middleware = ApiMetricsMiddleware(failing_app, metrics)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        del message

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "TRACE",
        "scheme": "http",
        "path": "/raw/private-value",
        "raw_path": b"/raw/private-value",
        "query_string": b"secret=canary",
        "root_path": "",
        "headers": [],
        "client": None,
        "server": None,
        "state": {},
    }
    with pytest.raises(RuntimeError, match="private failure"):
        await middleware(scope, receive, send)

    rendered = metrics.render().decode("utf-8")
    assert "corpuskit_http_requests_in_progress 0.0" in rendered
    assert "corpuskit_http_unhandled_exceptions_total 1.0" in rendered
    assert 'method="OTHER",route="/bounded/{item_id}"' in rendered
    assert "private-value" not in rendered
    assert "canary" not in rendered


def test_recursive_log_redaction_bounds_content_and_non_json_values() -> None:
    event = {
        "event": "safe.event",
        "authorization": "Bearer canary",
        "nested": {
            "apiKey": "api-canary",
            "safe": "visible",
            "deeper": {"one": {"two": {"three": {"four": {"five": "hidden"}}}}},
        },
        "sentences": ["private corpus text"],
        "long": "x" * 3_000,
        "bytes": b"private-bytes",
        "many": list(range(150)),
        "count": 3,
        "empty": None,
        "response_text": "generated-canary",
    }

    sanitized = redact_event(None, "info", event)

    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["sentences"] == "[REDACTED]"
    assert sanitized["nested"]["apiKey"] == "[REDACTED]"
    assert sanitized["nested"]["safe"] == "visible"
    assert "canary" not in repr(sanitized)
    assert "private corpus" not in repr(sanitized)
    assert "private-bytes" not in repr(sanitized)
    assert sanitized["bytes"] == "<bytes>"
    assert len(sanitized["long"]) == 2_048
    assert len(sanitized["many"]) == 100
    assert sanitized["count"] == 3
    assert sanitized["empty"] is None
    assert sanitized["response_text"] == "[REDACTED]"
    assert "[MAX_DEPTH]" in repr(sanitized)


def test_logging_configuration_uses_json_redaction_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basic_calls: list[dict[str, object]] = []
    structlog_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "corpuskit.telemetry.logging.logging.basicConfig",
        lambda **values: basic_calls.append(values),
    )
    monkeypatch.setattr(
        "corpuskit.telemetry.logging.structlog.configure",
        lambda **values: structlog_calls.append(values),
    )

    configure_structured_logging("NOT_A_LEVEL")

    assert basic_calls == [{"level": logging.INFO, "format": "%(message)s", "force": True}]
    assert len(structlog_calls) == 1
    assert redact_event in structlog_calls[0]["processors"]
    assert structlog_calls[0]["cache_logger_on_first_use"] is True


@pytest.mark.asyncio
async def test_access_log_uses_only_normalized_observation_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    class CapturingLogger:
        def info(self, event: str, **values: object) -> None:
            events.append(("info", event, values))

        def error(self, event: str, **values: object) -> None:
            events.append(("error", event, values))

    monkeypatch.setattr(
        "corpuskit.telemetry.logging.structlog.get_logger",
        lambda *_: CapturingLogger(),
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        scope["route"] = SimpleNamespace(path="/projects/{project_id}")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = StructuredAccessLogMiddleware(app)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        del message

    scope = _http_scope(
        path="/projects/tenant-canary?token=secret",
        method="PURGE",
        request_id="unsafe request id",
    )
    await middleware(scope, receive, send)

    assert len(events) == 1
    level, event_name, values = events[0]
    assert level == "info"
    assert event_name == "http.request"
    assert values["route"] == "/projects/{project_id}"
    assert values["method"] == "OTHER"
    assert values["request_id"] == "unavailable"
    assert values["status_code"] == 204
    serialized = repr(values)
    assert "tenant-canary" not in serialized
    assert "secret" not in serialized


@pytest.mark.asyncio
async def test_access_log_records_unhandled_outcome_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    class CapturingLogger:
        def info(self, event: str, **values: object) -> None:
            events.append(("info", event, values))

        def error(self, event: str, **values: object) -> None:
            events.append(("error", event, values))

    monkeypatch.setattr(
        "corpuskit.telemetry.logging.structlog.get_logger",
        lambda *_: CapturingLogger(),
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        scope["route"] = SimpleNamespace(path="/jobs/{run_id}")
        raise RuntimeError("exception-canary-secret")

    middleware = StructuredAccessLogMiddleware(app)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        del message

    scope = _http_scope(path="/jobs/private-id", method="GET", request_id="request-123")
    with pytest.raises(RuntimeError, match="exception-canary"):
        await middleware(scope, receive, send)

    assert len(events) == 1
    level, event_name, values = events[0]
    assert level == "error"
    assert event_name == "http.request"
    assert values["route"] == "/jobs/{run_id}"
    assert str(values["request_id"]).startswith("sha256:")
    assert values["request_id"] != "request-123"
    assert values["outcome"] == "unhandled_exception"
    assert values["status_code"] == 500
    assert "exception-canary" not in repr(events)


def _http_scope(*, path: str, method: str, request_id: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": None,
        "server": None,
        "state": {"request_id": request_id},
    }
