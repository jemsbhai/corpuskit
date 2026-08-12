"""HTTP acceptance contracts for bounded interactive workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from corpuskit.api.app import CapabilityReporter, create_app
from corpuskit.api.workflows import WorkflowService
from corpuskit.config import Settings
from corpuskit.domain import (
    CorpusEvaluation,
    CorpusSelection,
    CoverageUnit,
    DependencyUnavailableError,
    EvaluationTarget,
    G2PTranscription,
    SelectionAlgorithm,
    SelectionMetadata,
    SelectionOptions,
)
from corpuskit.domain.capabilities import CapabilityReport


class StubReporter:
    def report(self, *, force: bool = False) -> CapabilityReport:
        del force
        return CapabilityReport(checked_at=datetime(2026, 8, 11, tzinfo=UTC), ready=True)


def _g2p(text: str, language: str) -> G2PTranscription:
    return G2PTranscription(
        text=text,
        language=language,
        ipa="t",
        phonemes=("t",),
        diphones=(),
        triphones=(),
        phoneme_count=1,
        unique_phonemes=("t",),
    )


def _evaluation(
    language: str,
    unit: CoverageUnit,
    target: EvaluationTarget,
    total: int,
) -> CorpusEvaluation:
    return CorpusEvaluation(
        language=language,
        unit=unit,
        target_mode=target.mode,
        target_units=("t",),
        covered_units=("t",),
        missing_units=(),
        coverage=1.0,
        total_sentences=total,
        unit_counts=(),
        sentence_details=(),
        unit_sources=(),
        distribution=None,
        text_quality=None,
    )


class FakeWorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    def phonemize(self, text: str, *, language: str) -> G2PTranscription:
        self._raise()
        self.calls.append(("single", (text, language)))
        return _g2p(text, language)

    def phonemize_batch(
        self, texts: tuple[str, ...], *, language: str
    ) -> tuple[G2PTranscription, ...]:
        self._raise()
        self.calls.append(("batch", (texts, language)))
        return tuple(_g2p(text, language) for text in texts)

    def evaluate(
        self,
        sentences: tuple[str, ...],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
    ) -> CorpusEvaluation:
        self._raise()
        self.calls.append(("evaluate", (sentences, language, unit, target)))
        return _evaluation(language, unit, target, len(sentences))

    def select(
        self,
        candidates: tuple[str, ...],
        *,
        language: str,
        unit: CoverageUnit,
        target: EvaluationTarget,
        options: SelectionOptions,
    ) -> CorpusSelection:
        self._raise()
        self.calls.append(("select", (candidates, language, unit, target, options)))
        return CorpusSelection(
            selected_indices=(0,),
            selected_sentences=(candidates[0],),
            coverage=1.0,
            covered_units=("t",),
            missing_units=(),
            unit=unit,
            target_mode=target.mode,
            algorithm=options.algorithm,
            elapsed_seconds=0.01,
            iterations=1,
            metadata=SelectionMetadata(),
        )


def _client(service: FakeWorkflowService, **settings: Any) -> httpx.AsyncClient:
    resolved = Settings(environment="test", **settings)

    def reporter_factory(_: Settings) -> CapabilityReporter:
        return StubReporter()

    def service_factory(_: Settings) -> WorkflowService:
        return service

    app = create_app(
        resolved,
        reporter_factory=reporter_factory,
        workflow_service_factory=service_factory,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_g2p_single_and_batch_http_contracts() -> None:
    service = FakeWorkflowService()
    async with _client(service) as client:
        single = await client.post("/api/v1/g2p", json={"text": "", "language": "en-us"})
        batch = await client.post(
            "/api/v1/g2p/batch",
            json={"texts": ["one", ""], "language": "fr-fr"},
        )

    assert single.status_code == 200
    assert single.json()["phonemes"] == ["t"]
    assert batch.status_code == 200
    assert [item["text"] for item in batch.json()] == ["one", ""]
    assert [item[0] for item in service.calls] == ["single", "batch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["phoneme", "diphone", "triphone"])
@pytest.mark.parametrize(
    "target",
    [
        {"mode": "derived", "phonemes": []},
        {"mode": "explicit", "phonemes": ["t"]},
        {"mode": "phoible", "phonemes": []},
    ],
)
async def test_evaluation_exposes_every_unit_and_target_mode(
    unit: str,
    target: dict[str, object],
) -> None:
    service = FakeWorkflowService()
    async with _client(service) as client:
        response = await client.post(
            "/api/v1/evaluations",
            json={"sentences": ["text"], "language": "en-us", "unit": unit, "target": target},
        )

    assert response.status_code == 200
    assert response.json()["unit"] == unit
    assert response.json()["target_mode"] == target["mode"]
    assert response.json()["sentence_details"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", [item.value for item in SelectionAlgorithm])
async def test_selection_exposes_every_algorithm(algorithm: str) -> None:
    service = FakeWorkflowService()
    options: dict[str, object] = {"algorithm": algorithm, "max_sentences": 1}
    if algorithm == "distribution":
        options["target_distribution"] = [{"unit": "t", "weight": 1.0}]
    async with _client(service) as client:
        response = await client.post(
            "/api/v1/selections",
            json={
                "candidates": ["text"],
                "language": "en-us",
                "unit": "phoneme",
                "target": {"mode": "derived", "phonemes": []},
                "options": options,
            },
        )

    assert response.status_code == 200
    assert response.json()["algorithm"] == algorithm


@pytest.mark.asyncio
async def test_validation_and_application_errors_are_safe() -> None:
    service = FakeWorkflowService()
    async with _client(service) as client:
        validation = await client.post(
            "/api/v1/evaluations",
            json={"sentences": [], "unexpected": "secret"},
        )
        service.error = DependencyUnavailableError("corpus.select")
        unavailable = await client.post(
            "/api/v1/selections",
            json={"candidates": ["text"], "options": {"algorithm": "ilp"}},
            headers={"X-Request-ID": "request-7"},
        )

    assert validation.status_code == 422
    assert validation.json()["code"] == "validation_error"
    assert validation.json()["details"]
    assert "secret" not in validation.text
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "code": "dependency_unavailable",
        "message": "A required language-processing dependency is not available.",
        "operation": "corpus.select",
        "request_id": "request-7",
    }
    assert "private" not in unavailable.text


@pytest.mark.asyncio
async def test_request_body_limit_returns_413_before_dispatch() -> None:
    service = FakeWorkflowService()
    async with _client(service, max_upload_bytes=24) as client:
        response = await client.post("/api/v1/g2p", json={"text": "x" * 100})

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert service.calls == []


@pytest.mark.asyncio
async def test_chunked_request_body_stops_at_the_limit_before_dispatch() -> None:
    service = FakeWorkflowService()

    async def chunks():
        yield b'{"text":"'
        yield b"x" * 64
        yield b'"}'

    async with _client(service, max_upload_bytes=24) as client:
        response = await client.post(
            "/api/v1/g2p",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert response.headers["x-request-id"]
    assert service.calls == []


@pytest.mark.asyncio
async def test_ambiguous_request_framing_is_rejected_before_dispatch() -> None:
    service = FakeWorkflowService()
    async with _client(service, max_upload_bytes=128) as client:
        response = await client.post(
            "/api/v1/g2p",
            content=b'{"text":"safe"}',
            headers={
                "Content-Length": "15",
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert service.calls == []
