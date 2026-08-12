"""Operator CLI for PostgreSQL backup verification and isolated restore drills."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from corpuskit.operations.postgres_continuity import (
    DEFAULT_BACKUP_TIMEOUT_SECONDS,
    DEFAULT_MAX_ARCHIVE_BYTES,
    DEFAULT_RESTORE_TIMEOUT_SECONDS,
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    ContinuityError,
    ContinuityErrorCode,
    PostgresContinuity,
    PostgresToolchain,
)


def _add_common_arguments(parser: argparse.ArgumentParser, *, timeout: float) -> None:
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="absolute, private directory containing atomic backup bundles",
    )
    parser.add_argument(
        "--pg-bin-dir",
        type=Path,
        help="absolute directory containing pg_dump, pg_restore, and psql",
    )
    parser.add_argument("--timeout-seconds", type=float, default=timeout)
    parser.add_argument(
        "--max-archive-bytes",
        type=int,
        default=DEFAULT_MAX_ARCHIVE_BYTES,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpuskit-continuity",
        description=(
            "Create and verify PostgreSQL backup bundles, or restore one into an empty "
            "isolated drill database. Database connection material is read only from PG* "
            "environment variables."
        ),
    )
    commands = parser.add_subparsers(dest="operation", required=True)

    backup = commands.add_parser("backup", help="create and atomically publish a backup")
    _add_common_arguments(backup, timeout=DEFAULT_BACKUP_TIMEOUT_SECONDS)

    verify = commands.add_parser("verify", help="verify a backup without database access")
    _add_common_arguments(verify, timeout=DEFAULT_VERIFY_TIMEOUT_SECONDS)
    verify.add_argument("--bundle", required=True)

    restore = commands.add_parser(
        "restore-drill",
        help="restore only into an empty corpuskit_restore_drill_* database",
    )
    _add_common_arguments(restore, timeout=DEFAULT_RESTORE_TIMEOUT_SECONDS)
    restore.add_argument("--bundle", required=True)
    restore.add_argument(
        "--confirm",
        required=True,
        help="exact phrase: RESTORE <bundle> INTO EMPTY <PGDATABASE>",
    )
    return parser


def _execute(arguments: argparse.Namespace) -> BaseModel:
    tools = PostgresToolchain.discover(arguments.pg_bin_dir)
    continuity = PostgresContinuity(arguments.root, tools)
    common = {
        "timeout_seconds": arguments.timeout_seconds,
        "max_archive_bytes": arguments.max_archive_bytes,
    }
    if arguments.operation == "backup":
        return continuity.create_backup(**common)
    if arguments.operation == "verify":
        return continuity.verify_backup(arguments.bundle, **common)
    return continuity.restore_drill(
        arguments.bundle,
        confirmation=arguments.confirm,
        **common,
    )


def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"


def run(argv: Sequence[str] | None = None) -> int:
    """Run one bounded command and emit only credential-free machine-readable output."""

    arguments = _parser().parse_args(argv)
    try:
        report = _execute(arguments)
    except (KeyboardInterrupt, SystemExit):
        raise
    except ContinuityError as exc:
        sys.stderr.write(
            _json_line(
                {
                    "error_code": exc.code.value,
                    "message": str(exc),
                    "schema_id": "corpuskit.continuity-error.v1",
                    "status": "error",
                }
            )
        )
        refusal_codes = {
            ContinuityErrorCode.CONFIRMATION_REQUIRED,
            ContinuityErrorCode.INVALID_CONFIGURATION,
            ContinuityErrorCode.INVALID_PATH,
            ContinuityErrorCode.TOOL_UNAVAILABLE,
            ContinuityErrorCode.TOOL_VERSION,
            ContinuityErrorCode.UNSAFE_RESTORE_TARGET,
            ContinuityErrorCode.RESTORE_TARGET_NOT_EMPTY,
        }
        return 2 if exc.code in refusal_codes else 1
    except Exception:
        sys.stderr.write(
            _json_line(
                {
                    "error_code": "internal_error",
                    "message": "Continuity operation failed; sensitive details were redacted.",
                    "schema_id": "corpuskit.continuity-error.v1",
                    "status": "error",
                }
            )
        )
        return 1
    sys.stdout.write(_json_line(report.model_dump(mode="json")))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover - console-script path is tested directly
    main()


__all__ = ["main", "run"]
