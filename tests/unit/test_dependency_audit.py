"""Audits must account for every exported dependency and every local artifact."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.security import audit_dependencies as audit  # noqa: E402


@pytest.fixture
def approved_wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    manifest: dict[str, object] = {
        "name": "accelerate",
        "upstream_version": "1.15.0",
        "patched_version": "1.15.0+corpuskit.1",
        "wheel": "accelerate-1.15.0+corpuskit.1-py3-none-any.whl",
        "sha256": "a" * 64,
        "upstream_sha256": "b" * 64,
    }
    wheel = tmp_path / "vendor" / "accelerate" / str(manifest["wheel"])
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"verified fixture wheel")
    monkeypatch.setattr(audit, "_artifact_provenance", lambda _root: manifest)
    return manifest


def clean_result(expected: dict[str, str]) -> dict[str, object]:
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": []} for name, version in expected.items()
        ],
        "fixes": [],
    }


def test_normalizes_only_verified_wheel_and_preserves_other_hashes_and_markers(
    tmp_path: Path, approved_wheel: dict[str, object]
) -> None:
    other = (
        "# pinned graph\r\n"
        "packaging==26.2 ; python_version >= '3.12' \\\r\n"
        f"    --hash=sha256:{'c' * 64}\r\n"
        "    # via caller\r\n"
    )
    text = (
        other + f"./vendor/accelerate/{approved_wheel['wheel']} \\\n    --hash=sha256:{'a' * 64}\n"
    )
    normalized, expected, provenance = audit.normalize_requirements(text, repository_root=tmp_path)
    assert normalized.startswith(other)
    assert normalized.endswith(f"accelerate==1.15.0 --hash=sha256:{'b' * 64}\n")
    assert expected == {"packaging": "26.2", "accelerate": "1.15.0"}
    assert provenance == [approved_wheel]


def test_pep508_wheel_and_inactive_environment_marker(
    tmp_path: Path, approved_wheel: dict[str, object]
) -> None:
    wheel_uri = (tmp_path / "vendor" / "accelerate" / str(approved_wheel["wheel"])).as_uri()
    text = f"accelerate @ {wheel_uri}\ncolorama==0.4.6 ; python_version < '1'\n"
    normalized, expected, _ = audit.normalize_requirements(text, repository_root=tmp_path)
    assert normalized.startswith("accelerate==1.15.0\n")
    assert "colorama==0.4.6 ; python_version < '1'" in normalized
    assert expected == {"accelerate": "1.15.0"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "packaging>=26.0\n",
        "packaging==26.*\n",
        "packaging==26.0+unreviewed\n",
        "accelerate==1.15.0\n",
        "accelerate==1.15.0+corpuskit.1\n",
        "packaging @ https://example.com/packaging.whl\n",
        "-r another-requirements.txt\n",
        "--index-url https://example.com\n",
        "not valid requirement\n",
        "packaging==26.0 \\\n",
        "packaging==26.0\npackaging==25.0\n",
        f"packaging==26.0 --hash=sha256:{'c' * 64}\ncolorama==0.4.6\n",
        "packaging==26.0 --hash=sha256:not-a-hash\n",
    ],
)
def test_rejects_unresolved_malformed_and_unreviewed_requirements(text: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        audit.normalize_requirements(text)


def test_rejects_other_local_wheel_even_with_approved_filename(
    tmp_path: Path, approved_wheel: dict[str, object]
) -> None:
    other_wheel = tmp_path / str(approved_wheel["wheel"])
    other_wheel.write_bytes(b"not the verified wheel")
    with pytest.raises(ValueError, match="does not identify"):
        audit.normalize_requirements(f"./{other_wheel.name}\n", repository_root=tmp_path)


def test_rejects_mismatched_artifact_hash(
    tmp_path: Path, approved_wheel: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="hash does not match"):
        audit.normalize_requirements(
            f"./vendor/accelerate/{approved_wheel['wheel']} --hash=sha256:{'c' * 64}\n",
            repository_root=tmp_path,
        )


@pytest.mark.parametrize(
    "result",
    [
        [],
        {},
        {"dependencies": [], "fixes": []},
        {"dependencies": [{"name": "accelerate", "skip_reason": "unavailable"}], "fixes": []},
        {"dependencies": [{"name": "accelerate", "version": "1.15.0", "vulns": [{}]}], "fixes": []},
        {"dependencies": [{"name": "accelerate", "version": "1.15.0", "vulns": None}], "fixes": []},
        clean_result({"accelerate": "1.14.0"}),
        clean_result({"accelerate": "1.15.0", "unknown": "1.0"}),
        {"dependencies": [], "fixes": [], "error": "failed"},
    ],
)
def test_rejects_skipped_missing_unexpected_or_vulnerable_audit_results(result: object) -> None:
    with pytest.raises(ValueError, match=r".+"):
        audit.validate_audit_result(result, {"accelerate": "1.15.0"}, 0)


def test_rejects_nonzero_exit_even_with_complete_clean_json() -> None:
    expected = {"packaging": "26.2"}
    with pytest.raises(ValueError, match="exited unsuccessfully"):
        audit.validate_audit_result(clean_result(expected), expected, 2)


@pytest.mark.parametrize(("returncode", "stdout"), [(0, "malformed"), (2, ""), (0, "{}")])
def test_retains_raw_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    requirements = tmp_path / "export.txt"
    requirements.write_text("packaging==26.2\n", encoding="utf-8")
    output = tmp_path / "evidence.json"
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout, "diagnostic"),
    )
    result = audit.audit_requirements(requirements, output, repository_root=tmp_path)
    assert result["status"] == "failed"
    assert result["stderr"] == "diagnostic"
    assert output.with_suffix(".pip-audit.json").read_text(encoding="utf-8") == stdout
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "failed"


def test_full_audit_records_provenance_and_requires_installed_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved_wheel: dict[str, object]
) -> None:
    requirements = tmp_path / "export.txt"
    original = f"./vendor/accelerate/{approved_wheel['wheel']}\npackaging==26.2\n"
    requirements.write_text(original, encoding="utf-8")
    expected = {"accelerate": "1.15.0", "packaging": "26.2"}
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert "--no-deps" in command
        assert "--disable-pip" in command
        assert "--strict" in command
        assert "--ignore-vuln" not in command
        return subprocess.CompletedProcess(command, 0, json.dumps(clean_result(expected)), "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    monkeypatch.setattr(audit, "_installed_provenance", lambda _root: {"installed": "verified"})
    output = tmp_path / "audit.json"
    result = audit.audit_requirements(
        requirements, output, repository_root=tmp_path, verify_installed_accelerate=True
    )
    assert result["status"] == "passed"
    assert result["artifact_verification"] == [approved_wheel]
    assert result["installed_verification"] == {"installed": "verified"}
    assert output.with_suffix(".input-requirements.txt").read_text(encoding="utf-8") == original
    assert len(calls) == 1


def test_artifact_verification_failure_never_invokes_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requirements = tmp_path / "export.txt"
    requirements.write_text("./vendor/accelerate/modified.whl\n", encoding="utf-8")

    def reject(_root: Path) -> dict[str, object]:
        raise ValueError("Wheel RECORD does not match")

    monkeypatch.setattr(audit, "_artifact_provenance", reject)
    monkeypatch.setattr(audit.subprocess, "run", lambda *_a, **_kw: pytest.fail("audit started"))
    result = audit.audit_requirements(
        requirements, tmp_path / "audit.json", repository_root=tmp_path
    )
    assert result["status"] == "failed"
    assert result["error"] == "Wheel RECORD does not match"


def test_installed_file_tampering_blocks_audit_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved_wheel: dict[str, object]
) -> None:
    requirements = tmp_path / "export.txt"
    requirements.write_text(f"./vendor/accelerate/{approved_wheel['wheel']}\n", encoding="utf-8")

    def reject(_root: Path) -> dict[str, object]:
        raise ValueError("Installed file differs from verified wheel")

    monkeypatch.setattr(audit, "_installed_provenance", reject)
    monkeypatch.setattr(audit.subprocess, "run", lambda *_a, **_kw: pytest.fail("audit started"))
    result = audit.audit_requirements(
        requirements,
        tmp_path / "audit.json",
        repository_root=tmp_path,
        verify_installed_accelerate=True,
    )
    assert result["status"] == "failed"
    assert result["artifact_verification"] == [approved_wheel]
    assert result["error"] == "Installed file differs from verified wheel"


def test_rejects_duplicate_or_rewritten_audit_results() -> None:
    expected = {"packaging": "26.2"}
    row = {"name": "packaging", "version": "26.2", "vulns": []}
    with pytest.raises(ValueError, match="Duplicate"):
        audit.validate_audit_result({"dependencies": [row, row], "fixes": []}, expected, 0)
    with pytest.raises(ValueError, match="modified"):
        audit.validate_audit_result({"dependencies": [row], "fixes": [{}]}, expected, 0)
