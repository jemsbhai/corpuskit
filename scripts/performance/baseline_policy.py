"""Fail-closed lifecycle policy for approved performance baselines.

The one bootstrap exception is deliberately mechanical: scheduled automation may run
without a baseline only before Git has a HEAD or at the repository's root commit. A
release never receives that exception.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from scripts.performance.benchmark_contract import PerformanceContractError, validate_result

POLICY_SCHEMA = "corpuskit.performance-baseline-policy.v1"
PolicyMode = Literal["scheduled", "release"]


class BaselinePolicyError(ValueError):
    """Raised when repository state cannot support baseline evidence."""


@dataclass(frozen=True, slots=True)
class RepositoryState:
    head_revision: str | None
    parent_count: int | None

    @property
    def is_bootstrap(self) -> bool:
        return self.head_revision is None or self.parent_count == 0


def evaluate_policy(
    baseline: dict[str, Any] | None,
    *,
    mode: PolicyMode,
    expected_profile: str,
    repository: RepositoryState,
    baseline_revision_is_ancestor: bool | None,
) -> dict[str, object]:
    """Evaluate the baseline state without consulting Git or the filesystem."""

    if not expected_profile.strip():
        raise BaselinePolicyError("expected profile must be non-empty")
    if baseline is None:
        bootstrap_allowed = mode == "scheduled" and repository.is_bootstrap
        reason = (
            "repository_unborn"
            if bootstrap_allowed and repository.head_revision is None
            else "initial_root_commit"
            if bootstrap_allowed
            else "approved_baseline_missing"
        )
        return {
            "schema_version": POLICY_SCHEMA,
            "mode": mode,
            "passed": bootstrap_allowed,
            "baseline_present": False,
            "bootstrap_exception": bootstrap_allowed,
            "reason": reason,
            "expected_profile": expected_profile,
            "head_revision": repository.head_revision,
        }

    try:
        validate_result(baseline)
    except PerformanceContractError as exc:
        raise BaselinePolicyError(str(exc)) from exc
    profile = baseline["environment"]["profile_id"]
    if profile != expected_profile:
        raise BaselinePolicyError(
            f"approved baseline profile {profile!r} does not match {expected_profile!r}"
        )
    if repository.head_revision is None:
        raise BaselinePolicyError("a baseline cannot be approved before the repository has HEAD")
    if baseline_revision_is_ancestor is not True:
        raise BaselinePolicyError("baseline source revision is not an ancestor of HEAD")
    return {
        "schema_version": POLICY_SCHEMA,
        "mode": mode,
        "passed": True,
        "baseline_present": True,
        "bootstrap_exception": False,
        "reason": "approved_baseline_valid",
        "expected_profile": expected_profile,
        "head_revision": repository.head_revision,
        "baseline_revision": baseline["source"]["git_revision"],
    }


def load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise BaselinePolicyError("baseline path must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselinePolicyError("baseline is not readable JSON") from exc
    if not isinstance(value, dict):
        raise BaselinePolicyError("baseline must contain a JSON object")
    return value


def repository_state() -> RepositoryState:
    git = shutil.which("git")
    if git is None:
        raise BaselinePolicyError("Git is required to evaluate baseline provenance")
    head = _git(git, "rev-parse", "--verify", "HEAD", allow_failure=True)
    if head is None:
        return RepositoryState(head_revision=None, parent_count=None)
    if not _is_object_id(head):
        raise BaselinePolicyError("HEAD is not an exact Git object ID")
    parents = _git(git, "rev-list", "--parents", "-n", "1", "HEAD")
    if parents is None:
        raise BaselinePolicyError("cannot inspect HEAD parents")
    tokens = parents.split()
    if not tokens or tokens[0] != head:
        raise BaselinePolicyError("Git returned inconsistent HEAD ancestry")
    return RepositoryState(head_revision=head, parent_count=len(tokens) - 1)


def baseline_revision_is_ancestor(baseline: dict[str, Any], repository: RepositoryState) -> bool:
    if repository.head_revision is None:
        return False
    source = baseline.get("source")
    if not isinstance(source, dict):
        return False
    revision = source.get("git_revision")
    if not isinstance(revision, str) or not _is_object_id(revision):
        return False
    git = shutil.which("git")
    if git is None:
        raise BaselinePolicyError("Git is required to evaluate baseline ancestry")
    command = subprocess.run(  # noqa: S603 - Git executable resolved from PATH.
        [git, "merge-base", "--is-ancestor", revision, repository.head_revision],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if command.returncode not in {0, 1}:
        raise BaselinePolicyError("cannot verify baseline revision ancestry")
    return command.returncode == 0


def _git(
    executable: str,
    *arguments: str,
    allow_failure: bool = False,
) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - Git executable resolved from PATH.
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BaselinePolicyError("Git baseline inspection failed") from exc
    if result.returncode != 0:
        if allow_failure:
            return None
        raise BaselinePolicyError("Git baseline inspection failed")
    return result.stdout.strip()


def _is_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--expected-profile", required=True)
    parser.add_argument("--mode", choices=("scheduled", "release"), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    baseline = load_baseline(arguments.baseline)
    repository = repository_state()
    ancestor = baseline_revision_is_ancestor(baseline, repository) if baseline is not None else None
    evidence = evaluate_policy(
        baseline,
        mode=arguments.mode,
        expected_profile=arguments.expected_profile,
        repository=repository,
        baseline_revision_is_ancestor=ancestor,
    )
    encoded = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 1 if arguments.enforce and not bool(evidence["passed"]) else 0


if __name__ == "__main__":  # pragma: no cover - exercised by workflow contract
    raise SystemExit(main())
