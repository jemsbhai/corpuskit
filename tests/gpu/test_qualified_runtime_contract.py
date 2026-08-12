"""Static contracts for the checked isolated CUDA runtime lock."""

from __future__ import annotations

import importlib.metadata
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.gpu.qualified_runtime_acceptance import (  # noqa: E402
    EVIDENCE_SCHEMA,
    MODEL_ID,
    PEFT_CHAIN_CONTRACT,
    PEFT_CHAIN_MODE,
    PEFT_INFER_MODE,
    PEFT_STATE_SCHEMA,
    PEFT_TRAIN_MODE,
    PEFT_TRAIN_RECEIPT_SCHEMA,
    REVISION,
    TinyRuntime,
    _parser,
    _read_training_receipt,
    _rl_contract,
    _write_training_receipt,
    canonical_torch_record_sha256,
    main,
    parse_windows_profile_lock,
    validate_peft_chain_evidence,
    validate_peft_training_receipt,
    validate_quantized_generation_evidence,
)

from corpuskit.domain.model_runtime import ImmutableModelPin  # noqa: E402

PROFILE_LOCK = REPOSITORY_ROOT / "scripts/gpu/windows-cu132-v1.lock.txt"
QUALIFIED_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/qualified-gpu.yml"


def test_windows_cuda_profile_is_exact_and_substantive() -> None:
    lock_text = PROFILE_LOCK.read_text(encoding="utf-8")
    workflow = QUALIFIED_WORKFLOW.read_text(encoding="utf-8")
    packages = parse_windows_profile_lock(lock_text)
    assert len(packages) == 81
    assert packages["torch"] == "2.13.0+cu132"
    assert packages["corpusgen"] == "0.1.7"
    assert packages["transformers"] == "5.15.0"
    assert packages["safetensors"] == "0.8.0"
    wheel_sha256 = "0bcf7ae00b2e20ef2b53af2e764a4fd8646b913bfaaeba2b9c975e672e8c7902"
    assert f"# torch-wheel-sha256={wheel_sha256}" in lock_text
    assert (
        "torch @ https://download-r2.pytorch.org/whl/cu132/"
        "torch-2.13.0%2Bcu132-cp312-cp312-win_amd64.whl"
        f"#sha256={wheel_sha256}"
    ) in lock_text
    assert (
        "# torch-wheel-record-sha256="
        "f8b0f86cacb13585da12fec801316550b82f45863b80117de148593c9f02d8d1"
    ) in lock_text
    assert (
        "# torch-record-canonical-sha256="
        "bcca40a4130fe52ab0acdbdd96498217d6acb7f3a948455fd4172df401ca7907"
    ) in lock_text
    assert 'UV_VERSION: "0.12.3"' in workflow
    assert (
        "uv pip sync --python .qualified-gpu/Scripts/python.exe "
        "scripts/gpu/windows-cu132-v1.lock.txt"
    ) in workflow


def test_windows_cuda_profile_rejects_changed_official_wheel_hash() -> None:
    original = PROFILE_LOCK.read_text(encoding="utf-8")
    changed = original.replace(
        "0bcf7ae00b2e20ef2b53af2e764a4fd8646b913bfaaeba2b9c975e672e8c7902",
        "0" * 64,
    )
    with pytest.raises(RuntimeError, match="unsupported direct artifact"):
        parse_windows_profile_lock(changed)


def test_torch_record_pin_normalizes_only_exact_uv_install_additions() -> None:
    wheel_rows = "torch-2.13.0+cu132.dist-info/RECORD,,\ntorch/__init__.py,sha256=approved,42\n"
    uv_rows = (
        "../../Scripts/torchfrtrace.exe,sha256=path-dependent,46080\n"
        "../../Scripts/torchrun.exe,sha256=path-dependent,46080\n"
        "torch-2.13.0+cu132.dist-info/INSTALLER,sha256=installer,2\n"
        "torch-2.13.0+cu132.dist-info/REQUESTED,sha256=empty,0\n"
        "torch-2.13.0+cu132.dist-info/direct_url.json,sha256=provenance,116\n"
    )
    first = canonical_torch_record_sha256(wheel_rows + uv_rows)
    second = canonical_torch_record_sha256(
        wheel_rows + uv_rows.replace("path-dependent", "different-runner-path")
    )
    changed_payload_manifest = canonical_torch_record_sha256(
        wheel_rows.replace("sha256=approved", "sha256=tampered") + uv_rows
    )

    assert first == second
    assert changed_payload_manifest != first
    with pytest.raises(RuntimeError, match="unsupported uv layout"):
        canonical_torch_record_sha256(wheel_rows)


