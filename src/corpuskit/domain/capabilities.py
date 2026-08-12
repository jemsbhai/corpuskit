"""Runtime capability and readiness models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CapabilityId(StrEnum):
    """Stable identifiers exposed by the capability API."""

    CORPUSGEN_CORE = "corpusgen-core"
    ESPEAK_G2P = "espeak-g2p"
    PHOIBLE = "phoible"
    OPTIMIZATION = "optimization"
    REPOSITORY = "repository"
    LLM = "llm"
    LOCAL_MODEL = "local-model"
    CUDA = "cuda"
    PHON_DATG = "phon-datg"
    PHON_RL = "phon-rl"


class CapabilityState(StrEnum):
    """Availability state of an independently configurable capability."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CapabilityCheck(BaseModel):
    """One sanitized capability probe result."""

    model_config = ConfigDict(frozen=True)

    id: CapabilityId
    state: CapabilityState
    label: str
    detail: str
    remediation: str | None = None
    version: str | None = None
    required: bool = False


class CapabilityReport(BaseModel):
    """Complete, timestamped runtime capability report."""

    model_config = ConfigDict(frozen=True)

    checked_at: datetime
    checks: tuple[CapabilityCheck, ...]
    ready: bool
    missing_required: tuple[CapabilityId, ...] = Field(default_factory=tuple)
