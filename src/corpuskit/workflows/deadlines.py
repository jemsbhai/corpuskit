"""Parent-owned per-run process deadlines resolved from reviewed specification DTOs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from corpuskit.domain.datg import DatgGuidedGenerationRequest, DatgIndexBuildRequest
from corpuskit.domain.generation import RepositoryGenerationRequest
from corpuskit.domain.jobs import RunKind
from corpuskit.domain.model_runtime import (
    HostedGenerationRequest,
    LanguageModelAnalysisRequest,
    LocalGenerationRequest,
)
from corpuskit.domain.phon_rl import PhonRlTrainingRequest

PARENT_ACTIVITY_DEADLINE_CAP_SECONDS = 300.0


def activity_deadline_seconds(
    kind: RunKind,
    spec: Mapping[str, Any],
    *,
    server_cap_seconds: float = PARENT_ACTIVITY_DEADLINE_CAP_SECONDS,
) -> float:
    """Return a validated DTO deadline capped by immutable server policy."""

    if not math.isfinite(server_cap_seconds) or server_cap_seconds <= 0:
        raise ValueError("activity deadline server cap must be finite and positive")
    try:
        if kind is RunKind.GENERATE_REPOSITORY:
            requested = RepositoryGenerationRequest.model_validate(spec).activity_timeout_seconds
        elif kind is RunKind.GENERATE_LLM:
            requested = HostedGenerationRequest.model_validate(spec).activity_timeout_seconds
        elif kind is RunKind.GENERATE_LOCAL:
            requested = LocalGenerationRequest.model_validate(spec).activity_timeout_seconds
        elif kind is RunKind.PERPLEXITY:
            requested = LanguageModelAnalysisRequest.model_validate(spec).activity_timeout_seconds
        elif kind is RunKind.BUILD_DATG_INDEX:
            requested = DatgIndexBuildRequest.model_validate(spec).activity_timeout_seconds
        elif kind is RunKind.GENERATE_DATG:
            requested = DatgGuidedGenerationRequest.model_validate(spec).activity_timeout_seconds
        elif kind is RunKind.TRAIN_PHON_RL:
            requested = PhonRlTrainingRequest.model_validate(
                spec
            ).parameters.activity_timeout_seconds
        else:
            requested = server_cap_seconds
    except ValidationError:
        raise ValueError("invalid run activity deadline contract") from None
    return min(float(requested), server_cap_seconds)


__all__ = ["PARENT_ACTIVITY_DEADLINE_CAP_SECONDS", "activity_deadline_seconds"]
