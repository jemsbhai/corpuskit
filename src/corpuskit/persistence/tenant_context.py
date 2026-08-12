"""Validated transaction-local PostgreSQL tenant and service identity context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corpuskit.domain.platform import safe_audit_actor


class ServiceIdentity(StrEnum):
    USER = "user"
    DISPATCHER = "dispatcher"
    WORKER = "worker"
    ADOPTION = "adoption"
    MAINTENANCE = "maintenance"
    PLATFORM = "platform"


_SERVICE_ACTORS: dict[ServiceIdentity, str] = {
    ServiceIdentity.DISPATCHER: "service:dispatcher",
    ServiceIdentity.WORKER: "service:worker",
    ServiceIdentity.ADOPTION: "service:adoption",
    ServiceIdentity.MAINTENANCE: "service:maintenance",
    ServiceIdentity.PLATFORM: "service:platform",
}
_SAFE_SUBJECT = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")


class TenantContextError(RuntimeError):
    """A context is structurally invalid or unavailable for PostgreSQL access."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: UUID | None
    identity: ServiceIdentity
    actor_id: str

    @classmethod
    def user(cls, organization_id: UUID, subject: str) -> TenantContext:
        if _SAFE_SUBJECT.fullmatch(subject) is None:
            raise TenantContextError("user tenant context is invalid")
        return cls(organization_id, ServiceIdentity.USER, subject)

    @classmethod
    def service(
        cls,
        identity: ServiceIdentity,
        organization_id: UUID | None = None,
    ) -> TenantContext:
        if identity is ServiceIdentity.USER:
            raise TenantContextError("user contexts require a verified subject")
        if identity in {ServiceIdentity.WORKER, ServiceIdentity.ADOPTION, ServiceIdentity.PLATFORM}:
            if organization_id is None:
                raise TenantContextError("this service context requires an organization")
        elif identity is ServiceIdentity.DISPATCHER and organization_id is not None:
            raise TenantContextError(
                "dispatcher context is global and cannot accept an organization"
            )
        return cls(organization_id, identity, _SERVICE_ACTORS[identity])

    def validate(self) -> None:
        try:
            safe_audit_actor(self.actor_id)
        except ValueError as exc:
            if self.identity is not ServiceIdentity.USER:
                raise TenantContextError("service actor context is invalid") from exc
        if self.identity is ServiceIdentity.USER:
            if self.organization_id is None or _SAFE_SUBJECT.fullmatch(self.actor_id) is None:
                raise TenantContextError("user tenant context is invalid")
        elif self.actor_id != _SERVICE_ACTORS[self.identity]:
            raise TenantContextError("service actor context is invalid")


async def apply_postgresql_context(session: AsyncSession, context: TenantContext) -> None:
    """Set validated transaction-local GUCs using values, never interpolated SQL."""

    context.validate()
    await session.execute(
        text(
            "SELECT "
            "set_config('corpuskit.organization_id', :organization_id, true), "
            "set_config('corpuskit.identity', :identity, true), "
            "set_config('corpuskit.actor_id', :actor_id, true)"
        ),
        {
            "organization_id": (
                str(context.organization_id) if context.organization_id is not None else ""
            ),
            "identity": context.identity.value,
            "actor_id": context.actor_id,
        },
    )


__all__ = [
    "ServiceIdentity",
    "TenantContext",
    "TenantContextError",
    "apply_postgresql_context",
]
