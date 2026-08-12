"""Installed-wheel and standalone HTTP acceptance for the lab slice."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from corpuskit.adapters.corpusgen.lab import CorpusgenLabAdapter
from corpuskit.api.coverage_weighting_lab import coverage_weighting_lab_router
from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.errors import DependencyUnavailableError, EngineUnavailableError
from corpuskit.domain.lab import ExportReportRequest, RenderReportRequest, ReportVerbosity
from corpuskit.services.coverage_weighting_lab import CoverageWeightingLabService


class ReadyReporter:
    def report(self, *, force: bool = False) -> CapabilityReport:
        del force
        return CapabilityReport(
            checked_at=datetime(2026, 8, 11, tzinfo=UTC),
            checks=(),
            ready=True,
        )


class RouterG2PResult:
    def __init__(self) -> None:
        self.text = "hello"
        self.ipa = "h e"
        self.phonemes = ["h", "e"]
        self.language = "en-us"
        self.diphones = ["h-e"]
        self.triphones: list[str] = []
        self.phoneme_count = 2
        self.unique_phonemes = {"h", "e"}


class RouterG2P:
    backend = "acceptance-fake"

    def supported_languages(self) -> list[str]:
        return ["en-us"]

    def phonemize_variants(self, text: str, language: str = "en-us") -> list[RouterG2PResult]:
        del text, language
        return [RouterG2PResult()]


@pytest.mark.integration
def test_real_installed_engine_g2p_report_views_and_exports() -> None:
    adapter = CorpusgenLabAdapter()
    assert adapter.installed_version() == adapter.expected_version() == "0.1.7"

    try:
        languages = adapter.g2p_languages()
        variants = adapter.g2p_variants("hello", "en-us")
        rendered = [
            adapter.render_report(
                RenderReportRequest(sentences=("hello",), verbosity=verbosity)
            ).content
            for verbosity in ReportVerbosity
        ]
        json_export = adapter.export_report(ExportReportRequest(sentences=("hello",)))
        jsonld_export = adapter.export_report(
            ExportReportRequest(sentences=("hello",), format="jsonld")
        )
    except (DependencyUnavailableError, EngineUnavailableError) as exc:
        pytest.skip(f"real eSpeak prerequisite is unavailable ({exc.code.value})")

    assert "en-us" in languages.languages
    assert variants.variants[0].phonemes
    assert all(rendered)
    assert len(set(rendered)) == 3
    assert json.loads(json_export.canonical_json)["language"] == "en-us"
    jsonld = json.loads(jsonld_export.canonical_json)
    assert "@context" in jsonld
    assert jsonld_export.media_type == "application/ld+json"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_standalone_router_exposes_bounded_lab_workflows() -> None:
    adapter = CorpusgenLabAdapter(
        g2p_factory=RouterG2P,
        report_renderer=lambda request: f"{request.verbosity.value} report",
        report_exporter=lambda _: {"language": "en-us", "coverage": 1.0},
    )
    service = CoverageWeightingLabService(
        adapter,
        ReadyReporter(),
        Settings(environment="test"),
    )
    app = FastAPI()
    app.include_router(coverage_weighting_lab_router(service), prefix="/api/v1")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        runtime = await client.get("/api/v1/labs/runtime", params={"force": "true"})
        languages = await client.get("/api/v1/labs/g2p/languages")
        variants = await client.post(
            "/api/v1/labs/g2p/variants",
            json={"text": "hello", "language": "en-us"},
        )
        estimate = await client.post(
            "/api/v1/labs/coverage/estimate",
            json={"target_phonemes": ["a", "b"], "unit": "triphone"},
        )
        tracked = await client.post(
            "/api/v1/labs/coverage/track",
            json={
                "target_phonemes": ["a", "b"],
                "unit": "diphone",
                "phoneme_sequences": [["a", "b", "a"]],
                "weights": [{"unit": "b-b", "weight": 3}],
                "next_targets_limit": 2,
            },
        )
        uniform = await client.post(
            "/api/v1/labs/weights/compute",
            json={"strategy": "uniform", "target_units": ["b", "a"]},
        )
        validated = await client.post(
            "/api/v1/labs/weights/validate",
            json={
                "kind": "component",
                "weights": [{"unit": "coverage", "weight": 0}],
            },
        )
        invalid = await client.post(
            "/api/v1/labs/coverage/estimate",
            json={"target_phonemes": ["a", "a"]},
        )
        rendered = await client.post(
            "/api/v1/labs/reports/render",
            json={"sentences": ["hello"], "verbosity": "minimal"},
        )
        exported = await client.post(
            "/api/v1/labs/reports/export",
            json={"sentences": ["hello"], "format": "json"},
        )

    assert runtime.status_code == 200
    assert runtime.json()["compatible"] is True
    assert languages.json() == {
        "backend": "acceptance-fake",
        "languages": ["en-us"],
    }
    assert variants.json()["variants"][0]["phoneme_count"] == 2
    assert estimate.json()["estimated_target_size"] == 8
    assert tracked.json()["final"]["covered_units"] == ["a-b", "b-a"]
    assert tracked.json()["after_reset"]["covered_count"] == 0
    assert uniform.json()["weights"] == [
        {"unit": "a", "weight": 1.0},
        {"unit": "b", "weight": 1.0},
    ]
    assert validated.json() == {"kind": "component", "valid": True, "count": 1}
    assert rendered.json()["content"] == "minimal report"
    assert json.loads(exported.json()["canonical_json"])["coverage"] == 1.0
    assert invalid.status_code == 422
