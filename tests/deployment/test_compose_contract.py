"""Security and runtime contracts for the local Compose topology."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
ESPEAK_SERVICES = {
    "api",
    "worker-batch",
    "worker-external-provider",
    "worker-gpu-inference",
    "worker-gpu-training",
}
ESPEAK_TMPDIR = "/run/corpuskit-espeak"
GENERAL_TMPDIR = "/tmp"  # noqa: S108 - asserted container path, not a host temp file
XDG_CONFIG_HOME = "/tmp/corpuskit-xdg"  # noqa: S108 - asserted container path
WEB_CACHE = "/app/apps/web/.next/cache"


def _compose_config() -> dict[str, Any]:
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required for deployment contract tests"
    result = subprocess.run(  # noqa: S603 - absolute executable and fixed arguments
        [docker, "compose", "--profile", "*", "config"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_espeak_capable_services_confine_executable_temp_storage() -> None:
    services = _compose_config()["services"]
    for name in ESPEAK_SERVICES:
        service = services[name]
        tmpfs = set(service["tmpfs"])
        general_tmp = next(item for item in tmpfs if item.startswith(f"{GENERAL_TMPDIR}:"))
        espeak_tmp = next(item for item in tmpfs if item.startswith(f"{ESPEAK_TMPDIR}:"))

        assert service["environment"]["TMPDIR"] == ESPEAK_TMPDIR, name
        assert service["environment"]["XDG_CONFIG_HOME"] == XDG_CONFIG_HOME, name
        general_options = set(general_tmp.split(":", maxsplit=1)[1].split(","))
        assert "noexec" in general_options, name
        options = set(espeak_tmp.split(":", maxsplit=1)[1].split(","))
        assert options == {
            "rw",
            "exec",
            "nodev",
            "nosuid",
            "size=64m",
            "uid=10001",
            "gid=10001",
            "mode=0700",
        }, name


def test_non_execution_services_do_not_receive_executable_temp_storage() -> None:
    services = _compose_config()["services"]
    for name, service in services.items():
        if name in ESPEAK_SERVICES:
            continue
        assert service.get("environment", {}).get("TMPDIR") != ESPEAK_TMPDIR, name
        assert service.get("environment", {}).get("XDG_CONFIG_HOME") != XDG_CONFIG_HOME, name
        assert all(not item.startswith(f"{ESPEAK_TMPDIR}:") for item in service.get("tmpfs", [])), (
            name
        )


def test_read_only_web_runtime_has_only_bounded_writable_storage() -> None:
    web = _compose_config()["services"]["web"]

    assert web["read_only"] is True
    assert set(web["tmpfs"]) == {
        "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108 - asserted container path
        f"{WEB_CACHE}:rw,noexec,nosuid,size=64m,uid=1000,gid=1000,mode=0700",
    }


def test_minio_initialization_retries_transient_startup_failures() -> None:
    initializer = _compose_config()["services"]["minio-init"]
    command = initializer["command"]
    assert isinstance(command, list)
    assert len(command) == 1
    script = command[0]

    assert initializer["depends_on"]["minio"]["condition"] == "service_healthy"
    assert "until" in script
    assert "attempt=$$((attempt + 1))" in script
    assert 'if [ "$${attempt}" -ge 20 ]' in script
    assert "mc mb --ignore-existing" in script
    assert "mc anonymous set none" in script


def test_minio_services_build_pinned_source_instead_of_removed_registry_images() -> None:
    services = _compose_config()["services"]
    for service, target in [("minio", "minio-server"), ("minio-init", "minio-client")]:
        assert services[service]["build"]["dockerfile"] == "docker/minio.Dockerfile"
        assert services[service]["build"]["target"] == target
    assert services["minio"]["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        "http://127.0.0.1:9000/minio/health/live",
    ]
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "docker compose build minio minio-init" in workflow
    assert "quay.io/minio" not in (ROOT / "compose.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("target", ["server-build", "client-build"])
@pytest.mark.parametrize(
    ("scenario", "expected", "returncode"),
    [
        ("success", ["download", "diff", "verify", "build", "diff"], 0),
        (
            "transient",
            [
                "download",
                "sleep:5",
                "download",
                "sleep:10",
                "download",
                "diff",
                "verify",
                "build",
                "diff",
            ],
            0,
        ),
        ("persistent", ["download", "sleep:5", "download", "sleep:10", "download"], 1),
        ("changed-download", ["download", "diff"], 26),
        ("bad-cache", ["download", "diff", "verify"], 24),
        ("bad-build", ["download", "diff", "verify", "build"], 25),
        ("changed-build", ["download", "diff", "verify", "build", "diff"], 26),
    ],
)
def test_minio_download_retry_preserves_build_and_checksum_failures(
    target: str, scenario: str, expected: list[str], returncode: int
) -> None:
    """Execute the production retry chain with controlled transport/build failures."""
    shell = shutil.which("sh")
    assert shell is not None, "A POSIX shell is required for deployment contract tests"
    dockerfile = (ROOT / "docker/minio.Dockerfile").read_text(encoding="utf-8")
    stage = dockerfile.split(f"FROM build-base AS {target}\n", maxsplit=1)[1].split(
        "\nFROM ", maxsplit=1
    )[0]
    chain = "attempt=0" + stage.split("&& attempt=0", maxsplit=1)[1]
    chain = chain.replace("\\\n", "")
    assert (
        "ENV CGO_ENABLED=0 GODEBUG=http2client=0"
        in dockerfile.split("FROM build-base AS server-build", maxsplit=1)[0]
    )
    assert "GODEBUG" not in dockerfile.split("AS runtime\n", maxsplit=1)[1]

    mocks = """
downloads=0
diffs=0
timeout() {
    [ "$1 $2 $3" = "--signal=TERM --kill-after=10s 5m" ] || exit 90
    shift 3
    "$@"
}
sleep() { printf 'sleep:%s\n' "$1"; }
git() {
    [ "$*" = "diff --exit-code -- go.mod go.sum" ] || exit 91
    diffs=$((diffs + 1))
    echo diff
    case "$SCENARIO:$diffs" in
        changed-download:1|changed-build:2) return 26 ;;
    esac
    return 0
}
go() {
    [ "$GODEBUG" = "http2client=0" ] || exit 92
    case "$1 $2" in
        'mod download')
            downloads=$((downloads + 1))
            echo download
            if [ "$SCENARIO" = persistent ] ||
                { [ "$SCENARIO" = transient ] && [ "$downloads" -lt 3 ]; }; then
                return 23
            fi ;;
        'mod verify')
            [ "$GOPROXY" = off ] || exit 93
            echo verify
            [ "$SCENARIO" != bad-cache ] || return 24 ;;
        'build -mod=readonly')
            [ "$GOPROXY" = off ] || exit 94
            echo build
            [ "$SCENARIO" != bad-build ] || return 25 ;;
        *) exit 95 ;;
    esac
    return 0
}
"""
    result = subprocess.run(  # noqa: S603 - fixed executable and repository-owned shell fragment
        [shell, "-c", mocks + chain],
        env={**os.environ, "SCENARIO": scenario, "GODEBUG": "http2client=0"},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.stdout.splitlines() == expected, result.stderr
    assert result.returncode == returncode, result.stderr
