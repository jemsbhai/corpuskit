"""Read-only, content-addressed DATG index access for control-plane inspection."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from corpuskit.domain.datg import MAX_DATG_INDEX_BYTES, DatgIndexArtifact
from corpuskit.domain.errors import EngineUnavailableError

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


def canonical_datg_index_bytes(artifact: DatgIndexArtifact) -> bytes:
    """Serialize the already integrity-checked index using one stable representation."""

    payload = json.dumps(
        artifact.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not payload or len(payload) > MAX_DATG_INDEX_BYTES:
        raise EngineUnavailableError("datg.index.publication_size")
    return payload


def read_only_datg_cache_available(root: Path | None, *, declared_read_only: bool) -> bool:
    """Return whether an inspection root is present without opening an artifact."""

    if root is None or not declared_read_only or not root.is_absolute():
        return False
    try:
        return root.resolve(strict=True).is_dir()
    except (OSError, RuntimeError):
        return False


@dataclass(frozen=True, slots=True)
class ReadOnlyFilesystemDatgIndexCache:
    """Read one pre-provisioned index without creating or modifying cache files."""

    root: Path

    def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
        if _SHA256.fullmatch(cache_key_sha256) is None:
            raise EngineUnavailableError("datg.index.cache_key")
        try:
            root = self.root.resolve(strict=True)
            if not root.is_dir() or not self.root.is_absolute():
                raise EngineUnavailableError("datg.index.cache_boundary")
            candidate = root / f"{cache_key_sha256}.json"
            if candidate.is_symlink():
                raise EngineUnavailableError("datg.index.cache_boundary")
            if not candidate.exists():
                return None
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                raise EngineUnavailableError("datg.index.cache_boundary")
            if resolved.stat().st_size > MAX_DATG_INDEX_BYTES:
                raise EngineUnavailableError("datg.index.cache_size")
            artifact = DatgIndexArtifact.model_validate_json(resolved.read_bytes(), strict=True)
            if artifact.identity.cache_key_sha256 != cache_key_sha256:
                raise EngineUnavailableError("datg.index.cache_identity")
            return artifact
        except EngineUnavailableError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.index.cache") from None


@dataclass(frozen=True, slots=True)
class FilesystemDatgIndexPublisher:
    """Atomically publish parent-verified indexes into one dedicated writable root."""

    root: Path

    def publish(self, artifact: DatgIndexArtifact) -> int:
        key = artifact.identity.cache_key_sha256
        if _SHA256.fullmatch(key) is None:
            raise EngineUnavailableError("datg.index.publication_identity")
        payload = canonical_datg_index_bytes(artifact)
        temporary: Path | None = None
        try:
            root = self.root.resolve(strict=True)
            if not self.root.is_absolute() or not root.is_dir():
                raise EngineUnavailableError("datg.index.publication_boundary")
            candidate = root / f"{key}.json"
            if candidate.is_symlink():
                raise EngineUnavailableError("datg.index.publication_boundary")
            if candidate.exists():
                self._verify_existing(candidate, root, artifact)
                _sync_directory(root)
                return len(payload)

            descriptor, temporary_name = tempfile.mkstemp(
                dir=root,
                prefix=f".{key}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            resolved_temporary = temporary.resolve(strict=True)
            if not resolved_temporary.is_file() or not resolved_temporary.is_relative_to(root):
                raise EngineUnavailableError("datg.index.publication_boundary")
            with suppress(FileExistsError):
                os.link(resolved_temporary, candidate)
            self._verify_existing(candidate, root, artifact)
            _sync_directory(root)
            return len(payload)
        except EngineUnavailableError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.index.publication") from None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_existing(
        candidate: Path,
        root: Path,
        expected: DatgIndexArtifact,
    ) -> None:
        try:
            if candidate.is_symlink():
                raise EngineUnavailableError("datg.index.publication_boundary")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                raise EngineUnavailableError("datg.index.publication_boundary")
            size = resolved.stat().st_size
            if size < 1 or size > MAX_DATG_INDEX_BYTES:
                raise EngineUnavailableError("datg.index.publication_size")
            observed = DatgIndexArtifact.model_validate_json(resolved.read_bytes(), strict=True)
            if observed != expected:
                raise EngineUnavailableError("datg.index.publication_conflict")
        except EngineUnavailableError:
            raise
        except Exception:
            raise EngineUnavailableError("datg.index.publication_integrity") from None


def _sync_directory(root: Path) -> None:
    """Persist a newly linked directory entry on production POSIX filesystems."""

    if os.name == "nt":
        return
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class UnavailableDatgIndexCache:
    """Typed fail-closed cache used when no read-only inspection mount is configured."""

    def get(self, cache_key_sha256: str) -> DatgIndexArtifact | None:
        del cache_key_sha256
        raise EngineUnavailableError("datg.index.inspection_unavailable")


__all__ = [
    "FilesystemDatgIndexPublisher",
    "ReadOnlyFilesystemDatgIndexCache",
    "UnavailableDatgIndexCache",
    "canonical_datg_index_bytes",
    "read_only_datg_cache_available",
]
