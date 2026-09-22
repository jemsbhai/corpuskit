"""The patched dependency must retain its identity, dependency tree, and audited bytes."""

from __future__ import annotations

import csv
import importlib.metadata
import io
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.security import accelerate_patch as patch

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "vendor" / "accelerate", tmp_path / "vendor" / "accelerate")
    return tmp_path


def test_verified_artifact_preserves_complete_upstream_distribution() -> None:
    provenance = patch.verify_artifact(ROOT)
    assert provenance["patched_version"] == "1.15.0+corpuskit.1"
    assert provenance["upstream_version"] == "1.15.0"
    assert len(provenance["upstream_files"]) == 90
    assert any(item.startswith("torch>=") for item in provenance["upstream_requires_dist"])
    assert provenance["repaired_advisories"] == [
        "CVE-2026-69112",
        "PYSEC-2026-3804",
        "GHSA-4j2p-28q2-5m79",
    ]


@pytest.mark.parametrize("filename", [patch.WHEEL_NAME, "checkpoint-safety.patch"])
def test_artifact_verifier_rejects_modified_bytes(repository: Path, filename: str) -> None:
    file = repository / "vendor" / "accelerate" / filename
    file.write_bytes(file.read_bytes() + b"modified")

    with pytest.raises(ValueError, match="Accelerate"):
        patch.verify_artifact(repository)


@pytest.mark.parametrize(
    "key",
    ["upstream_version", "patched_version", "upstream_sha256", "wheel", "repaired_advisories"],
)
def test_artifact_verifier_rejects_substituted_provenance(repository: Path, key: str) -> None:
    path = repository / "vendor" / "accelerate" / "provenance.json"
    provenance = json.loads(path.read_text(encoding="utf-8"))
    provenance[key] = "unreviewed"
    path.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance"):
        patch.verify_artifact(repository)


def test_artifact_verifier_requires_complete_provenance(repository: Path) -> None:
    path = repository / "vendor" / "accelerate" / "provenance.json"
    provenance = json.loads(path.read_text(encoding="utf-8"))
    del provenance["upstream_files"]
    path.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete"):
        patch.verify_artifact(repository)


def test_wheel_record_detects_changed_member_even_with_valid_archive() -> None:
    wheel = ROOT / "vendor" / "accelerate" / patch.WHEEL_NAME
    files = patch._read_archive(wheel.read_bytes())
    files["accelerate/accelerator.py"] += b"\n# modified\n"

    with pytest.raises(ValueError, match="RECORD"):
        patch._verify_record(files, patch.DIST_INFO)


def test_wheel_record_rejects_duplicate_and_omitted_rows() -> None:
    wheel = ROOT / "vendor" / "accelerate" / patch.WHEEL_NAME
    files = patch._read_archive(wheel.read_bytes())
    key = f"{patch.DIST_INFO}/RECORD"
    lines = files[key].splitlines(keepends=True)
    lines[-1] = lines[0]
    files[key] = b"".join(lines)

    with pytest.raises(ValueError, match="RECORD"):
        patch._verify_record(files, patch.DIST_INFO)


def test_builder_rejects_untrusted_upstream_archive() -> None:
    with pytest.raises(ValueError, match="Upstream Accelerate wheel SHA-256"):
        patch.build_wheel(b"untrusted upstream", b"patch")


@pytest.mark.parametrize(
    "diff",
    [
        "--- a/other.py\n+++ b/other.py\n",
        "--- a/accelerate/utils/modeling.py\n+++ b/accelerate/utils/modeling.py\n"
        "@@ -1,1 +1,1 @@\n-wrong\n+new\n",
        "--- a/accelerate/utils/modeling.py\n+++ b/accelerate/utils/modeling.py\n"
        "@@ -1,2 +1,1 @@\n-old\n+new\n",
    ],
)
def test_patch_application_fails_closed_on_context_or_scope_drift(diff: str) -> None:
    with pytest.raises(ValueError, match="Patch"):
        patch._apply_patch("old\n", diff)


@pytest.fixture
def installed_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, SimpleNamespace]:
    installed = tmp_path / "installed"
    installed.mkdir()
    wheel = ROOT / "vendor" / "accelerate" / patch.WHEEL_NAME
    for name, data in patch._read_archive(wheel.read_bytes()).items():
        target = installed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    distribution = SimpleNamespace(
        version=patch.PATCHED_VERSION,
        locate_file=lambda name: installed / name,
        read_text=lambda name: (installed / patch.DIST_INFO / name).read_text(encoding="utf-8"),
    )
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: distribution)
    return installed, distribution


def test_installed_verifier_accepts_exact_wheel_files_and_installer_generated_cache(
    installed_distribution: tuple[Path, SimpleNamespace],
) -> None:
    installed, _ = installed_distribution
    cache = installed / "accelerate" / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"installer-generated bytecode")

    assert patch.verify_installed(ROOT)["installed_verified"] is True


@pytest.mark.parametrize("target", ["source", "record", "version", "extra-file"])
def test_installed_verifier_rejects_substitutions(
    installed_distribution: tuple[Path, SimpleNamespace], target: str
) -> None:
    installed, distribution = installed_distribution
    if target == "source":
        file = installed / "accelerate/utils/modeling.py"
        file.write_bytes(file.read_bytes() + b"\n# altered\n")
    elif target == "record":
        file = installed / patch.DIST_INFO / "RECORD"
        rows = list(csv.reader(io.StringIO(file.read_text(encoding="utf-8"))))
        rows[0][1] = "sha256=unverified"
        output = io.StringIO()
        csv.writer(output).writerows(rows)
        file.write_text(output.getvalue(), encoding="utf-8")
    elif target == "version":
        distribution.version = "1.15.0"
    else:
        (installed / "accelerate/unverified.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Installed Accelerate"):
        patch.verify_installed(ROOT)


def test_wheel_archive_rejects_duplicate_paths() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("accelerate/a.py", "original")
        with pytest.warns(UserWarning, match="Duplicate"):
            writer.writestr("accelerate/a.py", "substitution")

    with pytest.raises(ValueError, match="duplicate"):
        patch._read_archive(archive.getvalue())
