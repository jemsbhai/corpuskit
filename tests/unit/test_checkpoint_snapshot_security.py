"""Reject unsafe checkpoint indexes before optional model libraries open their shards."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from corpuskit.adapters.corpusgen.model_runtime import (
    TransformersLocalModelLoader,
    compute_snapshot_digest,
)
from corpuskit.domain.errors import EngineUnavailableError
from corpuskit.domain.model_runtime import ImmutableModelPin, ModelDevice, ModelQuantization


def snapshot_with_index(tmp_path: Path, index: object) -> Path:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "model.safetensors").write_bytes(b"safe weights")
    (snapshot / "model.safetensors.index.json").write_text(json.dumps(index), encoding="utf-8")
    return snapshot


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "shard",
    [
        "../outside.safetensors",
        "nested/../../outside.safetensors",
        "/outside.safetensors",
        "C:/outside.safetensors",
        r"C:\outside.safetensors",
        r"\\server\share\outside.safetensors",
        "model.safetensors:stream",
        "model\x00.safetensors",
        "pytorch_model.bin",
        "missing.safetensors",
        "",
        None,
        7,
        ["model.safetensors"],
    ],
)
def test_checkpoint_index_rejects_unsafe_or_missing_shards(
    tmp_path: Path, shard: object, wrapped: bool
) -> None:
    (tmp_path / "outside.safetensors").write_bytes(b"outside the snapshot but inside the cache")
    weight_map = {"weight": shard}
    snapshot = snapshot_with_index(tmp_path, {"weight_map": weight_map} if wrapped else weight_map)

    with pytest.raises(EngineUnavailableError) as rejected:
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)

    assert str(tmp_path) not in str(rejected.value)


@pytest.mark.parametrize("index", [[], {}, {"weight_map": None}, {"weight_map": {}}])
def test_checkpoint_index_requires_nonempty_mapping(tmp_path: Path, index: object) -> None:
    snapshot = snapshot_with_index(tmp_path, index)

    with pytest.raises(EngineUnavailableError) as rejected:
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    assert rejected.value.operation == "model_runtime.local.shard_index"


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[" * 1100 + b"]" * 1100])
def test_checkpoint_index_rejects_malformed_content(tmp_path: Path, raw: bytes) -> None:
    snapshot = snapshot_with_index(tmp_path, {})
    (snapshot / "model.safetensors.index.json").write_bytes(raw)

    with pytest.raises(EngineUnavailableError) as rejected:
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    assert rejected.value.operation == "model_runtime.local.shard_index"


def test_checkpoint_index_has_bounded_json_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "corpuskit.adapters.corpusgen.model_runtime._MAX_CHECKPOINT_INDEX_BYTES", 128
    )
    snapshot = snapshot_with_index(tmp_path, {"weight_map": {"weight": "model.safetensors"}})
    index_path = snapshot / "model.safetensors.index.json"
    index_path.write_bytes(index_path.read_bytes() + b" " * 129)

    with pytest.raises(EngineUnavailableError) as rejected:
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    assert rejected.value.operation == "model_runtime.local.shard_index"


@pytest.mark.parametrize(
    ("wrapped", "expected_digest"),
    [
        (False, "f0fab9d67d206309c877305993ce5e1e9edd1ba1b370dbf2e3794c204b0e0781"),
        (True, "072d7caf07d362ef5c651ac3fd5573f7008c13f8737e897833ecd517b2e47368"),
    ],
)
def test_checkpoint_index_allows_verified_nested_shards_and_hub_blobs(
    tmp_path: Path, wrapped: bool, expected_digest: str
) -> None:
    weight_map = {
        "first": "./model.safetensors",
        "second": "shards/model-00002.safetensors",
        "tied_second": "shards/model-00002.safetensors",
        "third": "model-00003.safetensors",
    }
    index = {"metadata": {"total_size": 12}, "weight_map": weight_map} if wrapped else weight_map
    snapshot = snapshot_with_index(tmp_path, index)
    (snapshot / "shards").mkdir()
    (snapshot / "shards/model-00002.safetensors").write_bytes(b"nested weights")
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "hash").write_bytes(b"hub weights")
    (snapshot / "model-00003.safetensors").symlink_to(blobs / "hash")

    digest = compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    # These fixture digests also match the committed v1 implementation before index validation.
    assert digest == expected_digest
    (blobs / "hash").write_bytes(b"modified hub weights")
    assert compute_snapshot_digest(snapshot, approved_cache_root=tmp_path) != digest


def test_checkpoint_index_rejects_shard_symlink_outside_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    snapshot = snapshot_with_index(cache, {"weight_map": {"weight": "escape.safetensors"}})
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"unapproved weights")
    (snapshot / "escape.safetensors").symlink_to(outside)

    with pytest.raises(EngineUnavailableError):
        compute_snapshot_digest(snapshot, approved_cache_root=cache)


def test_checkpoint_index_rejects_unhashed_symlink_directory(tmp_path: Path) -> None:
    snapshot = snapshot_with_index(tmp_path, {"weight_map": {"weight": "linked/model.safetensors"}})
    outside = tmp_path / "unhashed"
    outside.mkdir()
    (outside / "model.safetensors").write_bytes(b"not included in the snapshot manifest")
    (snapshot / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineUnavailableError):
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)


def test_checkpoint_index_rejects_directory_shards(tmp_path: Path) -> None:
    snapshot = snapshot_with_index(tmp_path, {"weight_map": {"weight": "directory.safetensors"}})
    (snapshot / "directory.safetensors").mkdir()

    with pytest.raises(EngineUnavailableError):
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes require mkfifo")
@pytest.mark.parametrize("name", ["pipe.safetensors", "model.safetensors.index.json"])
def test_checkpoint_snapshot_rejects_named_pipes_without_opening(tmp_path: Path, name: str) -> None:
    snapshot = snapshot_with_index(tmp_path, {"weight_map": {"weight": "pipe.safetensors"}})
    pipe = snapshot / name
    pipe.unlink(missing_ok=True)
    os.mkfifo(pipe)

    with pytest.raises(EngineUnavailableError) as rejected:
        compute_snapshot_digest(snapshot, approved_cache_root=tmp_path)
    assert rejected.value.operation == "model_runtime.local.snapshot_boundary"


def test_unsafe_checkpoint_is_rejected_before_tokenizer_or_model_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = snapshot_with_index(tmp_path, {"weight_map": {"weight": "../outside.safetensors"}})
    calls: list[object] = []
    fake = ModuleType("transformers")
    fake.AutoTokenizer = SimpleNamespace(from_pretrained=lambda *args, **_: calls.append(args))
    fake.AutoModelForCausalLM = SimpleNamespace(
        from_pretrained=lambda *args, **_: calls.append(args)
    )
    monkeypatch.setitem(sys.modules, "transformers", fake)

    with pytest.raises(EngineUnavailableError) as rejected:
        TransformersLocalModelLoader(lambda _: snapshot, approved_cache_root=tmp_path).load(
            ImmutableModelPin(model="acme/tiny", revision="a" * 40),
            device=ModelDevice.CPU,
            quantization=ModelQuantization.NONE,
            artifact_sha256="b" * 64,
        )
    assert rejected.value.operation == "model_runtime.local.shard_index"
    assert calls == []
