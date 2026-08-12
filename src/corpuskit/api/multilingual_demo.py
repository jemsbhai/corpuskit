"""Bounded HTTP endpoint for the curated multilingual demonstration suite."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from corpuskit.domain.multilingual_demo import MultilingualDemoRequest, MultilingualDemoResult


class MultilingualDemoRunner(Protocol):
    def run(self, request: MultilingualDemoRequest) -> MultilingualDemoResult: ...


def multilingual_demo_router(service: MultilingualDemoRunner) -> APIRouter:
    """Build the demo router; all submitted values are enum selectors, never arbitrary text."""

    router = APIRouter()

    @router.post("/labs/demos/multilingual", response_model=MultilingualDemoResult)
    async def run(
        request: Annotated[MultilingualDemoRequest, Body()],
    ) -> MultilingualDemoResult:
        return await run_in_threadpool(service.run, request)

    return router


__all__ = ["MultilingualDemoRunner", "multilingual_demo_router"]
