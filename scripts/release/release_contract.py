"""Fail-closed release artifact and version contract checks.

This module intentionally uses only the Python standard library so a release can
validate its own source and downloaded assets before installing project dependencies.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import re
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_NAME = "corpuskit-app"
EXPECTED_IMAGES: dict[str, tuple[str, str]] = {
    "api": ("docker/api.Dockerfile", "runtime"),
    "web": ("docker/web.Dockerfile", "runtime"),
    "worker-batch": ("docker/worker.Dockerfile", "worker-batch"),
    "worker-external-provider": (
        "docker/worker.Dockerfile",
        "worker-external-provider",
    ),
    "worker-gpu-inference": ("docker/worker.Dockerfile", "worker-gpu-inference"),
    "worker-gpu-training": ("docker/worker.Dockerfile", "worker-gpu-training"),
}
SEMVER_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<phase>alpha|beta|rc)\.(?P<number>0|[1-9]\d*))?$"
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ContractError(ValueError):
    """Raised when release input violates the immutable artifact contract."""


@dataclass(frozen=True)
class ReleaseVersion:
    """The supported, intentionally narrow SemVer release form."""

    major: int
    minor: int
    patch: int
    phase: str | None = None
    number: int | None = None

    @classmethod
    def parse(cls, value: str) -> ReleaseVersion:
        match = SEMVER_PATTERN.fullmatch(value)
        if match is None:
            raise ContractError(
                "release tag must be vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-(alpha|beta|rc).N"
            )
        phase = match.group("phase")
        number = int(match.group("number")) if phase is not None else None
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            phase=phase,
            number=number,
        )

    @property
    def semver(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.phase is not None:
            value += f"-{self.phase}.{self.number}"
        return value

    @property
    def tag(self) -> str:
        return f"v{self.semver}"

    @property
    def pep440(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.phase is None:
            return value
        phase = {"alpha": "a", "beta": "b", "rc": "rc"}[self.phase]
        return f"{value}{phase}{self.number}"

    @property
    def precedence(self) -> tuple[int, int, int, int, int]:
        phase_rank = {"alpha": 0, "beta": 1, "rc": 2, None: 3}[self.phase]
        return (self.major, self.minor, self.patch, phase_rank, self.number or 0)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_name(name: str) -> None:
    member = PurePosixPath(name)
    if not name or name.startswith(("/", "\\")) or "\\" in name or ".." in member.parts:
        raise ContractError(f"unsafe archive member: {name!r}")


def validate_versions(root: Path, tag_value: str, *, require_changelog: bool) -> ReleaseVersion:
    version = ReleaseVersion.parse(tag_value)
    with (root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    python_version = pyproject.get("project", {}).get("version")
    root_version = _load_json(root / "package.json").get("version")
    web_version = _load_json(root / "apps/web/package.json").get("version")
    expected = {
        "pyproject.toml": version.pep440,
        "package.json": version.semver,
        "apps/web/package.json": version.semver,
    }
    observed = {
        "pyproject.toml": python_version,
        "package.json": root_version,
        "apps/web/package.json": web_version,
    }
    mismatches = [
        f"{path}: expected {expected[path]!r}, observed {observed[path]!r}"
        for path in expected
        if expected[path] != observed[path]
    ]
    if mismatches:
        raise ContractError("version mismatch: " + "; ".join(mismatches))
    if require_changelog:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = re.compile(
            rf"^## \[{re.escape(version.semver)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
            re.MULTILINE,
        )
        if heading.search(changelog) is None:
            raise ContractError(
                f"CHANGELOG.md needs a dated '## [{version.semver}] - YYYY-MM-DD' section"
            )
    return version


def _validate_wheel(wheel_path: Path, version: ReleaseVersion) -> None:
    expected_prefix = f"corpuskit_app-{version.pep440}-"
    if not wheel_path.name.startswith(expected_prefix) or wheel_path.suffix != ".whl":
        raise ContractError(f"unexpected wheel filename: {wheel_path.name}")
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        for info in wheel.infolist():
            _safe_archive_name(info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ContractError(f"wheel contains a symbolic link: {info.filename}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(record_names) != 1 or len(entry_names) != 1:
            raise ContractError("wheel must contain one METADATA, RECORD, and entry_points.txt")
        message = BytesParser(policy=policy.default).parsebytes(wheel.read(metadata_names[0]))
        if message["Name"] != PROJECT_NAME or message["Version"] != version.pep440:
            raise ContractError("wheel core metadata name/version does not match release")
        if set(str(message["Requires-Python"]).split(",")) != {">=3.12", "<3.13"}:
            raise ContractError("wheel Requires-Python must remain >=3.12,<3.13")
        license_value = message["License-Expression"] or message["License"]
        if license_value != "Apache-2.0":
            raise ContractError("wheel must declare Apache-2.0")
        dependencies = message.get_all("Requires-Dist", [])
        if not any(requirement.startswith("corpusgen==0.1.7") for requirement in dependencies):
            raise ContractError("wheel must retain the exact corpusgen==0.1.7 dependency")
        if any(re.search(r"(?:\s@\s|file:|git\+)", requirement) for requirement in dependencies):
            raise ContractError("wheel cannot contain a direct URL, VCS, or local dependency")
        if "corpuskit/py.typed" not in names:
            raise ContractError("wheel is missing corpuskit/py.typed")
        entry_points = wheel.read(entry_names[0]).decode("utf-8")
        for command in (
            "corpuskit-api",
            "corpuskit-continuity",
            "corpuskit-db",
            "corpuskit-dispatcher",
            "corpuskit-maintenance",
            "corpuskit-phoible",
            "corpuskit-worker",
        ):
            if f"{command} =" not in entry_points:
                raise ContractError(f"wheel is missing the {command} console entry point")

        record_name = record_names[0]
        rows = csv.reader(wheel.read(record_name).decode("utf-8").splitlines())
        recorded: set[str] = set()
        for name, encoded_digest, size in rows:
            _safe_archive_name(name)
            recorded.add(name)
            if name == record_name:
                if encoded_digest or size:
                    raise ContractError("the RECORD row for RECORD must not hash itself")
                continue
            if not encoded_digest.startswith("sha256=") or not size:
                raise ContractError(f"wheel RECORD lacks a SHA-256 digest or size for {name}")
            content = wheel.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            if encoded_digest.removeprefix("sha256=").encode("ascii") != expected:
                raise ContractError(f"wheel RECORD digest mismatch for {name}")
            if int(size) != len(content):
                raise ContractError(f"wheel RECORD size mismatch for {name}")
        if recorded != set(names):
            raise ContractError("wheel RECORD does not enumerate every wheel member")


def _validate_sdist(sdist_path: Path, version: ReleaseVersion) -> None:
    expected = f"corpuskit_app-{version.pep440}.tar.gz"
    if sdist_path.name != expected:
        raise ContractError(f"unexpected source distribution filename: {sdist_path.name}")
    prefix = f"corpuskit_app-{version.pep440}/"
    with tarfile.open(sdist_path, mode="r:gz") as archive:
        names: set[str] = set()
        for member in archive.getmembers():
            _safe_archive_name(member.name)
            if not member.name.startswith(prefix):
                raise ContractError(f"sdist member escapes the expected root: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise ContractError(f"sdist contains a link or device: {member.name}")
            names.add(member.name)
        for required in (
            f"{prefix}LICENSE",
            f"{prefix}README.md",
            f"{prefix}pyproject.toml",
            f"{prefix}src/corpuskit/py.typed",
        ):
            if required not in names:
                raise ContractError(f"sdist is missing {required}")


def validate_distributions(root: Path, directory: Path, tag_value: str) -> dict[str, Any]:
    version = validate_versions(root, tag_value, require_changelog=False)
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    other_paths = sorted(
        (path for path in directory.iterdir() if path.is_file() and path not in {*wheels, *sdists}),
        key=lambda path: path.name,
    )
    if len(other_paths) == 1 and other_paths[0].name == ".gitignore":
        if other_paths[0].read_text(encoding="utf-8") not in {"*", "*\n"}:
            raise ContractError("the build-directory .gitignore has unexpected content")
        other_paths = []
    other = [path.name for path in other_paths]
    if len(wheels) != 1 or len(sdists) != 1 or other:
        raise ContractError(
            "dist must contain exactly one wheel and one sdist before release metadata; "
            f"observed wheels={len(wheels)}, sdists={len(sdists)}, other={other}"
        )
    _validate_wheel(wheels[0], version)
    _validate_sdist(sdists[0], version)
    return {
        "schema_version": 1,
        "tag": version.tag,
        "artifacts": [
            {"name": path.name, "sha256": _sha256(path), "size": path.stat().st_size}
            for path in (wheels[0], sdists[0])
        ],
    }


def write_image_record(args: argparse.Namespace) -> None:
    version = ReleaseVersion.parse(args.tag)
    expected = EXPECTED_IMAGES.get(args.component)
    if expected != (args.dockerfile, args.target):
        raise ContractError(
            f"unexpected Docker build contract for {args.component}: "
            f"{args.dockerfile!r}, {args.target!r}"
        )
    if not SHA256_PATTERN.fullmatch(args.digest):
        raise ContractError("image digest must be a lowercase sha256 digest")
    if not COMMIT_PATTERN.fullmatch(args.source_sha):
        raise ContractError("source SHA must be a lowercase 40-character Git commit ID")
    repository = args.repository.lower()
    expected_image = f"ghcr.io/{repository}-{args.component}"
    if args.image.lower() != expected_image:
        raise ContractError(f"image must be {expected_image}")
    record = {
        "schema_version": 1,
        "component": args.component,
        "dockerfile": args.dockerfile,
        "target": args.target,
        "platform": "linux/amd64",
        "image": expected_image,
        "tag": version.tag,
        "digest": args.digest,
        "reference": f"{expected_image}@{args.digest}",
        "source_sha": args.source_sha,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _validate_sbom(path: Path, expected_format: str) -> None:
    document = _load_json(path)
    if expected_format == "spdx":
        if not str(document.get("spdxVersion", "")).startswith("SPDX-2."):
            raise ContractError(f"{path.name} is not an SPDX 2.x JSON document")
        if not isinstance(document.get("packages"), list):
            raise ContractError(f"{path.name} has no SPDX package list")
    elif document.get("bomFormat") != "CycloneDX" or not isinstance(
        document.get("components"), list
    ):
        raise ContractError(f"{path.name} is not a CycloneDX JSON document")


def assemble_manifest(args: argparse.Namespace) -> None:
    root = Path(args.root)
    assets = Path(args.assets)
    version = validate_versions(root, args.tag, require_changelog=False)
    if not COMMIT_PATTERN.fullmatch(args.source_sha):
        raise ContractError("source SHA must be a lowercase 40-character Git commit ID")
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(assets.glob("*.image.json")):
        record = _load_json(path)
        component = record.get("component")
        if not isinstance(component, str) or component in records:
            raise ContractError(f"duplicate or invalid image component in {path.name}")
        expected = EXPECTED_IMAGES.get(component)
        if expected != (record.get("dockerfile"), record.get("target")):
            raise ContractError(f"invalid Docker contract in {path.name}")
        expected_image = f"ghcr.io/{args.repository.lower()}-{component}"
        if (
            record.get("schema_version") != 1
            or record.get("image") != expected_image
            or record.get("tag") != version.tag
            or record.get("source_sha") != args.source_sha
            or record.get("platform") != "linux/amd64"
            or not isinstance(record.get("digest"), str)
            or SHA256_PATTERN.fullmatch(record["digest"]) is None
            or record.get("reference") != f"{expected_image}@{record.get('digest')}"
        ):
            raise ContractError(f"image identity mismatch in {path.name}")
        records[component] = record
    if set(records) != set(EXPECTED_IMAGES):
        raise ContractError(
            f"image records must be exactly {sorted(EXPECTED_IMAGES)}; observed {sorted(records)}"
        )

    wheels = sorted(assets.glob("*.whl"))
    sdists = sorted(assets.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ContractError("release assets need exactly one wheel and one source distribution")
    distributions = [
        {"name": path.name, "sha256": _sha256(path), "size": path.stat().st_size}
        for path in (*wheels, *sdists)
    ]
    distribution_record = _load_json(assets / "python-distributions.json")
    if (
        distribution_record.get("schema_version") != 1
        or distribution_record.get("tag") != version.tag
        or distribution_record.get("artifacts") != distributions
    ):
        raise ContractError("python-distributions.json does not match the built distributions")

    sboms: list[dict[str, Any]] = []
    subjects = ["corpuskit-app", *sorted(EXPECTED_IMAGES)]
    for subject in subjects:
        for sbom_format, suffix in (("spdx", "spdx.json"), ("cyclonedx", "cdx.json")):
            path = assets / f"{subject}-{version.tag}.{suffix}"
            if not path.is_file():
                raise ContractError(f"missing {sbom_format} SBOM for {subject}: {path.name}")
            _validate_sbom(path, sbom_format)
            signature = assets / f"{path.name}.sigstore.json"
            if not signature.is_file():
                raise ContractError(f"missing Sigstore bundle for {path.name}")
            sboms.append(
                {
                    "name": path.name,
                    "format": sbom_format,
                    "subject": subject,
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
        for predicate in ("provenance", "spdx", "cdx"):
            bundle = assets / f"{subject}-{version.tag}.{predicate}.attestation.sigstore.json"
            if not bundle.is_file():
                raise ContractError(f"missing GitHub {predicate} attestation for {subject}")

    for distribution in distributions:
        if not (assets / f"{distribution['name']}.sigstore.json").is_file():
            raise ContractError(f"missing Sigstore bundle for {distribution['name']}")

    manifest = {
        "schema_version": 1,
        "tag": version.tag,
        "version": version.semver,
        "python_version": version.pep440,
        "repository": args.repository.lower(),
        "source_sha": args.source_sha,
        "workflow_run": args.workflow_run,
        "python_distributions": distributions,
        "sboms": sboms,
        "images": [records[name] for name in sorted(records)],
    }
    output = Path(args.output)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_checksums(directory: Path, output: Path) -> None:
    excluded = {output.name, f"{output.name}.sigstore.json"}
    paths = sorted(
        path for path in directory.iterdir() if path.is_file() and path.name not in excluded
    )
    if not paths:
        raise ContractError("cannot create a checksum file for an empty asset directory")
    lines = [f"{_sha256(path)}  {path.name}" for path in paths]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def verify_assets(args: argparse.Namespace) -> None:
    directory = Path(args.assets)
    manifest = _load_json(directory / "release-manifest.json")
    version = ReleaseVersion.parse(args.tag)
    if manifest.get("tag") != version.tag:
        raise ContractError("release manifest tag does not match requested tag")
    if args.repository and manifest.get("repository") != args.repository.lower():
        raise ContractError("release manifest repository does not match")
    if args.source_sha and manifest.get("source_sha") != args.source_sha:
        raise ContractError("release manifest source SHA does not match")
    if {item.get("component") for item in manifest.get("images", [])} != set(EXPECTED_IMAGES):
        raise ContractError("release manifest does not contain all six image components")

    checksum_path = directory / "SHA256SUMS"
    entries: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if match is None or match.group(2) in entries:
            raise ContractError(f"invalid or duplicate SHA256SUMS row: {line!r}")
        entries[match.group(2)] = match.group(1)
    for name, expected in entries.items():
        path = directory / name
        if not path.is_file() or _sha256(path) != expected:
            raise ContractError(f"checksum verification failed for {name}")
    allowed_unchecked = {
        "SHA256SUMS",
        "SHA256SUMS.sigstore.json",
        "SHA256SUMS.provenance.attestation.sigstore.json",
    }
    observed = {path.name for path in directory.iterdir() if path.is_file()}
    unchecked = observed - set(entries)
    if unchecked != allowed_unchecked:
        raise ContractError(f"unexpected unchecked release assets: {sorted(unchecked)}")


def validate_rollback(candidate_value: str, rollback_value: str) -> None:
    candidate = ReleaseVersion.parse(candidate_value)
    rollback = ReleaseVersion.parse(rollback_value)
    if rollback.precedence >= candidate.precedence:
        raise ContractError("rollback version must have lower SemVer precedence than candidate")


def _write_github_output(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ContractError(f"GitHub output {key} contains a newline")
            handle.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    versions = subparsers.add_parser("versions")
    versions.add_argument("--root", default=".")
    versions.add_argument("--tag", required=True)
    versions.add_argument("--source-sha")
    versions.add_argument("--require-changelog", action="store_true")
    versions.add_argument("--github-output")

    normalize = subparsers.add_parser("normalize-version")
    normalize.add_argument("--tag", required=True)

    distributions = subparsers.add_parser("distributions")
    distributions.add_argument("--root", default=".")
    distributions.add_argument("--directory", required=True)
    distributions.add_argument("--tag", required=True)
    distributions.add_argument("--output")

    image_record = subparsers.add_parser("image-record")
    image_record.add_argument("--component", required=True)
    image_record.add_argument("--dockerfile", required=True)
    image_record.add_argument("--target", required=True)
    image_record.add_argument("--image", required=True)
    image_record.add_argument("--digest", required=True)
    image_record.add_argument("--tag", required=True)
    image_record.add_argument("--repository", required=True)
    image_record.add_argument("--source-sha", required=True)
    image_record.add_argument("--output", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--root", default=".")
    manifest.add_argument("--assets", required=True)
    manifest.add_argument("--tag", required=True)
    manifest.add_argument("--repository", required=True)
    manifest.add_argument("--source-sha", required=True)
    manifest.add_argument("--workflow-run", required=True)
    manifest.add_argument("--output", required=True)

    checksums = subparsers.add_parser("checksums")
    checksums.add_argument("--directory", required=True)
    checksums.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify-assets")
    verify.add_argument("--assets", required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--repository")
    verify.add_argument("--source-sha")

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--candidate", required=True)
    rollback.add_argument("--rollback", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "normalize-version":
            version = ReleaseVersion.parse(args.tag)
            sys.stdout.write(
                json.dumps(
                    {
                        "tag": version.tag,
                        "version": version.semver,
                        "pep440": version.pep440,
                        "prerelease": str(version.phase is not None).lower(),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        elif args.command == "versions":
            version = validate_versions(
                Path(args.root), args.tag, require_changelog=args.require_changelog
            )
            if args.source_sha and not COMMIT_PATTERN.fullmatch(args.source_sha):
                raise ContractError("source SHA must be a lowercase 40-character Git commit ID")
            result = {
                "tag": version.tag,
                "version": version.semver,
                "pep440": version.pep440,
                "prerelease": str(version.phase is not None).lower(),
            }
            if args.github_output:
                _write_github_output(Path(args.github_output), result)
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        elif args.command == "distributions":
            result = validate_distributions(Path(args.root), Path(args.directory), args.tag)
            rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
            if args.output:
                Path(args.output).write_text(rendered, encoding="utf-8")
            sys.stdout.write(rendered)
        elif args.command == "image-record":
            write_image_record(args)
        elif args.command == "manifest":
            assemble_manifest(args)
        elif args.command == "checksums":
            write_checksums(Path(args.directory), Path(args.output))
        elif args.command == "verify-assets":
            verify_assets(args)
        elif args.command == "rollback":
            validate_rollback(args.candidate, args.rollback)
        else:  # pragma: no cover - argparse makes this unreachable
            raise AssertionError(f"unhandled command: {args.command}")
    except (ContractError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        sys.stderr.write(f"release contract failed: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
