"""Build and enforce conservative, scoped mutmut 3 evidence from per-file metadata."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_SCHEMA = "corpuskit.mutation-evidence.v1"
OVERALL_THRESHOLD = 0.75
CRITICAL_THRESHOLD = 0.90
MINIMUM_OVERALL_MUTANTS = 50
MINIMUM_CRITICAL_MUTANTS = 25

SCOPED_MODULES = (
    "src/corpuskit/auth/dependencies.py",
    "src/corpuskit/domain/corpora.py",
    "src/corpuskit/services/run_admission.py",
)
CRITICAL_MODULES = (
    "src/corpuskit/auth/dependencies.py",
    "src/corpuskit/services/run_admission.py",
)

_STATUS_BY_EXIT_CODE: dict[int | None, str] = {
    None: "not_checked",
    0: "survived",
    1: "killed",
    2: "interrupted",
    3: "killed",
    5: "no_tests",
    24: "timeout",
    33: "no_tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "caught_by_type_check",
    152: "timeout",
    255: "timeout",
    -9: "segfault",
    -11: "segfault",
    -24: "timeout",
}


class MutationEvidenceError(ValueError):
    """Raised when mutation output is incomplete or cannot support a score."""


@dataclass(frozen=True, slots=True)
class Score:
    killed: int
    eligible: int
    score: float
    threshold: float
    minimum_mutants: int

    @property
    def passed(self) -> bool:
        return self.eligible >= self.minimum_mutants and self.score >= self.threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "killed": self.killed,
            "eligible": self.eligible,
            "score": self.score,
            "threshold": self.threshold,
            "minimum_mutants": self.minimum_mutants,
            "passed": self.passed,
        }


def load_mutation_counts(mutants_root: Path) -> dict[str, Counter[str]]:
    """Load the exact configured module set; missing or unknown verdicts fail closed."""

    result: dict[str, Counter[str]] = {}
    for module in SCOPED_MODULES:
        meta_path = mutants_root / f"{module}.meta"
        try:
            value = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MutationEvidenceError(f"missing or invalid mutation metadata: {module}") from exc
        exit_codes = value.get("exit_code_by_key") if isinstance(value, dict) else None
        if not isinstance(exit_codes, dict) or not exit_codes:
            raise MutationEvidenceError(f"mutation metadata contains no mutants: {module}")
        counts: Counter[str] = Counter()
        for raw_code in exit_codes.values():
            if raw_code is not None and (
                not isinstance(raw_code, int) or isinstance(raw_code, bool)
            ):
                raise MutationEvidenceError(f"invalid mutmut exit code in {module}")
            status = _STATUS_BY_EXIT_CODE.get(raw_code)
            if status is None:
                raise MutationEvidenceError(f"unknown mutmut exit code {raw_code!r} in {module}")
            counts[status] += 1
        result[module] = counts
    return result


def calculate_score(counts: Counter[str], *, threshold: float, minimum_mutants: int) -> Score:
    """Score kills conservatively; every non-skipped unresolved verdict is a miss."""

    eligible = sum(counts.values()) - counts["skipped"]
    killed = counts["killed"] + counts["caught_by_type_check"]
    score = killed / eligible if eligible else 0.0
    if not math.isfinite(score):
        raise MutationEvidenceError("mutation score is not finite")
    return Score(
        killed=killed,
        eligible=eligible,
        score=score,
        threshold=threshold,
        minimum_mutants=minimum_mutants,
    )


def build_evidence(mutants_root: Path) -> dict[str, object]:
    per_module = load_mutation_counts(mutants_root)
    overall_counts: Counter[str] = Counter()
    critical_counts: Counter[str] = Counter()
    for module, counts in per_module.items():
        overall_counts.update(counts)
        if module in CRITICAL_MODULES:
            critical_counts.update(counts)
    overall = calculate_score(
        overall_counts,
        threshold=OVERALL_THRESHOLD,
        minimum_mutants=MINIMUM_OVERALL_MUTANTS,
    )
    critical = calculate_score(
        critical_counts,
        threshold=CRITICAL_THRESHOLD,
        minimum_mutants=MINIMUM_CRITICAL_MUTANTS,
    )
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "tool": {"name": "mutmut", "version": importlib.metadata.version("mutmut")},
        "scoring": {
            "formula": "(killed + caught_by_type_check) / (total - skipped)",
            "unresolved_verdicts_are_misses": True,
        },
        "scope": {
            "modules": list(SCOPED_MODULES),
            "critical_modules": list(CRITICAL_MODULES),
        },
        "overall": overall.to_dict(),
        "critical": critical.to_dict(),
        "verdicts": dict(sorted(overall_counts.items())),
        "per_module_verdicts": {
            module: dict(sorted(counts.items())) for module, counts in per_module.items()
        },
        "passed": overall.passed and critical.passed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutants-root", type=Path, default=Path("mutants"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enforce", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    evidence = build_evidence(arguments.mutants_root)
    encoded = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 1 if arguments.enforce and not bool(evidence["passed"]) else 0


if __name__ == "__main__":  # pragma: no cover - exercised by scheduled automation
    raise SystemExit(main())