@pytest.mark.parametrize(
    "replacement",
    [
        "torch>=2.13.0",
        "torch @ https://attacker.invalid/torch.whl",
        "torch==2.13.0\ntorch==2.13.0",
    ],
)
def test_windows_cuda_profile_rejects_unpinned_or_unapproved_torch(
    replacement: str,
) -> None:
    original = PROFILE_LOCK.read_text(encoding="utf-8")
    changed = original.replace(
        "torch @ https://download-r2.pytorch.org/whl/cu132/"
        "torch-2.13.0%2Bcu132-cp312-cp312-win_amd64.whl",
        replacement,
    )
    with pytest.raises(RuntimeError):
        parse_windows_profile_lock(changed)


def test_peft_chain_mode_requests_exact_allowlisted_adapter_training() -> None:
    runtime = TinyRuntime(
        root=Path("/qualified/model-cache"),
        snapshot=Path("/qualified/model-cache/snapshots") / REVISION,
        digest="b" * 64,
        pin=ImmutableModelPin(model=MODEL_ID, revision=REVISION),
    )
    entry, _, request, _ = _rl_contract(runtime)

    assert request.parameters.use_peft is True
    assert request.parameters.peft_rank == 2
    assert request.parameters.peft_alpha == 4
    assert entry.allow_peft is True
    assert entry.allowed_peft_ranks == (2,)
    assert entry.allowed_peft_alphas == (4,)


def test_cli_exposes_only_fail_closed_peft_phase_commands() -> None:
    training = _parser().parse_args(["--mode", PEFT_TRAIN_MODE, "--state-root", "chain-state"])
    inference = _parser().parse_args(
        [
            "--mode",
            PEFT_INFER_MODE,
            "--state-root",
            "chain-state",
            "--output",
            "qualified.json",
        ]
    )
    assert training.mode == PEFT_TRAIN_MODE
    assert inference.mode == PEFT_INFER_MODE

    with pytest.raises(SystemExit):
        _parser().parse_args(["--mode", "training", "--output", "qualified.json"])
    with pytest.raises(SystemExit):
        _parser().parse_args(["--mode", PEFT_CHAIN_MODE, "--output", "qualified.json"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--mode", PEFT_TRAIN_MODE, "--state-root", "state", "--output", "leak.json"],
        ["--mode", PEFT_TRAIN_MODE],
        ["--mode", PEFT_INFER_MODE, "--state-root", "state"],
        ["--mode", "inference", "--output", "evidence.json", "--state-root", "state"],
    ],
)
def test_cli_rejects_ambiguous_phase_state_and_output_contracts(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit):
        main(arguments)


def test_workflow_crosses_exact_training_and_inference_image_boundary() -> None:
    workflow = QUALIFIED_WORKFLOW.read_text(encoding="utf-8")
    linux_chain = workflow.split(
        "- name: Run PEFT training-image to inference-image acceptance chain",
        maxsplit=1,
    )[1].split("- name: Verify PEFT chain state was deleted", maxsplit=1)[0]
    docker_runs = linux_chain.split("docker run")[1:]
    training_run = next(block for block in docker_runs if "--mode peft-train" in block)
    inference_run = next(block for block in docker_runs if "--mode peft-infer" in block)
    assert "corpuskit-worker-gpu-training:qualified" in training_run
    assert "corpuskit-worker-gpu-inference:qualified" not in training_run
    assert "corpuskit-worker-gpu-inference:qualified" in inference_run
    assert "corpuskit-worker-gpu-training:qualified" not in inference_run
    assert "CORPUSKIT_ACCEPTANCE_TRAINING_IMAGE_DIGEST" in inference_run
    for phase_run in (training_run, inference_run):
        assert "--network none" in phase_run
        assert "--read-only" in phase_run
        assert "target=/chain-state" in phase_run
    assert workflow.count("--mode peft-train") == 2
    assert workflow.count("--mode peft-infer") == 2
    assert "--mode peft-chain" not in workflow
    assert workflow.count("peft-chain.json") == 2


