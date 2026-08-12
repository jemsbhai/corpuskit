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
