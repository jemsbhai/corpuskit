"""Versioned performance-result contract and regression comparison.

The comparator is deliberately independent from the benchmark implementation. A
release job can therefore validate retained JSON evidence without importing the
application or re-running a noisy timing workload.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RESULT_SCHEMA = "corpuskit.performance-result.v1"
COMPARISON_SCHEMA = "corpuskit.performance-comparison.v1"
PROFILE_SCHEMA = "corpuskit.performance-profile.v1"
DEFAULT_MAX_REGRESSION = 0.10
MINIMUM_COMPARABLE_SAMPLES = 20
MINIMUM_COMPARABLE_WARMUPS = 3

_PROFILE_FIELDS = (
    "profile_schema_version",
    "profile_id",
    "os",
    "os_release",
    "architecture",
    "python",
    "python_implementation",
    "cpu_model",
    "logical_cpu_count",
)


class PerformanceContractError(ValueError):
    """Raised when retained performance evidence is not safe to compare."""


@dataclass(frozen=True, slots=True)
class Regression:
    benchmark: str
    baseline_p95_seconds: float
    observed_p95_seconds: float
    change_ratio: float


@dataclass(frozen=True, slots=True)
class AbsoluteViolation:
    benchmark: str
    percentile: str
    limit_seconds: float
    observed_seconds: float


@dataclass(frozen=True, slots=True)
class Comparison:
    comparable: bool
    profile_id: str
    fixture_sha256: str
    regressions: tuple[Regression, ...]
    absolute_violations: tuple[AbsoluteViolation, ...]
    worker_memory_violations: tuple[str, ...]
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.comparable
            and not self.regressions
            and not self.absolute_violations
            and not self.worker_memory_violations
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COMPARISON_SCHEMA,
            "comparable": self.comparable,
            "passed": self.passed,
            "profile_id": self.profile_id,
            "fixture_sha256": self.fixture_sha256,
            "reason": self.reason,
            "regressions": [
                {
                    "benchmark": item.benchmark,
                    "baseline_p95_seconds": item.baseline_p95_seconds,
                    "observed_p95_seconds": item.observed_p95_seconds,
                    "change_ratio": item.change_ratio,
                }
                for item in self.regressions
            ],
            "absolute_violations": [
                {
                    "benchmark": item.benchmark,
                    "percentile": item.percentile,
                    "limit_seconds": item.limit_seconds,
                    "observed_seconds": item.observed_seconds,
                }
                for item in self.absolute_violations
            ],
            "worker_memory_violations": list(self.worker_memory_violations),
        }


def percentile(samples: tuple[float, ...], probability: float) -> float:
    """Return a deterministic nearest-rank percentile for positive durations."""

    if not samples or not 0.0 < probability <= 1.0:
        raise PerformanceContractError("percentiles require samples and 0 < p <= 1")
    if any(not math.isfinite(value) or value < 0 for value in samples):
        raise PerformanceContractError("performance samples must be finite and nonnegative")
    ordered = sorted(samples)
    rank = max(1, math.ceil(probability * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: tuple[float, ...]) -> dict[str, float | int]:
    if not samples:
        raise PerformanceContractError("a benchmark requires at least one sample")
    return {
        "samples": len(samples),
        "minimum_seconds": min(samples),
        "median_seconds": percentile(samples, 0.50),
        "p95_seconds": percentile(samples, 0.95),
        "p99_seconds": percentile(samples, 0.99),
        "maximum_seconds": max(samples),
    }


def compare_results(
    baseline: dict[str, Any],
    observed: dict[str, Any],
    *,
    maximum_regression: float = DEFAULT_MAX_REGRESSION,
) -> Comparison:
    """Compare exact-profile p95 values and every declared absolute limit."""

    if not 0 <= maximum_regression <= 1:
        raise PerformanceContractError("maximum regression must be between zero and one")
    validate_result(baseline)
    validate_result(observed)
    baseline_profile = str(baseline["environment"]["profile_id"])
    observed_profile = str(observed["environment"]["profile_id"])
    baseline_fixture = str(baseline["fixture"]["sha256"])
    observed_fixture = str(observed["fixture"]["sha256"])
    memory_violations = _worker_memory_violations(observed)
    if baseline["suite_version"] != observed["suite_version"]:
        return Comparison(
            comparable=False,
            profile_id=observed_profile,
            fixture_sha256=observed_fixture,
            regressions=(),
            absolute_violations=_absolute_violations(observed),
            worker_memory_violations=memory_violations,
            reason="suite_version_mismatch",
        )
    if baseline_fixture != observed_fixture:
        return Comparison(
            comparable=False,
            profile_id=observed_profile,
            fixture_sha256=observed_fixture,
            regressions=(),
            absolute_violations=(),
            worker_memory_violations=memory_violations,
            reason="fixture_mismatch",
        )
    profile_differs = any(
        baseline["environment"][field] != observed["environment"][field]
        for field in _PROFILE_FIELDS
    )
    if baseline_profile != observed_profile or profile_differs:
        return Comparison(
            comparable=False,
            profile_id=observed_profile,
            fixture_sha256=observed_fixture,
            regressions=(),
            absolute_violations=_absolute_violations(observed),
            worker_memory_violations=memory_violations,
            reason="profile_mismatch",
        )

    baseline_benchmarks = baseline["benchmarks"]
    observed_benchmarks = observed["benchmarks"]
    if set(baseline_benchmarks) != set(observed_benchmarks):
        raise PerformanceContractError("baseline and observation benchmark sets differ")
    regressions: list[Regression] = []
    for name in sorted(baseline_benchmarks):
        baseline_p95 = _finite_number(
            baseline_benchmarks[name].get("p95_seconds"), f"{name}.p95_seconds"
        )
        observed_p95 = _finite_number(
            observed_benchmarks[name].get("p95_seconds"), f"{name}.p95_seconds"
        )
        if baseline_p95 <= 0:
            raise PerformanceContractError("baseline p95 values must be positive")
        change = (observed_p95 - baseline_p95) / baseline_p95
        if change > maximum_regression + 1e-12:
            regressions.append(
                Regression(
                    benchmark=name,
                    baseline_p95_seconds=baseline_p95,
                    observed_p95_seconds=observed_p95,
                    change_ratio=change,
                )
            )
    return Comparison(
        comparable=True,
        profile_id=observed_profile,
        fixture_sha256=observed_fixture,
        regressions=tuple(regressions),
        absolute_violations=_absolute_violations(observed),
        worker_memory_violations=memory_violations,
    )


def _worker_memory_violations(result: dict[str, Any]) -> tuple[str, ...]:
    memory = result["worker_memory"]
    violations: list[str] = []
    if memory.get("measured") is not True:
        violations.append("worker_memory_not_measured")
        return tuple(violations)
    if memory.get("jobs") != 100:
        violations.append("worker_memory_job_count_not_100")
    if memory.get("within_ten_percent") is not True:
        violations.append("worker_memory_not_within_ten_percent")
    if memory.get("monotonic_growth") is not False:
        violations.append("worker_memory_monotonic_growth")
    if memory.get("passed") is not True:
        violations.append("worker_memory_probe_failed")
    return tuple(violations)


def _absolute_violations(result: dict[str, Any]) -> tuple[AbsoluteViolation, ...]:
    violations: list[AbsoluteViolation] = []
    for name, benchmark in sorted(result["benchmarks"].items()):
        limits = benchmark.get("limits_seconds")
        if not isinstance(limits, dict) or not limits:
            raise PerformanceContractError(f"{name}.limits_seconds must be non-empty")
        for percentile_name, raw_limit in sorted(limits.items()):
            if percentile_name not in {"p95", "p99"}:
                raise PerformanceContractError(f"unsupported absolute percentile {percentile_name}")
            limit = _finite_number(raw_limit, f"{name}.{percentile_name}.limit")
            observed = _finite_number(
                benchmark.get(f"{percentile_name}_seconds"),
                f"{name}.{percentile_name}_seconds",
            )
            if limit <= 0:
                raise PerformanceContractError("absolute limits must be positive")
            if observed > limit:
                violations.append(
                    AbsoluteViolation(
                        benchmark=name,
                        percentile=percentile_name,
                        limit_seconds=limit,
                        observed_seconds=observed,
                    )
                )
    return tuple(violations)


def validate_result(value: dict[str, Any]) -> None:
    """Validate one clean, comparable performance-result document."""

    if value.get("schema_version") != RESULT_SCHEMA:
        raise PerformanceContractError("unsupported performance result schema")
    if value.get("suite_version") != 1:
        raise PerformanceContractError("unsupported performance suite version")
    environment = value.get("environment")
    fixture = value.get("fixture")
    benchmarks = value.get("benchmarks")
    protocol = value.get("measurement_protocol")
    memory = value.get("worker_memory")
    source = value.get("source")
    if not isinstance(environment, dict):
        raise PerformanceContractError("performance result requires an environment profile")
    for field in _PROFILE_FIELDS:
        field_value = environment.get(field)
        if field == "logical_cpu_count":
            if not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 1:
                raise PerformanceContractError("logical CPU count must be a positive integer")
        elif not isinstance(field_value, str) or not field_value.strip():
            raise PerformanceContractError(f"environment.{field} must be a non-empty string")
    if environment["profile_schema_version"] != PROFILE_SCHEMA:
        raise PerformanceContractError("unsupported performance profile schema")
    if not isinstance(fixture, dict) or not isinstance(fixture.get("sha256"), str):
        raise PerformanceContractError("performance result requires a fixture digest")
    if fixture.get("schema_version") != "corpuskit.performance-fixture.v1":
        raise PerformanceContractError("unsupported performance fixture schema")
    digest = fixture["sha256"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise PerformanceContractError("fixture digest must be lowercase SHA-256")
    if not isinstance(benchmarks, dict) or not benchmarks:
        raise PerformanceContractError("performance result requires benchmark measurements")
    if not isinstance(protocol, dict):
        raise PerformanceContractError("performance result requires a measurement protocol")
    if protocol.get("clock") != "perf_counter_ns" or protocol.get("percentile_method") != (
        "nearest_rank"
    ):
        raise PerformanceContractError("unsupported measurement protocol")
    samples_required = protocol.get("samples_per_benchmark")
    warmups = protocol.get("warmups_per_benchmark")
    if (
        not isinstance(samples_required, int)
        or isinstance(samples_required, bool)
        or samples_required < MINIMUM_COMPARABLE_SAMPLES
    ):
        raise PerformanceContractError("comparable evidence requires at least 20 samples")
    if (
        not isinstance(warmups, int)
        or isinstance(warmups, bool)
        or warmups < MINIMUM_COMPARABLE_WARMUPS
    ):
        raise PerformanceContractError("comparable evidence requires at least three warmups")
    if not isinstance(memory, dict) or memory.get("schema_version") != (
        "corpuskit.worker-memory-result.v1"
    ):
        raise PerformanceContractError("performance result requires worker-memory evidence")
    _validate_clean_source(source)
    for name, benchmark in benchmarks.items():
        if not isinstance(name, str) or not isinstance(benchmark, dict):
            raise PerformanceContractError("benchmark entries must be named objects")
        samples = benchmark.get("samples")
        if samples != samples_required or isinstance(samples, bool):
            raise PerformanceContractError(f"{name}.samples must match the protocol")
        for metric in (
            "minimum_seconds",
            "median_seconds",
            "p95_seconds",
            "p99_seconds",
            "maximum_seconds",
        ):
            _finite_number(benchmark.get(metric), f"{name}.{metric}")


def _validate_clean_source(value: object) -> None:
    if not isinstance(value, dict):
        raise PerformanceContractError("performance result requires source provenance")
    revision = value.get("git_revision")
    if (
        not isinstance(revision, str)
        or len(revision) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise PerformanceContractError("source revision must be an exact Git object ID")
    if value.get("git_dirty") is not False:
        raise PerformanceContractError("performance evidence requires a clean source checkout")
    for field in ("corpuskit_version", "corpusgen_version"):
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise PerformanceContractError(f"source.{field} must be a non-empty string")


def _finite_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PerformanceContractError(f"{label} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise PerformanceContractError(f"{label} must be finite and nonnegative")
    return normalized


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceContractError(f"cannot load {path}") from exc
    if not isinstance(value, dict):
        raise PerformanceContractError(f"{path} must contain a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("observed", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--maximum-regression", type=float, default=DEFAULT_MAX_REGRESSION)
    parser.add_argument("--enforce", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.output is not None:
        resolved_output = arguments.output.resolve()
        if resolved_output in {arguments.baseline.resolve(), arguments.observed.resolve()}:
            raise PerformanceContractError(
                "comparison output must not overwrite baseline or observation evidence"
            )
    comparison = compare_results(
        _load(arguments.baseline),
        _load(arguments.observed),
        maximum_regression=arguments.maximum_regression,
    )
    encoded = json.dumps(comparison.to_dict(), indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    enforcement_failed = arguments.enforce and comparison.comparable and not comparison.passed
    return 1 if enforcement_failed else 0


if __name__ == "__main__":  # pragma: no cover - exercised through the command contract
    raise SystemExit(main())
