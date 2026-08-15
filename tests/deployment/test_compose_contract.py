"""Security and runtime contracts for the local Compose topology."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

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
