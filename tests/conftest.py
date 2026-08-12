"""Shared test fixtures."""

from __future__ import annotations

import importlib.metadata
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from corpuskit.api.app import CapabilityReporter, create_app
from corpuskit.config import Settings
from corpuskit.domain.capabilities import (
    CapabilityCheck,
    CapabilityId,
    CapabilityReport,
    CapabilityState,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_RUNTIME_DISTRIBUTIONS = frozenset(
    {"bitsandbytes", "corpusgen", "peft", "torch", "transformers"}
)


def _locked_local_runtime_versions() -> dict[str, str]:
    """Read synthetic-runtime version facts from the checked dependency lock."""

    locked = tomllib.loads((_REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = {
        package["name"]: package["version"]
        for package in locked["package"]
        if package["name"] in _LOCAL_RUNTIME_DISTRIBUTIONS
    }
    if versions.keys() != _LOCAL_RUNTIME_DISTRIBUTIONS:
        missing = sorted(_LOCAL_RUNTIME_DISTRIBUTIONS - versions.keys())
        raise RuntimeError(f"local runtime distributions are missing from uv.lock: {missing}")
    return versions


@pytest.fixture
def locked_local_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Expose locked version metadata to tests that build synthetic local-runtime facts.

    The general backend profile deliberately does not install the multi-gigabyte CUDA runtime.
    Tests using this fixture still validate exact locked versions, while tests for missing imports
    and missing distribution metadata remain opt-in and can override this seam explicitly.
    """

    versions = _locked_local_runtime_versions()
    installed_version = importlib.metadata.version

    def locked_version(distribution: str) -> str:
        normalized = distribution.lower().replace("_", "-")
        if normalized in versions:
            return versions[normalized]
        return installed_version(distribution)

    monkeypatch.setattr(importlib.metadata, "version", locked_version)
    return versions


class FakeReporter:
    """Deterministic reporter used by API contract tests."""

    def __init__(self, report: CapabilityReport) -> None:
        self._report = report
        self.calls = 0

    def report(self, *, force: bool = False) -> CapabilityReport:
        del force
        self.calls += 1
        return self._report


@pytest.fixture
def ready_report() -> CapabilityReport:
    return CapabilityReport(
        checked_at=datetime(2026, 8, 11, tzinfo=UTC),
        checks=(
            CapabilityCheck(
                id=CapabilityId.CORPUSGEN_CORE,
                state=CapabilityState.AVAILABLE,
                label="CorpusGen engine",
                detail="Ready.",
                version="0.1.7",
                required=True,
            ),
        ),
        ready=True,
    )


@pytest.fixture
def client_factory() -> Callable[[CapabilityReport], httpx.AsyncClient]:
    def build(report: CapabilityReport) -> httpx.AsyncClient:
        settings = Settings(environment="test", api_docs_enabled=True)

        def reporter_factory(_: Settings) -> CapabilityReporter:
            return FakeReporter(report)

        app = create_app(settings, reporter_factory=reporter_factory)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    return build
