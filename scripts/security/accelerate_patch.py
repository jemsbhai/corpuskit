"""Build and verify the narrowly patched, complete Accelerate distribution."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import re
import sys
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

UPSTREAM_VERSION = "1.15.0"
PATCHED_VERSION = "1.15.0+corpuskit.1"
UPSTREAM_SHA256 = "97eacca0b73e45cb867dbf8c5d5d4dc32219544300e0c8992c7334dc2ef33cec"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/8a/4c/"
    "34f0450479d01195027260da68d8a3880683f1640c3ca5adf64acb3185f1/"
    "accelerate-1.15.0-py3-none-any.whl"
)
WHEEL_NAME = f"accelerate-{PATCHED_VERSION}-py3-none-any.whl"
DIST_INFO = f"accelerate-{PATCHED_VERSION}.dist-info"
_VERSION_NOTICE = "# Modified by CorpusKit to identify the CVE-2026-69112 patched distribution."
_SOURCE_NAME = "accelerate/utils/modeling.py"
_MAX_WHEEL_BYTES = 4 * 1024 * 1024


def _root(repository_root: Path | None) -> Path:
    return repository_root if repository_root is not None else Path(__file__).resolve().parents[2]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_archive(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(
            name.startswith("/") or ".." in name.split("/") or "\\" in name for name in names
        ):
            raise ValueError("Wheel contains duplicate or unsafe archive paths.")
        if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
            raise ValueError("Wheel exceeds its uncompressed size limit.")
        return {name: archive.read(name) for name in names}


def _verify_record(files: dict[str, bytes], dist_info: str) -> None:
    record_name = f"{dist_info}/RECORD"
    rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
    if any(len(row) != 3 for row in rows) or len(rows) != len(files):
        raise ValueError("Wheel RECORD does not cover every archive member exactly once.")
    if len({row[0] for row in rows}) != len(rows) or {row[0] for row in rows} != set(files):
        raise ValueError("Wheel RECORD member set does not match the archive.")
    for name, digest, size in rows:
        if name == record_name:
            if digest or size:
                raise ValueError("Wheel RECORD must not hash itself.")
            continue
        actual = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=")
        if digest != f"sha256={actual.decode()}" or size != str(len(files[name])):
            raise ValueError(f"Wheel RECORD does not match {name}.")


def _apply_patch(source: str, patch: str) -> str:
    """Apply one strict unified diff, refusing drift or unreviewed additional files."""
    lines = patch.splitlines(keepends=True)
    if lines[:2] != [f"--- a/{_SOURCE_NAME}\n", f"+++ b/{_SOURCE_NAME}\n"]:
        raise ValueError("Patch must modify only the reviewed checkpoint loader.")
    original = source.splitlines(keepends=True)
    result: list[str] = []
    consumed = 0
    cursor = 2
    while cursor < len(lines):
        match = re.fullmatch(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n", lines[cursor])
        if match is None:
            raise ValueError("Malformed patch hunk header.")
        old_start, old_count, _new_start, new_count = map(int, match.groups())
        start = old_start - 1
        if start < consumed:
            raise ValueError("Overlapping patch hunks.")
        result.extend(original[consumed:start])
        consumed = start
        cursor += 1
        old_seen = new_seen = 0
        while cursor < len(lines) and not lines[cursor].startswith("@@ "):
            line = lines[cursor]
            marker, content = line[:1], line[1:]
            if marker not in {" ", "+", "-"}:
                raise ValueError("Unexpected patch content.")
            if marker in {" ", "-"}:
                if consumed >= len(original) or original[consumed] != content:
                    raise ValueError("Patch context differs from the pinned upstream wheel.")
                consumed += 1
                old_seen += 1
            if marker in {" ", "+"}:
                result.append(content)
                new_seen += 1
            cursor += 1
        if (old_seen, new_seen) != (old_count, new_count):
            raise ValueError("Patch hunk length does not match its header.")
    result.extend(original[consumed:])
    return "".join(result)


def build_wheel(upstream: bytes, patch: bytes) -> bytes:
    """Repackage the pinned upstream wheel with only the reviewed code/version changes."""
    if _sha256(upstream) != UPSTREAM_SHA256:
        raise ValueError("Upstream Accelerate wheel SHA-256 differs from the approved release.")
    original = _read_archive(upstream)
    old_info = f"accelerate-{UPSTREAM_VERSION}.dist-info"
    _verify_record(original, old_info)
    files = {name.replace(f"{old_info}/", f"{DIST_INFO}/"): data for name, data in original.items()}
    files[_SOURCE_NAME] = _apply_patch(
        files[_SOURCE_NAME].decode("utf-8"), patch.decode("utf-8")
    ).encode("utf-8")
    init = files["accelerate/__init__.py"]
    old_version = f'__version__ = "{UPSTREAM_VERSION}"'.encode()
    if init.count(old_version) != 1:
        raise ValueError("Unexpected upstream version declaration.")
    files["accelerate/__init__.py"] = init.replace(
        old_version, f'__version__ = "{PATCHED_VERSION}"\n{_VERSION_NOTICE}'.encode()
    )
    metadata_name = f"{DIST_INFO}/METADATA"
    old_metadata_version = f"\nVersion: {UPSTREAM_VERSION}\n".encode()
    if files[metadata_name].count(old_metadata_version) != 1:
        raise ValueError("Unexpected upstream distribution metadata.")
    files[metadata_name] = files[metadata_name].replace(
        old_metadata_version, f"\nVersion: {PATCHED_VERSION}\n".encode()
    )
    record_name = f"{DIST_INFO}/RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(files):
        if name == record_name:
            writer.writerow((name, "", ""))
        else:
            digest = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=")
            writer.writerow((name, f"sha256={digest.decode()}", str(len(files[name]))))
    files[record_name] = output.getvalue().encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return result.getvalue()


def verify_artifact(repository_root: Path | None = None) -> dict[str, Any]:
    """Verify the canonical patched wheel, provenance, full RECORD, metadata, and license."""
    vendor = _root(repository_root) / "vendor" / "accelerate"
    provenance = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
    expected = {
        "name": "accelerate",
        "upstream_version": UPSTREAM_VERSION,
        "patched_version": PATCHED_VERSION,
        "upstream_url": UPSTREAM_URL,
        "upstream_sha256": UPSTREAM_SHA256,
        "wheel": WHEEL_NAME,
        "patch": "checkpoint-safety.patch",
        "repaired_advisories": ["CVE-2026-69112", "PYSEC-2026-3804", "GHSA-4j2p-28q2-5m79"],
    }
    if not isinstance(provenance, dict) or any(provenance.get(k) != v for k, v in expected.items()):
        raise ValueError("Accelerate provenance differs from the approved release and patch.")
    required = {
        "sha256",
        "size",
        "patch_sha256",
        "upstream_requires_dist",
        "upstream_license_sha256",
        "patched_modeling_sha256",
        "upstream_files",
    }
    if not required <= provenance.keys():
        raise ValueError("Accelerate provenance is incomplete.")
    data = (vendor / WHEEL_NAME).read_bytes()
    if _sha256(data) != provenance["sha256"] or len(data) != provenance["size"]:
        raise ValueError("Patched Accelerate wheel does not match its pinned SHA-256/size.")
    if _sha256((vendor / "checkpoint-safety.patch").read_bytes()) != provenance["patch_sha256"]:
        raise ValueError("Accelerate source patch does not match its provenance.")
    files = _read_archive(data)
    _verify_record(files, DIST_INFO)
    old_info = f"accelerate-{UPSTREAM_VERSION}.dist-info"
    upstream_files = {
        name.replace(f"{old_info}/", f"{DIST_INFO}/"): digest
        for name, digest in provenance["upstream_files"].items()
    }
    if set(upstream_files) != set(files):
        raise ValueError("Patched wheel changed the upstream archive member set.")
    for name, digest in upstream_files.items():
        if name in {_SOURCE_NAME, f"{DIST_INFO}/RECORD"}:
            continue
        content = files[name]
        if name == "accelerate/__init__.py":
            content = content.replace(
                f'__version__ = "{PATCHED_VERSION}"\n{_VERSION_NOTICE}'.encode(),
                f'__version__ = "{UPSTREAM_VERSION}"'.encode(),
            )
        elif name == f"{DIST_INFO}/METADATA":
            content = content.replace(
                f"\nVersion: {PATCHED_VERSION}\n".encode(),
                f"\nVersion: {UPSTREAM_VERSION}\n".encode(),
            )
        if _sha256(content) != digest:
            raise ValueError(f"Patched wheel changed an unapproved upstream file: {name}.")
    metadata = BytesParser().parsebytes(files[f"{DIST_INFO}/METADATA"])
    if metadata["Name"] != "accelerate" or metadata["Version"] != PATCHED_VERSION:
        raise ValueError("Patched wheel metadata has an unexpected package identity.")
    if metadata.get_all("Requires-Dist", []) != provenance["upstream_requires_dist"]:
        raise ValueError("Patched wheel changed or omitted upstream dependencies.")
    if _sha256(files[f"{DIST_INFO}/licenses/LICENSE"]) != provenance["upstream_license_sha256"]:
        raise ValueError("Patched wheel did not retain the complete upstream license.")
    if _sha256(files[_SOURCE_NAME]) != provenance["patched_modeling_sha256"]:
        raise ValueError("Patched checkpoint loader does not match its reviewed source.")
    return provenance


def verify_installed(repository_root: Path | None = None) -> dict[str, Any]:
    """Verify all installed wheel-owned bytes, including source and dependency metadata."""
    provenance = verify_artifact(repository_root)
    try:
        distribution = importlib.metadata.distribution("accelerate")
    except importlib.metadata.PackageNotFoundError:
        raise ValueError("Patched Accelerate is not installed.") from None
    if distribution.version != PATCHED_VERSION:
        raise ValueError("Installed Accelerate is not the approved patched version.")
    wheel = _root(repository_root) / "vendor" / "accelerate" / WHEEL_NAME
    files = _read_archive(wheel.read_bytes())
    installed_record = distribution.read_text("RECORD")
    if installed_record is None:
        raise ValueError("Installed Accelerate has no RECORD.")
    rows = list(csv.reader(io.StringIO(installed_record)))
    if any(len(row) != 3 for row in rows) or len({row[0] for row in rows}) != len(rows):
        raise ValueError("Installed Accelerate RECORD is malformed.")
    record = {name: (digest, size) for name, digest, size in rows}
    for name, expected in files.items():
        # Installers append their own RECORD rows for generated entry points and bytecode.
        if name == f"{DIST_INFO}/RECORD":
            continue
        installed = Path(str(distribution.locate_file(name)))
        if not installed.is_file() or installed.read_bytes() != expected:
            raise ValueError(f"Installed Accelerate differs from the verified wheel: {name}.")
        digest = base64.urlsafe_b64encode(hashlib.sha256(expected).digest()).rstrip(b"=")
        if record.get(name) != (f"sha256={digest.decode()}", str(len(expected))):
            raise ValueError(
                f"Installed Accelerate RECORD differs from the verified wheel: {name}."
            )
    package_root = Path(str(distribution.locate_file("accelerate")))
    for item in package_root.rglob("*"):
        if item.is_file() and not (item.suffix == ".pyc" and item.parent.name == "__pycache__"):
            relative = "accelerate/" + item.relative_to(package_root).as_posix()
            if relative not in files:
                raise ValueError("Installed Accelerate includes unverified package files.")
    return {**provenance, "installed_verified": True}


def _download_upstream() -> bytes:
    # The URL is a reviewed immutable PyPI artifact; the digest is checked before archive use.
    with urllib.request.urlopen(UPSTREAM_URL, timeout=60) as response:
        data: bytes = response.read(_MAX_WHEEL_BYTES + 1)
    if len(data) > _MAX_WHEEL_BYTES:
        raise ValueError("Upstream wheel exceeds its download size limit.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "verify-installed", "rebuild-check", "build"))
    parser.add_argument("--upstream-wheel", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    provenance = verify_installed() if args.command == "verify-installed" else verify_artifact()
    if args.command in {"rebuild-check", "build"}:
        upstream = args.upstream_wheel.read_bytes() if args.upstream_wheel else _download_upstream()
        patch = (_root(None) / "vendor" / "accelerate" / "checkpoint-safety.patch").read_bytes()
        rebuilt = build_wheel(upstream, patch)
        if _sha256(rebuilt) != provenance["sha256"]:
            raise ValueError("Rebuilt wheel does not match the reviewed artifact.")
        if args.command == "build":
            if args.output is None:
                parser.error("build requires --output")
            args.output.write_bytes(rebuilt)
    sys.stdout.write(
        json.dumps({"verified": True, "version": PATCHED_VERSION, "sha256": provenance["sha256"]})
        + "\n"
    )


if __name__ == "__main__":
    main()
