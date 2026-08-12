"""Capability discovery for the pinned CorpusGen runtime."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from threading import Lock
from time import monotonic

from corpuskit.adapters.corpusgen.phoible_provisioning import PHOIBLE_COMMIT, PHOIBLE_SHA256
from corpuskit.config import Settings
from corpuskit.domain.capabilities import (
    CapabilityCheck,
    CapabilityId,
    CapabilityReport,
    CapabilityState,
)

CORPUSGEN_VERSION = "0.1.7"


class CorpusgenCapabilityProbe:
    """Probe CorpusGen and optional runtime profiles with bounded caching."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] = monotonic,
        home: Path | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._home = home or Path.home()
        self._cached: CapabilityReport | None = None
        self._cached_at = 0.0
        self._lock = Lock()

    def report(self, *, force: bool = False) -> CapabilityReport:
        """Return a sanitized report, reusing it for the configured TTL."""

        with self._lock:
            return self._report_locked(force=force)

    def _report_locked(self, *, force: bool) -> CapabilityReport:
        """Compute one probe at a time so concurrent readiness calls share its result."""

        now = self._clock()
        if (
            not force
            and self._cached is not None
            and now - self._cached_at <= self._settings.capability_cache_seconds
        ):
            return self._cached

        checks = (
            self._probe_core(),
            self._probe_espeak(),
            self._probe_phoible(),
            self._probe_extra(
                CapabilityId.OPTIMIZATION,
                "Optimization algorithms",
                ("pulp", "pymoo"),
                'Install the CPU worker profile: pip install "corpuskit-app[optimization]"',
            ),
            self._probe_extra(
                CapabilityId.REPOSITORY,
                "Repository and Hugging Face import",
                ("datasets", "huggingface_hub"),
                'Install the repository worker profile: pip install "corpuskit-app[repository]"',
            ),
            self._probe_extra(
                CapabilityId.LLM,
                "Hosted LLM generation",
                ("litellm",),
                'Install the LLM worker profile: pip install "corpuskit-app[llm]"',
            ),
            self._probe_extra(
                CapabilityId.LOCAL_MODEL,
                "Local model inference",
                ("torch", "transformers"),
                'Install the local worker profile: pip install "corpuskit-app[local]"',
            ),
            self._probe_cuda(),
            self._probe_extra(
                CapabilityId.PHON_DATG,
                "Phon-DATG guidance",
                ("torch", "transformers"),
                "Configure a local-model CPU or GPU worker.",
            ),
            self._probe_extra(
                CapabilityId.PHON_RL,
                "Phon-RL training and adapters",
                ("torch", "transformers", "peft"),
                "Configure the GPU worker profile with PEFT support.",
            ),
        )
        required = self._settings.required_capabilities
        normalized = tuple(
            check.model_copy(update={"required": check.id.value in required}) for check in checks
        )
        missing = tuple(
            check.id
            for check in normalized
            if check.required and check.state is not CapabilityState.AVAILABLE
        )
        result = CapabilityReport(
            checked_at=datetime.now(UTC),
            checks=normalized,
            ready=not missing,
            missing_required=missing,
        )
        self._cached = result
        self._cached_at = now
        return result

    def _probe_core(self) -> CapabilityCheck:
        try:
            installed = metadata.version("corpusgen")
        except metadata.PackageNotFoundError:
            return self._unavailable(
                CapabilityId.CORPUSGEN_CORE,
                "CorpusGen engine",
                "CorpusGen is not installed.",
                "Install the locked CorpusKit Python environment.",
            )
        if installed != CORPUSGEN_VERSION:
            return CapabilityCheck(
                id=CapabilityId.CORPUSGEN_CORE,
                state=CapabilityState.DEGRADED,
                label="CorpusGen engine",
                detail=f"Expected CorpusGen {CORPUSGEN_VERSION}; found {installed}.",
                remediation="Restore the lockfile or complete an adapter compatibility upgrade.",
                version=installed,
            )
        try:
            __import__("corpusgen")
        except Exception as exc:  # dependency import failures must be surfaced, not leaked
            return self._unavailable(
                CapabilityId.CORPUSGEN_CORE,
                "CorpusGen engine",
                f"CorpusGen {installed} failed its import check ({type(exc).__name__}).",
                "Inspect the worker dependency installation and platform compatibility.",
                version=installed,
            )
        return CapabilityCheck(
            id=CapabilityId.CORPUSGEN_CORE,
            state=CapabilityState.AVAILABLE,
            label="CorpusGen engine",
            detail="Pinned CorpusGen adapter contract is available.",
            version=installed,
        )

    def _probe_espeak(self) -> CapabilityCheck:
        if importlib.util.find_spec("phonemizer") is None:
            return self._unavailable(
                CapabilityId.ESPEAK_G2P,
                "eSpeak NG G2P",
                "The phonemizer dependency is unavailable.",
                "Install the locked worker environment and eSpeak NG.",
            )
        try:
            from corpusgen.g2p.manager import G2PManager

            result = G2PManager().phonemize("healthcheck", language="en-us")
            if not result.phonemes:
                raise RuntimeError("health check returned no phonemes")
        except Exception as exc:
            return self._unavailable(
                CapabilityId.ESPEAK_G2P,
                "eSpeak NG G2P",
                f"A real G2P health check failed ({type(exc).__name__}).",
                "Install an architecture-compatible eSpeak NG library and verify "
                "phonemizer discovery.",
            )
        return CapabilityCheck(
            id=CapabilityId.ESPEAK_G2P,
            state=CapabilityState.AVAILABLE,
            label="eSpeak NG G2P",
            detail="A real English phonemization health check succeeded.",
        )

    def _probe_phoible(self) -> CapabilityCheck:
        csv_path = self._home / ".corpusgen" / "phoible.csv"
        if not csv_path.is_file():
            return self._unavailable(
                CapabilityId.PHOIBLE,
                "PHOIBLE inventory data",
                "The pinned PHOIBLE snapshot is not provisioned.",
                "Run the documented PHOIBLE provisioning job before enabling inventory workflows.",
                version=PHOIBLE_COMMIT,
            )
        digest = self._sha256(csv_path)
        if digest != PHOIBLE_SHA256:
            return self._unavailable(
                CapabilityId.PHOIBLE,
                "PHOIBLE inventory data",
                "The provisioned PHOIBLE snapshot failed checksum verification.",
                "Replace it using the pinned, checksum-verified provisioning job.",
                version=PHOIBLE_COMMIT,
            )
        return CapabilityCheck(
            id=CapabilityId.PHOIBLE,
            state=CapabilityState.AVAILABLE,
            label="PHOIBLE inventory data",
            detail="The pinned PHOIBLE snapshot passed checksum verification.",
            version=PHOIBLE_COMMIT,
        )

    def _probe_extra(
        self,
        capability_id: CapabilityId,
        label: str,
        modules: tuple[str, ...],
        remediation: str,
    ) -> CapabilityCheck:
        missing = tuple(name for name in modules if importlib.util.find_spec(name) is None)
        if missing:
            return self._unavailable(
                capability_id,
                label,
                f"Optional modules are unavailable: {', '.join(missing)}.",
                remediation,
            )
        return CapabilityCheck(
            id=capability_id,
            state=CapabilityState.AVAILABLE,
            label=label,
            detail="Required optional modules are installed.",
        )

    def _probe_cuda(self) -> CapabilityCheck:
        if importlib.util.find_spec("torch") is None:
            return self._unavailable(
                CapabilityId.CUDA,
                "CUDA acceleration",
                "PyTorch is not installed in this process.",
                "Use a dedicated GPU worker image for CUDA workloads.",
            )
        try:
            torch = importlib.import_module("torch")
            available = bool(torch.cuda.is_available())
            cuda_version = torch.version.cuda
        except Exception as exc:
            return self._unavailable(
                CapabilityId.CUDA,
                "CUDA acceleration",
                f"PyTorch CUDA discovery failed ({type(exc).__name__}).",
                "Verify the GPU worker driver and PyTorch build.",
            )
        if not available:
            return self._unavailable(
                CapabilityId.CUDA,
                "CUDA acceleration",
                "PyTorch is installed but no compatible CUDA device is available.",
                "Schedule this capability on a configured GPU worker.",
                version=cuda_version,
            )
        return CapabilityCheck(
            id=CapabilityId.CUDA,
            state=CapabilityState.AVAILABLE,
            label="CUDA acceleration",
            detail="PyTorch reports an available CUDA device.",
            version=cuda_version,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _unavailable(
        capability_id: CapabilityId,
        label: str,
        detail: str,
        remediation: str,
        *,
        version: str | None = None,
    ) -> CapabilityCheck:
        return CapabilityCheck(
            id=capability_id,
            state=CapabilityState.UNAVAILABLE,
            label=label,
            detail=detail,
            remediation=remediation,
            version=version,
        )
