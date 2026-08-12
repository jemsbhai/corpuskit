"""HTTP surface for safe CorpusGen CLI command previews."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from corpuskit.domain.cli_parity import CliCommandPreview, CliPreviewRequest


class CliPreviewService(Protocol):
    def preview(self, request: CliPreviewRequest) -> CliCommandPreview: ...


def cli_parity_router(service: CliPreviewService) -> APIRouter:
    """Create the CLI parity lab router without executing user-provided commands."""

    router = APIRouter()

    @router.post("/labs/cli/preview", response_model=CliCommandPreview)
    async def preview(
        request: Annotated[CliPreviewRequest, Body(discriminator="workflow")],
    ) -> CliCommandPreview:
        return await run_in_threadpool(service.preview, request)

    return router


__all__ = ["CliPreviewService", "cli_parity_router"]
