"""Capability model invariants."""

from __future__ import annotations

from datetime import UTC, datetime

from corpuskit.domain.capabilities import (
    CapabilityCheck,
    CapabilityId,
    CapabilityReport,
    CapabilityState,
)


def test_capability_report_serializes_stable_enum_values() -> None:
    report = CapabilityReport(
        checked_at=datetime(2026, 8, 11, tzinfo=UTC),
        checks=(
            CapabilityCheck(
                id=CapabilityId.PHOIBLE,
                state=CapabilityState.UNAVAILABLE,
                label="PHOIBLE",
                detail="Not provisioned.",
                remediation="Provision the pinned snapshot.",
                required=True,
            ),
        ),
        ready=False,
        missing_required=(CapabilityId.PHOIBLE,),
    )

    payload = report.model_dump(mode="json")

    assert payload["checks"][0]["id"] == "phoible"
    assert payload["checks"][0]["state"] == "unavailable"
    assert payload["missing_required"] == ["phoible"]
