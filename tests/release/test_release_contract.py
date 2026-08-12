from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SCRIPT = REPOSITORY_ROOT / "scripts/release/release_contract.py"
EXPECTED_IMAGES: dict[str, tuple[str, str]] = {
    "api": ("docker/api.Dockerfile", "runtime"),
    "web": ("docker/web.Dockerfile", "runtime"),
    "worker-batch": ("docker/worker.Dockerfile", "worker-batch"),
    "worker-external-provider": ("docker/worker.Dockerfile", "worker-external-provider"),
    "worker-gpu-inference": ("docker/worker.Dockerfile", "worker-gpu-inference"),
    "worker-gpu-training": ("docker/worker.Dockerfile", "worker-gpu-training"),
}
RELEASE_WORKFLOWS = (
    REPOSITORY_ROOT / ".github/workflows/release.yml",
    REPOSITORY_ROOT / ".github/workflows/verify-promotion.yml",
    REPOSITORY_ROOT / ".github/workflows/publish-pypi.yml",
)
CI_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/ci.yml"
QUALITY_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/quality-scheduled.yml"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
UV_LOCK = REPOSITORY_ROOT / "uv.lock"
PINNED_WORKFLOWS = (*RELEASE_WORKFLOWS, CI_WORKFLOW, QUALITY_WORKFLOW)
ACTION_REFERENCE = re.compile(r"^\s*uses:\s*([^\s#]+)(?:\s+#\s*(\S+))?\s*$", re.MULTILINE)
FULL_ACTION_SHA = re.compile(r"^[^/@\s]+/[^@\s]+@[0-9a-f]{40}$")


