"""Fail-closed PostgreSQL backup, offline verification, and restore drills."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from corpuskit.domain.continuity import (
    ARCHIVE_FILENAME,
    BUNDLE_ID_PATTERN,
    MANIFEST_DIGEST_FILENAME,
    MANIFEST_FILENAME,
    BackupCreationReport,
    BackupVerificationReport,
    PostgresBackupManifest,
    RestoreDrillReport,
    is_alembic_revision,
    manifest_digest_line,
    parse_manifest_file,
)

DEFAULT_BACKUP_TIMEOUT_SECONDS: Final = 1_800.0
DEFAULT_VERIFY_TIMEOUT_SECONDS: Final = 300.0
DEFAULT_RESTORE_TIMEOUT_SECONDS: Final = 1_800.0
DEFAULT_MAX_ARCHIVE_BYTES: Final = 10 * 1024**4
MIN_TIMEOUT_SECONDS: Final = 5.0
MAX_TIMEOUT_SECONDS: Final = 14_400.0
MAX_MANIFEST_BYTES: Final = 64 * 1024
MAX_TOC_BYTES: Final = 64 * 1024 * 1024
SUPPORTED_POSTGRES_MAJORS: Final = frozenset({16, 17, 18})
RESTORE_DATABASE_PATTERN: Final = re.compile(
    r"corpuskit_restore_drill_[a-z0-9](?:[a-z0-9_]{0,37}[a-z0-9])?",
    flags=re.ASCII,
)
_PARTIAL_PATTERN: Final = re.compile(r"\.ckpg-[0-9a-f]{24}\.partial", flags=re.ASCII)
_VERSION_PATTERNS: Final = {
    "pg_dump": re.compile(
        rb"\Apg_dump \(PostgreSQL\) ([0-9]{1,2}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)",
        flags=re.ASCII,
    ),
    "pg_restore": re.compile(
        rb"\Apg_restore \(PostgreSQL\) ([0-9]{1,2}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)",
        flags=re.ASCII,
    ),
    "psql": re.compile(
        rb"\Apsql \(PostgreSQL\) ([0-9]{1,2}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)",
        flags=re.ASCII,
    ),
}
_POSTGRES_ENV_KEYS: Final = frozenset(
    {
        "PGCHANNELBINDING",
        "PGDATABASE",
        "PGHOST",
        "PGHOSTADDR",
        "PGPASSFILE",
        "PGPASSWORD",
        "PGPORT",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSSLCRL",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLMODE",
        "PGSSLROOTCERT",
        "PGTARGETSESSIONATTRS",
        "PGUSER",
    }
)
_SYSTEM_ENV_KEYS: Final = frozenset(
    {"COMSPEC", "HOME", "LANG", "LC_ALL", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
)
_USER_RELATION_COUNT_SQL: Final = """\
SELECT count(*)
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp_%'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f');
"""
_ALEMBIC_REVISION_SQL: Final = "SELECT version_num FROM public.alembic_version LIMIT 1;"
_ALLOWED_BUNDLE_MEMBERS: Final = frozenset(
    {ARCHIVE_FILENAME, MANIFEST_FILENAME, MANIFEST_DIGEST_FILENAME}
)


class ContinuityErrorCode(StrEnum):
    INVALID_PATH = "invalid_path"
    INVALID_CONFIGURATION = "invalid_configuration"
    TOOL_UNAVAILABLE = "tool_unavailable"
    TOOL_VERSION = "tool_version"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_FAILED = "process_failed"
    BACKUP_INTEGRITY = "backup_integrity"
    CONFIRMATION_REQUIRED = "confirmation_required"
    UNSAFE_RESTORE_TARGET = "unsafe_restore_target"
    RESTORE_TARGET_NOT_EMPTY = "restore_target_not_empty"
    RESTORE_VALIDATION_FAILED = "restore_validation_failed"


_SAFE_MESSAGES: Final = {
    ContinuityErrorCode.INVALID_PATH: "A continuity path failed strict validation.",
    ContinuityErrorCode.INVALID_CONFIGURATION: (
        "PostgreSQL continuity configuration is incomplete or invalid."
    ),
    ContinuityErrorCode.TOOL_UNAVAILABLE: "A required PostgreSQL client tool is unavailable.",
    ContinuityErrorCode.TOOL_VERSION: "PostgreSQL client tool versions are unsupported or unsafe.",
    ContinuityErrorCode.PROCESS_TIMEOUT: "A bounded PostgreSQL operation timed out.",
    ContinuityErrorCode.PROCESS_FAILED: (
        "A PostgreSQL operation failed; sensitive details were redacted."
    ),
    ContinuityErrorCode.BACKUP_INTEGRITY: "Backup integrity verification failed.",
    ContinuityErrorCode.CONFIRMATION_REQUIRED: (
        "The exact restore-drill confirmation was not supplied."
    ),
    ContinuityErrorCode.UNSAFE_RESTORE_TARGET: (
        "Restore drills require an explicitly named isolated drill database."
    ),
    ContinuityErrorCode.RESTORE_TARGET_NOT_EMPTY: (
        "Restore drills refuse a target that already contains user relations."
    ),
    ContinuityErrorCode.RESTORE_VALIDATION_FAILED: (
        "The isolated restore completed without the required CorpusKit validation evidence."
    ),
}


class ContinuityError(RuntimeError):
    """Public-safe operational failure that never embeds subprocess or connection details."""

    def __init__(self, code: ContinuityErrorCode) -> None:
        self.code = code
        super().__init__(_SAFE_MESSAGES[code])


class ProcessRunner(Protocol):
    """Narrow subprocess boundary used by production code and deterministic tests."""

    def capture(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> bytes: ...

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> None: ...


class BoundedProcessRunner:
    """Run absolute executables without a shell and discard all diagnostic streams."""

    def capture(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> bytes:
        try:
            result = subprocess.run(  # noqa: S603 - absolute allowlisted tool paths only
                list(arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(environment),
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContinuityError(ContinuityErrorCode.PROCESS_TIMEOUT) from exc
        except OSError as exc:
            raise ContinuityError(ContinuityErrorCode.PROCESS_FAILED) from exc
        if result.returncode != 0 or len(result.stdout) > max_output_bytes:
            raise ContinuityError(ContinuityErrorCode.PROCESS_FAILED)
        return result.stdout

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        try:
            result = subprocess.run(  # noqa: S603 - absolute allowlisted tool paths only
                list(arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(environment),
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContinuityError(ContinuityErrorCode.PROCESS_TIMEOUT) from exc
        except OSError as exc:
            raise ContinuityError(ContinuityErrorCode.PROCESS_FAILED) from exc
        if result.returncode != 0:
            raise ContinuityError(ContinuityErrorCode.PROCESS_FAILED)


@dataclass(frozen=True, slots=True)
class PostgresToolchain:
    pg_dump: Path
    pg_restore: Path
    psql: Path

    @classmethod
    def discover(cls, bin_directory: Path | None = None) -> PostgresToolchain:
        """Resolve all client tools to absolute regular executable files."""

        tools: dict[str, Path] = {}
        for name in ("pg_dump", "pg_restore", "psql"):
            if bin_directory is None:
                found = shutil.which(name)
                if found is None:
                    raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE)
                candidate = Path(found)
            else:
                directory = _strict_directory(bin_directory, private=False)
                suffix = ".exe" if os.name == "nt" else ""
                candidate = directory / f"{name}{suffix}"
            try:
                resolved = candidate.resolve(strict=True)
                file_stat = resolved.stat()
            except OSError as exc:
                raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE) from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE)
            if os.name != "nt" and not os.access(resolved, os.X_OK):
                raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE)
            # Keep the absolute invocation path: Debian's three PostgreSQL client
            # commands intentionally resolve to one ``pg_wrapper`` and dispatch by
            # argv[0]. The resolved target is used only for regular/executable checks.
            tools[name] = Path(os.path.abspath(candidate))
        return cls(
            pg_dump=tools["pg_dump"],
            pg_restore=tools["pg_restore"],
            psql=tools["psql"],
        )


@dataclass(frozen=True, slots=True)
class ValidatedToolchain:
    paths: PostgresToolchain
    pg_dump_version: str
    pg_restore_version: str
    psql_version: str


@dataclass(slots=True)
class _Deadline:
    expires_at: float
    monotonic: Callable[[], float]

    @classmethod
    def start(cls, timeout_seconds: float, monotonic: Callable[[], float]) -> _Deadline:
        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)
        return cls(expires_at=monotonic() + timeout_seconds, monotonic=monotonic)

    def remaining(self) -> float:
        remaining = self.expires_at - self.monotonic()
        if remaining <= 0:
            raise ContinuityError(ContinuityErrorCode.PROCESS_TIMEOUT)
        return remaining


@dataclass(frozen=True, slots=True)
class _VerifiedBundle:
    manifest: PostgresBackupManifest
    report: BackupVerificationReport
    archive_path: Path


class PostgresContinuity:
    """Create and prove credential-free backup evidence with destructive guardrails."""

    def __init__(
        self,
        backup_root: Path,
        tools: PostgresToolchain,
        *,
        runner: ProcessRunner | None = None,
        process_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        self._root = _strict_directory(backup_root, private=True)
        self._tools = tools
        self._runner = runner or BoundedProcessRunner()
        self._source_environment = dict(process_environment or os.environ)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._nonce = nonce or (lambda: secrets.token_hex(12))

    def create_backup(
        self,
        *,
        timeout_seconds: float = DEFAULT_BACKUP_TIMEOUT_SECONDS,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> BackupCreationReport:
        """Create, inspect, and atomically publish one private backup bundle."""

        _validate_max_archive_bytes(max_archive_bytes)
        deadline = _Deadline.start(timeout_seconds, self._monotonic)
        tools = self._validate_toolchain(deadline)
        postgres_environment = self._postgres_environment(require_restore_target=False)
        partial = self._new_partial_directory()
        try:
            archive_path = partial / ARCHIVE_FILENAME
            self._runner.run(
                (
                    str(tools.paths.pg_dump),
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--no-password",
                    f"--file={archive_path}",
                ),
                environment=postgres_environment,
                timeout_seconds=deadline.remaining(),
            )
            _make_private_file(archive_path)
            archive_sha256, archive_size = _hash_regular_file(
                archive_path,
                deadline=deadline,
                max_bytes=max_archive_bytes,
            )
            toc_entry_count = self._inspect_archive(
                archive_path,
                partial,
                tools,
                deadline,
            )
            created_at = _utc(self._clock())
            nonce = self._validated_nonce()
            bundle_id = (
                f"ckpg_{created_at.strftime('%Y%m%dT%H%M%S%fZ')}_{archive_sha256[:12]}_{nonce[:12]}"
            )
            manifest = PostgresBackupManifest(
                bundle_id=bundle_id,
                created_at=created_at,
                archive_sha256=archive_sha256,
                archive_size_bytes=archive_size,
                toc_entry_count=toc_entry_count,
                pg_dump_version=tools.pg_dump_version,
                pg_restore_version=tools.pg_restore_version,
            )
            _write_private_file(partial / MANIFEST_FILENAME, manifest.canonical_file_bytes())
            _write_private_file(
                partial / MANIFEST_DIGEST_FILENAME,
                manifest_digest_line(manifest),
            )
            _fsync_file(archive_path)
            _fsync_directory(partial)
            final_path = self._root / bundle_id
            if final_path.exists():
                raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
            os.rename(partial, final_path)
            _fsync_directory(self._root)
            return BackupCreationReport(
                bundle_id=bundle_id,
                archive_sha256=archive_sha256,
                archive_size_bytes=archive_size,
                manifest_sha256=manifest.sha256,
                created_at=created_at,
            )
        except ContinuityError:
            raise
        except (OSError, ValueError) as exc:
            raise ContinuityError(ContinuityErrorCode.PROCESS_FAILED) from exc
        finally:
            if partial.exists():
                _remove_partial_directory(self._root, partial)

    def verify_backup(
        self,
        bundle_id: str,
        *,
        timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> BackupVerificationReport:
        """Verify hashes, canonical metadata, size, and archive TOC without a database."""

        _validate_max_archive_bytes(max_archive_bytes)
        deadline = _Deadline.start(timeout_seconds, self._monotonic)
        tools = self._validate_toolchain(deadline)
        return self._verify_bundle(bundle_id, tools, deadline, max_archive_bytes).report

    def restore_drill(
        self,
        bundle_id: str,
        *,
        confirmation: str,
        timeout_seconds: float = DEFAULT_RESTORE_TIMEOUT_SECONDS,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> RestoreDrillReport:
        """Restore only into an empty, explicitly named ephemeral drill database."""

        _validate_max_archive_bytes(max_archive_bytes)
        deadline = _Deadline.start(timeout_seconds, self._monotonic)
        tools = self._validate_toolchain(deadline)
        verified = self._verify_bundle(bundle_id, tools, deadline, max_archive_bytes)
        environment = self._postgres_environment(require_restore_target=True)
        target_database = environment["PGDATABASE"]
        expected_confirmation = restore_confirmation(bundle_id, target_database)
        if not hmac.compare_digest(confirmation, expected_confirmation):
            raise ContinuityError(ContinuityErrorCode.CONFIRMATION_REQUIRED)

        existing_relations = self._query_nonnegative_integer(
            tools.paths.psql,
            environment,
            _USER_RELATION_COUNT_SQL,
            deadline,
        )
        if existing_relations != 0:
            raise ContinuityError(ContinuityErrorCode.RESTORE_TARGET_NOT_EMPTY)

        started_at = _utc(self._clock())
        self._runner.run(
            (
                str(tools.paths.pg_restore),
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "--no-password",
                "--dbname",
                target_database,
                str(verified.archive_path),
            ),
            environment=environment,
            timeout_seconds=deadline.remaining(),
        )
        restored_relations = self._query_nonnegative_integer(
            tools.paths.psql,
            environment,
            _USER_RELATION_COUNT_SQL,
            deadline,
        )
        alembic_revision = self._query_scalar(
            tools.paths.psql,
            environment,
            _ALEMBIC_REVISION_SQL,
            deadline,
            max_output_bytes=128,
        )
        if restored_relations == 0 or not is_alembic_revision(alembic_revision):
            raise ContinuityError(ContinuityErrorCode.RESTORE_VALIDATION_FAILED)
        return RestoreDrillReport(
            bundle_id=bundle_id,
            archive_sha256=verified.manifest.archive_sha256,
            started_at=started_at,
            completed_at=_utc(self._clock()),
            restored_relation_count=restored_relations,
            alembic_revision=alembic_revision,
            pg_restore_version=tools.pg_restore_version,
        )

    def _validate_toolchain(self, deadline: _Deadline) -> ValidatedToolchain:
        versions: dict[str, str] = {}
        base_environment = _base_process_environment(self._source_environment)
        for name, path in (
            ("pg_dump", self._tools.pg_dump),
            ("pg_restore", self._tools.pg_restore),
            ("psql", self._tools.psql),
        ):
            _validate_executable(path)
            output = self._runner.capture(
                (str(path), "--version"),
                environment=base_environment,
                timeout_seconds=min(10.0, deadline.remaining()),
                max_output_bytes=512,
            )
            matched = _VERSION_PATTERNS[name].match(output.strip())
            if matched is None:
                raise ContinuityError(ContinuityErrorCode.TOOL_VERSION)
            versions[name] = matched.group(1).decode("ascii")
        majors = {int(version.split(".", 1)[0]) for version in versions.values()}
        if len(majors) != 1 or not majors.issubset(SUPPORTED_POSTGRES_MAJORS):
            raise ContinuityError(ContinuityErrorCode.TOOL_VERSION)
        return ValidatedToolchain(
            paths=self._tools,
            pg_dump_version=versions["pg_dump"],
            pg_restore_version=versions["pg_restore"],
            psql_version=versions["psql"],
        )

    def _verify_bundle(
        self,
        bundle_id: str,
        tools: ValidatedToolchain,
        deadline: _Deadline,
        max_archive_bytes: int,
    ) -> _VerifiedBundle:
        bundle = _strict_bundle(self._root, bundle_id)
        manifest_payload = _read_regular_file(bundle / MANIFEST_FILENAME, MAX_MANIFEST_BYTES)
        try:
            manifest = parse_manifest_file(manifest_payload)
        except (ValueError, UnicodeError) as exc:
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY) from exc
        if manifest.bundle_id != bundle_id:
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
        digest_payload = _read_regular_file(
            bundle / MANIFEST_DIGEST_FILENAME,
            256,
        )
        if not hmac.compare_digest(digest_payload, manifest_digest_line(manifest)):
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
        if int(manifest.pg_dump_version.split(".", 1)[0]) != int(
            tools.pg_restore_version.split(".", 1)[0]
        ):
            raise ContinuityError(ContinuityErrorCode.TOOL_VERSION)

        archive_path = bundle / ARCHIVE_FILENAME
        archive_sha256, archive_size = _hash_regular_file(
            archive_path,
            deadline=deadline,
            max_bytes=max_archive_bytes,
        )
        if archive_size != manifest.archive_size_bytes or not hmac.compare_digest(
            archive_sha256,
            manifest.archive_sha256,
        ):
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)

        partial = self._new_partial_directory()
        try:
            toc_entry_count = self._inspect_archive(archive_path, partial, tools, deadline)
        finally:
            _remove_partial_directory(self._root, partial)
        if toc_entry_count != manifest.toc_entry_count:
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
        report = BackupVerificationReport(
            bundle_id=bundle_id,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size,
            manifest_sha256=manifest.sha256,
            toc_entry_count=toc_entry_count,
            pg_restore_version=tools.pg_restore_version,
            verified_at=_utc(self._clock()),
        )
        return _VerifiedBundle(manifest=manifest, report=report, archive_path=archive_path)

    def _inspect_archive(
        self,
        archive_path: Path,
        work_directory: Path,
        tools: ValidatedToolchain,
        deadline: _Deadline,
    ) -> int:
        toc_path = work_directory / "archive.toc"
        self._runner.run(
            (
                str(tools.paths.pg_restore),
                "--list",
                f"--file={toc_path}",
                str(archive_path),
            ),
            environment=_base_process_environment(self._source_environment),
            timeout_seconds=deadline.remaining(),
        )
        payload = _read_regular_file(toc_path, MAX_TOC_BYTES)
        toc_path.unlink()
        count = sum(
            1
            for line in payload.splitlines()
            if line.strip() and not line.lstrip().startswith(b";")
        )
        if count <= 0 or count > 50_000_000:
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
        return count

    def _query_nonnegative_integer(
        self,
        psql: Path,
        environment: Mapping[str, str],
        query: str,
        deadline: _Deadline,
    ) -> int:
        value = self._query_scalar(
            psql,
            environment,
            query,
            deadline,
            max_output_bytes=64,
        )
        if re.fullmatch(r"[0-9]{1,8}", value, flags=re.ASCII) is None:
            raise ContinuityError(ContinuityErrorCode.RESTORE_VALIDATION_FAILED)
        parsed = int(value)
        if parsed > 50_000_000:
            raise ContinuityError(ContinuityErrorCode.RESTORE_VALIDATION_FAILED)
        return parsed

    def _query_scalar(
        self,
        psql: Path,
        environment: Mapping[str, str],
        query: str,
        deadline: _Deadline,
        *,
        max_output_bytes: int,
    ) -> str:
        output = self._runner.capture(
            (
                str(psql),
                "--no-psqlrc",
                "--no-password",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command",
                query,
            ),
            environment=environment,
            timeout_seconds=deadline.remaining(),
            max_output_bytes=max_output_bytes,
        )
        try:
            return output.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ContinuityError(ContinuityErrorCode.RESTORE_VALIDATION_FAILED) from exc

    def _postgres_environment(self, *, require_restore_target: bool) -> dict[str, str]:
        environment = _base_process_environment(self._source_environment)
        for key in _POSTGRES_ENV_KEYS:
            value = self._source_environment.get(key)
            if value is None:
                continue
            if not value or len(value) > 16_384 or "\x00" in value:
                raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)
            environment[key] = value
        if "PGDATABASE" not in environment or not ({"PGUSER", "PGSERVICE"} & environment.keys()):
            raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)
        if (
            require_restore_target
            and RESTORE_DATABASE_PATTERN.fullmatch(environment["PGDATABASE"]) is None
        ):
            raise ContinuityError(ContinuityErrorCode.UNSAFE_RESTORE_TARGET)
        environment["PGAPPNAME"] = "corpuskit-continuity"
        environment["PGCONNECT_TIMEOUT"] = "10"
        return environment

    def _new_partial_directory(self) -> Path:
        nonce = self._validated_nonce()
        partial = self._root / f".ckpg-{nonce}.partial"
        try:
            partial.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError as exc:
            raise ContinuityError(ContinuityErrorCode.INVALID_PATH) from exc
        return partial

    def _validated_nonce(self) -> str:
        nonce = self._nonce()
        if re.fullmatch(r"[0-9a-f]{24}", nonce, flags=re.ASCII) is None:
            raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)
        return nonce


def restore_confirmation(bundle_id: str, target_database: str) -> str:
    """Build the exact non-secret phrase required before a drill mutates its target."""

    if re.fullmatch(BUNDLE_ID_PATTERN, bundle_id, flags=re.ASCII) is None:
        raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)
    if RESTORE_DATABASE_PATTERN.fullmatch(target_database) is None:
        raise ContinuityError(ContinuityErrorCode.UNSAFE_RESTORE_TARGET)
    return f"RESTORE {bundle_id} INTO EMPTY {target_database}"


def _validate_max_archive_bytes(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= 100 * 1024**4:
        raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)


def _base_process_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if key.upper() in _SYSTEM_ENV_KEYS and value and "\x00" not in value
    }
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


def _strict_directory(path: Path, *, private: bool) -> Path:
    if not path.is_absolute() or _is_indirection(path):
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    try:
        resolved = path.resolve(strict=True)
        path_stat = resolved.stat()
    except OSError as exc:
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH) from exc
    if _normalized_path(path) != _normalized_path(resolved) or not stat.S_ISDIR(path_stat.st_mode):
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    if private and os.name != "nt":
        if path_stat.st_mode & 0o022:
            raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
        if hasattr(os, "geteuid") and path_stat.st_uid != os.geteuid():
            raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    return resolved


def _strict_bundle(root: Path, bundle_id: str) -> Path:
    if re.fullmatch(BUNDLE_ID_PATTERN, bundle_id, flags=re.ASCII) is None:
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    bundle = root / bundle_id
    if _is_indirection(bundle):
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    try:
        resolved = bundle.resolve(strict=True)
        members = {member.name for member in resolved.iterdir()}
    except OSError as exc:
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH) from exc
    if resolved.parent != root or not resolved.is_dir() or members != _ALLOWED_BUNDLE_MEMBERS:
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    return resolved


def _validate_executable(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        path_stat = resolved.stat()
    except OSError as exc:
        raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE) from exc
    if not path.is_absolute() or not stat.S_ISREG(path_stat.st_mode):
        raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE)
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise ContinuityError(ContinuityErrorCode.TOOL_UNAVAILABLE)


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    if _is_indirection(path):
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_bytes:
                raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
            payload = handle.read(max_bytes + 1)
    except ContinuityError:
        raise
    except OSError as exc:
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY) from exc
    if len(payload) != file_stat.st_size or len(payload) > max_bytes:
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
    return payload


def _hash_regular_file(
    path: Path,
    *,
    deadline: _Deadline,
    max_bytes: int,
) -> tuple[str, int]:
    if _is_indirection(path):
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size <= 0
                or before.st_size > max_bytes
            ):
                raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
            total = 0
            while chunk := handle.read(1024 * 1024):
                deadline.remaining()
                total += len(chunk)
                if total > max_bytes:
                    raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except ContinuityError:
        raise
    except OSError as exc:
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY) from exc
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
    return digest.hexdigest(), total


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _make_private_file(path: Path) -> None:
    if _is_indirection(path):
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
    try:
        path_stat = path.stat()
        if not stat.S_ISREG(path_stat.st_mode):
            raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY)
        path.chmod(0o600)
    except ContinuityError:
        raise
    except OSError as exc:
        raise ContinuityError(ContinuityErrorCode.BACKUP_INTEGRITY) from exc


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for ``_commit``/``fsync``.
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_partial_directory(root: Path, partial: Path) -> None:
    if partial.parent != root or _PARTIAL_PATTERN.fullmatch(partial.name) is None:
        raise ContinuityError(ContinuityErrorCode.INVALID_PATH)
    if partial.exists():
        shutil.rmtree(partial)


def _is_indirection(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        return bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContinuityError(ContinuityErrorCode.INVALID_CONFIGURATION)
    return value.astimezone(UTC)


__all__ = [
    "DEFAULT_BACKUP_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ARCHIVE_BYTES",
    "DEFAULT_RESTORE_TIMEOUT_SECONDS",
    "DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "BoundedProcessRunner",
    "ContinuityError",
    "ContinuityErrorCode",
    "PostgresContinuity",
    "PostgresToolchain",
    "restore_confirmation",
]
