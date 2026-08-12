"""Pinned PHOIBLE provisioning, redaction, and CLI contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corpusgen.inventory import phoible as corpusgen_phoible

from corpuskit.adapters.corpusgen import phoible_provisioning as provisioning_module
from corpuskit.adapters.corpusgen.phoible_provisioning import (
    PHOIBLE_FILENAME,
    PHOIBLE_URL,
    PhoibleCacheState,
    PhoibleProvisionAction,
    PhoibleProvisioningError,
    PhoibleSnapshot,
    PhoibleSnapshotProvisioner,
)
from corpuskit.operations import phoible_cli


def test_snapshot_identity_matches_pinned_corpusgen_017_contract() -> None:
    assert provisioning_module.PHOIBLE_COMMIT == corpusgen_phoible._PHOIBLE_COMMIT
    assert provisioning_module.PHOIBLE_SHA256 == corpusgen_phoible._PHOIBLE_CSV_SHA256
    assert provisioning_module.PHOIBLE_URL == corpusgen_phoible._PHOIBLE_CSV_URL


def _snapshot(payload: bytes) -> PhoibleSnapshot:
    return PhoibleSnapshot(
        revision="a" * 40,
        url=PHOIBLE_URL,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
    )


def _fetch(payload: bytes) -> provisioning_module.ChunkFetcher:
    def fetch(url: str, timeout: float, expected_bytes: int) -> Iterable[bytes]:
        assert url == PHOIBLE_URL
        assert 1 <= timeout <= 300
        assert expected_bytes == len(payload)
        return (payload[:2], b"", payload[2:])

    return fetch


def test_status_distinguishes_missing_wrong_size_wrong_hash_and_ready(tmp_path: Path) -> None:
    payload = b"pinned-data"
    provisioner = PhoibleSnapshotProvisioner(tmp_path, snapshot=_snapshot(payload))

    missing = provisioner.status()
    destination = tmp_path / PHOIBLE_FILENAME
    destination.parent.mkdir(exist_ok=True)
    destination.write_bytes(b"short")
    wrong_size = provisioner.status()
    destination.write_bytes(b"x" * len(payload))
    wrong_hash = provisioner.status()
    destination.write_bytes(payload)
    ready = provisioner.status()

    assert missing.state is PhoibleCacheState.MISSING
    assert missing.actual_bytes is None
    assert wrong_size.state is PhoibleCacheState.INVALID
    assert wrong_size.actual_bytes == 5
    assert wrong_hash.state is PhoibleCacheState.INVALID
    assert ready.state is PhoibleCacheState.READY
    assert ready.ready is True
    assert "path" not in ready.public_dict()
    assert "cache" not in ready.public_dict()


def test_status_rejects_non_regular_destination(tmp_path: Path) -> None:
    destination = tmp_path / PHOIBLE_FILENAME
    destination.mkdir()
    status = PhoibleSnapshotProvisioner(tmp_path, snapshot=_snapshot(b"payload")).status()

    assert status.state is PhoibleCacheState.INVALID
    assert status.actual_bytes is None


def test_status_sanitizes_filesystem_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"payload"
    destination = tmp_path / PHOIBLE_FILENAME
    destination.write_bytes(payload)
    real_stat = Path.stat

    def fail_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == destination:
            raise OSError("secret directory")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_stat)

    with pytest.raises(PhoibleProvisioningError) as raised:
        PhoibleSnapshotProvisioner(tmp_path, snapshot=_snapshot(payload)).status()

    assert raised.value.code == "cache_unreadable"
    assert "secret" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_provision_streams_verifies_and_atomically_installs(tmp_path: Path) -> None:
    payload = b"InventoryID,Phoneme\n1,p\n"
    provisioner = PhoibleSnapshotProvisioner(
        tmp_path,
        snapshot=_snapshot(payload),
        fetcher=_fetch(payload),
    )

    installed = provisioner.provision(timeout_seconds=5)
    unchanged = provisioner.provision(timeout_seconds=5)

    assert installed.action is PhoibleProvisionAction.INSTALLED
    assert unchanged.action is PhoibleProvisionAction.ALREADY_PRESENT
    assert installed.status.ready is True
    assert (tmp_path / PHOIBLE_FILENAME).read_bytes() == payload
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_provision_installs_owner_only_snapshot(tmp_path: Path) -> None:
    payload = b"InventoryID,Phoneme\n1,p\n"
    destination = tmp_path / PHOIBLE_FILENAME

    PhoibleSnapshotProvisioner(
        tmp_path,
        snapshot=_snapshot(payload),
        fetcher=_fetch(payload),
    ).provision(timeout_seconds=5)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_force_download_failure_preserves_valid_cache(tmp_path: Path) -> None:
    payload = b"known-good"
    destination = tmp_path / PHOIBLE_FILENAME
    destination.write_bytes(payload)
    provisioner = PhoibleSnapshotProvisioner(
        tmp_path,
        snapshot=_snapshot(payload),
        fetcher=_fetch(b"known-baad"),
    )

    with pytest.raises(PhoibleProvisioningError, match="SHA-256") as raised:
        provisioner.provision(force=True)

    assert raised.value.code == "checksum_mismatch"
    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        ((b"tiny",), "invalid_snapshot"),
        ((b"payload-plus",), "invalid_snapshot"),
        (("not-bytes",), "invalid_snapshot"),
    ],
)
def test_provision_rejects_wrong_size_and_non_bytes(
    tmp_path: Path, chunks: tuple[object, ...], code: str
) -> None:
    payload = b"payload"

    def fetch(_url: str, _timeout: float, _expected: int) -> Iterable[bytes]:
        return chunks  # type: ignore[return-value]

    provisioner = PhoibleSnapshotProvisioner(tmp_path, snapshot=_snapshot(payload), fetcher=fetch)

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision()

    assert raised.value.code == code
    assert not (tmp_path / PHOIBLE_FILENAME).exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_provision_from_offline_regular_file_never_calls_fetcher(tmp_path: Path) -> None:
    payload = b"air-gapped-snapshot"
    source = tmp_path / "source.csv"
    cache = tmp_path / "cache"
    source.write_bytes(payload)

    def forbidden_fetch(_url: str, _timeout: float, _expected: int) -> Iterable[bytes]:
        raise AssertionError("network fetcher must not run")

    result = PhoibleSnapshotProvisioner(
        cache, snapshot=_snapshot(payload), fetcher=forbidden_fetch
    ).provision(source_file=source)

    assert result.action is PhoibleProvisionAction.INSTALLED
    assert (cache / PHOIBLE_FILENAME).read_bytes() == payload


def test_provision_rejects_missing_offline_source_without_leaking_path(tmp_path: Path) -> None:
    source = tmp_path / "sensitive" / "dataset.csv"
    provisioner = PhoibleSnapshotProvisioner(tmp_path / "cache", snapshot=_snapshot(b"x"))

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision(source_file=source)

    assert raised.value.code == "source_unavailable"
    assert str(source) not in str(raised.value)


def test_provision_sanitizes_offline_source_metadata_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "secret-source.csv"
    real_is_symlink = Path.is_symlink

    def fail_source(path: Path) -> bool:
        if path == source:
            raise OSError("private mount")
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fail_source)
    provisioner = PhoibleSnapshotProvisioner(tmp_path / "cache", snapshot=_snapshot(b"x"))

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision(source_file=source)

    assert raised.value.code == "source_unavailable"
    assert str(source) not in str(raised.value)


@pytest.mark.parametrize("timeout", [0.0, 0.99, 300.01, 999.0])
def test_provision_rejects_unbounded_timeout(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(PhoibleProvisioningError) as raised:
        PhoibleSnapshotProvisioner(tmp_path, snapshot=_snapshot(b"x")).provision(
            timeout_seconds=timeout
        )

    assert raised.value.code == "invalid_timeout"


def test_provision_sanitizes_fetcher_failure(tmp_path: Path) -> None:
    def fail(_url: str, _timeout: float, _expected: int) -> Iterable[bytes]:
        raise OSError("credential=secret and private path")

    provisioner = PhoibleSnapshotProvisioner(tmp_path, snapshot=_snapshot(b"x"), fetcher=fail)

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision()

    assert raised.value.code == "download_failed"
    assert "secret" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_provision_reports_unwritable_cache_without_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        provisioning_module.tempfile,
        "mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("private cache path")),
    )
    provisioner = PhoibleSnapshotProvisioner(
        tmp_path, snapshot=_snapshot(b"x"), fetcher=_fetch(b"x")
    )

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision()

    assert raised.value.code == "cache_unwritable"
    assert str(tmp_path) not in str(raised.value)


def test_atomic_replace_failure_preserves_existing_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"known-good"
    destination = tmp_path / PHOIBLE_FILENAME
    destination.write_bytes(payload)
    monkeypatch.setattr(
        provisioning_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("private filesystem detail")),
    )
    provisioner = PhoibleSnapshotProvisioner(
        tmp_path, snapshot=_snapshot(payload), fetcher=_fetch(payload)
    )

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision(force=True)

    assert raised.value.code == "install_failed"
    assert "private" not in str(raised.value)
    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob(".*.tmp"))


def test_provision_enforces_post_install_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"payload"
    provisioner = PhoibleSnapshotProvisioner(
        tmp_path, snapshot=_snapshot(payload), fetcher=_fetch(payload)
    )
    real_status = provisioner.status
    statuses = iter(
        (
            real_status(),
            SimpleNamespace(ready=False),
        )
    )
    monkeypatch.setattr(provisioner, "status", lambda: next(statuses))

    with pytest.raises(PhoibleProvisioningError) as raised:
        provisioner.provision()

    assert raised.value.code == "install_verification_failed"


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        url: str,
        *,
        content_length: str | None = None,
    ) -> None:
        self._buffer = io.BytesIO(payload)
        self._url = url
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


def test_https_fetcher_requires_allowlisted_https_target() -> None:
    with pytest.raises(PhoibleProvisioningError) as raised:
        list(provisioning_module._https_chunks("http://example.test/data", 5, 1))

    assert raised.value.code == "unsafe_download_target"


@pytest.mark.parametrize(
    ("final_url", "content_length", "code"),
    [
        ("https://raw.githubusercontent.com/other", "7", "unexpected_redirect"),
        (PHOIBLE_URL, "invalid", "invalid_snapshot"),
        (PHOIBLE_URL, "8", "invalid_snapshot"),
    ],
)
def test_https_fetcher_rejects_redirect_or_bad_length(
    monkeypatch: pytest.MonkeyPatch,
    final_url: str,
    content_length: str,
    code: str,
) -> None:
    url = PHOIBLE_URL

    class FakeOpener:
        def open(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            return _FakeResponse(b"payload", final_url, content_length=content_length)

    monkeypatch.setattr(
        provisioning_module.urllib.request,
        "build_opener",
        lambda *_handlers: FakeOpener(),
    )

    with pytest.raises(PhoibleProvisioningError) as raised:
        list(provisioning_module._https_chunks(url, 5, 7))

    assert raised.value.code == code


def test_https_fetcher_streams_exact_response_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"payload"
    url = PHOIBLE_URL
    captured: dict[str, Any] = {}

    class FakeOpener:
        def open(self, request: Any, *, timeout: float) -> _FakeResponse:
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return _FakeResponse(payload, url, content_length=str(len(payload)))

    monkeypatch.setattr(
        provisioning_module.urllib.request,
        "build_opener",
        lambda *_handlers: FakeOpener(),
    )

    result = b"".join(provisioning_module._https_chunks(url, 12, len(payload)))

    assert result == payload
    assert captured["url"] == url
    assert captured["timeout"] == 12
    assert captured["headers"]["Accept-encoding"] == "identity"


def test_https_fetcher_allows_missing_content_length_when_hash_layer_will_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"payload"
    url = PHOIBLE_URL

    class FakeOpener:
        def open(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            return _FakeResponse(payload, url)

    monkeypatch.setattr(
        provisioning_module.urllib.request,
        "build_opener",
        lambda *_handlers: FakeOpener(),
    )

    assert b"".join(provisioning_module._https_chunks(url, 5, len(payload))) == payload


def test_https_fetcher_sanitizes_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenOpener:
        def open(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("proxy credential and private route")

    monkeypatch.setattr(
        provisioning_module.urllib.request,
        "build_opener",
        lambda *_handlers: BrokenOpener(),
    )

    with pytest.raises(PhoibleProvisioningError) as raised:
        list(provisioning_module._https_chunks(PHOIBLE_URL, 5, 1))

    assert raised.value.code == "download_failed"
    assert "credential" not in str(raised.value)


def test_offline_chunk_reader_sanitizes_read_failure(tmp_path: Path) -> None:
    missing = tmp_path / "private-source.csv"

    with pytest.raises(PhoibleProvisioningError) as raised:
        list(provisioning_module._file_chunks(missing))

    assert raised.value.code == "source_unavailable"
    assert str(missing) not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        PHOIBLE_URL.replace("https://", "http://"),
        PHOIBLE_URL.replace("https://", "https://user:password@"),
        PHOIBLE_URL.replace(".com/", ".com:444/"),
        f"{PHOIBLE_URL}?download=true",
        f"{PHOIBLE_URL}#fragment",
        "https://raw.githubusercontent.com:invalid/phoible.csv",
    ],
)
def test_https_fetcher_rejects_noncanonical_url_without_opening_network(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    network_called = False

    def forbidden_opener(*_handlers: object) -> object:
        nonlocal network_called
        network_called = True
        raise AssertionError("network must not be opened")

    monkeypatch.setattr(provisioning_module.urllib.request, "build_opener", forbidden_opener)

    with pytest.raises(PhoibleProvisioningError) as raised:
        list(provisioning_module._https_chunks(url, 5, 1))

    assert raised.value.code == "unsafe_download_target"
    assert network_called is False


def test_redirect_handler_rejects_before_a_second_host_is_contacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contacted: list[str] = []
    second_url = "https://attacker.example/phoible.csv"

    class RedirectingOpener:
        def __init__(self, handler: provisioning_module._RejectRedirects) -> None:
            self._handler = handler

        def open(self, request: Any, **_kwargs: object) -> object:
            contacted.append(request.full_url)
            redirected = self._handler.redirect_request(
                request,
                object(),
                302,
                "Found",
                {},
                second_url,
            )
            contacted.append(redirected.full_url)  # pragma: no cover - must not be reached
            raise AssertionError("redirect handler unexpectedly permitted a second request")

    def build_opener(handler: object) -> RedirectingOpener:
        assert isinstance(handler, provisioning_module._RejectRedirects)
        return RedirectingOpener(handler)

    monkeypatch.setattr(provisioning_module.urllib.request, "build_opener", build_opener)

    with pytest.raises(PhoibleProvisioningError) as raised:
        list(provisioning_module._https_chunks(PHOIBLE_URL, 5, 1))

    assert raised.value.code == "unexpected_redirect"
    assert contacted == [PHOIBLE_URL]


def test_directory_fsync_closes_descriptor_and_suppresses_platform_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(provisioning_module.os, "open", lambda *_args: 42)
    monkeypatch.setattr(
        provisioning_module.os, "fsync", lambda descriptor: calls.append(("fsync", descriptor))
    )
    monkeypatch.setattr(
        provisioning_module.os, "close", lambda descriptor: calls.append(("close", descriptor))
    )

    provisioning_module._fsync_directory(tmp_path)

    assert calls == [("fsync", 42), ("close", 42)]

    monkeypatch.setattr(
        provisioning_module.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("unsupported")),
    )
    monkeypatch.setattr(
        provisioning_module.os,
        "close",
        lambda _descriptor: (_ for _ in ()).throw(OSError("already closed")),
    )
    provisioning_module._fsync_directory(tmp_path)


def test_cli_status_and_provision_emit_deterministic_path_free_json(tmp_path: Path) -> None:
    payload = b"payload"
    source = tmp_path / "sensitive-source.csv"
    source.write_bytes(payload)
    cache = tmp_path / "private-cache"
    provisioner = PhoibleSnapshotProvisioner(cache, snapshot=_snapshot(payload))
    provision_stdout = io.StringIO()
    provision_stderr = io.StringIO()

    provision_exit = phoible_cli.run(
        ["provision", "--source-file", str(source), "--json"],
        provisioner=provisioner,
        stdout=provision_stdout,
        stderr=provision_stderr,
    )
    status_stdout = io.StringIO()
    status_exit = phoible_cli.run(
        ["status", "--json"], provisioner=provisioner, stdout=status_stdout
    )

    provision_output = provision_stdout.getvalue()
    status_payload = json.loads(status_stdout.getvalue())
    assert provision_exit == 0
    assert status_exit == 0
    assert provision_stderr.getvalue() == ""
    assert json.loads(provision_output)["action"] == "installed"
    assert status_payload["state"] == "ready"
    assert "path" not in provision_output
    assert str(source) not in provision_output
    assert str(cache) not in provision_output


def test_cli_returns_not_ready_and_redacts_unexpected_failures(tmp_path: Path) -> None:
    missing = PhoibleSnapshotProvisioner(tmp_path / "missing-cache", snapshot=_snapshot(b"x"))
    stdout = io.StringIO()

    assert phoible_cli.run(["status"], provisioner=missing, stdout=stdout) == 1
    assert stdout.getvalue().startswith("PHOIBLE missing:")

    broken = SimpleNamespace(status=lambda: (_ for _ in ()).throw(OSError("top secret")))
    stderr = io.StringIO()
    exit_code = phoible_cli.run(
        ["status"],
        provisioner=broken,
        stdout=io.StringIO(),
        stderr=stderr,  # type: ignore[arg-type]
    )

    assert exit_code == 1
    assert "OSError" in stderr.getvalue()
    assert "secret" not in stderr.getvalue()


def test_cli_reports_stable_provisioning_error_code() -> None:
    class BrokenProvisioner:
        def provision(self, **_kwargs: object) -> None:
            raise PhoibleProvisioningError("checksum_mismatch", "Snapshot verification failed.")

    stderr = io.StringIO()
    exit_code = phoible_cli.run(
        ["provision"],
        provisioner=BrokenProvisioner(),  # type: ignore[arg-type]
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert stderr.getvalue() == (
        "PHOIBLE provisioning failed [checksum_mismatch]: Snapshot verification failed.\n"
    )


def test_cli_plain_provision_output_and_main_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"payload"
    source = tmp_path / "source.csv"
    source.write_bytes(payload)
    output = io.StringIO()
    provisioner = PhoibleSnapshotProvisioner(tmp_path / "cache", snapshot=_snapshot(payload))

    assert (
        phoible_cli.run(
            ["provision", "--source-file", str(source)],
            provisioner=provisioner,
            stdout=output,
        )
        == 0
    )
    assert output.getvalue().startswith("PHOIBLE ready: action=installed")

    monkeypatch.setattr(phoible_cli, "run", lambda: 7)
    with pytest.raises(SystemExit) as raised:
        phoible_cli.main()
    assert raised.value.code == 7
