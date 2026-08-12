"""Cross-role serialization for project lifecycle transitions."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_PROJECT_LOCK_PERSON = b"ck-project-lock"


async def lock_project_lifecycle(session: AsyncSession, project_id: UUID) -> None:
    """Serialize project writes without granting runtime roles table UPDATE.

    PostgreSQL requires ``UPDATE`` privilege for every relation named in a row-locking
    clause, including ``FOR KEY SHARE``. Worker and maintenance identities intentionally
    do not have that privilege on projects, so all project lifecycle participants take a
    transaction-scoped advisory lock before re-reading authoritative state instead.
    SQLite remains a local single-process test backend and has no advisory-lock primitive.
    """

    if session.get_bind().dialect.name != "postgresql":
        return
    await session.scalar(select(func.pg_advisory_xact_lock(_project_advisory_key(project_id))))


def _project_advisory_key(project_id: UUID) -> int:
    digest = hashlib.blake2b(
        project_id.bytes,
        digest_size=8,
        person=_PROJECT_LOCK_PERSON,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


__all__ = ["lock_project_lifecycle"]