def run_contract(*arguments: str) -> subprocess.CompletedProcess[str]:
    # The executable and script path are fixed; only argv values reach argparse.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(CONTRACT_SCRIPT), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_manifest_fixture(directory: Path) -> None:
    tag = "v0.1.0-alpha.1"
    source_sha = "a" * 40
    wheel = directory / "corpuskit_app-0.1.0a1-py3-none-any.whl"
    sdist = directory / "corpuskit_app-0.1.0a1.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    artifacts = [
        {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in (wheel, sdist)
    ]
    (directory / "python-distributions.json").write_text(
        json.dumps({"schema_version": 1, "tag": tag, "artifacts": artifacts}),
        encoding="utf-8",
    )
    for path in (wheel, sdist):
        (directory / f"{path.name}.sigstore.json").write_text("{}", encoding="utf-8")

    subjects = ["corpuskit-app", *sorted(EXPECTED_IMAGES)]
    for subject in subjects:
        (directory / f"{subject}-{tag}.spdx.json").write_text(
            json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}),
            encoding="utf-8",
        )
        (directory / f"{subject}-{tag}.cdx.json").write_text(
            json.dumps({"bomFormat": "CycloneDX", "components": []}),
            encoding="utf-8",
        )
        for suffix in ("spdx.json", "cdx.json"):
            (directory / f"{subject}-{tag}.{suffix}.sigstore.json").write_text(
                "{}", encoding="utf-8"
            )
        for predicate in ("provenance", "spdx", "cdx"):
            (directory / f"{subject}-{tag}.{predicate}.attestation.sigstore.json").write_text(
                "{}", encoding="utf-8"
            )

    for index, (component, (dockerfile, target)) in enumerate(EXPECTED_IMAGES.items(), start=1):
        digest = f"sha256:{index:064x}"
        image = f"ghcr.io/example/corpuskit-{component}"
        record = {
            "schema_version": 1,
            "component": component,
            "dockerfile": dockerfile,
            "target": target,
            "platform": "linux/amd64",
            "image": image,
            "tag": tag,
            "digest": digest,
            "reference": f"{image}@{digest}",
            "source_sha": source_sha,
        }
        (directory / f"{component}-{tag}.image.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


@pytest.mark.parametrize(
    ("tag", "semver", "pep440", "prerelease"),
    [
        ("v1.2.3", "1.2.3", "1.2.3", False),
        ("v1.2.3-alpha.4", "1.2.3-alpha.4", "1.2.3a4", True),
        ("1.2.3-beta.5", "1.2.3-beta.5", "1.2.3b5", True),
        ("v1.2.3-rc.6", "1.2.3-rc.6", "1.2.3rc6", True),
    ],
)
def test_release_version_normalization(
    tag: str, semver: str, pep440: str, prerelease: bool
) -> None:
    result = run_contract("normalize-version", "--tag", tag)
    assert result.returncode == 0, result.stderr
    version = json.loads(result.stdout)
    assert version["version"] == semver
    assert version["pep440"] == pep440
    assert (version["prerelease"] == "true") is prerelease


@pytest.mark.parametrize(
    "tag",
    [
        "1.2",
        "release-1.2.3",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-dev.1",
        "v1.2.3-alpha",
        "v1.2.3+build",
        "v1.2.3-alpha.01",
    ],
)
def test_release_version_rejects_ambiguous_tags(tag: str) -> None:
    result = run_contract("normalize-version", "--tag", tag)
    assert result.returncode == 2
    assert "release contract failed" in result.stderr


def test_repository_versions_agree() -> None:
    result = run_contract("versions", "--tag", "v0.1.0-alpha.1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pep440"] == "0.1.0a1"


def test_changelog_has_the_exact_dated_tag_gate() -> None:
    result = run_contract("versions", "--tag", "v0.1.0-alpha.1", "--require-changelog")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == "0.1.0-alpha.1"


def test_release_build_backend_is_exactly_pinned_and_locked() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    build_requirements = project["build-system"]["requires"]
    build_group = project["dependency-groups"]["build"]

    assert build_requirements == ["hatchling==1.32.0"]
    assert build_group == build_requirements

    locked = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    application = next(
        package for package in locked["package"] if package["name"] == "corpuskit-app"
    )
    assert application["dev-dependencies"]["build"] == [{"name": "hatchling"}]
    assert application["metadata"]["requires-dev"]["build"] == [
        {"name": "hatchling", "specifier": "==1.32.0"}
    ]

    hatchling = [package for package in locked["package"] if package["name"] == "hatchling"]
    assert [package["version"] for package in hatchling] == ["1.32.0"]
    assert hatchling[0]["sdist"]["hash"].startswith("sha256:")
    assert hatchling[0]["wheels"]
    assert all(wheel["hash"].startswith("sha256:") for wheel in hatchling[0]["wheels"])


def test_release_build_uses_only_the_frozen_nonisolated_build_group() -> None:
    workflow = RELEASE_WORKFLOWS[0].read_text(encoding="utf-8")

    assert "UV_PROJECT_ENVIRONMENT: ${{ runner.temp }}/corpuskit-build-environment" in workflow
    assert "uv lock --check" in workflow
    assert "uv sync --frozen --only-group build --no-install-project" in workflow
    assert 'source "${UV_PROJECT_ENVIRONMENT}/bin/activate"' in workflow
    assert 'metadata.version("hatchling") == "1.32.0"' in workflow
    assert "uv build --no-sources --no-build-isolation --no-index --out-dir dist" in workflow
    assert "uv build --no-sources --out-dir dist" not in workflow


def test_rollback_requires_lower_semver_precedence() -> None:
    assert run_contract("rollback", "--candidate", "v1.1.0", "--rollback", "v1.0.9").returncode == 0
    assert (
        run_contract("rollback", "--candidate", "v1.1.0", "--rollback", "v1.1.0-rc.1").returncode
        == 0
    )
    assert run_contract("rollback", "--candidate", "v1.1.0", "--rollback", "v1.1.0").returncode == 2
    assert (
        run_contract("rollback", "--candidate", "v1.1.0-rc.1", "--rollback", "v1.1.0").returncode
        == 2
    )


def test_all_ci_and_release_actions_are_full_reviewed_commit_pins() -> None:
    observed: set[str] = set()
    for workflow in PINNED_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        references = ACTION_REFERENCE.findall(text)
        assert references, workflow
        for reference, version_comment in references:
            assert FULL_ACTION_SHA.fullmatch(reference), (workflow, reference)
            assert re.fullmatch(r"v?\d+\.\d+\.\d+", version_comment), (
                workflow,
                reference,
                version_comment,
            )
            observed.add(reference)
    assert any(reference.startswith("actions/attest@") for reference in observed)
    assert any(reference.startswith("sigstore/cosign-installer@") for reference in observed)
    assert any(reference.startswith("pypa/gh-action-pypi-publish@") for reference in observed)


def test_release_workflows_exclude_known_unsafe_shortcuts() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in RELEASE_WORKFLOWS)
    assert "continue-on-error" not in combined
    assert "pull_request_target" not in combined
    assert re.search(r"curl\b[^\n]*\|\s*(?:ba)?sh", combined) is None
    assert "PYPI_TOKEN" not in combined
    assert "password: ${{ secrets." not in combined
    assert "certificate-identity-regexp" not in combined
    assert "certificate-oidc-issuer-regexp" not in combined