def test_workflow_deletes_ephemeral_state_and_uploads_json_only() -> None:
    workflow = QUALIFIED_WORKFLOW.read_text(encoding="utf-8")
    assert "trap cleanup_state EXIT" in workflow
    assert 'test ! -e "${RUNNER_TEMP}/corpuskit-qualified-peft-state"' in workflow
    assert "Remove-Item -LiteralPath $stateRoot -Recurse -Force" in workflow
    assert workflow.count("path: artifacts/qualified-gpu/*.json") == 2
    assert "path: artifacts/qualified-gpu\n" not in workflow
    assert "path: ${RUNNER_TEMP}" not in workflow


def test_workflow_runs_both_bitsandbytes_modes_in_baseline_artifact() -> None:
    workflow = QUALIFIED_WORKFLOW.read_text(encoding="utf-8")
    harness = (REPOSITORY_ROOT / "scripts/gpu/qualified_runtime_acceptance.py").read_text(
        encoding="utf-8"
    )

    assert "4-bit/8-bit quantization" in workflow
    assert "ModelQuantization.FOUR_BIT, ModelQuantization.EIGHT_BIT" in harness
    assert '"quantized_generation": quantized_generation' in harness


def test_complete_quantized_generation_evidence_is_accepted() -> None:
    validate_quantized_generation_evidence(_valid_quantized_generation_evidence())


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("4bit", "parameter_devices"), ["cpu"]),
        (("4bit", "quantized_module_types"), ["Linear"]),
        (("4bit", "loaded_in_4bit"), False),
        (("8bit", "manifest_quantization"), "4bit"),
        (("8bit", "loaded_in_8bit"), False),
        (("8bit", "accepted_count"), True),
        (("8bit", "generation_coverage"), 0.0),
    ],
)
def test_quantized_generation_evidence_rejects_incomplete_proofs(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    evidence = deepcopy(_valid_quantized_generation_evidence())
    target = evidence[path[0]]
    assert isinstance(target, dict)
    target[path[1]] = replacement

    with pytest.raises(RuntimeError, match=r"qualified .* generation evidence"):
        validate_quantized_generation_evidence(evidence)


def test_quantized_generation_evidence_requires_both_declared_modes() -> None:
    evidence = _valid_quantized_generation_evidence()
    del evidence["8bit"]
    with pytest.raises(RuntimeError, match="both quantization modes"):
        validate_quantized_generation_evidence(evidence)


def test_complete_peft_chain_evidence_is_accepted() -> None:
    validate_peft_chain_evidence(_valid_peft_chain_evidence())


def test_training_receipt_is_bounded_authenticated_and_state_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORPUSKIT_ACCEPTANCE_PHASE_KEY", "9" * 64)
    receipt = _valid_training_receipt()
    validate_peft_training_receipt(receipt)
    written = _write_training_receipt(tmp_path, receipt)
    loaded, digest = _read_training_receipt(tmp_path)

    assert loaded == written
    assert len(digest) == 64
    assert (tmp_path / "peft-train-receipt.json").stat().st_size < 32 * 1024


def test_training_receipt_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORPUSKIT_ACCEPTANCE_PHASE_KEY", "9" * 64)
    _write_training_receipt(tmp_path, _valid_training_receipt())
    path = tmp_path / "peft-train-receipt.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["state"]["checkpoint_sha256"] = "0" * 64
    path.chmod(0o600)
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(RuntimeError, match="authentication"):
        _read_training_receipt(tmp_path)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), "corpuskit.qualified-gpu-acceptance.v1"),
        (("mode",), "training"),
        (("phases", "training", "execution", "source_revision"), "local-uncommitted"),
        (("phases", "inference", "execution", "source_revision"), "f" * 40),
        (("phases", "training", "execution", "phase_role"), "gpu-inference"),
        (("phases", "inference", "execution", "image_digest"), "sha256:mutable"),
        (("phases", "training", "cuda", "actual_cuda_tensor_device"), "cpu"),
        (("phases", "inference", "cuda", "model_snapshot_sha256"), "6" * 64),
        (("training", "peft_requested"), False),
        (("training", "training_handler_profile"), "batch-cpu"),
        (("training", "training_result_adopted"), False),
        (("training", "training_phase_receipt_verified"), False),
        (("training", "training_phase_receipt_sha256"), "invalid"),
        (("training", "trusted_materialization_read_only"), False),
        (("training", "trusted_materialization_cleaned"), False),
        (("training", "generation_handler_profile"), "gpu-training"),
        (("training", "generation_result_adopted"), False),
        (("training", "checkpoint_files"), ["adapter_model.bin"]),
        (("training", "checkpoint_bytes"), 0),
        (("training", "adapter_artifact_sha256"), "9" * 64),
        (("training", "adapter_checkpoint_sha256"), "8" * 64),
        (("training", "generation_model_snapshot_sha256"), "6" * 64),
        (("training", "active_child_pids_after_cancellation"), 1),
        (
            ("training", "checkpoint_compatibility", "base_model_snapshot_sha256"),
            "7" * 64,
        ),
        (("training", "checkpoint_compatibility", "peft_version"), "mutable"),
    ],
)
def test_peft_chain_evidence_rejects_missing_or_inconsistent_proofs(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    evidence = deepcopy(_valid_peft_chain_evidence())
    target: dict[str, object] = evidence
    for segment in path[:-1]:
        nested = target[segment]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = replacement

    with pytest.raises(RuntimeError, match="qualified PEFT"):
        validate_peft_chain_evidence(evidence)


def _valid_peft_chain_evidence() -> dict[str, object]:
    model_digest = "b" * 64
    training_artifact = "c" * 64
    checkpoint = "d" * 64
    corpusgen_version = importlib.metadata.version("corpusgen")
    torch_version = importlib.metadata.version("torch")
    transformers_version = importlib.metadata.version("transformers")
    peft_version = importlib.metadata.version("peft")
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "mode": PEFT_CHAIN_MODE,
        "phases": {
            "training": {
                "execution": _container_execution("gpu-training", "a"),
                "cuda": _cuda_proof(model_digest),
            },
            "inference": {
                "execution": _container_execution("gpu-inference", "2"),
                "cuda": _cuda_proof(model_digest),
            },
        },
        "training": {
            "contract": PEFT_CHAIN_CONTRACT,
            "peft_requested": True,
            "training_device": "cuda",
            "training_handler_profile": "gpu-training",
            "ppo_steps": 2,
            "progress_steps": [0, 1],
            "durable_progress_completed": [1, 2],
            "checkpoint_sha256": checkpoint,
            "checkpoint_bytes": 1_024,
            "checkpoint_files": ["adapter_config.json", "adapter_model.safetensors"],
            "checkpoint_compatibility": {
                "peft_adapter": True,
                "base_model_id": MODEL_ID,
                "base_model_revision": REVISION,
                "base_model_snapshot_sha256": model_digest,
                "tokenizer_id": MODEL_ID,
                "tokenizer_revision": REVISION,
                "tokenizer_snapshot_sha256": model_digest,
                "corpusgen_version": corpusgen_version,
                "torch_version": torch_version,
                "transformers_version": transformers_version,
                "peft_version": peft_version,
            },
            "checkpoint_safetensors_files": 1,
            "adapter_tensor_count": 4,
            "adapter_tensors_are_lora": True,
            "peft_compatibility_validated": True,
            "training_run_state": "succeeded",
            "training_result_adopted": True,
            "training_artifact_id": "00000000-0000-4000-8000-000000000010",
            "training_artifact_sha256": training_artifact,
            "training_artifact_integrity": True,
            "training_history_sensitive_payload_absent": True,
            "training_phase_receipt_verified": True,
            "training_phase_receipt_sha256": "3" * 64,
            "trusted_materialization_authorized": True,
            "trusted_materialization_read_only": True,
            "trusted_materialization_one_use": True,
            "trusted_materialization_cleaned": True,
            "trusted_materialization_files": [
                "adapter_config.json",
                "adapter_model.safetensors",
            ],
            "generation_device": "cuda",
            "generation_handler_profile": "gpu-inference",
            "generation_run_state": "succeeded",
            "generation_result_adopted": True,
            "generation_artifact_id": "00000000-0000-4000-8000-000000000011",
            "generation_artifact_sha256": "f" * 64,
            "generation_artifact_integrity": True,
            "generation_output_sha256": "1" * 64,
            "generation_coverage": 1.0,
            "generation_model_id": MODEL_ID,
            "generation_model_revision": REVISION,
            "generation_model_snapshot_sha256": model_digest,
            "guidance_strategy": "phon_rl",
            "adapter_artifact_sha256": training_artifact,
            "adapter_checkpoint_sha256": checkpoint,
            "durable_history_sensitive_payload_absent": True,
            "cancellation_code": "run_cancelled",
            "active_child_pids_after_cancellation": 0,
            "late_staging_prevented": True,
        },
    }


