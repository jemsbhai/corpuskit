"""Traceability-document invariants for the 75-capability CorpusGen contract."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_MATRIX = _ROOT / "docs" / "product" / "capability-matrix.md"
_ROW = re.compile(
    r"^\|\s*`(?P<requirement>CK-[A-Z0-9]+-[0-9]{3})` .*"
    r"\|\s*`(?P<acceptance>AT-[^`]+)`\s*\|\s*"
    r"(?P<status>Planned|Implemented|Verified|Deferred)\s*\|"
)
_TOTAL = re.compile(
    r"^\|\s*\*\*Total\*\*\s*\|\s*\*\*(?P<count>\d+)\*\*\s*"
    r"\|\s*\*\*(?P<planned>\d+)\*\*\s*\|\s*\*\*(?P<implemented>\d+)\*\*\s*"
    r"\|\s*\*\*(?P<verified>\d+)\*\*\s*\|$"
)


def test_capability_matrix_has_unique_traceable_rows_and_an_exact_summary() -> None:
    lines = _MATRIX.read_text(encoding="utf-8").splitlines()
    rows = [match.groupdict() for line in lines if (match := _ROW.match(line))]

    assert len(rows) == 75
    assert len({row["requirement"] for row in rows}) == len(rows)
    assert len({row["acceptance"] for row in rows}) == len(rows)

    total_match = next(match for line in lines if (match := _TOTAL.match(line)))
    statuses = Counter(row["status"] for row in rows)
    assert int(total_match["count"]) == len(rows)
    assert int(total_match["planned"]) == statuses["Planned"]
    assert int(total_match["implemented"]) == statuses["Implemented"]
    assert int(total_match["verified"]) == statuses["Verified"]
    assert statuses["Deferred"] == 0
