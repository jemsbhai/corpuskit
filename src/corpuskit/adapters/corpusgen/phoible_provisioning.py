"""Offline-safe provisioning for CorpusGen's pinned PHOIBLE snapshot.

The ordinary API and worker processes never call this module's network fetcher. Operators
invoke the narrow provisioning CLI explicitly, or run the equivalent one-shot deployment
job, before enabling the DATA runtime profile.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PHOIBLE_COMMIT = "b92abff4f4ca2544eece4d9eff5c707f8d508d0c"
PHOIBLE_SHA256 = "395e0977c3a5402af9cd5effd4ffdf0e47396336241fac534a4706e3cd8a7ecf"
PHOIBLE_BYTES = 24_578_868
PHOIBLE_URL = f"https://raw.githubusercontent.com/phoible/dev/{PHOIBLE_COMMIT}/data/phoible.csv"
PHOIBLE_FILENAME = "phoible.csv"

_CHUNK_BYTES = 64 * 1024
_ALLOWED_DOWNLOAD_HOST = "raw.githubusercontent.com"


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Fail before urllib follows any redirect to a second network location."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise PhoibleProvisioningError(
            "unexpected_redirect", "The pinned PHOIBLE endpoint redirected unexpectedly."
        )


@dataclass(frozen=True, slots=True)
class PhoibleSnapshot:
    """Immutable supply-chain identity for one PHOIBLE CSV snapshot."""

    revision: str
    url: str
    sha256: str
    byte_count: int
    filename: str = PHOIBLE_FILENAME


PINNED_PHOIBLE_SNAPSHOT = PhoibleSnapshot(
    revision=PHOIBLE_COMMIT,
    url=PHOIBLE_URL,
    sha256=PHOIBLE_SHA256,
    byte_count=PHOIBLE_BYTES,
)


class PhoibleCacheState(StrEnum):
    """Public, path-free state of the local pinned snapshot."""

    READY = "ready"
    MISSING = "missing"
    INVALID = "invalid"


class PhoibleProvisionAction(StrEnum):
    """Whether a provisioning invocation changed the cache."""

    INSTALLED = "installed"
    ALREADY_PRESENT = "already_present"


@dataclass(frozen=True, slots=True)
class PhoibleCacheStatus:
    """Sanitized snapshot status safe for logs and operator automation."""

    state: PhoibleCacheState
    revision: str
    expected_sha256: str
    expected_bytes: int
    actual_bytes: int | None

    @property
    def ready(self) -> bool:
        """Return whether the exact pinned bytes are installed."""

        return self.state is PhoibleCacheState.READY

    def public_dict(self) -> dict[str, bool | int | str | None]:
        """Return a stable representation that deliberately omits filesystem paths."""

        return {
            "state": self.state.value,
            "ready": self.ready,
            "revision": self.revision,
            "sha256": self.expected_sha256,
            "expected_bytes": self.expected_bytes,
            "actual_bytes": self.actual_bytes,
        }


@dataclass(frozen=True, slots=True)
class PhoibleProvisionResult:
    """Successful provisioning outcome."""

    action: PhoibleProvisionAction
    status: PhoibleCacheStatus

    def public_dict(self) -> dict[str, bool | int | str | None]:
        """Return stable, path-free output for the CLI and deployment logs."""

        return {"action": self.action.value, **self.status.public_dict()}


class PhoibleProvisioningError(RuntimeError):
    """Provisioning failure with a stable public code and redacted message."""

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code


ChunkFetcher = Callable[[str, float, int], Iterable[bytes]]


class PhoibleSnapshotProvisioner:
    """Verify and atomically install the exact CorpusGen 0.1.7 data snapshot."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        snapshot: PhoibleSnapshot = PINNED_PHOIBLE_SNAPSHOT,
        fetcher: ChunkFetcher | None = None,
    ) -> None:
        self._cache_dir = cache_dir if cache_dir is not None else Path.home() / ".corpusgen"
        self._snapshot = snapshot
        self._fetcher = fetcher or _https_chunks

    def status(self) -> PhoibleCacheStatus:
        """Hash the installed file and report readiness without returning its path."""

        destination = self._destination
        try:
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                return self._status(PhoibleCacheState.INVALID, actual_bytes=None)
            if not destination.exists():
                return self._status(PhoibleCacheState.MISSING, actual_bytes=None)
            actual_bytes = destination.stat().st_size
            if actual_bytes != self._snapshot.byte_count:
                return self._status(PhoibleCacheState.INVALID, actual_bytes=actual_bytes)
            actual_sha256 = _sha256_file(destination)
        except OSError:
            raise PhoibleProvisioningError(
                "cache_unreadable",
                "The PHOIBLE cache could not be verified; filesystem details were redacted.",
            ) from None

        state = (
            PhoibleCacheState.READY
            if actual_sha256 == self._snapshot.sha256
            else PhoibleCacheState.INVALID
        )
        return self._status(state, actual_bytes=actual_bytes)

    def provision(
        self,
        *,
        source_file: Path | None = None,
        force: bool = False,
        timeout_seconds: float = 30.0,
    ) -> PhoibleProvisionResult:
        """Install the pinned bytes from HTTPS or a trusted offline source.

        Bytes are streamed into the destination directory, bounded by the exact pinned
        length, fsynced, and checksum-verified before ``os.replace`` makes them visible.
        Therefore a partial, oversized, or corrupt transfer cannot replace an existing
        valid snapshot.
        """

        if not 1.0 <= timeout_seconds <= 300.0:
            raise PhoibleProvisioningError(
                "invalid_timeout", "The download timeout must be between 1 and 300 seconds."
            )

        current = self.status()
        if current.ready and not force:
            return PhoibleProvisionResult(PhoibleProvisionAction.ALREADY_PRESENT, current)

        if source_file is None:
            failure_code = "download_failed"
            failure_message = (
                "The pinned PHOIBLE snapshot could not be downloaded; details were redacted."
            )
        else:
            try:
                source_is_valid = not source_file.is_symlink() and source_file.is_file()
            except OSError:
                source_is_valid = False
            if not source_is_valid:
                raise PhoibleProvisioningError(
                    "source_unavailable",
                    "The offline PHOIBLE source must be a readable regular file.",
                )
            failure_code = "source_unavailable"
            failure_message = "The offline PHOIBLE source could not be read; details were redacted."

        try:
            chunks = (
                self._fetcher(
                    self._snapshot.url,
                    timeout_seconds,
                    self._snapshot.byte_count,
                )
                if source_file is None
                else _file_chunks(source_file)
            )
            self._atomic_install(chunks)
        except PhoibleProvisioningError:
            raise
        except Exception:
            raise PhoibleProvisioningError(failure_code, failure_message) from None

        installed = self.status()
        if not installed.ready:  # defensive postcondition around filesystem replacement
            raise PhoibleProvisioningError(
                "install_verification_failed",
                "The installed PHOIBLE snapshot did not pass post-install verification.",
            )
        return PhoibleProvisionResult(PhoibleProvisionAction.INSTALLED, installed)

    @property
    def _destination(self) -> Path:
        return self._cache_dir / self._snapshot.filename

    def _status(self, state: PhoibleCacheState, *, actual_bytes: int | None) -> PhoibleCacheStatus:
        return PhoibleCacheStatus(
            state=state,
            revision=self._snapshot.revision,
            expected_sha256=self._snapshot.sha256,
            expected_bytes=self._snapshot.byte_count,
            actual_bytes=actual_bytes,
        )

    def _atomic_install(self, chunks: Iterable[bytes]) -> None:
        try:
            self._cache_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
            file_descriptor, raw_temp_path = tempfile.mkstemp(
                prefix=".phoible-", suffix=".tmp", dir=self._cache_dir
            )
        except OSError:
            raise PhoibleProvisioningError(
                "cache_unwritable",
                "The PHOIBLE cache could not be prepared; filesystem details were redacted.",
            ) from None

        temp_path = Path(raw_temp_path)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(file_descriptor, "wb") as destination:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise PhoibleProvisioningError(
                            "invalid_snapshot", "The PHOIBLE source returned invalid bytes."
                        )
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._snapshot.byte_count:
                        raise PhoibleProvisioningError(
                            "invalid_snapshot",
                            "The PHOIBLE source exceeded the pinned snapshot size.",
                        )
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())

            if total != self._snapshot.byte_count:
                raise PhoibleProvisioningError(
                    "invalid_snapshot", "The PHOIBLE source size did not match the pinned snapshot."
                )
            if digest.hexdigest() != self._snapshot.sha256:
                raise PhoibleProvisioningError(
                    "checksum_mismatch",
                    "The PHOIBLE source failed pinned SHA-256 verification.",
                )

            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self._destination)
            _fsync_directory(self._cache_dir)
        except PhoibleProvisioningError:
            raise
        except OSError:
            raise PhoibleProvisioningError(
                "install_failed",
                "The PHOIBLE snapshot could not be installed; filesystem details were redacted.",
            ) from None
        finally:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)


