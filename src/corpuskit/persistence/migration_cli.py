"""Safe, intentionally narrow Alembic commands for operators and CI."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DATABASE_URL_ENV = "CORPUSKIT_DATABASE_URL"
SUPPORTED_ASYNC_DRIVERS = frozenset({"postgresql+asyncpg", "sqlite+aiosqlite"})


class MigrationConfigurationError(RuntimeError):
    """Raised for invalid migration configuration without echoing its value."""


def resolve_database_url(explicit_url: str | None = None) -> str:
    """Return a validated async database URL without logging credentials."""

    raw_url = explicit_url if explicit_url is not None else os.getenv(DATABASE_URL_ENV)
    if not raw_url:
        raise MigrationConfigurationError(
            f"{DATABASE_URL_ENV} must be set explicitly; no database default is used."
        )
    try:
        parsed = make_url(raw_url)
    except ArgumentError as exc:
        raise MigrationConfigurationError("The migration database URL is invalid.") from exc
    if parsed.drivername not in SUPPORTED_ASYNC_DRIVERS:
        raise MigrationConfigurationError(
            "The migration database URL must use postgresql+asyncpg or sqlite+aiosqlite."
        )
    if not parsed.database:
        raise MigrationConfigurationError("The migration database URL must name a database.")
    return raw_url


def build_alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic configuration anchored to the installed package."""

    script_location = Path(__file__).with_name("alembic").resolve()
    config = Config()
    config.set_main_option("script_location", script_location.as_posix())
    config.set_main_option("timezone", "UTC")
    config.attributes["database_url"] = resolve_database_url(database_url)
    return config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpuskit-db",
        description="Run bounded CorpusKit database migration operations.",
    )
    parser.add_argument(
        "operation",
        choices=("upgrade", "current", "check"),
        help="upgrade to head, show the current revision, or detect model/schema drift",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Run one allowlisted operation and return a process status code."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = build_alembic_config()
        if args.operation == "upgrade":
            command.upgrade(config, "head")
        elif args.operation == "current":
            command.current(config, verbose=False)
        else:
            command.check(config)
    except MigrationConfigurationError:
        sys.stderr.write(
            "Migration configuration is invalid; database details were not displayed.\n"
        )
        return 2
    except Exception as exc:  # Alembic and drivers expose many exception subclasses.
        sys.stderr.write(
            f"Migration failed ({type(exc).__name__}); sensitive details were redacted.\n"
        )
        return 1
    return 0


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run())


__all__ = [
    "DATABASE_URL_ENV",
    "MigrationConfigurationError",
    "build_alembic_config",
    "main",
    "resolve_database_url",
    "run",
]
