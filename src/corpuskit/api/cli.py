"""Command-line entry point for the API process."""

from __future__ import annotations

import uvicorn

from corpuskit.config import RuntimeRole, get_settings
from corpuskit.telemetry import configure_structured_logging


def main() -> None:
    """Run the API using an import string so worker processes initialize safely."""

    settings = get_settings()
    if settings.runtime_role is not RuntimeRole.API:
        raise RuntimeError("corpuskit-api requires CORPUSKIT_RUNTIME_ROLE=api")
    configure_structured_logging(settings.log_level)
    uvicorn.run(
        "corpuskit.api.app:app",
        host=settings.api_bind_host,
        port=8000,
        reload=False,
        proxy_headers=True,
    )