def _https_chunks(url: str, timeout_seconds: float, expected_bytes: int) -> Iterator[bytes]:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        url != PHOIBLE_URL
        or parsed.scheme != "https"
        or parsed.hostname != _ALLOWED_DOWNLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise PhoibleProvisioningError(
            "unsafe_download_target", "The pinned PHOIBLE download target is not allowlisted."
        )

    request = urllib.request.Request(  # noqa: S310 - HTTPS and host validated above.
        url,
        headers={
            "Accept": "text/csv",
            "Accept-Encoding": "identity",
            "User-Agent": "CorpusKit-PHOIBLE-Provisioner/0.1",
        },
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        # The URL was restricted to HTTPS on the single allowlisted host above.
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise PhoibleProvisioningError(
                    "unexpected_redirect", "The pinned PHOIBLE endpoint redirected unexpectedly."
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_bytes = int(content_length)
                except ValueError:
                    raise PhoibleProvisioningError(
                        "invalid_snapshot", "The PHOIBLE endpoint returned an invalid length."
                    ) from None
                if declared_bytes != expected_bytes:
                    raise PhoibleProvisioningError(
                        "invalid_snapshot",
                        "The PHOIBLE endpoint length did not match the pinned snapshot.",
                    )
            while chunk := response.read(_CHUNK_BYTES):
                yield chunk
    except PhoibleProvisioningError:
        raise
    except Exception:
        raise PhoibleProvisioningError(
            "download_failed",
            "The pinned PHOIBLE snapshot could not be downloaded; details were redacted.",
        ) from None


def _file_chunks(path: Path) -> Iterator[bytes]:
    try:
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK_BYTES):
                yield chunk
    except OSError:
        raise PhoibleProvisioningError(
            "source_unavailable",
            "The offline PHOIBLE source could not be read; details were redacted.",
        ) from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """Persist the directory entry where the platform supports directory fsync."""

    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


__all__ = [
    "PHOIBLE_BYTES",
    "PHOIBLE_COMMIT",
    "PHOIBLE_FILENAME",
    "PHOIBLE_SHA256",
    "PHOIBLE_URL",
    "PINNED_PHOIBLE_SNAPSHOT",
    "PhoibleCacheState",
    "PhoibleCacheStatus",
    "PhoibleProvisionAction",
    "PhoibleProvisionResult",
    "PhoibleProvisioningError",
    "PhoibleSnapshot",
    "PhoibleSnapshotProvisioner",
]
