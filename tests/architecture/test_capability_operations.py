"""Operational traceability invariants for every CorpusGen capability family."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_MATRIX = _ROOT / "docs" / "product" / "capability-matrix.md"
_OPERATIONS = _ROOT / "docs" / "product" / "capability-operations.md"
_REQUIREMENT = re.compile(r"^\|\s*`(?P<id>CK-[A-Z0-9]+-[0-9]{3})`", re.MULTILINE)
_OPERATIONAL_ROW = re.compile(
    r"^\|\s*`(?P<prefix>CK-[A-Z0-9]+)-\*`\s*"
    r"\|\s*(?P<docs>.+?)\s*"
    r"\|\s*(?P<telemetry>.+?)\s*"
    r"\|\s*(?P<owner>.+?)\s*"
    r"\|\s*(?P<failure>.+?)\s*\|$",
    re.MULTILINE,
)
_LINK = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")


def test_every_capability_family_has_complete_operational_traceability() -> None:
    requirement_ids = _REQUIREMENT.findall(_MATRIX.read_text(encoding="utf-8"))
    rows = {
        match["prefix"]: match.groupdict()
        for match in _OPERATIONAL_ROW.finditer(_OPERATIONS.read_text(encoding="utf-8"))
    }
    expected_prefixes = {value.rsplit("-", 1)[0] for value in requirement_ids}

    assert len(requirement_ids) == 75
    assert set(rows) == expected_prefixes
    assert len(rows) == len(expected_prefixes)
    for row in rows.values():
        assert _LINK.search(row["docs"])
        assert "corpuskit_" in row["telemetry"] or row["prefix"] in {
            "CK-CLI",
            "CK-REP",
        }
        assert len(row["owner"].split()) >= 2
        assert any(word in row["failure"].lower() for word in ("fail", "reject", "return"))


def test_every_operational_document_link_resolves_inside_the_repository() -> None:
    source = _OPERATIONS.read_text(encoding="utf-8")
    missing: list[str] = []
    for match in _LINK.finditer(source):
        target = match["target"].split("#", 1)[0]
        if not target or "://" in target:
            continue
        candidate = (_OPERATIONS.parent / target).resolve()
        if not candidate.is_relative_to(_ROOT) or not candidate.is_file():
            missing.append(target)

    assert missing == []
