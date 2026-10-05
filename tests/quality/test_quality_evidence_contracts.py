"""Deterministic tests for scheduled/release quality-evidence policy."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.performance.baseline_policy import (  # noqa: E402
    BaselinePolicyError,
    RepositoryState,
    evaluate_policy,
)
from scripts.performance.benchmark_contract import PROFILE_SCHEMA, RESULT_SCHEMA  # noqa: E402
from scripts.quality.coverage_contract import (  # noqa: E402
    CoverageContractError,
    evaluate_coverage,
)
from scripts.quality.junit_contract import JunitContractError, evaluate_junit  # noqa: E402

PROFILE = "github-actions-ubuntu-24.04-x64"


def test_root_quality_scripts_forward_to_the_web_workspace() -> None:
    package = json.loads((REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["lint"] == "npm run lint --workspace @corpuskit/web --"
    assert package["scripts"]["test"] == "npm run test --workspace @corpuskit/web --"
    assert package["scripts"]["test:auth"] == "npm run test:auth --workspace @corpuskit/web --"
    assert package["scripts"]["test:e2e"] == "npm run test:e2e --workspace @corpuskit/web --"
    assert package["scripts"]["test:workbenches"] == (
        "npm run test:workbenches --workspace @corpuskit/web --"
    )


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        pytest.param([], False, id="no-ci-evidence"),
        pytest.param([("push", "success", "a")], True, id="exact-sha-push"),
        pytest.param([("workflow_dispatch", "success", "a")], True, id="exact-sha-dispatch"),
        pytest.param([("pull_request", "success", "a")], False, id="pr-tests-merge-not-head"),
        pytest.param([("pull_request_target", "success", "a")], False, id="pr-target-tests-base"),
        pytest.param([("workflow_run", "success", "a")], False, id="untrusted-event"),
        pytest.param([("push", "failure", "a")], False, id="failed-ci"),
        pytest.param([("workflow_dispatch", None, "a")], False, id="unfinished-ci"),
        pytest.param([("push", "success", "b")], False, id="other-commit"),
        pytest.param(
            [("pull_request", "success", "a"), ("push", "failure", "a")],
            False,
            id="pr-success-cannot-rescue-failed-push",
        ),
        pytest.param(
            [("push", "failure", "a"), ("workflow_dispatch", "success", "a")],
            True,
            id="successful-dispatch-after-failed-push",
        ),
    ],
)
def test_scheduled_gate_requires_successful_ci_for_the_actual_checkout(
    runs: list[tuple[str, str | None, str]], expected: bool
) -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required to execute the scheduled workflow's actual CI evidence filter")

    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/quality-scheduled.yml").read_text(encoding="utf-8")
    )
    gate = workflow["jobs"]["exact-sha-ci"]["steps"][0]["run"]
    expression = re.search(r"jq -e --arg sha [^\n]+ '\n(.*?)\n\s*' <<<", gate, re.DOTALL)
    assert expression is not None, "The scheduled gate must expose its jq evidence filter"
    response = {
        "workflow_runs": [
            {"event": event, "conclusion": conclusion, "head_sha": character * 40}
            for event, conclusion, character in runs
        ]
    }
    result = subprocess.run(  # noqa: S603 - fixed executable; repository-owned filter, no shell.
        [jq, "-e", "--arg", "sha", "a" * 40, expression.group(1)],
        input=json.dumps(response),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == (0 if expected else 1), result.stderr
    assert json.loads(result.stdout) is expected


def _baseline() -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "suite_version": 1,
        "environment": {
            "profile_schema_version": PROFILE_SCHEMA,
            "profile_id": PROFILE,
            "os": "Linux",
            "os_release": "6",
            "architecture": "x86_64",
            "python": "3.12.13",
            "python_implementation": "CPython",
            "cpu_model": "Fixture CPU",
            "logical_cpu_count": 4,
        },
        "source": {
            "git_revision": "a" * 40,
            "git_dirty": False,
            "corpuskit_version": "0.1.0-alpha.1",
            "corpusgen_version": "0.1.7",
        },
        "fixture": {
            "schema_version": "corpuskit.performance-fixture.v1",
            "sha256": "b" * 64,
        },
        "measurement_protocol": {
            "clock": "perf_counter_ns",
            "percentile_method": "nearest_rank",
            "samples_per_benchmark": 20,
            "warmups_per_benchmark": 3,
        },
        "benchmarks": {
            "fixture": {
                "samples": 20,
                "minimum_seconds": 0.1,
                "median_seconds": 0.2,
                "p95_seconds": 0.3,
                "p99_seconds": 0.4,
                "maximum_seconds": 0.4,
                "limits_seconds": {"p95": 1.0},
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


def test_baseline_bootstrap_exception_is_scheduled_only_and_root_commit_only() -> None:
    unborn = RepositoryState(head_revision=None, parent_count=None)
    root = RepositoryState(head_revision="c" * 40, parent_count=0)
    later = RepositoryState(head_revision="d" * 40, parent_count=1)

    assert (
        evaluate_policy(
            None,
            mode="scheduled",
            expected_profile=PROFILE,
            repository=unborn,
            baseline_revision_is_ancestor=None,
        )["passed"]
        is True
    )
    assert (
        evaluate_policy(
            None,
            mode="scheduled",
            expected_profile=PROFILE,
            repository=root,
            baseline_revision_is_ancestor=None,
        )["passed"]
        is True
    )
    assert (
        evaluate_policy(
            None,
            mode="scheduled",
            expected_profile=PROFILE,
            repository=later,
            baseline_revision_is_ancestor=None,
        )["passed"]
        is False
    )
    assert (
        evaluate_policy(
            None,
            mode="release",
            expected_profile=PROFILE,
            repository=root,
            baseline_revision_is_ancestor=None,
        )["passed"]
        is False
    )


def test_approved_baseline_requires_exact_profile_clean_schema_and_ancestry() -> None:
    repository = RepositoryState(head_revision="c" * 40, parent_count=1)
    evidence = evaluate_policy(
        _baseline(),
        mode="release",
        expected_profile=PROFILE,
        repository=repository,
        baseline_revision_is_ancestor=True,
    )
    assert evidence["passed"] is True
    assert evidence["bootstrap_exception"] is False

    with pytest.raises(BaselinePolicyError, match="does not match"):
        evaluate_policy(
            _baseline(),
            mode="release",
            expected_profile="other",
            repository=repository,
            baseline_revision_is_ancestor=True,
        )
    with pytest.raises(BaselinePolicyError, match="not an ancestor"):
        evaluate_policy(
            _baseline(),
            mode="release",
            expected_profile=PROFILE,
            repository=repository,
            baseline_revision_is_ancestor=False,
        )


def test_coverage_contract_enforces_line_and_branch_rates_independently() -> None:
    value = {
        "meta": {"branch_coverage": True},
        "totals": {
            "covered_lines": 90,
            "num_statements": 100,
            "covered_branches": 85,
            "num_branches": 100,
        },
    }
    result = evaluate_coverage(
        value,
        minimum_line_percent=90,
        minimum_branch_percent=85,
    )
    assert result.passed is True

    value["totals"]["covered_branches"] = 84
    assert (
        evaluate_coverage(
            value,
            minimum_line_percent=90,
            minimum_branch_percent=85,
        ).passed
        is False
    )


def test_coverage_contract_rejects_non_branch_or_impossible_evidence() -> None:
    with pytest.raises(CoverageContractError, match="branch measurement"):
        evaluate_coverage(
            {"meta": {"branch_coverage": False}, "totals": {}},
            minimum_line_percent=90,
            minimum_branch_percent=85,
        )
    with pytest.raises(CoverageContractError, match="cannot exceed"):
        evaluate_coverage(
            {
                "meta": {"branch_coverage": True},
                "totals": {
                    "covered_lines": 2,
                    "num_statements": 1,
                    "covered_branches": 1,
                    "num_branches": 1,
                },
            },
            minimum_line_percent=90,
            minimum_branch_percent=85,
        )


def test_junit_contract_requires_non_skipped_acceptance_cases(tmp_path: Path) -> None:
    passing = tmp_path / "passing.xml"
    passing.write_text(
        '<testsuites><testsuite><testcase name="one"/>'
        '<testcase name="two"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    result = evaluate_junit(passing)
    assert result.passed is True
    assert result.tests == 2

    skipped = tmp_path / "skipped.xml"
    skipped.write_text(
        '<testsuite><testcase name="one"><skipped/></testcase></testsuite>',
        encoding="utf-8",
    )
    assert evaluate_junit(skipped).passed is False


def test_junit_contract_rejects_empty_or_malformed_evidence(tmp_path: Path) -> None:
    empty = tmp_path / "empty.xml"
    empty.write_text("<testsuite/>", encoding="utf-8")
    with pytest.raises(JunitContractError, match="at least one"):
        evaluate_junit(empty)

    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<not-junit/>", encoding="utf-8")
    with pytest.raises(JunitContractError, match="root"):
        evaluate_junit(malformed)


def test_ci_runs_only_for_updates_and_quality_uses_the_completed_push_sha() -> None:
    workflows = REPOSITORY_ROOT / ".github/workflows"
    for path in workflows.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        trigger = workflow.get("on", workflow.get(True, {}))
        assert "schedule" not in trigger, path.name
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    assert set(ci.get("on", ci.get(True))) == {"push", "pull_request"}
    assert ci.get("on", ci.get(True))["pull_request"]["types"] == ["opened", "synchronize"]
    deployment = yaml.safe_load((workflows / "deployment.yml").read_text(encoding="utf-8"))
    deployment_trigger = deployment.get("on", deployment.get(True))
    assert set(deployment_trigger) == {"push", "pull_request"}
    assert deployment_trigger["pull_request"]["types"] == ["opened", "synchronize"]
    quality = yaml.safe_load((workflows / "quality-scheduled.yml").read_text(encoding="utf-8"))
    assert quality.get("on", quality.get(True)) == {
        "workflow_run": {"workflows": ["CI"], "branches": ["main"], "types": ["completed"]}
    }
    gate = quality["jobs"]["exact-sha-ci"]
    assert "if" not in gate
    script = gate["steps"][0]["run"]
    assert 'test "${SOURCE_CONCLUSION}" = success' in script
    assert 'test "${SOURCE_EVENT}" = push' in script
    assert 'test "${SOURCE_ATTEMPT}" = 1' in script
    for job in quality["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
