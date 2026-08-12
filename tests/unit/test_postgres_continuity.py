"""Strict PostgreSQL continuity contracts, orchestration, and CLI behavior."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from corpuskit.domain.continuity import (
    ARCHIVE_FILENAME,
    MANIFEST_DIGEST_FILENAME,
    MANIFEST_FILENAME,
    BackupCreationReport,
    PostgresBackupManifest,
    RestoreDrillReport,
    manifest_digest_line,
    parse_manifest_file,
)
from corpuskit.operations import continuity_cli
from corpuskit.operations import postgres_continuity as continuity
from corpuskit.operations.postgres_continuity import (
    BoundedProcessRunner,
    ContinuityError,
    ContinuityErrorCode,
    PostgresContinuity,
    PostgresToolchain,
    restore_confirmation,
)

_CREATED_AT = datetime(2026, 8, 11, 22, 15, 30, 123456, tzinfo=UTC)
_NAIVE_CREATED_AT = _CREATED_AT.replace(tzinfo=None)
_NONCE = "0123456789abcdef01234567"
_ARCHIVE = b"PGDMP\x01\x0f\x00credential-free-fake-archive"
_TOC = b"; Archive created by pg_dump\n1; 0 0 TABLE public projects corpuskit\n"
_PASSWORD_CANARY = "continuity-password-must-never-leak"


class AdvancingClock:
    def __init__(self) -> None:
        self._next = _CREATED_AT

    def __call__(self) -> datetime:
        result = self._next
        self._next += timedelta(seconds=1)
        return result


class FakeRunner:
    def __init__(self) -> None:
        self.capture_calls: list[tuple[tuple[str, ...], dict[str, str], float, int]] = []
        self.run_calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []
        self.versions = {
            "pg_dump": b"pg_dump (PostgreSQL) 17.9\n",
            "pg_restore": b"pg_restore (PostgreSQL) 17.9\n",
            "psql": b"psql (PostgreSQL) 17.9\n",
        }
        self.initial_relations = 0
        self.restored_relations = 17
        self.alembic_revision = "0006_maintenance_cursors"
        self.count_output: bytes | None = None
        self.revision_output: bytes | None = None
        self.restored = False
        self.archive = _ARCHIVE
        self.toc = _TOC
        self.fail_run_for: str | None = None

    def capture(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> bytes:
        call = (tuple(arguments), dict(environment), timeout_seconds, max_output_bytes)
        self.capture_calls.append(call)
        executable = Path(arguments[0]).stem
        if arguments[1:] == ["--version"] or tuple(arguments[1:]) == ("--version",):
            return self.versions[executable]
        query = arguments[-1]
        if "count(*)" in query:
            if self.count_output is not None:
                return self.count_output
            count = self.restored_relations if self.restored else self.initial_relations
            return f"{count}\n".encode("ascii")
        if "alembic_version" in query:
            if self.revision_output is not None:
                return self.revision_output
            return f"{self.alembic_revision}\n".encode("ascii")
        raise AssertionError(f"unexpected capture operation: {executable}")

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        call = (tuple(arguments), dict(environment), timeout_seconds)
        self.run_calls.append(call)
        executable = Path(arguments[0]).stem
        if self.fail_run_for == executable:
            raise ContinuityError(ContinuityErrorCode.PROCESS_FAILED)
        if executable == "pg_dump":
            output = next(
                value.split("=", 1)[1] for value in arguments if value.startswith("--file=")
            )
            Path(output).write_bytes(self.archive)
            return
        if executable == "pg_restore" and "--list" in arguments:
            output = next(
                value.split("=", 1)[1] for value in arguments if value.startswith("--file=")
            )
            Path(output).write_bytes(self.toc)
            return
        if executable == "pg_restore":
            self.restored = True
            return
        raise AssertionError(f"unexpected run operation: {executable}")


def _toolchain(tmp_path: Path) -> PostgresToolchain:
    paths: list[Path] = []
    for name in ("pg_dump", "pg_restore", "psql"):
        path = (tmp_path / name).resolve()
        path.write_bytes(b"test executable")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        paths.append(path)
    return PostgresToolchain(*paths)


def _private_root(tmp_path: Path) -> Path:
    root = (tmp_path / "backups").resolve()
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    return root


def _environment(database: str = "corpuskit_source") -> dict[str, str]:
    return {
        "PGDATABASE": database,
        "PGHOST": "127.0.0.1",
        "PGPASSWORD": _PASSWORD_CANARY,
        "PGPORT": "5432",
        "PGUSER": "continuity-user-canary",
        "UNRELATED_SECRET": "must-not-reach-child",
    }


def _service(
    tmp_path: Path,
    *,
    root: Path | None = None,
    runner: FakeRunner | None = None,
    environment: Mapping[str, str] | None = None,
    clock: Any | None = None,
    monotonic: Any | None = None,
    nonce: Any | None = None,
) -> tuple[PostgresContinuity, FakeRunner, Path]:
    selected_root = root or _private_root(tmp_path)
    selected_runner = runner or FakeRunner()
    service = PostgresContinuity(
        selected_root,
        _toolchain(tmp_path),
        runner=selected_runner,
        process_environment=_environment() if environment is None else environment,
        clock=clock or AdvancingClock(),
        monotonic=monotonic or (lambda: 10.0),
        nonce=nonce or (lambda: _NONCE),
    )
    return service, selected_runner, selected_root


def _created_backup(
    tmp_path: Path,
) -> tuple[PostgresContinuity, FakeRunner, Path, BackupCreationReport]:
    service, runner, root = _service(tmp_path)
    return service, runner, root, service.create_backup()


def test_manifest_is_canonical_credential_free_and_digest_bound() -> None:
    archive_digest = hashlib.sha256(_ARCHIVE).hexdigest()
    bundle_id = f"ckpg_20260811T221530123456Z_{archive_digest[:12]}_0123456789ab"
    manifest = PostgresBackupManifest(
        bundle_id=bundle_id,
        created_at=_CREATED_AT,
        archive_sha256=archive_digest,
        archive_size_bytes=len(_ARCHIVE),
        toc_entry_count=1,
        pg_dump_version="17.9",
        pg_restore_version="17.8",
    )

    payload = manifest.canonical_file_bytes()

    assert parse_manifest_file(payload) == manifest
    assert payload.endswith(b"\n")
    assert manifest_digest_line(manifest) == f"{manifest.sha256}  manifest.json\n".encode()
    for forbidden in (b"host", b"password", b"username", b"database_url"):
        assert forbidden not in payload.lower()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"bundle_id": "ckpg_20260811T221530123456Z_000000000000_0123456789ab"}, "digest"),
        ({"pg_restore_version": "16.9"}, "same PostgreSQL major"),
        ({"created_at": _NAIVE_CREATED_AT}, "offset"),
    ],
)
def test_manifest_rejects_unbound_or_unsafe_metadata(
    changes: dict[str, object], message: str
) -> None:
    archive_digest = hashlib.sha256(_ARCHIVE).hexdigest()
    values: dict[str, object] = {
        "bundle_id": f"ckpg_20260811T221530123456Z_{archive_digest[:12]}_0123456789ab",
        "created_at": _CREATED_AT,
        "archive_sha256": archive_digest,
        "archive_size_bytes": len(_ARCHIVE),
        "toc_entry_count": 1,
        "pg_dump_version": "17.9",
        "pg_restore_version": "17.9",
    }
    values.update(changes)

    with pytest.raises(ValidationError, match=message):
        PostgresBackupManifest(**values)


def test_manifest_parser_rejects_semantically_valid_noncanonical_json() -> None:
    archive_digest = hashlib.sha256(_ARCHIVE).hexdigest()
    manifest = PostgresBackupManifest(
        bundle_id=f"ckpg_20260811T221530123456Z_{archive_digest[:12]}_0123456789ab",
        created_at=_CREATED_AT,
        archive_sha256=archive_digest,
        archive_size_bytes=len(_ARCHIVE),
        toc_entry_count=1,
        pg_dump_version="17.9",
        pg_restore_version="17.9",
    )
    noncanonical = json.dumps(manifest.model_dump(mode="json"), indent=2).encode()

    with pytest.raises(ValueError, match="canonical"):
        parse_manifest_file(noncanonical)


def test_restore_report_rejects_negative_duration() -> None:
    archive_digest = hashlib.sha256(_ARCHIVE).hexdigest()
    bundle_id = f"ckpg_20260811T221530123456Z_{archive_digest[:12]}_0123456789ab"

    with pytest.raises(ValidationError, match="cannot precede"):
        RestoreDrillReport(
            bundle_id=bundle_id,
            archive_sha256=archive_digest,
            started_at=_CREATED_AT,
            completed_at=_CREATED_AT - timedelta(seconds=1),
            restored_relation_count=1,
            alembic_revision="0006_maintenance_cursors",
            pg_restore_version="17.9",
        )


def test_create_backup_publishes_one_atomic_private_bundle(tmp_path: Path) -> None:
    service, runner, root = _service(tmp_path)

    report = service.create_backup()

    bundle = root / report.bundle_id
    assert {path.name for path in bundle.iterdir()} == {
        ARCHIVE_FILENAME,
        MANIFEST_FILENAME,
        MANIFEST_DIGEST_FILENAME,
    }
    assert bundle.joinpath(ARCHIVE_FILENAME).read_bytes() == _ARCHIVE
    manifest = parse_manifest_file(bundle.joinpath(MANIFEST_FILENAME).read_bytes())
    assert manifest.archive_sha256 == report.archive_sha256
    assert manifest.archive_size_bytes == len(_ARCHIVE)
    assert manifest.toc_entry_count == 1
    assert bundle.joinpath(MANIFEST_DIGEST_FILENAME).read_bytes() == manifest_digest_line(manifest)
    assert not list(root.glob("*.partial"))
    all_arguments = "\n".join(argument for call, _, _ in runner.run_calls for argument in call)
    assert _PASSWORD_CANARY not in all_arguments
    assert "continuity-user-canary" not in all_arguments
    assert "127.0.0.1" not in all_arguments
    dump_call = next(call for call, _, _ in runner.run_calls if Path(call[0]).stem == "pg_dump")
    assert {"--format=custom", "--no-owner", "--no-privileges", "--no-password"} <= set(dump_call)
    for _, child_environment, _, _ in runner.capture_calls:
        assert "UNRELATED_SECRET" not in child_environment


def test_backup_failure_removes_unpublished_partial_directory(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.fail_run_for = "pg_dump"
    service, _, root = _service(tmp_path, runner=runner)

    with pytest.raises(ContinuityError) as raised:
        service.create_backup()

    assert raised.value.code is ContinuityErrorCode.PROCESS_FAILED
    assert list(root.iterdir()) == []


def test_backup_refuses_final_collision_and_sanitizes_publish_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, root = _service(tmp_path)
    digest = hashlib.sha256(_ARCHIVE).hexdigest()
    collision = root / (
        f"ckpg_{_CREATED_AT.strftime('%Y%m%dT%H%M%S%fZ')}_{digest[:12]}_{_NONCE[:12]}"
    )
    collision.mkdir()
    with pytest.raises(ContinuityError) as exists:
        service.create_backup()
    assert exists.value.code is ContinuityErrorCode.INVALID_PATH
    assert not list(root.glob("*.partial"))

    collision.rmdir()
    monkeypatch.setattr(continuity.os, "rename", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ContinuityError) as publish:
        service.create_backup()
    assert publish.value.code is ContinuityErrorCode.PROCESS_FAILED
    assert list(root.iterdir()) == []


def test_offline_verification_needs_no_pg_connection_environment(tmp_path: Path) -> None:
    service, runner, root, created = _created_backup(tmp_path)
    offline = PostgresContinuity(
        root,
        service._tools,
        runner=runner,
        process_environment={},
        clock=AdvancingClock(),
        monotonic=lambda: 10.0,
        nonce=lambda: _NONCE,
    )

    verified = offline.verify_backup(created.bundle_id)

    assert verified.archive_sha256 == created.archive_sha256
    assert verified.manifest_sha256 == created.manifest_sha256
    assert verified.toc_entry_count == 1
    list_environments = [
        environment
        for call, environment, _ in runner.run_calls
        if Path(call[0]).stem == "pg_restore" and "--list" in call
    ]
    assert list_environments
    assert all(
        not any(key.startswith("PG") for key in environment) for environment in list_environments
    )


@pytest.mark.parametrize("member", [ARCHIVE_FILENAME, MANIFEST_FILENAME, MANIFEST_DIGEST_FILENAME])
def test_verification_detects_every_published_member_tamper(tmp_path: Path, member: str) -> None:
    service, _, root, created = _created_backup(tmp_path)
    target = root / created.bundle_id / member
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(ContinuityError) as raised:
        service.verify_backup(created.bundle_id)

    assert raised.value.code is ContinuityErrorCode.BACKUP_INTEGRITY


def test_verification_rejects_extra_member_and_bad_toc(tmp_path: Path) -> None:
    service, runner, root, created = _created_backup(tmp_path)
    bundle = root / created.bundle_id
    bundle.joinpath("unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ContinuityError) as extra:
        service.verify_backup(created.bundle_id)
    assert extra.value.code is ContinuityErrorCode.INVALID_PATH

    bundle.joinpath("unexpected.txt").unlink()
    runner.toc = b"; comments only\n"
    with pytest.raises(ContinuityError) as toc:
        service.verify_backup(created.bundle_id)
    assert toc.value.code is ContinuityErrorCode.BACKUP_INTEGRITY


def test_verification_binds_bundle_identity_tool_major_and_toc_count(tmp_path: Path) -> None:
    service, runner, root, created = _created_backup(tmp_path)
    source = root / created.bundle_id
    alternate_id = created.bundle_id[:-12] + "fedcba987654"
    shutil.copytree(source, root / alternate_id)
    with pytest.raises(ContinuityError) as identity:
        service.verify_backup(alternate_id)
    assert identity.value.code is ContinuityErrorCode.BACKUP_INTEGRITY

    manifest_path = source / MANIFEST_FILENAME
    manifest = parse_manifest_file(manifest_path.read_bytes())
    older = manifest.model_copy(update={"pg_dump_version": "16.9", "pg_restore_version": "16.9"})
    manifest_path.write_bytes(older.canonical_file_bytes())
    source.joinpath(MANIFEST_DIGEST_FILENAME).write_bytes(manifest_digest_line(older))
    with pytest.raises(ContinuityError) as major:
        service.verify_backup(created.bundle_id)
    assert major.value.code is ContinuityErrorCode.TOOL_VERSION

    manifest_path.write_bytes(manifest.canonical_file_bytes())
    source.joinpath(MANIFEST_DIGEST_FILENAME).write_bytes(manifest_digest_line(manifest))
    runner.toc += b"2; 0 0 TABLE public corpora corpuskit\n"
    with pytest.raises(ContinuityError) as toc:
        service.verify_backup(created.bundle_id)
    assert toc.value.code is ContinuityErrorCode.BACKUP_INTEGRITY


def test_restore_drill_requires_exact_confirmation_then_validates_schema(tmp_path: Path) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    target = "corpuskit_restore_drill_acceptance_01"
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(target),
    )
    confirmation = restore_confirmation(created.bundle_id, target)

    report = service.restore_drill(created.bundle_id, confirmation=confirmation)

    assert isinstance(report, RestoreDrillReport)
    assert report.restored_relation_count == 17
    assert report.alembic_revision == "0006_maintenance_cursors"
    assert report.completed_at > report.started_at
    restore_call = next(
        call
        for call, _, _ in runner.run_calls
        if Path(call[0]).stem == "pg_restore" and "--list" not in call
    )
    assert {"--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges"} <= set(
        restore_call
    )
    command_line = "\n".join(restore_call)
    assert _PASSWORD_CANARY not in command_line
    assert "continuity-user-canary" not in command_line
    assert "127.0.0.1" not in command_line


def test_restore_refuses_wrong_confirmation_before_database_query(tmp_path: Path) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    target = "corpuskit_restore_drill_refusal"
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(target),
    )
    captures_before = len(runner.capture_calls)

    with pytest.raises(ContinuityError) as raised:
        service.restore_drill(created.bundle_id, confirmation="not accepted")

    assert raised.value.code is ContinuityErrorCode.CONFIRMATION_REQUIRED
    later_captures = runner.capture_calls[captures_before:]
    assert all("--command" not in call for call, _, _, _ in later_captures)
    assert runner.restored is False


@pytest.mark.parametrize(
    "database",
    ["corpuskit", "production", "Corpuskit_restore_drill_upper", "corpuskit_restore_drill_bad-1"],
)
def test_restore_refuses_every_target_outside_ephemeral_namespace(
    tmp_path: Path, database: str
) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(database),
    )

    with pytest.raises(ContinuityError) as raised:
        service.restore_drill(created.bundle_id, confirmation="anything")

    assert raised.value.code is ContinuityErrorCode.UNSAFE_RESTORE_TARGET
    assert runner.restored is False


def test_restore_refuses_nonempty_target_without_mutation(tmp_path: Path) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    runner.initial_relations = 1
    target = "corpuskit_restore_drill_nonempty"
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(target),
    )

    with pytest.raises(ContinuityError) as raised:
        service.restore_drill(
            created.bundle_id,
            confirmation=restore_confirmation(created.bundle_id, target),
        )

    assert raised.value.code is ContinuityErrorCode.RESTORE_TARGET_NOT_EMPTY
    assert runner.restored is False


@pytest.mark.parametrize(
    ("relations", "revision"),
    [(0, "0006_maintenance_cursors"), (4, "invalid revision with spaces")],
)
def test_restore_postcondition_is_fail_closed(
    tmp_path: Path, relations: int, revision: str
) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    runner.restored_relations = relations
    runner.alembic_revision = revision
    target = "corpuskit_restore_drill_postcheck"
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(target),
    )

    with pytest.raises(ContinuityError) as raised:
        service.restore_drill(
            created.bundle_id,
            confirmation=restore_confirmation(created.bundle_id, target),
        )

    assert raised.value.code is ContinuityErrorCode.RESTORE_VALIDATION_FAILED


@pytest.mark.parametrize("count", [b"-1\n", b"not-a-count\n", b"50000001\n"])
def test_restore_rejects_invalid_or_excessive_relation_count(tmp_path: Path, count: bytes) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    runner.count_output = count
    target = "corpuskit_restore_drill_bad_count"
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(target),
    )

    with pytest.raises(ContinuityError) as raised:
        service.restore_drill(
            created.bundle_id,
            confirmation=restore_confirmation(created.bundle_id, target),
        )

    assert raised.value.code is ContinuityErrorCode.RESTORE_VALIDATION_FAILED
    assert runner.restored is False


def test_restore_rejects_non_ascii_scalar_output(tmp_path: Path) -> None:
    _, runner, root, created = _created_backup(tmp_path)
    runner.revision_output = b"revision-\xff\n"
    target = "corpuskit_restore_drill_non_ascii"
    service, _, _ = _service(
        tmp_path,
        root=root,
        runner=runner,
        environment=_environment(target),
    )

    with pytest.raises(ContinuityError) as raised:
        service.restore_drill(
            created.bundle_id,
            confirmation=restore_confirmation(created.bundle_id, target),
        )

    assert raised.value.code is ContinuityErrorCode.RESTORE_VALIDATION_FAILED


@pytest.mark.parametrize(
    ("versions", "code"),
    [
        (
            {
                "pg_dump": b"pg_dump (PostgreSQL) 17.9\n",
                "pg_restore": b"pg_restore (PostgreSQL) 16.9\n",
                "psql": b"psql (PostgreSQL) 17.9\n",
            },
            ContinuityErrorCode.TOOL_VERSION,
        ),
        (
            {
                "pg_dump": b"pg_dump (PostgreSQL) 15.9\n",
                "pg_restore": b"pg_restore (PostgreSQL) 15.9\n",
                "psql": b"psql (PostgreSQL) 15.9\n",
            },
            ContinuityErrorCode.TOOL_VERSION,
        ),
        (
            {
                "pg_dump": b"unexpected output containing secret\n",
                "pg_restore": b"pg_restore (PostgreSQL) 17.9\n",
                "psql": b"psql (PostgreSQL) 17.9\n",
            },
            ContinuityErrorCode.TOOL_VERSION,
        ),
    ],
)
def test_tool_versions_must_be_parseable_supported_and_same_major(
    tmp_path: Path,
    versions: dict[str, bytes],
    code: ContinuityErrorCode,
) -> None:
    runner = FakeRunner()
    runner.versions = versions
    service, _, _ = _service(tmp_path, runner=runner)

    with pytest.raises(ContinuityError) as raised:
        service.create_backup()

    assert raised.value.code is code


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"PGDATABASE": "source"},
        {"PGUSER": "user"},
        {"PGDATABASE": "source", "PGUSER": "bad\x00user"},
        {"PGDATABASE": "source", "PGUSER": "x" * 16_385},
    ],
)
def test_backup_requires_bounded_explicit_libpq_environment(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    service, _, _ = _service(tmp_path, environment=environment)

    with pytest.raises(ContinuityError) as raised:
        service.create_backup()

    assert raised.value.code is ContinuityErrorCode.INVALID_CONFIGURATION


@pytest.mark.parametrize(
    ("timeout", "maximum"),
    [(4.9, 1024), (14_401.0, 1024), (30.0, 0), (30.0, True), (30.0, 101 * 1024**4)],
)
def test_resource_limits_are_bounded_before_subprocesses(
    tmp_path: Path, timeout: float, maximum: int
) -> None:
    service, runner, _ = _service(tmp_path)

    with pytest.raises(ContinuityError) as raised:
        service.create_backup(timeout_seconds=timeout, max_archive_bytes=maximum)

    assert raised.value.code is ContinuityErrorCode.INVALID_CONFIGURATION
    assert runner.capture_calls == []
    assert runner.run_calls == []


def test_expired_deadline_and_naive_clock_fail_closed(tmp_path: Path) -> None:
    times = iter((0.0, 6.0))
    expired, runner, _ = _service(tmp_path, monotonic=lambda: next(times))
    with pytest.raises(ContinuityError) as timeout:
        expired.create_backup(timeout_seconds=5.0)
    assert timeout.value.code is ContinuityErrorCode.PROCESS_TIMEOUT
    assert runner.run_calls == []

    naive, _, _ = _service(tmp_path, clock=lambda: _NAIVE_CREATED_AT)
    with pytest.raises(ContinuityError) as clock:
        naive.create_backup()
    assert clock.value.code is ContinuityErrorCode.INVALID_CONFIGURATION


def test_invalid_nonce_and_archive_size_fail_without_publication(tmp_path: Path) -> None:
    invalid_nonce, _, root = _service(tmp_path, nonce=lambda: "unsafe")
    with pytest.raises(ContinuityError) as nonce:
        invalid_nonce.create_backup()
    assert nonce.value.code is ContinuityErrorCode.INVALID_CONFIGURATION
    assert list(root.iterdir()) == []

    runner = FakeRunner()
    runner.archive = b""
    empty, _, empty_root = _service(tmp_path, runner=runner)
    with pytest.raises(ContinuityError) as archive:
        empty.create_backup()
    assert archive.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    assert list(empty_root.iterdir()) == []


def test_partial_name_collision_is_never_reused(tmp_path: Path) -> None:
    service, _, root = _service(tmp_path)
    partial = root / f".ckpg-{_NONCE}.partial"
    partial.mkdir()

    with pytest.raises(ContinuityError) as raised:
        service.create_backup()

    assert raised.value.code is ContinuityErrorCode.INVALID_PATH
    assert partial.exists()


def test_restore_confirmation_validates_both_identifiers() -> None:
    with pytest.raises(ContinuityError) as bundle:
        restore_confirmation("../bundle", "corpuskit_restore_drill_safe")
    assert bundle.value.code is ContinuityErrorCode.INVALID_CONFIGURATION
    with pytest.raises(ContinuityError) as target:
        restore_confirmation(
            "ckpg_20260811T221530123456Z_0123456789ab_abcdef012345",
            "production",
        )
    assert target.value.code is ContinuityErrorCode.UNSAFE_RESTORE_TARGET


def test_strict_root_and_bundle_identifiers_reject_ambiguous_paths(tmp_path: Path) -> None:
    tools = _toolchain(tmp_path)
    relative = Path("relative-backups")
    with pytest.raises(ContinuityError) as path:
        PostgresContinuity(relative, tools, runner=FakeRunner())
    assert path.value.code is ContinuityErrorCode.INVALID_PATH

    service, _, _, created = _created_backup(tmp_path)
    for bundle in ("../escape", created.bundle_id.upper(), "", "ckpg_bad"):
        with pytest.raises(ContinuityError) as invalid:
            service.verify_backup(bundle)
        assert invalid.value.code is ContinuityErrorCode.INVALID_PATH

    missing_root = (tmp_path / "missing-root").resolve()
    with pytest.raises(ContinuityError) as missing:
        PostgresContinuity(missing_root, tools, runner=FakeRunner())
    assert missing.value.code is ContinuityErrorCode.INVALID_PATH
    file_root = (tmp_path / "root-file").resolve()
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ContinuityError) as not_directory:
        PostgresContinuity(file_root, tools, runner=FakeRunner())
    assert not_directory.value.code is ContinuityErrorCode.INVALID_PATH

    valid_missing_bundle = "ckpg_20260811T221530123456Z_0123456789ab_abcdef012345"
    with pytest.raises(ContinuityError) as absent_bundle:
        service.verify_backup(valid_missing_bundle)
    assert absent_bundle.value.code is ContinuityErrorCode.INVALID_PATH


def test_discover_toolchain_uses_absolute_regular_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = (tmp_path / "pgbin").resolve()
    directory.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("pg_dump", "pg_restore", "psql"):
        target = directory / f"{name}{suffix}"
        target.write_bytes(b"executable")
        target.chmod(target.stat().st_mode | stat.S_IXUSR)

    tools = PostgresToolchain.discover(directory)

    assert tools.pg_dump == (directory / f"pg_dump{suffix}").resolve()
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ContinuityError) as missing:
        PostgresToolchain.discover()
    assert missing.value.code is ContinuityErrorCode.TOOL_UNAVAILABLE

    mapping = {
        name: str(directory / f"{name}{suffix}") for name in ("pg_dump", "pg_restore", "psql")
    }
    monkeypatch.setattr("shutil.which", mapping.get)
    discovered = PostgresToolchain.discover()
    assert discovered.psql == Path(mapping["psql"])

    empty_directory = (tmp_path / "empty-pgbin").resolve()
    empty_directory.mkdir()
    with pytest.raises(ContinuityError) as incomplete:
        PostgresToolchain.discover(empty_directory)
    assert incomplete.value.code is ContinuityErrorCode.TOOL_UNAVAILABLE

    directory_candidate = (tmp_path / "directory-pgbin").resolve()
    directory_candidate.mkdir()
    (directory_candidate / f"pg_dump{suffix}").mkdir()
    with pytest.raises(ContinuityError) as nonregular:
        PostgresToolchain.discover(directory_candidate)
    assert nonregular.value.code is ContinuityErrorCode.TOOL_UNAVAILABLE


def test_missing_or_nonregular_tool_is_rejected_at_each_invocation(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    tools = _toolchain(tmp_path)
    tools.pg_dump.unlink()
    missing = PostgresContinuity(root, tools, runner=FakeRunner())
    with pytest.raises(ContinuityError) as unavailable:
        missing.create_backup()
    assert unavailable.value.code is ContinuityErrorCode.TOOL_UNAVAILABLE

    directory_tool = (tmp_path / "directory-tool").resolve()
    directory_tool.mkdir()
    invalid_tools = PostgresToolchain(directory_tool, tools.pg_restore, tools.psql)
    nonregular = PostgresContinuity(root, invalid_tools, runner=FakeRunner())
    with pytest.raises(ContinuityError) as invalid:
        nonregular.create_backup()
    assert invalid.value.code is ContinuityErrorCode.TOOL_UNAVAILABLE


def test_bounded_process_runner_discards_failures_and_limits_output(tmp_path: Path) -> None:
    del tmp_path
    runner = BoundedProcessRunner()
    environment = dict(os.environ)
    executable = str(Path(sys.executable).resolve())

    assert (
        runner.capture(
            (executable, "-c", "print('ok')"),
            environment=environment,
            timeout_seconds=5,
            max_output_bytes=16,
        ).strip()
        == b"ok"
    )
    runner.run(
        (executable, "-c", "pass"),
        environment=environment,
        timeout_seconds=5,
    )
    with pytest.raises(ContinuityError) as oversized:
        runner.capture(
            (executable, "-c", "print('x' * 20)"),
            environment=environment,
            timeout_seconds=5,
            max_output_bytes=4,
        )
    assert oversized.value.code is ContinuityErrorCode.PROCESS_FAILED
    with pytest.raises(ContinuityError) as failed:
        runner.run(
            (executable, "-c", "raise SystemExit(3)"),
            environment=environment,
            timeout_seconds=5,
        )
    assert failed.value.code is ContinuityErrorCode.PROCESS_FAILED
    with pytest.raises(ContinuityError) as capture_failed:
        runner.capture(
            (executable, "-c", "raise SystemExit(4)"),
            environment=environment,
            timeout_seconds=5,
            max_output_bytes=4,
        )
    assert capture_failed.value.code is ContinuityErrorCode.PROCESS_FAILED
    with pytest.raises(ContinuityError) as timeout:
        runner.capture(
            (executable, "-c", "import time; time.sleep(2)"),
            environment=environment,
            timeout_seconds=0.01,
            max_output_bytes=4,
        )
    assert timeout.value.code is ContinuityErrorCode.PROCESS_TIMEOUT
    with pytest.raises(ContinuityError) as run_timeout:
        runner.run(
            (executable, "-c", "import time; time.sleep(2)"),
            environment=environment,
            timeout_seconds=0.01,
        )
    assert run_timeout.value.code is ContinuityErrorCode.PROCESS_TIMEOUT
    missing_executable = str((Path.cwd() / "definitely-missing-executable").resolve())
    with pytest.raises(ContinuityError) as capture_os_error:
        runner.capture(
            (missing_executable,),
            environment=environment,
            timeout_seconds=5,
            max_output_bytes=4,
        )
    assert capture_os_error.value.code is ContinuityErrorCode.PROCESS_FAILED
    with pytest.raises(ContinuityError) as run_os_error:
        runner.run(
            (missing_executable,),
            environment=environment,
            timeout_seconds=5,
        )
    assert run_os_error.value.code is ContinuityErrorCode.PROCESS_FAILED


def test_file_security_primitives_fail_closed_on_indirection_and_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = (tmp_path / "payload").resolve()
    file_path.write_bytes(b"ab")
    deadline = continuity._Deadline.start(10, lambda: 0.0)

    monkeypatch.setattr(continuity, "_is_indirection", lambda _: True)
    with pytest.raises(ContinuityError) as read_link:
        continuity._read_regular_file(file_path, 10)
    assert read_link.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as hash_link:
        continuity._hash_regular_file(file_path, deadline=deadline, max_bytes=10)
    assert hash_link.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as private_link:
        continuity._make_private_file(file_path)
    assert private_link.value.code is ContinuityErrorCode.BACKUP_INTEGRITY

    monkeypatch.setattr(continuity, "_is_indirection", lambda _: False)
    with pytest.raises(ContinuityError) as missing:
        continuity._read_regular_file(tmp_path / "missing", 10)
    assert missing.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as too_large:
        continuity._read_regular_file(file_path, 1)
    assert too_large.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as hash_too_large:
        continuity._hash_regular_file(file_path, deadline=deadline, max_bytes=1)
    assert hash_too_large.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as missing_hash:
        continuity._hash_regular_file(tmp_path / "missing", deadline=deadline, max_bytes=10)
    assert missing_hash.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as directory:
        continuity._make_private_file(tmp_path)
    assert directory.value.code is ContinuityErrorCode.BACKUP_INTEGRITY
    with pytest.raises(ContinuityError) as missing_private:
        continuity._make_private_file(tmp_path / "missing")
    assert missing_private.value.code is ContinuityErrorCode.BACKUP_INTEGRITY

    with pytest.raises(ContinuityError) as unsafe_cleanup:
        continuity._remove_partial_directory(tmp_path.resolve(), file_path)
    assert unsafe_cleanup.value.code is ContinuityErrorCode.INVALID_PATH
    safe_missing = tmp_path.resolve() / f".ckpg-{_NONCE}.partial"
    continuity._remove_partial_directory(tmp_path.resolve(), safe_missing)


def test_file_security_primitives_detect_short_reads_growth_and_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = (tmp_path / "payload").resolve()
    file_path.write_bytes(b"ab")
    deadline = continuity._Deadline.start(10, lambda: 0.0)
    real_fstat = os.fstat

    with monkeypatch.context() as scoped:

        def reported_larger(descriptor: int) -> SimpleNamespace:
            actual = real_fstat(descriptor)
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_size=actual.st_size + 1,
                st_mtime_ns=actual.st_mtime_ns,
            )

        scoped.setattr(continuity.os, "fstat", reported_larger)
        with pytest.raises(ContinuityError) as short_read:
            continuity._read_regular_file(file_path, 10)
        assert short_read.value.code is ContinuityErrorCode.BACKUP_INTEGRITY

    with monkeypatch.context() as scoped:

        def reported_smaller(descriptor: int) -> SimpleNamespace:
            actual = real_fstat(descriptor)
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_size=1,
                st_mtime_ns=actual.st_mtime_ns,
            )

        scoped.setattr(continuity.os, "fstat", reported_smaller)
        with pytest.raises(ContinuityError) as growth:
            continuity._hash_regular_file(file_path, deadline=deadline, max_bytes=1)
        assert growth.value.code is ContinuityErrorCode.BACKUP_INTEGRITY

    with monkeypatch.context() as scoped:
        fstat_calls = 0

        def changed_mtime(descriptor: int) -> SimpleNamespace:
            nonlocal fstat_calls
            fstat_calls += 1
            actual = real_fstat(descriptor)
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_size=actual.st_size,
                st_mtime_ns=actual.st_mtime_ns + int(fstat_calls > 1),
            )

        scoped.setattr(continuity.os, "fstat", changed_mtime)
        with pytest.raises(ContinuityError) as mutation:
            continuity._hash_regular_file(file_path, deadline=deadline, max_bytes=10)
        assert mutation.value.code is ContinuityErrorCode.BACKUP_INTEGRITY


def test_indirection_probe_fails_closed_when_filesystem_metadata_errors() -> None:
    class ExplodingPath:
        def is_symlink(self) -> bool:
            raise OSError("metadata failure")

    assert continuity._is_indirection(ExplodingPath()) is True  # type: ignore[arg-type]


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_posix_rejects_public_roots_nonexecutables_and_bundle_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _toolchain(tmp_path)
    public_root = _private_root(tmp_path)
    public_root.chmod(0o722)
    with pytest.raises(ContinuityError) as writable:
        PostgresContinuity(public_root, tools, runner=FakeRunner())
    assert writable.value.code is ContinuityErrorCode.INVALID_PATH

    public_root.chmod(0o700)
    real_uid = os.geteuid()
    monkeypatch.setattr(continuity.os, "geteuid", lambda: real_uid + 1)
    with pytest.raises(ContinuityError) as ownership:
        PostgresContinuity(public_root, tools, runner=FakeRunner())
    assert ownership.value.code is ContinuityErrorCode.INVALID_PATH
    monkeypatch.setattr(continuity.os, "geteuid", lambda: real_uid)

    tools.pg_dump.chmod(0o600)
    service = PostgresContinuity(public_root, tools, runner=FakeRunner())
    with pytest.raises(ContinuityError) as executable:
        service.create_backup()
    assert executable.value.code is ContinuityErrorCode.TOOL_UNAVAILABLE
    tools.pg_dump.chmod(0o700)

    service, _, root = _service(tmp_path, root=public_root)
    created = service.create_backup()
    alternate_id = created.bundle_id[:-12] + "fedcba987654"
    (root / alternate_id).symlink_to(root / created.bundle_id, target_is_directory=True)
    with pytest.raises(ContinuityError) as symlink:
        service.verify_backup(alternate_id)
    assert symlink.value.code is ContinuityErrorCode.INVALID_PATH


def test_cli_success_and_all_failures_are_compact_and_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = BackupCreationReport(
        bundle_id=(
            f"ckpg_20260811T221530123456Z_{hashlib.sha256(_ARCHIVE).hexdigest()[:12]}_0123456789ab"
        ),
        archive_sha256=hashlib.sha256(_ARCHIVE).hexdigest(),
        archive_size_bytes=len(_ARCHIVE),
        manifest_sha256="a" * 64,
        created_at=_CREATED_AT,
    )
    monkeypatch.setattr(continuity_cli, "_execute", lambda _: report)
    assert continuity_cli.run(["backup", "--root", str(Path.cwd().resolve())]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["schema_id"] == "corpuskit.postgres-backup-created.v1"
    assert output.err == ""

    for code, expected_exit in (
        (ContinuityErrorCode.INVALID_PATH, 2),
        (ContinuityErrorCode.BACKUP_INTEGRITY, 1),
    ):

        def fail(_: object, *, selected: ContinuityErrorCode = code) -> None:
            raise ContinuityError(selected)

        monkeypatch.setattr(continuity_cli, "_execute", fail)
        assert continuity_cli.run(["backup", "--root", str(Path.cwd().resolve())]) == expected_exit
        failure = capsys.readouterr()
        parsed = json.loads(failure.err)
        assert parsed["error_code"] == code.value
        assert _PASSWORD_CANARY not in failure.err

    def unexpected(_: object) -> None:
        raise RuntimeError(_PASSWORD_CANARY)

    monkeypatch.setattr(continuity_cli, "_execute", unexpected)
    assert continuity_cli.run(["backup", "--root", str(Path.cwd().resolve())]) == 1
    failure = capsys.readouterr()
    assert "internal_error" in failure.err
    assert _PASSWORD_CANARY not in failure.err


def test_cli_dispatches_backup_verify_and_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    report = SimpleNamespace(model_dump=lambda **_: {})

    class FakeContinuity:
        def __init__(self, root: Path, tools: object) -> None:
            calls.append(("init", (root, tools), {}))

        def create_backup(self, **kwargs: object) -> object:
            calls.append(("backup", (), kwargs))
            return report

        def verify_backup(self, *args: object, **kwargs: object) -> object:
            calls.append(("verify", args, kwargs))
            return report

        def restore_drill(self, *args: object, **kwargs: object) -> object:
            calls.append(("restore", args, kwargs))
            return report

    tools = object()
    monkeypatch.setattr(continuity_cli.PostgresToolchain, "discover", lambda _: tools)
    monkeypatch.setattr(continuity_cli, "PostgresContinuity", FakeContinuity)
    root = Path.cwd().resolve()

    for argv, operation in (
        (["backup", "--root", str(root)], "backup"),
        (["verify", "--root", str(root), "--bundle", "bundle"], "verify"),
        (
            [
                "restore-drill",
                "--root",
                str(root),
                "--bundle",
                "bundle",
                "--confirm",
                "phrase",
            ],
            "restore",
        ),
    ):
        arguments = continuity_cli._parser().parse_args(argv)
        assert continuity_cli._execute(arguments) is report
        assert calls[-1][0] == operation


def test_cli_main_preserves_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(continuity_cli, "run", lambda: 7)

    with pytest.raises(SystemExit) as raised:
        continuity_cli.main()

    assert raised.value.code == 7
