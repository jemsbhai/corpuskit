"""Smoke the adapter against the installed CorpusGen wheel and real eSpeak."""

from __future__ import annotations

import shutil
from importlib import metadata

import httpx
import pytest

from corpuskit.adapters.corpusgen import CorpusgenAdapter
from corpuskit.adapters.corpusgen.phoible_provisioning import PHOIBLE_COMMIT
from corpuskit.adapters.corpusgen.probe import (
    CORPUSGEN_VERSION,
    CorpusgenCapabilityProbe,
)
from corpuskit.api.app import create_app
from corpuskit.config import Settings
from corpuskit.domain import DependencyUnavailableError, EngineUnavailableError
from corpuskit.domain.capabilities import CapabilityId, CapabilityState


@pytest.mark.integration
def test_installed_wheel_real_espeak_smoke() -> None:
    assert metadata.version("corpusgen") == "0.1.7"
    distribution = metadata.distribution("corpusgen")
    assert "site-packages" in str(distribution.locate_file("")).lower()

    try:
        result = CorpusgenAdapter().phonemize("hello", language="en-us")
    except (DependencyUnavailableError, EngineUnavailableError) as exc:
        pytest.skip(f"real eSpeak prerequisite is unavailable ({exc.code.value})")

    assert result.text == "hello"
    assert result.language == "en-us"
    assert result.ipa
    assert result.phonemes
    assert result.phoneme_count == len(result.phonemes)


@pytest.mark.integration
def test_real_capability_probe_reports_pinned_core_espeak_and_phoible() -> None:
    report = CorpusgenCapabilityProbe(Settings(environment="test", _env_file=None)).report(
        force=True
    )
    checks = {check.id: check for check in report.checks}

    assert checks[CapabilityId.CORPUSGEN_CORE].state is CapabilityState.AVAILABLE
    assert checks[CapabilityId.CORPUSGEN_CORE].version == CORPUSGEN_VERSION
    assert checks[CapabilityId.ESPEAK_G2P].state is CapabilityState.AVAILABLE
    assert checks[CapabilityId.PHOIBLE].state is CapabilityState.AVAILABLE
    assert checks[CapabilityId.PHOIBLE].version == PHOIBLE_COMMIT


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_espeak_through_application_and_http_boundaries() -> None:
    if shutil.which("espeak-ng") is None and shutil.which("espeak") is None:
        pytest.skip("real eSpeak prerequisite is unavailable")

    app = create_app(Settings(environment="test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/g2p",
            json={"text": "hello", "language": "en-us"},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "hello"
    assert response.json()["language"] == "en-us"
    assert response.json()["ipa"]
    assert response.json()["phonemes"]
