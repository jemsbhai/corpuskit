"""CorpusGen runtime capability discovery tests."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import sys
from importlib import metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from corpuskit.adapters.corpusgen import probe as probe_module
from corpuskit.adapters.corpusgen.probe import CorpusgenCapabilityProbe
from corpuskit.config import Settings
from corpuskit.domain.capabilities import CapabilityCheck, CapabilityId, CapabilityState


def _settings(**overrides: Any) -> Settings:
    return Settings(environment="test", _env_file=None, **overrides)


def _available(capability_id: CapabilityId) -> CapabilityCheck:
    return CapabilityCheck(
        id=capability_id,
        state=CapabilityState.AVAILABLE,
        label=capability_id.value,
        detail="Ready.",
    )


def test_report_marks_requirements_and_caches_results(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10.0]
    instance = CorpusgenCapabilityProbe(
        _settings(
            required_capabilities={"corpusgen-core", "phoible"},
            capability_cache_seconds=60,
        ),
        clock=lambda: now[0],
    )
    calls = {"core": 0}

    def core() -> CapabilityCheck:
        calls["core"] += 1
        return _available(CapabilityId.CORPUSGEN_CORE)

    monkeypatch.setattr(instance, "_probe_core", core)
    monkeypatch.setattr(instance, "_probe_espeak", lambda: _available(CapabilityId.ESPEAK_G2P))
    monkeypatch.setattr(instance, "_probe_phoible", lambda: _available(CapabilityId.PHOIBLE))
    monkeypatch.setattr(
        instance,
        "_probe_extra",
        lambda capability_id, _label, _modules, _remediation: _available(capability_id),
    )
    monkeypatch.setattr(instance, "_probe_cuda", lambda: _available(CapabilityId.CUDA))

    first = instance.report()
    cached = instance.report()
    refreshed = instance.report(force=True)

    assert first is cached
    assert refreshed is not first
    assert calls["core"] == 2
    assert first.ready is True
    assert {check.id for check in first.checks if check.required} == {
        CapabilityId.CORPUSGEN_CORE,
        CapabilityId.PHOIBLE,
    }


def test_report_becomes_not_ready_for_a_degraded_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = CorpusgenCapabilityProbe(
        _settings(required_capabilities={"corpusgen-core"}),
    )
    degraded = CapabilityCheck(
        id=CapabilityId.CORPUSGEN_CORE,
        state=CapabilityState.DEGRADED,
        label="CorpusGen",
        detail="Wrong version.",
    )
    monkeypatch.setattr(instance, "_probe_core", lambda: degraded)
    monkeypatch.setattr(instance, "_probe_espeak", lambda: _available(CapabilityId.ESPEAK_G2P))
    monkeypatch.setattr(instance, "_probe_phoible", lambda: _available(CapabilityId.PHOIBLE))
    monkeypatch.setattr(
        instance,
        "_probe_extra",
        lambda capability_id, _label, _modules, _remediation: _available(capability_id),
    )
    monkeypatch.setattr(instance, "_probe_cuda", lambda: _available(CapabilityId.CUDA))

    report = instance.report()

    assert report.ready is False
    assert report.missing_required == (CapabilityId.CORPUSGEN_CORE,)


def test_core_probe_reports_missing_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(probe_module.metadata, "version", missing)

    result = CorpusgenCapabilityProbe(_settings())._probe_core()

    assert result.state is CapabilityState.UNAVAILABLE
    assert "not installed" in result.detail


def test_core_probe_reports_contract_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.metadata, "version", lambda _: "9.9.9")

    result = CorpusgenCapabilityProbe(_settings())._probe_core()

    assert result.state is CapabilityState.DEGRADED
    assert result.version == "9.9.9"


def test_core_probe_sanitizes_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.metadata, "version", lambda _: probe_module.CORPUSGEN_VERSION)
    real_import = builtins.__import__

    def broken_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "corpusgen":
            raise OSError("secret platform detail")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)

    result = CorpusgenCapabilityProbe(_settings())._probe_core()

    assert result.state is CapabilityState.UNAVAILABLE
    assert "OSError" in result.detail
    assert "secret platform detail" not in result.detail


def test_core_probe_accepts_exact_importable_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.metadata, "version", lambda _: probe_module.CORPUSGEN_VERSION)

    result = CorpusgenCapabilityProbe(_settings())._probe_core()

    assert result.state is CapabilityState.AVAILABLE
    assert result.version == probe_module.CORPUSGEN_VERSION


def test_espeak_probe_requires_phonemizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)

    result = CorpusgenCapabilityProbe(_settings())._probe_espeak()

    assert result.state is CapabilityState.UNAVAILABLE
    assert "phonemizer" in result.detail


@pytest.mark.parametrize("phonemes", [[], None])
def test_espeak_probe_rejects_empty_or_failed_healthcheck(
    monkeypatch: pytest.MonkeyPatch, phonemes: list[str] | None
) -> None:
    from corpusgen.g2p import manager

    class FakeManager:
        def phonemize(self, _text: str, *, language: str) -> Any:
            assert language == "en-us"
            if phonemes is None:
                raise RuntimeError("sensitive backend error")
            return SimpleNamespace(phonemes=phonemes)

    monkeypatch.setattr(importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(manager, "G2PManager", FakeManager)

    result = CorpusgenCapabilityProbe(_settings())._probe_espeak()

    assert result.state is CapabilityState.UNAVAILABLE
    assert "RuntimeError" in result.detail
    assert "sensitive backend error" not in result.detail


def test_espeak_probe_runs_real_contract_through_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corpusgen.g2p import manager

    fake_manager = SimpleNamespace(
        phonemize=lambda _text, language: SimpleNamespace(phonemes=["h", "ɛ", "l", "oʊ"])
    )
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(manager, "G2PManager", lambda: fake_manager)

    result = CorpusgenCapabilityProbe(_settings())._probe_espeak()

    assert result.state is CapabilityState.AVAILABLE


def test_phoible_probe_distinguishes_missing_corrupt_and_valid_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance = CorpusgenCapabilityProbe(_settings(), home=tmp_path)

    missing = instance._probe_phoible()
    csv_path = tmp_path / ".corpusgen" / "phoible.csv"
    csv_path.parent.mkdir()
    csv_path.write_bytes(b"corrupt")
    corrupt = instance._probe_phoible()
    expected = hashlib.sha256(b"corrupt").hexdigest()
    monkeypatch.setattr(probe_module, "PHOIBLE_SHA256", expected)
    valid = instance._probe_phoible()

    assert missing.state is CapabilityState.UNAVAILABLE
    assert corrupt.state is CapabilityState.UNAVAILABLE
    assert valid.state is CapabilityState.AVAILABLE
    assert valid.version == probe_module.PHOIBLE_COMMIT


def test_optional_extra_probe_reports_exact_missing_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "installed" else None,
    )
    instance = CorpusgenCapabilityProbe(_settings())

    missing = instance._probe_extra(
        CapabilityId.OPTIMIZATION,
        "Optimization",
        ("installed", "missing"),
        "Install it.",
    )
    available = instance._probe_extra(
        CapabilityId.OPTIMIZATION,
        "Optimization",
        ("installed",),
        "Install it.",
    )

    assert missing.state is CapabilityState.UNAVAILABLE
    assert missing.detail.endswith("missing.")
    assert available.state is CapabilityState.AVAILABLE


def test_cuda_probe_handles_absent_cpu_error_and_available_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = CorpusgenCapabilityProbe(_settings())
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    absent = instance._probe_cuda()

    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    fake_torch.version = SimpleNamespace(cuda="13.0")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: object())
    cpu_only = instance._probe_cuda()

    fake_torch.cuda = SimpleNamespace(is_available=lambda: True)  # type: ignore[attr-defined]
    available = instance._probe_cuda()

    def fail() -> bool:
        raise OSError("driver secret")

    fake_torch.cuda = SimpleNamespace(is_available=fail)  # type: ignore[attr-defined]
    failed = instance._probe_cuda()

    assert absent.state is CapabilityState.UNAVAILABLE
    assert cpu_only.state is CapabilityState.UNAVAILABLE
    assert cpu_only.version == "13.0"
    assert available.state is CapabilityState.AVAILABLE
    assert failed.state is CapabilityState.UNAVAILABLE
    assert "OSError" in failed.detail
    assert "driver secret" not in failed.detail