def test_ci_verifies_the_exact_pgdg_primary_key_before_apt_trust() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    expected = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"
    negative = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF0"
    assert expected in workflow
    assert negative in workflow
    assert '"${#primary_fingerprints[@]}" -eq 1' in workflow
    assert '"${primary_fingerprints[0]}" == "${expected_fingerprint}"' in workflow
    download = workflow.index("https://www.postgresql.org/media/keys/ACCC4CF8.asc")
    validation = workflow.index(expected)
    trust_install = workflow.index(
        "/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc", download
    )
    repository = workflow.index("apt.postgresql.org/pub/repos/apt", trust_install)
    assert download < validation < trust_install < repository


def test_ci_service_and_direct_run_images_are_digest_pinned() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"image: postgres:17\.9-bookworm@sha256:[0-9a-f]{64}$", workflow, re.MULTILINE)
    assert len(re.findall(r"temporalio/temporal:1\.8\.2@sha256:[0-9a-f]{64}", workflow)) == 1
    assert (
        len(
            re.findall(
                r"minio/minio:RELEASE\.2025-09-07T16-13-09Z@sha256:[0-9a-f]{64}",
                workflow,
            )
        )
        == 1
    )


def test_ci_combined_replay_gate_attests_worker_and_role_separation() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/integration/test_combined_reproducibility_runtime.py" in workflow
    for role in ("app", "dispatcher", "worker", "adoption"):
        assert f"CORPUSKIT_TEST_POSTGRES_{role.upper()}_URL" in workflow
        assert f"corpuskit_{role}_combined" in workflow
    assert 'worker_image_digest="$(docker image inspect' in workflow
    assert (
        "test \"$(docker inspect corpuskit-worker-combined-ci --format '{{.Config.User}}')\""
        in workflow
    )
    assert "--read-only" in workflow
    assert "--tmpfs /tmp:rw,noexec,nosuid,size=64m" in workflow
    assert (
        "--tmpfs /run/corpuskit-espeak:rw,exec,nodev,nosuid,size=64m,uid=10001,gid=10001,mode=0700"
    ) in workflow
    assert "--env TMPDIR=/run/corpuskit-espeak" in workflow


def test_tag_release_builds_every_exact_image_once() -> None:
    release = RELEASE_WORKFLOWS[0].read_text(encoding="utf-8")
    trigger = release.split("permissions:", maxsplit=1)[0]
    assert "workflow_dispatch" not in trigger
    assert '"v*.*.*"' in trigger
    assert release.count("docker/build-push-action@") == 1
    assert "no-cache: true" in release
    assert "push: true" in release
    assert "provenance: false" in release
    for component, (dockerfile, target) in EXPECTED_IMAGES.items():
        assert f"component: {component}" in release
        assert f"dockerfile: {dockerfile}" in release
        assert f"target: {target}" in release
    assert "gh release verify" in release
    assert "gh release verify-asset" in release
    assert "IMMUTABLE_RELEASES_CONFIGURED" in release
    assert "/immutable-releases" not in release
    assert "github.run_attempt" in release
    assert "verification.verified == true" in release
    assert '.verification.reason == "valid"' in release
    assert release.count("--network none") >= 2


def test_release_requires_exact_sha_ci_and_scheduled_quality() -> None:
    release = RELEASE_WORKFLOWS[0].read_text(encoding="utf-8")
    assert 'for required_workflow in "ci.yml" "quality-scheduled.yml"; do' in release
    assert (
        "actions/workflows/${required_workflow}/runs?head_sha=${SOURCE_SHA}"
        "&status=success&per_page=100"
    ) in release
    assert "select(.head_sha == $sha)" in release
    assert 'select(.conclusion == "success")' in release


def test_required_ci_installs_optional_contracts_and_runs_real_datg_acceptance() -> None:
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    scheduled = QUALITY_WORKFLOW.read_text(encoding="utf-8")

    assert "--extra optimization --extra llm" in ci
    assert "--extra optimization --extra repository --extra llm" in scheduled
    assert 'uv run python -c "import tokenizers, torch, transformers"' in ci
    assert (
        "tests/integration/test_datg_publication.py::"
        "test_real_offline_transformers_index_publish_inspect_and_logit_preview"
    ) in ci
    local_runtime = ci.split(
        "      - name: Exercise real offline safetensors generation, analysis, and DATG preview\n",
        maxsplit=1,
    )[1].split("\n      - name:", maxsplit=1)[0]
    assert "--junitxml=reports/local-model/junit.xml" in local_runtime
    assert "scripts.quality.junit_contract" in local_runtime
    assert "--enforce" in local_runtime


