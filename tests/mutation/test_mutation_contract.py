"""Fail-closed tests for retained mutmut score evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.mutation.check_score import (  # noqa: E402
    CRITICAL_MODULES,
    SCOPED_MODULES,
    MutationEvidenceError,
    build_evidence,
)


def _write_metadata(root: Path, *, killed: int, survived: int) -> None:
    for module in SCOPED_MODULES:
        path = root / f"{module}.meta"
        path.parent.mkdir(parents=True, exist_ok=True)
        verdicts = {f"killed-{index}": 1 for index in range(killed)}
        verdicts.update({f"survived-{index}": 0 for index in range(survived)})
        path.write_text(json.dumps({"exit_code_by_key": verdicts}), encoding="utf-8")


def _object(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def test_score_enforces_overall_and_critical_thresholds(tmp_path: Path) -> None:
    _write_metadata(tmp_path, killed=18, survived=2)
    evidence = build_evidence(tmp_path)
    assert _object(evidence["overall"])["score"] == pytest.approx(0.9)
    assert _object(evidence["critical"])["score"] == pytest.approx(0.9)
    assert evidence["passed"] is True
    assert _object(evidence["scope"])["critical_modules"] == list(CRITICAL_MODULES)


def test_score_penalizes_unresolved_verdicts_and_fails_closed(tmp_path: Path) -> None:
    _write_metadata(tmp_path, killed=17, survived=3)
    first = tmp_path / f"{SCOPED_MODULES[0]}.meta"
    value = json.loads(first.read_text(encoding="utf-8"))
    value["exit_code_by_key"]["not-run"] = None
    first.write_text(json.dumps(value), encoding="utf-8")
    evidence = build_evidence(tmp_path)
    assert _object(evidence["critical"])["score"] < 0.90
    assert evidence["passed"] is False

    first.unlink()
    with pytest.raises(MutationEvidenceError, match="missing or invalid"):
        build_evidence(tmp_path)


def test_unknown_mutmut_exit_code_is_rejected(tmp_path: Path) -> None:
    _write_metadata(tmp_path, killed=20, survived=0)
    first = tmp_path / f"{SCOPED_MODULES[0]}.meta"
    value = json.loads(first.read_text(encoding="utf-8"))
    value["exit_code_by_key"]["unknown"] = 99
    first.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(MutationEvidenceError, match="unknown mutmut exit code"):
        build_evidence(tmp_path)
