"""Bounded, privacy-preserving progress shared by child handlers and durable parents."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_DURABLE_PROGRESS_MESSAGES = 128
MAX_DURABLE_PROGRESS_TOTAL = 10_000
MAX_PROCESS_PROGRESS_BYTES = 512


class RunProgressPhase(StrEnum):
    """Finite phases that are safe to expose through the public run-event feed."""

    VALIDATING = "validating"
    PREPARING_REPOSITORY = "preparing_repository"
    GENERATING = "generating"
    CANDIDATE_ACCEPTED = "candidate_accepted"
    STAGING_RESULT = "staging_result"
    PREPARING_TRAINING = "preparing_training"
    TRAINING = "training"
    CHECKPOINTING = "checkpointing"
    FINISHED = "finished"
    FAILED = "failed"


class DurableRunProgress(BaseModel):
    """One strict progress projection with no free-form or user-authored fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    sequence: int = Field(ge=0, lt=MAX_DURABLE_PROGRESS_MESSAGES)
    phase: RunProgressPhase
    completed: int | None = Field(default=None, ge=0, le=MAX_DURABLE_PROGRESS_TOTAL)
    total: int | None = Field(default=None, ge=1, le=MAX_DURABLE_PROGRESS_TOTAL)
    coverage: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    accepted_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_DURABLE_PROGRESS_TOTAL,
    )

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (self.completed is None) != (self.total is None):
            raise ValueError("progress completed and total must be provided together")
        if self.completed is not None and self.total is not None and self.completed > self.total:
            raise ValueError("progress completed cannot exceed total")
        return self


def normalize_progress(value: DurableRunProgress | Mapping[str, Any]) -> DurableRunProgress:
    """Validate an untrusted child value and expose one stable failure code upstream."""

    if isinstance(value, DurableRunProgress):
        return value
    try:
        return DurableRunProgress.model_validate(value)
    except (TypeError, ValueError, ValidationError):
        raise ValueError("invalid_progress") from None


__all__ = [
    "MAX_DURABLE_PROGRESS_MESSAGES",
    "MAX_DURABLE_PROGRESS_TOTAL",
    "MAX_PROCESS_PROGRESS_BYTES",
    "DurableRunProgress",
    "RunProgressPhase",
    "normalize_progress",
]