def test_exact_python_digests_smoke_real_espeak_and_pinned_phoible_offline() -> None:
    release = RELEASE_WORKFLOWS[0].read_text(encoding="utf-8")
    smoke = release.split(
        "      - name: Smoke real phonology from the exact Python digest\n", maxsplit=1
    )[1].split("\n      - name:", maxsplit=1)[0]

    assert "matrix.runtime == 'python'" in smoke
    assert 'reference="${IMAGE}@${DIGEST}"' in smoke
    assert "--read-only" in smoke
    assert "--cap-drop ALL" in smoke
    assert "--security-opt no-new-privileges" in smoke
    assert '--user "${configured_user}"' in smoke
    assert "--network bridge" in smoke
    assert "corpuskit-phoible" in smoke
    assert "provision --json" in smoke
    assert "--network none" in smoke
    assert "/run/corpuskit-espeak:rw,exec,nodev,nosuid,size=64m" in smoke
    assert "TMPDIR=/run/corpuskit-espeak" in smoke
    assert "target=/home/corpuskit/.corpusgen,readonly" in smoke
    assert smoke.index("provision --json") < smoke.index("--network none")
    assert "PhoibleSnapshotProvisioner().status()" in smoke
    assert "PHOIBLE_SHA256" in smoke
    assert "PHOIBLE_BYTES" in smoke
    assert "PHOIBLE_COMMIT" in smoke
    assert 'CorpusgenAdapter().phonemize("hello", language="en-us")' in smoke
    assert 'CorpusgenInventoryAdapter().inventory("en-us")' in smoke


def test_every_release_image_target_exists_in_its_dockerfile() -> None:
    for component, (dockerfile, target) in EXPECTED_IMAGES.items():
        text = (REPOSITORY_ROOT / dockerfile).read_text(encoding="utf-8")
        stages = set(re.findall(r"^FROM\s+\S+(?:\s+AS\s+([a-z0-9-]+))?$", text, re.MULTILINE))
        assert target in stages, (component, dockerfile, target, stages)


def test_promotion_can_only_verify_existing_digests() -> None:
    promotion = RELEASE_WORKFLOWS[1].read_text(encoding="utf-8")
    forbidden = (
        "docker/build-push-action@",
        "docker build ",
        "imagetools create",
        "uv build",
        "npm run build",
        "gh release create",
        "gh release upload",
    )
    for value in forbidden:
        assert value not in promotion
    assert "gh release verify" in promotion
    assert "cosign verify" in promotion
    assert "gh attestation verify" in promotion
    assert promotion.count("attestations: read") == 1
    assert "attestations: write" not in promotion
    assert "rollback" in promotion
    assert "candidate_images" in promotion


def test_pypi_workflow_is_manual_verified_and_environment_gated() -> None:
    publication = RELEASE_WORKFLOWS[2].read_text(encoding="utf-8")
    assert "workflow_dispatch:" in publication
    assert "PYPI_TRUSTED_PUBLISHER_CONFIGURED" in publication
    assert "environment:\n      name: pypi" in publication
    assert "id-token: write" in publication
    assert "gh release verify" in publication
    assert "cosign verify-blob" in publication
    assert publication.count("attestations: read") == 1
    assert "attestations: write" not in publication
    assert "uv build" not in publication
    assert "docker/build-push-action@" not in publication
    assert publication.count("pypa/gh-action-pypi-publish@") == 1
    publish_job = publication.split("\n  publish:\n", maxsplit=1)[1]
    assert publish_job.count("      - name:") == 2


def test_distribution_smoke_loads_daemons_without_starting_them() -> None:
    release = RELEASE_WORKFLOWS[0].read_text(encoding="utf-8")
    command_loop = release.split("          for command in \\\n", maxsplit=1)[1].split(
        "; do\n", maxsplit=1
    )[0]
    for daemon in ("corpuskit-api", "corpuskit-dispatcher", "corpuskit-worker"):
        assert daemon not in command_loop
        assert f'              "{daemon}",' in release
    for command in (
        "corpuskit-continuity",
        "corpuskit-db",
        "corpuskit-maintenance",
        "corpuskit-phoible",
    ):
        assert command in command_loop


