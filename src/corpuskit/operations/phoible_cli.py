"""Operator CLI for the pinned PHOIBLE data snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from corpuskit.adapters.corpusgen.phoible_provisioning import (
    PhoibleCacheStatus,
    PhoibleProvisioningError,
    PhoibleProvisionResult,
    PhoibleSnapshotProvisioner,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpuskit-phoible",
        description="Verify or atomically provision CorpusKit's pinned PHOIBLE snapshot.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    status = subparsers.add_parser("status", help="checksum-verify the installed snapshot")
    status.add_argument("--json", action="store_true", help="emit stable machine-readable JSON")

    provision = subparsers.add_parser(
        "provision", help="install the exact pinned snapshot when absent or invalid"
    )
    provision.add_argument(
        "--source-file",
        type=Path,
        help="read a pre-fetched snapshot instead of making an HTTPS request",
    )
    provision.add_argument(
        "--force", action="store_true", help="re-fetch even when the current snapshot is valid"
    )
    provision.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="HTTPS connection/read timeout, from 1 to 300 seconds",
    )
    provision.add_argument("--json", action="store_true", help="emit stable machine-readable JSON")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    provisioner: PhoibleSnapshotProvisioner | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one allowlisted PHOIBLE operation and return a process exit code."""

    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    resolved = provisioner or PhoibleSnapshotProvisioner()

    try:
        if args.operation == "status":
            status = resolved.status()
            _write_status(output, status, as_json=args.json)
            return 0 if status.ready else 1

        result = resolved.provision(
            source_file=args.source_file,
            force=args.force,
            timeout_seconds=args.timeout_seconds,
        )
        _write_result(output, result, as_json=args.json)
        return 0
    except PhoibleProvisioningError as exc:
        errors.write(f"PHOIBLE provisioning failed [{exc.code}]: {exc}\n")
        return 1
    except Exception as exc:
        errors.write(
            f"PHOIBLE provisioning failed ({type(exc).__name__}); details were redacted.\n"
        )
        return 1


def _write_status(output: TextIO, status: PhoibleCacheStatus, *, as_json: bool) -> None:
    if as_json:
        output.write(json.dumps(status.public_dict(), sort_keys=True, separators=(",", ":")))
        output.write("\n")
        return
    output.write(
        f"PHOIBLE {status.state.value}: revision={status.revision} "
        f"sha256={status.expected_sha256} bytes={status.actual_bytes or 0}\n"
    )


def _write_result(output: TextIO, result: PhoibleProvisionResult, *, as_json: bool) -> None:
    if as_json:
        output.write(json.dumps(result.public_dict(), sort_keys=True, separators=(",", ":")))
        output.write("\n")
        return
    output.write(
        f"PHOIBLE {result.status.state.value}: action={result.action.value} "
        f"revision={result.status.revision} sha256={result.status.expected_sha256} "
        f"bytes={result.status.actual_bytes or 0}\n"
    )


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run())


__all__ = ["main", "run"]