def _valid_training_receipt() -> dict[str, object]:
    evidence = _valid_peft_chain_evidence()
    phases = evidence["phases"]
    training = evidence["training"]
    assert isinstance(phases, dict)
    assert isinstance(phases["training"], dict)
    assert isinstance(training, dict)
    training_facts = {
        key: value
        for key, value in training.items()
        if key
        not in {
            "training_phase_receipt_verified",
            "training_phase_receipt_sha256",
            "trusted_materialization_authorized",
            "trusted_materialization_read_only",
            "trusted_materialization_one_use",
            "trusted_materialization_cleaned",
            "trusted_materialization_files",
            "generation_device",
            "generation_handler_profile",
            "generation_run_state",
            "generation_result_adopted",
            "generation_artifact_id",
            "generation_artifact_sha256",
            "generation_artifact_integrity",
            "generation_output_sha256",
            "generation_coverage",
            "generation_model_id",
            "generation_model_revision",
            "generation_model_snapshot_sha256",
            "guidance_strategy",
            "adapter_artifact_sha256",
            "adapter_checkpoint_sha256",
            "durable_history_sensitive_payload_absent",
        }
    }
    training_phase = phases["training"]
    assert isinstance(training_phase, dict)
    return {
        "schema_version": PEFT_TRAIN_RECEIPT_SCHEMA,
        "recorded_at": "2026-08-12T00:00:00+00:00",
        "phase": PEFT_TRAIN_MODE,
        "execution": deepcopy(training_phase["execution"]),
        "cuda": deepcopy(training_phase["cuda"]),
        "training": training_facts,
        "state": {
            "schema_id": PEFT_STATE_SCHEMA,
            "organization_id": "00000000-0000-4000-8000-000000000001",
            "project_id": "00000000-0000-4000-8000-000000000003",
            "training_run_id": "00000000-0000-4000-8000-000000000012",
            "training_spec_sha256": "4" * 64,
            "training_artifact_id": "00000000-0000-4000-8000-000000000010",
            "training_artifact_sha256": "c" * 64,
            "checkpoint_sha256": "d" * 64,
            "model_snapshot_sha256": "b" * 64,
        },
    }


