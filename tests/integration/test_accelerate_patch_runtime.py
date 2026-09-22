"""Exercise the actual patched Accelerate APIs with tiny CPU checkpoints and hostile indexes."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.security.accelerate_patch import verify_installed  # noqa: E402

torch = pytest.importorskip("torch", reason="the local-model profile requires PyTorch")
accelerate = pytest.importorskip("accelerate", reason="the local-model profile requires Accelerate")
safetensors = pytest.importorskip("safetensors.torch")
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def verified_distribution() -> None:
    assert verify_installed()["installed_verified"] is True


@pytest.fixture(params=["load_checkpoint_in_model", "load_checkpoint_and_dispatch"])
def load_checkpoint(request: pytest.FixtureRequest) -> Callable[[Path], Any]:
    if request.param == "load_checkpoint_in_model":
        from accelerate.utils import load_checkpoint_in_model

        loader = load_checkpoint_in_model
    else:
        loader = accelerate.load_checkpoint_and_dispatch

    def load(checkpoint: Path) -> Any:
        model = torch.nn.Linear(2, 2, bias=False)
        with torch.no_grad():
            model.weight.zero_()
        loader(model, str(checkpoint), device_map={"": "cpu"})
        return model

    return load


def _weights() -> dict[str, Any]:
    return {"weight": torch.arange(4, dtype=torch.float32).reshape(2, 2)}


def _index(snapshot: Path, shard: object, *, wrapped: bool = True) -> Path:
    snapshot.mkdir(parents=True, exist_ok=True)
    mapping = {"weight": shard}
    index = snapshot / "model.safetensors.index.json"
    index.write_text(json.dumps({"weight_map": mapping} if wrapped else mapping), encoding="utf-8")
    return index


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("extension", ["bin", "safetensors"])
@pytest.mark.parametrize("direct_index", [False, True])
def test_real_loaders_preserve_sharded_formats(
    tmp_path: Path,
    load_checkpoint: Callable[[Path], Any],
    wrapped: bool,
    extension: str,
    direct_index: bool,
) -> None:
    snapshot = tmp_path / "snapshot"
    shard_name = f"shards/part.{extension}"
    index = _index(snapshot, shard_name, wrapped=wrapped)
    (snapshot / "shards").mkdir()
    shard = snapshot / shard_name
    if extension == "safetensors":
        safetensors.save_file(_weights(), str(shard))
    else:
        torch.save(_weights(), shard)

    loaded = load_checkpoint(index if direct_index else snapshot)
    assert torch.equal(loaded.weight, _weights()["weight"])


def test_real_loaders_preserve_hub_blob_symlinks(
    tmp_path: Path, load_checkpoint: Callable[[Path], Any]
) -> None:
    repository = tmp_path / "models--acme--tiny"
    blobs = repository / "blobs"
    snapshot = repository / "snapshots" / ("a" * 40)
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    weight_blob = blobs / ("b" * 64)
    safetensors.save_file(_weights(), str(weight_blob))
    index_blob = blobs / ("c" * 64)
    index_blob.write_text(
        json.dumps({"weight_map": {"weight": "model-00001.safetensors"}}), encoding="utf-8"
    )
    (snapshot / "model-00001.safetensors").symlink_to(weight_blob)
    (snapshot / "model.safetensors.index.json").symlink_to(index_blob)

    loaded = load_checkpoint(snapshot)
    assert torch.equal(loaded.weight, _weights()["weight"])


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "shard",
    [
        "../outside.safetensors",
        "sub/../../outside.safetensors",
        "/outside.safetensors",
        "C:/outside.safetensors",
        r"C:\outside.safetensors",
        r"\\server\share\outside.safetensors",
        "shard.safetensors:stream",
        "\x00.safetensors",
        "missing.safetensors",
        "",
        None,
        ["shard.safetensors"],
    ],
)
def test_real_loaders_reject_unsafe_index_paths_before_loading_any_weights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load_checkpoint: Callable[[Path], Any],
    wrapped: bool,
    shard: object,
) -> None:
    import accelerate.utils.modeling as modeling

    snapshot = tmp_path / "snapshot"
    _index(snapshot, shard, wrapped=wrapped)
    safetensors.save_file(_weights(), str(tmp_path / "outside.safetensors"))

    def forbidden_load(*_: object, **__: object) -> None:
        pytest.fail("The unsafe checkpoint reached the weight reader.")

    monkeypatch.setattr(modeling, "load_state_dict", forbidden_load)
    with pytest.raises(ValueError, match="Checkpoint"):
        load_checkpoint(snapshot)


@pytest.mark.parametrize("index_content", ["{", "[]", "{}", '{"weight_map":null}', "[" * 1100])
def test_real_loaders_reject_malformed_indexes(
    tmp_path: Path, load_checkpoint: Callable[[Path], Any], index_content: str
) -> None:
    snapshot = tmp_path / "snapshot"
    index = _index(snapshot, "part.safetensors")
    index.write_text(index_content, encoding="utf-8")

    with pytest.raises(ValueError, match="Checkpoint index"):
        load_checkpoint(snapshot)


def test_real_loaders_reject_oversized_indexes(
    tmp_path: Path, load_checkpoint: Callable[[Path], Any]
) -> None:
    snapshot = tmp_path / "snapshot"
    index = _index(snapshot, "part.safetensors")
    with index.open("ab") as stream:
        stream.write(b" " * (16 * 1024 * 1024))

    with pytest.raises(ValueError, match="size limit"):
        load_checkpoint(snapshot)


@pytest.mark.parametrize("kind", ["file", "directory", "index", "hub-blobs-directory"])
def test_real_loaders_reject_links_outside_model_boundary(
    tmp_path: Path, load_checkpoint: Callable[[Path], Any], kind: str
) -> None:
    snapshot = tmp_path / "repo" / "snapshots" / ("a" * 40)
    outside = tmp_path / "outside"
    outside.mkdir()
    safetensors.save_file(_weights(), str(outside / "part.safetensors"))
    if kind == "directory":
        _index(snapshot, "linked/part.safetensors")
        (snapshot / "linked").symlink_to(outside, target_is_directory=True)
    elif kind == "index":
        snapshot.mkdir(parents=True)
        index = outside / "index.json"
        index.write_text(json.dumps({"weight_map": {"weight": "part.safetensors"}}))
        (snapshot / "model.safetensors.index.json").symlink_to(index)
    else:
        _index(snapshot, "part.safetensors")
        if kind == "hub-blobs-directory":
            (tmp_path / "repo" / "blobs").symlink_to(outside, target_is_directory=True)
            (snapshot / "part.safetensors").symlink_to(tmp_path / "repo/blobs/part.safetensors")
        else:
            (snapshot / "part.safetensors").symlink_to(outside / "part.safetensors")

    with pytest.raises(ValueError, match="Checkpoint files"):
        load_checkpoint(snapshot)


@pytest.mark.parametrize("kind", ["directory", "broken-symlink"])
def test_real_loaders_reject_nonfiles(
    tmp_path: Path, load_checkpoint: Callable[[Path], Any], kind: str
) -> None:
    snapshot = tmp_path / "snapshot"
    _index(snapshot, "part.safetensors")
    if kind == "directory":
        (snapshot / "part.safetensors").mkdir()
    else:
        (snapshot / "part.safetensors").symlink_to(snapshot / "missing")

    with pytest.raises(ValueError, match="Checkpoint files"):
        load_checkpoint(snapshot)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes require mkfifo")
@pytest.mark.parametrize(
    "name",
    ["part.safetensors", "model.safetensors.index.json", "model.safetensors", "pytorch_model.bin"],
)
def test_real_loaders_reject_fifo_indexes_and_shards_without_blocking(
    tmp_path: Path, load_checkpoint: Callable[[Path], Any], name: str
) -> None:
    snapshot = tmp_path / "snapshot"
    _index(snapshot, "part.safetensors")
    target = snapshot / name
    target.unlink(missing_ok=True)
    os.mkfifo(target)

    with pytest.raises(ValueError, match="Checkpoint files"):
        load_checkpoint(snapshot)
