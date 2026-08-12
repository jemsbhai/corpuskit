"""Standalone router acceptance, including the no-network HTTP boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from corpuskit.adapters.corpusgen.generation import CorpusgenGenerationAdapter
from corpuskit.adapters.corpusgen.scoring import CorpusgenScoringAdapter
from corpuskit.api.generation_scoring import generation_scoring_router
from corpuskit.domain.errors import ApplicationError
from corpuskit.domain.generation import HuggingFaceRepositorySpec
from corpuskit.services.generation_scoring import (
    GenerationCoordinator,
    GenerationPreviewService,
    ScoringService,
)

HF_REVISION = "f" * 40


def _huggingface_policy() -> HuggingFaceRepositorySpec:
    return HuggingFaceRepositorySpec(
        dataset="owner/corpus",
        config="clean",
        split="train",
        text_column="text",
        revision=HF_REVISION,
        language="en-us",
        max_samples=100,
    )


@pytest_asyncio.fixture
async def generation_client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(
        generation_scoring_router(
            GenerationPreviewService(
                GenerationCoordinator(
                    CorpusgenGenerationAdapter(),
                    allowed_huggingface_sources=(_huggingface_policy(),),
                )
            ),
            ScoringService(CorpusgenScoringAdapter()),
        ),
        prefix="/api/v1",
    )

    @app.exception_handler(ApplicationError)
    async def application_error(_: Request, error: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": error.code.value, "operation": error.operation},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_generation_preview_and_composite_routes(
    generation_client: httpx.AsyncClient,
) -> None:
    generation = await generation_client.post(
        "/api/v1/generation/preview",
        json={
            "source": {
                "kind": "prephonemized",
                "entries": [
                    {"source_id": "p", "text": "Pat.", "phonemes": ["p"]},
                    {"source_id": "b", "text": "Bat.", "phonemes": ["b"]},
                ],
            },
            "target": {"phonemes": ["p", "b"], "unit": "phoneme"},
            "stopping": {
                "max_sentences": 2,
                "max_iterations": 3,
                "timeout_seconds": 1,
            },
        },
    )
    composite = await generation_client.post(
        "/api/v1/scoring/composite",
        json={
            "target": {"phonemes": ["p", "b"]},
            "candidates": [
                {"source_id": "p", "text": "Pat.", "phonemes": ["p"]},
                {"source_id": "b", "text": "Bat.", "phonemes": ["b"]},
            ],
            "commit_source_id": "p",
        },
    )

    assert generation.status_code == 200
    assert generation.json()["execution_mode"] == "synchronous_preview"
    assert [item["source_id"] for item in generation.json()["accepted"]] == ["p", "b"]
    assert composite.status_code == 200
    assert composite.json()["committed"]["source_id"] == "p"
    assert composite.json()["covered_units_after"] == ["p"]


@pytest.mark.asyncio
async def test_synchronous_composite_fluency_fails_closed_without_model_loading(
    generation_client: httpx.AsyncClient,
) -> None:
    response = await generation_client.post(
        "/api/v1/scoring/composite",
        json={
            "target": {"phonemes": ["p"]},
            "candidates": [
                {"source_id": "p", "text": "Pat.", "phonemes": ["p"]},
            ],
            "options": {
                "weights": {
                    "coverage": 0,
                    "phonotactic": 0,
                    "readability": 0,
                    "fluency": 1,
                }
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["operation"] == "scoring.composite.fluency_worker_only"


@pytest.mark.asyncio
async def test_ngram_artifact_and_readability_routes(
    generation_client: httpx.AsyncClient,
) -> None:
    scorer = await generation_client.post(
        "/api/v1/scoring/ngram/scorers",
        json={"mode": "corpus_trained", "sequences": [{"phonemes": ["p", "a"]}]},
    )
    constraint = await generation_client.post(
        "/api/v1/scoring/ngram/constraints",
        json={"sequences": [{"phonemes": ["p", "a"]}]},
    )
    score = await generation_client.post(
        "/api/v1/scoring/phonotactics",
        json={
            "artifact": scorer.json(),
            "sequences": [{"phonemes": ["p", "a"]}],
        },
    )
    readability = await generation_client.post(
        "/api/v1/scoring/readability",
        json={"texts": ["Reading simple stories can help students learn new ideas.", "你好世界"]},
    )

    assert scorer.status_code == 200
    assert constraint.status_code == 200
    assert constraint.json()["artifact_type"].endswith("constraint")
    assert score.status_code == 200
    assert 0 <= score.json()["scores"][0] <= 1
    assert readability.status_code == 200
    assert [item["status"] for item in readability.json()["results"]] == [
        "available",
        "unavailable",
    ]


@pytest.mark.asyncio
async def test_huggingface_preview_never_invokes_network_loader(
    generation_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.generate.backends import repository

    called = False

    def forbidden_loader(*_: object, **__: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("HTTP attempted remote dataset loading")

    monkeypatch.setattr(repository, "_load_hf_dataset", forbidden_loader)
    response = await generation_client.post(
        "/api/v1/generation/preview",
        json={
            "source": {
                "kind": "hugging_face",
                "spec": {
                    "dataset": "owner/corpus",
                    "config": "clean",
                    "split": "train",
                    "text_column": "text",
                    "revision": HF_REVISION,
                },
            },
            "target": {"phonemes": ["p"]},
        },
    )

    assert response.status_code == 422
    assert response.json()["operation"] == "generation.preview.remote_source"
    assert called is False

    validation = await generation_client.post(
        "/api/v1/generation/repository/validate",
        json={
            "source": {
                "kind": "hugging_face",
                "spec": {
                    **_huggingface_policy().model_dump(mode="json"),
                    "max_samples": 50,
                },
            },
            "target": {"phonemes": ["p"]},
            "stopping": {
                "max_sentences": 1,
                "max_iterations": 1,
                "timeout_seconds": 1,
            },
            "activity_timeout_seconds": 2,
        },
    )
    assert validation.status_code == 200
    assert validation.json() == {
        "schema_id": "corpuskit.repository-generation-validation.v1",
        "operation": "repository_generation",
        "valid": True,
        "worker_only": True,
        "network_during_validation": False,
        "source_kind": "hugging_face",
        "source_item_limit": 50,
        "activity_timeout_seconds": 2.0,
    }
    assert called is False
