"""Deterministic tests for benchmark fixtures, evidence, and comparison semantics."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.performance.benchmark_contract import (  # noqa: E402
    PROFILE_SCHEMA,
    RESULT_SCHEMA,
    PerformanceContractError,
    compare_results,
    main,
    percentile,
    summarize,
)
from scripts.performance.run_benchmarks import (  # noqa: E402
    BenchmarkFixtureError,
    _benchmark_app,
    _memory_probe,
    expanded_evaluation_sentences,
    export_inputs,
    has_monotonic_memory_growth,
    load_fixture,
    selection_inputs,
)

from corpuskit.domain.jobs import RunKind  # noqa: E402
from corpuskit.services.rate_limits import DisabledRateLimiter  # noqa: E402
from corpuskit.workflows.handlers import HandlerRegistry  # noqa: E402

FIXTURE = Path("tests/fixtures/performance/v1.json")


def test_benchmark_app_explicitly_disables_rate_limiting() -> None:
    app = _benchmark_app()

    assert isinstance(app.state.rate_limiter, DisabledRateLimiter)


@pytest.mark.asyncio
async def test_memory_probe_composes_evaluate_registry_without_artifact_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, _digest = load_fixture(FIXTURE)
    evaluation_sentences = expanded_evaluation_sentences(fixture)
    executions: list[RunKind] = []

    class StubProcessExecutionRunner:
        def __init__(
            self,
            handlers: HandlerRegistry,
            *,
            hard_timeout_seconds: float,
        ) -> None:
            assert RunKind.EVALUATE in handlers.kinds
            assert RunKind.SELECT in handlers.kinds
            assert hard_timeout_seconds == 60.0

        async def execute(
            self,
            kind: RunKind,
            spec: dict[str, Any],
            *,
            tick: Any,
            tick_seconds: float,
        ) -> dict[str, Any]:
            assert kind is RunKind.EVALUATE
            assert tick_seconds == 0.25
            await tick()
            executions.append(kind)
            return {"total_sentences": len(spec["sentences"])}

    class FakeMemoryInfo:
        rss = 100_000_000

    class FakeProcess:
        def memory_info(self) -> FakeMemoryInfo:
            return FakeMemoryInfo()

    fake_psutil = ModuleType("psutil")
    fake_psutil.Process = FakeProcess  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(
        "scripts.performance.run_benchmarks.ProcessExecutionRunner",
        StubProcessExecutionRunner,
    )

    result = await _memory_probe(fixture, evaluation_sentences)

    assert executions == [RunKind.EVALUATE] * 105
    assert result["jobs"] == 100
    assert result["within_ten_percent"] is True
    assert result["monotonic_growth"] is False
    assert result["passed"] is True


def _result(*, profile: str = "reference", p95: float = 1.0) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "suite_version": 1,
        "environment": {
            "profile_schema_version": PROFILE_SCHEMA,
            "profile_id": profile,
            "os": "TestOS",
            "os_release": "1",
            "architecture": "x86_64",
            "python": "3.12.2",
            "python_implementation": "CPython",
            "cpu_model": "Fixture CPU",
            "logical_cpu_count": 8,
        },
        "fixture": {
            "schema_version": "corpuskit.performance-fixture.v1",
            "sha256": "a" * 64,
        },
        "source": {
            "git_revision": "b" * 40,
            "git_dirty": False,
            "corpuskit_version": "0.1.0-alpha.1",
            "corpusgen_version": "0.1.1",
        },
        "measurement_protocol": {
            "clock": "perf_counter_ns",
            "percentile_method": "nearest_rank",
            "samples_per_benchmark": 20,
            "warmups_per_benchmark": 3,
        },
        "benchmarks": {
            "operation": {
                "samples": 20,
                "minimum_seconds": 0.5,
                "median_seconds": 0.75,
                "p95_seconds": p95,
                "p99_seconds": p95,
                "maximum_seconds": p95,
                "limits_seconds": {"p95": 2.0, "p99": 2.5},
            }
        },
        "worker_memory": {
            "schema_version": "corpuskit.worker-memory-result.v1",
            "measured": True,
            "jobs": 100,
            "within_ten_percent": True,
            "monotonic_growth": False,
            "passed": True,
        },
    }


def test_nearest_rank_summary_is_stable_and_rejects_invalid_samples() -> None:
    samples = tuple(float(value) for value in range(1, 21))
    assert percentile(samples, 0.50) == 10.0
    assert percentile(samples, 0.95) == 19.0
    assert percentile(samples, 0.99) == 20.0
    assert summarize(samples) == {
        "samples": 20,
        "minimum_seconds": 1.0,
        "median_seconds": 10.0,
        "p95_seconds": 19.0,
        "p99_seconds": 20.0,
        "maximum_seconds": 20.0,
    }
    with pytest.raises(PerformanceContractError):
        percentile((), 0.95)
    with pytest.raises(PerformanceContractError):
        percentile((float("nan"),), 0.95)


def test_comparator_enforces_exact_profile_fixture_and_more_than_ten_percent() -> None:
    baseline = _result(p95=1.0)
    at_boundary = compare_results(baseline, _result(p95=1.1))
    regressed = compare_results(baseline, _result(p95=1.100_001))

    assert at_boundary.passed is True
    assert not at_boundary.regressions
    assert regressed.passed is False
    assert regressed.regressions[0].benchmark == "operation"
    assert regressed.regressions[0].change_ratio == pytest.approx(0.100_001)

    mismatch = compare_results(baseline, _result(profile="different"))
    assert mismatch.comparable is False
    assert mismatch.reason == "profile_mismatch"

    other_fixture = _result()
    other_fixture["fixture"]["sha256"] = "b" * 64
    assert compare_results(baseline, other_fixture).reason == "fixture_mismatch"

    different_platform = _result()
    different_platform["environment"]["os_release"] = "2"
    assert compare_results(baseline, different_platform).reason == "profile_mismatch"


def test_comparator_rejects_weak_sampling_and_failed_memory() -> None:
    weak = _result()
    weak["measurement_protocol"]["samples_per_benchmark"] = 5
    weak["benchmarks"]["operation"]["samples"] = 5
    with pytest.raises(PerformanceContractError, match="at least 20"):
        compare_results(weak, weak)

    observed = _result()
    observed["worker_memory"]["within_ten_percent"] = False
    observed["worker_memory"]["passed"] = False
    comparison = compare_results(_result(), observed)
    assert comparison.passed is False
    assert comparison.worker_memory_violations == (
        "worker_memory_not_within_ten_percent",
        "worker_memory_probe_failed",
    )


def test_comparator_rejects_dirty_or_unidentified_source_evidence() -> None:
    dirty = _result()
    dirty["source"]["git_dirty"] = True
    with pytest.raises(PerformanceContractError, match="clean source"):
        compare_results(dirty, _result())

    unidentified = _result()
    unidentified["source"]["git_revision"] = "uncommitted"
    with pytest.raises(PerformanceContractError, match="Git object ID"):
        compare_results(_result(), unidentified)


def test_absolute_limits_are_reported_independently_of_relative_change() -> None:
    baseline = _result(p95=2.6)
    observed = _result(p95=2.6)
    comparison = compare_results(baseline, observed)
    assert not comparison.regressions
    assert comparison.absolute_violations[0].percentile == "p95"
    assert comparison.absolute_violations[1].percentile == "p99"
    assert comparison.passed is False


def test_cli_never_modifies_approved_baseline(tmp_path: Path) -> None:
    baseline_path = tmp_path / "approved.json"
    observed_path = tmp_path / "observed.json"
    comparison_path = tmp_path / "comparison.json"
    baseline_bytes = (json.dumps(_result(), sort_keys=True) + "\n").encode()
    baseline_path.write_bytes(baseline_bytes)
    observed_path.write_text(json.dumps(_result(p95=1.05)), encoding="utf-8")

    assert main([str(baseline_path), str(observed_path), "--output", str(comparison_path)]) == 0
    assert baseline_path.read_bytes() == baseline_bytes
    assert json.loads(comparison_path.read_text(encoding="utf-8"))["passed"] is True

    with pytest.raises(PerformanceContractError, match="must not overwrite"):
        main([str(baseline_path), str(observed_path), "--output", str(baseline_path)])


def test_cli_does_not_fail_a_noncomparable_profile(tmp_path: Path) -> None:
    baseline_path = tmp_path / "approved.json"
    observed_path = tmp_path / "observed.json"
    baseline_path.write_text(json.dumps(_result()), encoding="utf-8")
    observed_path.write_text(json.dumps(_result(profile="other")), encoding="utf-8")

    assert main([str(baseline_path), str(observed_path), "--enforce"]) == 0


def test_versioned_fixture_has_exact_acceptance_cardinalities_and_stable_digest() -> None:
    fixture, first_digest = load_fixture(FIXTURE)
    repeated_fixture, second_digest = load_fixture(FIXTURE)
    candidates, phonemes, targets, maximum = selection_inputs(fixture)
    version, sentences = export_inputs(fixture)

    assert fixture == repeated_fixture
    assert first_digest == second_digest
    assert len(first_digest) == 64
    assert len(expanded_evaluation_sentences(fixture)) == 100
    assert len(candidates) == len(phonemes) == 1_000
    assert targets == {"p", "b", "t", "k", "m", "n", "s", "z"}
    assert maximum == 32
    assert version.sentence_count == len(sentences) == 10_000
    assert sentences[0].ordinal == 0
    assert sentences[-1].ordinal == 9_999


def test_fixture_rejects_cardinality_drift(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["selection"]["candidate_count"] = 999
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(BenchmarkFixtureError, match="candidate_count"):
        load_fixture(invalid)


def test_memory_growth_rule_ignores_noise_but_flags_material_monotonic_growth() -> None:
    assert has_monotonic_memory_growth((101, 102, 103), 100) is True
    assert has_monotonic_memory_growth((101, 100, 103), 100) is False
    assert has_monotonic_memory_growth((100, 101, 102), 100) is False
    with pytest.raises(BenchmarkFixtureError):
        has_monotonic_memory_growth((1, 2), 1)