def _container_execution(role: str, image_digit: str) -> dict[str, object]:
    return {
        "runtime_kind": "container-image",
        "image_digest": f"sha256:{image_digit * 64}",
        "source_revision": "e" * 40,
        "network": "none",
        "phase_role": role,
        "corpusgen_version": importlib.metadata.version("corpusgen"),
    }


def _cuda_proof(model_digest: str) -> dict[str, object]:
    return {
        "actual_cuda_tensor_device": "cuda:0",
        "actual_cuda_tensor_squared_sum": 30.0,
        "model_snapshot_sha256": model_digest,
        "torch_version": importlib.metadata.version("torch"),
    }


def _valid_quantized_generation_evidence() -> dict[str, object]:
    common = {
        "parameter_devices": ["cuda:0"],
        "accepted_count": 1,
        "generation_coverage": 1.0,
        "generated_text_sha256": "a" * 64,
        "bitsandbytes_version": importlib.metadata.version("bitsandbytes"),
    }
    return {
        "4bit": {
            **common,
            "mode": "4bit",
            "manifest_quantization": "4bit",
            "quantized_module_types": ["Linear4bit"],
            "loaded_in_4bit": True,
            "loaded_in_8bit": False,
        },
        "8bit": {
            **common,
            "mode": "8bit",
            "manifest_quantization": "8bit",
            "quantized_module_types": ["Linear8bitLt"],
            "loaded_in_4bit": False,
            "loaded_in_8bit": True,
        },
    }
