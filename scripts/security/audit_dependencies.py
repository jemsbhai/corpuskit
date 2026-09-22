"""Audit a locked dependency graph without silently skipping reviewed local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[2]
_HASH = re.compile(r"\s+--hash[= ]sha256:([0-9a-f]{64})(?=\s|$)")


def _artifact_provenance(root: Path) -> dict[str, object]:
    from scripts.security.accelerate_patch import verify_artifact

    return verify_artifact(root)


def _installed_provenance(root: Path) -> dict[str, object]:
    from scripts.security.accelerate_patch import verify_installed

    return verify_installed(root)


def _requirement_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    pending = ""
    for line in text.splitlines(keepends=True):
        pending += line
        if not line.rstrip("\r\n").endswith("\\"):
            blocks.append(pending)
            pending = ""
    if pending:
        raise ValueError("Unterminated requirement continuation")
    return blocks


def _wheel_path(reference: str, root: Path) -> Path:
    parsed = urlsplit(reference)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise ValueError("Unsupported wheel URL")
        return Path(url2pathname(parsed.path)).resolve(strict=True)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Only the checked-in Accelerate wheel may be normalized")
    return (root / reference).resolve(strict=True)


def normalize_requirements(
    text: str, *, repository_root: Path = ROOT
) -> tuple[str, dict[str, str], list[dict[str, object]]]:
    """Preserve every ordinary pin; map only the verified local wheel to its upstream identity."""

    output: list[str] = []
    expected: dict[str, str] = {}
    provenance: list[dict[str, object]] = []
    hash_presence: list[bool] = []
    for block in _requirement_blocks(text):
        logical = re.sub(r"\\\r?\n", " ", block).strip()
        if not logical or logical.startswith("#"):
            output.append(block)
            continue
        logical = re.split(r"\s+#", logical, maxsplit=1)[0]
        hashes = _HASH.findall(logical)
        requirement_text = _HASH.sub("", logical).strip()
        hash_presence.append(bool(hashes))
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement:
            # uv exports a local wheel as a bare path instead of a PEP 508 name @ URL.
            if ".whl" not in requirement_text or requirement_text.startswith("-"):
                raise ValueError("Malformed or unsupported requirement") from None
            try:
                requirement = Requirement(f"accelerate @ {requirement_text}")
            except InvalidRequirement:
                raise ValueError("Malformed local wheel requirement") from None
        name = canonicalize_name(requirement.name)
        if requirement.url is not None:
            if name != "accelerate" or requirement.extras:
                raise ValueError("Unreviewed direct dependency cannot be audited")
            manifest = _artifact_provenance(repository_root)
            if manifest.get("name") != "accelerate" or manifest.get("upstream_version") != "1.15.0":
                raise ValueError("Unexpected patched dependency identity")
            wheel = repository_root / "vendor" / "accelerate" / str(manifest["wheel"])
            if _wheel_path(requirement.url, repository_root) != wheel.resolve(strict=True):
                raise ValueError("Requirement does not identify the verified Accelerate wheel")
            if hashes and set(hashes) != {manifest["sha256"]}:
                raise ValueError("Requirement hash does not match the verified wheel")
            normalized = "accelerate==1.15.0"
            if requirement.marker is not None:
                normalized += f" ; {requirement.marker}"
            if hashes:
                normalized += f" --hash=sha256:{manifest['upstream_sha256']}"
            output.append(normalized + "\n")
            provenance.append(manifest)
            version = "1.15.0"
        else:
            if name == "accelerate":
                raise ValueError("Accelerate must use the verified checked-in patched wheel")
            pins = list(requirement.specifier)
            if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
                raise ValueError(f"Dependency {name} is not pinned to an exact version")
            try:
                parsed_version = Version(pins[0].version)
            except InvalidVersion:
                raise ValueError(f"Dependency {name} has an invalid pinned version") from None
            if parsed_version.local is not None:
                raise ValueError(f"Unreviewed local version for {name}")
            version = str(parsed_version)
            output.append(block)
        if requirement.marker is None or requirement.marker.evaluate():
            if name in expected and expected[name] != version:
                raise ValueError(f"Conflicting pins for {name}")
            expected[name] = version
    if not expected:
        raise ValueError("Requirements contain no applicable dependencies")
    if any(hash_presence) and not all(hash_presence):
        raise ValueError("Hashed exports must include hashes for every requirement")
    return "".join(output), expected, provenance


def validate_audit_result(raw: object, expected: dict[str, str], returncode: int) -> None:
    """Require complete successful package results, without skips or advisory exemptions."""

    if not isinstance(raw, dict) or set(raw) != {"dependencies", "fixes"}:
        raise ValueError("Malformed pip-audit JSON result")
    dependencies = raw["dependencies"]
    if not isinstance(dependencies, list) or raw["fixes"] != []:
        raise ValueError("Malformed or unexpectedly modified audit results")
    observed: dict[str, str] = {}
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"name", "version", "vulns"}:
            raise ValueError("Skipped or malformed dependency in audit results")
        name_value, version_value = dependency["name"], dependency["version"]
        if not isinstance(name_value, str) or not isinstance(version_value, str):
            raise ValueError("Malformed dependency identity in audit results")
        name = canonicalize_name(name_value)
        if name in observed:
            raise ValueError(f"Duplicate audit result for {name}")
        if dependency["vulns"] != []:
            raise ValueError(f"Vulnerabilities or malformed findings reported for {name}")
        observed[name] = str(Version(version_value))
    if observed != expected:
        raise ValueError("Audit dependency identities do not exactly match the exported graph")
    if returncode != 0:
        raise ValueError(f"pip-audit exited unsuccessfully ({returncode})")


def audit_requirements(
    requirement_path: Path,
    output_path: Path,
    *,
    repository_root: Path = ROOT,
    verify_installed_accelerate: bool = False,
) -> dict[str, Any]:
    """Retain the input, normalized requirements, raw result and verification evidence."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {"schema": "corpuskit.dependency-audit.v1", "status": "failed"}
    try:
        original = requirement_path.read_bytes()
        evidence["input_sha256"] = hashlib.sha256(original).hexdigest()
        output_path.with_suffix(".input-requirements.txt").write_bytes(original)
        normalized, expected, provenance = normalize_requirements(
            original.decode("utf-8"), repository_root=repository_root
        )
        evidence["artifact_verification"] = provenance
        evidence["expected_dependencies"] = expected
        if verify_installed_accelerate:
            if not provenance or "accelerate" not in expected:
                raise ValueError(
                    "Installed verification requires the patched Accelerate requirement"
                )
            evidence["installed_verification"] = _installed_provenance(repository_root)
        normalized_path = output_path.with_suffix(".requirements.txt").resolve()
        normalized_bytes = normalized.encode("utf-8")
        normalized_path.write_bytes(normalized_bytes)
        evidence["normalized_sha256"] = hashlib.sha256(normalized_bytes).hexdigest()
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "--requirement",
            str(normalized_path),
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--format",
            "json",
            "--progress-spinner",
            "off",
        ]
        evidence["pip_audit_version"] = importlib.metadata.version("pip-audit")
        evidence["command"] = command
        completed = subprocess.run(  # noqa: S603 - fixed module/flags; requirements remain data.
            command, capture_output=True, text=True, check=False, timeout=600, cwd=repository_root
        )
        evidence["returncode"] = completed.returncode
        evidence["stderr"] = completed.stderr
        output_path.with_suffix(".pip-audit.json").write_text(completed.stdout, encoding="utf-8")
        result = json.loads(completed.stdout)
        evidence["pip_audit"] = result
        validate_audit_result(result, expected, completed.returncode)
        evidence["status"] = "passed"
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        importlib.metadata.PackageNotFoundError,
    ) as exc:
        evidence["error"] = str(exc)
    output_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify-installed-accelerate", action="store_true")
    args = parser.parse_args(argv)
    result = audit_requirements(
        args.requirements,
        args.output,
        verify_installed_accelerate=args.verify_installed_accelerate,
    )
    if result["status"] != "passed":
        sys.stderr.write(str(result.get("error", "Dependency audit failed")) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
