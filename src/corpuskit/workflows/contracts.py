"""Small, non-sensitive values allowed across Temporal history boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RunWorkflowReference:
    """Opaque durable-run identity; deliberately excludes specifications and text."""

    organization_id: str
    run_id: str
    spec_sha256: str

    def validate(self) -> RunWorkflowReference:
        """Return this reference after strict canonical UUID/hash validation."""

        organization_id = UUID(self.organization_id)
        run_id = UUID(self.run_id)
        if str(organization_id) != self.organization_id or str(run_id) != self.run_id:
            raise ValueError("workflow references require canonical UUIDs")
        if _SHA256.fullmatch(self.spec_sha256) is None:
            raise ValueError("workflow references require a lowercase SHA-256 digest")
        return self


def workflow_id(reference: RunWorkflowReference) -> str:
    """Build the one stable Temporal workflow ID for a persisted run."""

    reference.validate()
    return f"corpuskit-run-{reference.organization_id}-{reference.run_id}"


__all__ = ["RunWorkflowReference", "workflow_id"]
