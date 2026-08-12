"""Require a non-empty JUnit acceptance set with no skipped or failed cases."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_SCHEMA = "corpuskit.junit-acceptance-evidence.v1"


class JunitContractError(ValueError):
    """Raised when JUnit evidence is missing or structurally invalid."""


@dataclass(frozen=True, slots=True)
class JunitResult:
    tests: int
    skipped: int
    failures: int
    errors: int

    @property
    def passed(self) -> bool:
        return self.tests > 0 and self.skipped == self.failures == self.errors == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_SCHEMA,
            "tests": self.tests,
            "skipped": self.skipped,
            "failures": self.failures,
            "errors": self.errors,
            "passed": self.passed,
        }


def evaluate_junit(path: Path) -> JunitResult:
    try:
        root = ElementTree.parse(path).getroot()  # noqa: S314 - trusted local test evidence.
    except (OSError, ElementTree.ParseError) as exc:
        raise JunitContractError("JUnit evidence is not readable XML") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise JunitContractError("JUnit root must be testsuite or testsuites")
    cases = list(root.iter("testcase"))
    if not cases:
        raise JunitContractError("JUnit evidence must contain at least one test case")
    return JunitResult(
        tests=len(cases),
        skipped=sum(case.find("skipped") is not None for case in cases),
        failures=sum(case.find("failure") is not None for case in cases),
        errors=sum(case.find("error") is not None for case in cases),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = evaluate_junit(arguments.junit)
    encoded = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 1 if arguments.enforce and not result.passed else 0


if __name__ == "__main__":  # pragma: no cover - exercised by scheduled automation
    raise SystemExit(main())