def test_every_docker_from_has_a_declared_sha256_digest() -> None:
    for dockerfile in sorted((REPOSITORY_ROOT / "docker").glob("*.Dockerfile")):
        text = dockerfile.read_text(encoding="utf-8")
        digest_args = dict(
            re.findall(r"^ARG ([A-Z_]+_IMAGE_DIGEST)=(sha256:[0-9a-f]{64})$", text, re.MULTILINE)
        )
        assert digest_args, dockerfile
        from_lines = re.findall(r"^FROM (.+)$", text, re.MULTILINE)
        assert from_lines, dockerfile
        for line in from_lines:
            match = re.search(r"@\$\{([A-Z_]+_IMAGE_DIGEST)\}", line)
            if match is None and re.fullmatch(r"[a-z][a-z0-9-]* AS [a-z][a-z0-9-]*", line):
                continue
            assert match is not None, (dockerfile, line)
            assert match.group(1) in digest_args, (dockerfile, line)


def test_docker_build_context_excludes_local_state_and_secret_files() -> None:
    patterns = {
        line.strip()
        for line in (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        ".git",
        ".venv",
        ".tmp-*",
        ".coverage",
        "coverage.xml",
        "coverage",
        "htmlcov",
        "node_modules",
        "**/node_modules",
        ".next",
        "**/.next",
        ".env",
        ".env.*",
        "!.env.example",
        ".corpuskit",
        "artifacts",
        "data",
        "*.db",
        "*.sqlite3",
        "playwright-report",
        "test-results",
    } <= patterns


def test_sdist_selection_is_independent_of_dirty_vcs_state() -> None:
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.hatch.build.targets.sdist]" in pyproject
    assert 'only-include = ["src", "CHANGELOG.md"]' in pyproject


def test_image_record_enforces_component_identity(tmp_path: Path) -> None:
    output = tmp_path / "api.image.json"
    base_arguments = (
        "image-record",
        "--component",
        "api",
        "--dockerfile",
        "docker/api.Dockerfile",
        "--target",
        "runtime",
        "--image",
        "ghcr.io/example/corpuskit-api",
        "--digest",
        "sha256:" + "a" * 64,
        "--tag",
        "v1.2.3",
        "--repository",
        "example/corpuskit",
        "--source-sha",
        "b" * 40,
        "--output",
        str(output),
    )
    result = run_contract(*base_arguments)
    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["reference"].endswith("@sha256:" + "a" * 64)
    assert re.fullmatch(r"[0-9a-f]{40}", record["source_sha"])

    invalid = list(base_arguments)
    invalid[invalid.index("runtime")] = "worker-batch"
    assert run_contract(*invalid).returncode == 2


def test_manifest_requires_exact_distribution_metadata_and_all_attestations(
    tmp_path: Path,
) -> None:
    _write_manifest_fixture(tmp_path)
    output = tmp_path / "release-manifest.json"
    arguments = (
        "manifest",
        "--assets",
        str(tmp_path),
        "--tag",
        "v0.1.0-alpha.1",
        "--repository",
        "example/corpuskit",
        "--source-sha",
        "a" * 40,
        "--workflow-run",
        "https://github.com/example/corpuskit/actions/runs/1",
        "--output",
        str(output),
    )
    result = run_contract(*arguments)
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert len(manifest["images"]) == 6
    assert len(manifest["sboms"]) == 14

    (tmp_path / "web-v0.1.0-alpha.1.provenance.attestation.sigstore.json").unlink()
    result = run_contract(*arguments)
    assert result.returncode == 2
    assert "missing GitHub provenance attestation for web" in result.stderr


def test_checksum_manifest_rejects_unchecked_or_tampered_assets(tmp_path: Path) -> None:
    manifest = {
        "tag": "v1.2.3",
        "repository": "example/corpuskit",
        "source_sha": "a" * 40,
        "images": [{"component": component} for component in EXPECTED_IMAGES],
    }
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "artifact.whl").write_bytes(b"trusted")
    result = run_contract(
        "checksums",
        "--directory",
        str(tmp_path),
        "--output",
        str(tmp_path / "SHA256SUMS"),
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "SHA256SUMS.sigstore.json").write_text("{}", encoding="utf-8")
    (tmp_path / "SHA256SUMS.provenance.attestation.sigstore.json").write_text(
        "{}", encoding="utf-8"
    )
    verify_arguments = (
        "verify-assets",
        "--assets",
        str(tmp_path),
        "--tag",
        "v1.2.3",
        "--repository",
        "example/corpuskit",
        "--source-sha",
        "a" * 40,
    )
    assert run_contract(*verify_arguments).returncode == 0

    (tmp_path / "artifact.whl").write_bytes(b"tampered")
    result = run_contract(*verify_arguments)
    assert result.returncode == 2
    assert "checksum" in result.stderr
