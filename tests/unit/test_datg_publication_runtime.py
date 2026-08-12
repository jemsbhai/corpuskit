"""Fail-closed DATG parent-publication runtime configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from corpuskit.config import Settings
from corpuskit.domain.datg import DatgQuantization, DatgRuntimePolicyEntry, DatgSnapshotPin
from corpuskit.persistence.datg_cache import FilesystemDatgIndexPublisher
from corpuskit.worker import runtime


def _policy() -> DatgRuntimePolicyEntry:
    pin = DatgSnapshotPin(
        repository_id="acme/tiny-datg",
        revision="a" * 40,
        snapshot_sha256="b" * 64,
    )
    return DatgRuntimePolicyEntry(
        runtime_id="tiny-datg",
        model=pin,
        tokenizer=pin,
        allowed_quantizations=(DatgQuantization.NONE,),
    )


def _settings(
    *,
    worker_profile: Literal["batch-cpu", "gpu-inference"] = "batch-cpu",
    publication_root: Path | None = None,
) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        worker_profile=worker_profile,
        temporal_task_queue=worker_profile,
        worker_datg_runtime_policies=(_policy(),),
        worker_datg_index_publish_root=publication_root,
        _env_file=None,
    )


def test_batch_parent_requires_preprovisioned_absolute_publication_root(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="absolute parent publication root"):
        runtime._datg_index_publisher(_settings())

    missing = (tmp_path / "missing").resolve()
    with pytest.raises(RuntimeError, match="must be provisioned"):
        runtime._datg_index_publisher(_settings(publication_root=missing))

    root = (tmp_path / "published").resolve()
    root.mkdir()
    publisher = runtime._datg_index_publisher(_settings(publication_root=root))
    assert publisher == FilesystemDatgIndexPublisher(root)


def test_non_batch_worker_cannot_receive_publication_root(tmp_path: Path) -> None:
    root = (tmp_path / "published").resolve()
    root.mkdir()
    with pytest.raises(RuntimeError, match="only the DATG batch worker"):
        runtime._datg_index_publisher(
            _settings(
                worker_profile="gpu-inference",
                publication_root=root,
            )
        )
