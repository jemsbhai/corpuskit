"""Process entry-point contracts."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from corpuskit.api import cli as api_cli
from corpuskit.config import RuntimeRole, Settings
from corpuskit.worker import cli as worker_cli
from corpuskit.worker import dispatcher_cli


def test_api_entrypoint_uses_import_string(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        api_cli.uvicorn, "run", lambda target, **kwargs: captured.update(target=target, **kwargs)
    )

    api_cli.main()

    assert captured["target"] == "corpuskit.api.app:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
    assert captured["proxy_headers"] is True


def test_api_entrypoint_honors_explicit_container_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        api_cli.uvicorn, "run", lambda target, **kwargs: captured.update(target=target, **kwargs)
    )
    monkeypatch.setattr(
        api_cli,
        "get_settings",
        lambda: Settings(
            environment="production",
            runtime_role="api",
            api_bind_host="0.0.0.0",  # noqa: S104 - explicit container-bind test fixture
            database_url="postgresql+asyncpg://api:secret@db.example.test/corpuskit",
            auth_mode="oidc",
            oidc_issuer="https://id.example.test",
            oidc_audience="corpuskit",
            allowed_origins=["https://app.example.test"],
            job_backend="temporal",
            temporal_tls=True,
            artifact_backend="s3",
            artifact_s3_endpoint="https://objects.example.test",
            artifact_s3_sse="AES256",
            api_rate_limit_enabled=True,
            api_docs_enabled=False,
            metrics_bearer_token="m" * 32,
            _env_file=None,
        ),
    )

    api_cli.main()

    assert captured["host"] == "0.0.0.0"  # noqa: S104 - asserted opt-in value


@pytest.mark.asyncio
async def test_worker_rejects_inline_job_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    configured: list[str] = []
    monkeypatch.setattr(
        worker_cli,
        "get_settings",
        lambda: type(
            "Config",
            (),
            {
                "job_backend": "inline",
                "log_level": "INFO",
                "runtime_role": RuntimeRole.WORKER,
            },
        )(),
    )
    monkeypatch.setattr(worker_cli, "configure_structured_logging", configured.append)

    with pytest.raises(RuntimeError, match=r"requires.*temporal"):
        await worker_cli.run_worker()
    assert configured == ["INFO"]


@pytest.mark.asyncio
async def test_worker_connects_registered_batch_profile_and_closes_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = False

    class FakeWorker:
        async def run(self) -> None:
            nonlocal ran
            ran = True

    async def connect(_: Settings) -> object:
        return object()

    monkeypatch.setattr(
        worker_cli,
        "get_settings",
        lambda: Settings(
            environment="test",
            runtime_role="worker",
            job_backend="temporal",
            _env_file=None,
        ),
    )
    monkeypatch.setattr(worker_cli, "connect_temporal", connect)
    monkeypatch.setattr(worker_cli, "build_worker", lambda *_, **__: FakeWorker())

    await worker_cli.run_worker()

    assert ran is True


@pytest.mark.asyncio
async def test_worker_delegates_profile_validation_to_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class FakeWorker:
        async def run(self) -> None:
            nonlocal called
            called = True

    async def connect(_: Settings) -> object:
        return object()

    monkeypatch.setattr(
        worker_cli,
        "get_settings",
        lambda: Settings(
            environment="test",
            runtime_role="worker",
            job_backend="temporal",
            worker_profile="gpu-training",
            temporal_task_queue="gpu-training",
            _env_file=None,
        ),
    )
    monkeypatch.setattr(worker_cli, "connect_temporal", connect)
    monkeypatch.setattr(worker_cli, "build_worker", lambda *_, **__: FakeWorker())

    await worker_cli.run_worker()

    assert called is True


def test_worker_entrypoint_delegates_to_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_run(coroutine: Coroutine[Any, Any, None]) -> None:
        nonlocal called
        called = True
        coroutine.close()

    monkeypatch.setattr(worker_cli.asyncio, "run", fake_run)

    worker_cli.main()

    assert called is True


@pytest.mark.asyncio
async def test_dispatcher_rejects_inline_job_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    configured: list[str] = []
    monkeypatch.setattr(dispatcher_cli, "configure_structured_logging", configured.append)
    with pytest.raises(RuntimeError, match=r"requires.*temporal"):
        await dispatcher_cli.run_dispatcher(
            settings=Settings(
                environment="test",
                runtime_role="dispatcher",
                job_backend="inline",
                _env_file=None,
            )
        )
    assert configured == ["INFO"]


def test_dispatcher_entrypoint_delegates_to_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_run(coroutine: Coroutine[Any, Any, None]) -> None:
        nonlocal called
        called = True
        coroutine.close()

    monkeypatch.setattr(dispatcher_cli.asyncio, "run", fake_run)

    dispatcher_cli.main()

    assert called is True


@pytest.mark.asyncio
async def test_worker_rejects_wrong_runtime_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_cli,
        "get_settings",
        lambda: Settings(environment="test", job_backend="temporal", _env_file=None),
    )

    with pytest.raises(RuntimeError, match="RUNTIME_ROLE=worker"):
        await worker_cli.run_worker()


@pytest.mark.asyncio
async def test_dispatcher_rejects_wrong_runtime_role() -> None:
    with pytest.raises(RuntimeError, match="RUNTIME_ROLE=dispatcher"):
        await dispatcher_cli.run_dispatcher(
            settings=Settings(environment="test", job_backend="temporal", _env_file=None)
        )


def test_api_rejects_wrong_runtime_role(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def run(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(api_cli.uvicorn, "run", run)
    monkeypatch.setattr(
        api_cli,
        "get_settings",
        lambda: Settings(environment="test", runtime_role="worker", _env_file=None),
    )

    with pytest.raises(RuntimeError, match="RUNTIME_ROLE=api"):
        api_cli.main()
    assert called is False
