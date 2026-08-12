"""Enforce independent line and branch thresholds from Coverage.py JSON."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "corpuskit.coverage-threshold-evidence.v1"


class CoverageContractError(ValueError):
    """Raised when coverage evidence is missing or structurally invalid."""


@dataclass(frozen=True, slots=True)
class CoverageResult:
    line_percent: float
    branch_percent: float
    minimum_line_percent: float
    minimum_branch_percent: float

    @property
    def passed(self) -> bool:
        return (
            self.line_percent >= self.minimum_line_percent
            and self.branch_percent >= self.minimum_branch_percent
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_SCHEMA,
            "line_percent": self.line_percent,
            "branch_percent": self.branch_percent,
            "minimum_line_percent": self.minimum_line_percent,
            "minimum_branch_percent": self.minimum_branch_percent,
            "passed": self.passed,
        }


def evaluate_coverage(
    value: dict[str, Any],
    *,
    minimum_line_percent: float,
    minimum_branch_percent: float,
) -> CoverageResult:
    for threshold in (minimum_line_percent, minimum_branch_percent):
        if not math.isfinite(threshold) or not 0 <= threshold <= 100:
            raise CoverageContractError("coverage thresholds must be finite percentages")
    meta = value.get("meta")
    totals = value.get("totals")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        raise CoverageContractError("coverage evidence must enable branch measurement")
    if not isinstance(totals, dict):
        raise CoverageContractError("coverage evidence requires totals")
    covered_lines = _count(totals, "covered_lines")
    statements = _count(totals, "num_statements", positive=True)
    covered_branches = _count(totals, "covered_branches")
    branches = _count(totals, "num_branches", positive=True)
    if covered_lines > statements or covered_branches > branches:
        raise CoverageContractError("covered counts cannot exceed measured counts")
    return CoverageResult(
        line_percent=100.0 * covered_lines / statements,
        branch_percent=100.0 * covered_branches / branches,
        minimum_line_percent=minimum_line_percent,
        minimum_branch_percent=minimum_branch_percent,
    )


def _count(totals: dict[str, Any], key: str, *, positive: bool = False) -> int:
    value = totals.get(key)
    lower_bound = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or value < lower_bound:
        requirement = "positive" if positive else "nonnegative"
        raise CoverageContractError(f"coverage total {key} must be a {requirement} integer")
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoverageContractError("coverage evidence is not readable JSON") from exc
    if not isinstance(value, dict):
        raise CoverageContractError("coverage evidence must contain a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("--minimum-line", type=float, required=True)
    parser.add_argument("--minimum-branch", type=float, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = evaluate_coverage(
        _load(arguments.coverage),
        minimum_line_percent=arguments.minimum_line,
        minimum_branch_percent=arguments.minimum_branch,
    )
    encoded = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 1 if arguments.enforce and not result.passed else 0


if __name__ == "__main__":  # pragma: no cover - exercised by scheduled automation
    raise SystemExit(main())
