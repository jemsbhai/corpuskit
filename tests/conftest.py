"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

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
