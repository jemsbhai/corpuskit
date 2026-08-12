"""Executable-evidence invariants for every capability acceptance ID."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

_ROOT = Path(__file__).parents[2]
_MATRIX = _ROOT / "docs" / "product" / "capability-matrix.md"
_REGISTRY = _ROOT / "docs" / "product" / "acceptance-evidence.json"
_MATRIX_ROW = re.compile(
    r"^\|\s*`(?P<requirement>CK-[A-Z0-9]+-[0-9]{3})` .*"
    r"\|\s*`(?P<acceptance>AT-[^`]+)`\s*\|\s*"
    r"(?P<status>Planned|Implemented|Verified|Deferred)\s*\|",
    re.MULTILINE,
)
_PYTEST_SYMBOL = re.compile(r"^test_[a-z0-9_]+$")
_WEB_TEST_SUFFIXES = frozenset({".ts", ".tsx"})


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_registry() -> dict[str, Any]:
    value = json.loads(
        _REGISTRY.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    assert isinstance(value, dict)
    return value


def _assert_executable_reference(reference: object) -> None:
    assert isinstance(reference, dict)
    assert set(reference) == {"path", "symbol"}
    path_text = reference["path"]
    symbol = reference["symbol"]
    assert isinstance(path_text, str)
    assert isinstance(symbol, str)

    relative = PurePosixPath(path_text)
    assert not relative.is_absolute()
    assert relative.parts
    assert relative.parts[0] in {"tests", "apps"}
    assert ".." not in relative.parts
    candidate = (_ROOT / Path(*relative.parts)).resolve()
    assert candidate.is_relative_to(_ROOT)
    assert candidate.is_file(), path_text

    source = candidate.read_text(encoding="utf-8")
    if candidate.suffix == ".py":
        assert relative.parts[0] == "tests"
        assert _PYTEST_SYMBOL.fullmatch(symbol)
        definition = re.compile(
            rf"^(?:async\s+)?def\s+{re.escape(symbol)}\s*\(",
            re.MULTILINE,
        )
        assert definition.search(source), f"{path_text}::{symbol}"
        return

    assert candidate.suffix in _WEB_TEST_SUFFIXES
    assert path_text.startswith("apps/web/")
    title = re.escape(symbol)
    executable = re.compile(rf"\b(?:it|test)\s*\(\s*(['\"]){title}\1")
    assert executable.search(source), f"{path_text}::{symbol}"


def test_every_matrix_acceptance_id_has_existing_executable_evidence() -> None:
    matrix_rows = {
        match["requirement"]: {
            "acceptance_test_id": match["acceptance"],
            "status": match["status"],
        }
        for match in _MATRIX_ROW.finditer(_MATRIX.read_text(encoding="utf-8"))
    }
    registry = _load_registry()

    assert set(registry) == {"schema_version", "description", "entries"}
    assert registry["schema_version"] == 1
    assert isinstance(registry["description"], str)
    assert registry["description"].strip()
    entries = registry["entries"]
    assert isinstance(entries, list)
    assert len(matrix_rows) == len(entries) == 75

    by_requirement: dict[str, dict[str, Any]] = {}
    acceptance_ids: set[str] = set()
    for value in entries:
        assert isinstance(value, dict)
        assert set(value) in (
            {"requirement_id", "acceptance_test_id", "evidence_level", "references"},
            {
                "requirement_id",
                "acceptance_test_id",
                "evidence_level",
                "gap",
                "references",
            },
        )
        requirement_id = value["requirement_id"]
        acceptance_id = value["acceptance_test_id"]
        assert isinstance(requirement_id, str)
        assert requirement_id not in by_requirement
        assert isinstance(acceptance_id, str)
        assert acceptance_id not in acceptance_ids
        by_requirement[requirement_id] = value
        acceptance_ids.add(acceptance_id)

        level = value["evidence_level"]
        assert level in {"acceptance", "partial"}
        if level == "partial":
            assert set(value) == {
                "requirement_id",
                "acceptance_test_id",
                "evidence_level",
                "gap",
                "references",
            }
            assert isinstance(value["gap"], str)
            assert len(value["gap"].strip()) >= 40
        else:
            assert "gap" not in value

        references = value["references"]
        assert isinstance(references, list)
        assert references
        identities = {
            (reference.get("path"), reference.get("symbol"))
            for reference in references
            if isinstance(reference, dict)
        }
        assert len(identities) == len(references)
        for reference in references:
            _assert_executable_reference(reference)

    assert set(by_requirement) == set(matrix_rows)
    assert acceptance_ids == {row["acceptance_test_id"] for row in matrix_rows.values()}
    for requirement_id, matrix in matrix_rows.items():
        evidence = by_requirement[requirement_id]
        assert evidence["acceptance_test_id"] == matrix["acceptance_test_id"]
        if matrix["status"] == "Verified":
            assert evidence["evidence_level"] == "acceptance", requirement_id
