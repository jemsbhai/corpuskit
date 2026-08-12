"""Verified identity and tenant context models."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuthRole(StrEnum):
    """Organization-scoped application roles, ordered by policy rather than token input."""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Principal(BaseModel):
    """Identity and tenant scope derived exclusively from trusted authentication data."""

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1, max_length=255)
    organization_id: UUID
    role: AuthRole
    display_name: str | None = Field(default=None, max_length=160)


DEMO_PRINCIPAL = Principal(
    subject="demo-user",
    organization_id=UUID("00000000-0000-4000-8000-000000000001"),
    role=AuthRole.OWNER,
    display_name="CorpusKit Demo User",
)


__all__ = ["DEMO_PRINCIPAL", "AuthRole", "Principal"]
