# Phon-RL reward lab and training boundary

CorpusKit exposes CorpusGen's phonetic reward and PPO primitives through a bounded CPU laboratory.
Phon-RL training is durable-worker-only: it is never executed by an HTTP request. The exact
`gpu-training` composition, parent deadline, canonical staged-result adoption, and trusted
execution-facts lifecycle are wired; the real pinned CPU train-to-adopt-to-generate chain passes
locally, while an exact-candidate qualified CUDA run remains a release gate.

Only `corpuskit.adapters.corpusgen.phon_rl` imports CorpusGen's Phon-RL modules. Domain, service,
HTTP-lab, and worker layers exchange strict versioned CorpusKit DTOs.

## Reward state and PPO laboratory

Every reward request contains its complete immutable `PhonRlRewardState`; no mutable reward object
is shared across requests. Peek never mutates state. A successful commit records one source ID and
increments the revision. Duplicate IDs or upstream failure leave the caller's state unchanged.
Coverage reward is normalized by target size for phoneme, diphone, and triphone units.

Token rewards call real G2P at decoded GPT, SentencePiece, or whitespace word boundaries. A missing
unit rewards exactly once per response even if a token or phoneme repeats. G2P errors are sanitized
hard failures, never empty success. Hierarchical inspection returns both non-mutating sentence and
token results. Optional externally computed phonotactic, fluency, and reference-log-probability
signals must be finite; fluency takes precedence when both fluency and reference log probability
are supplied.

The PPO lab wraps public `compute_log_probs_from_logits`, `compute_kl_penalty`, `compute_gae`,
`ppo_clip_loss`, and `ValueHead`. Tensor DTOs require finite, rectangular, bounded values,
compatible shapes, valid action IDs, and aligned masks. CPU value-head construction uses
`torch.random.fork_rng` so its bounded seed does not perturb the caller's global RNG state. These
are mathematical primitives, not a model-quality claim.

`app.py` mounts `phon_rl_lab_router()` at:

- `POST /api/v1/phon-rl/reward/peek`
- `POST /api/v1/phon-rl/reward/commit`
- `POST /api/v1/phon-rl/reward/tokens`
- `POST /api/v1/phon-rl/reward/hierarchical`
- `POST /api/v1/phon-rl/ppo/log-probabilities`
- `POST /api/v1/phon-rl/ppo/kl-penalty`
- `POST /api/v1/phon-rl/ppo/gae`
- `POST /api/v1/phon-rl/ppo/clip-loss`
- `POST /api/v1/phon-rl/ppo/value-head`
- `POST /api/v1/phon-rl/training/validate`
- `POST /api/v1/phon-rl/training/estimate`

There is no HTTP training endpoint. Validation/estimation perform DTO, allowlist, profile, and
arithmetic checks only; they cannot resolve a snapshot, load a model, open a provider client, or
download data. Viewer roles may use the bounded stateless reward/PPO lab. Training
validation/estimation and durable submission require owner, admin, or editor. The `/advanced`
workbench exposes those exact boundaries and links a successfully queued run to `/jobs`.

## Exact offline training policy

Clients select a non-sensitive `runtime_id`. A server-owned default-deny policy maps it to one
exact model/tokenizer repository, lowercase 40-character revision, verified snapshot SHA-256,
approved read-only cache-root ID, GPU profile, optional PEFT ranks/alphas, and built-in dynamic
prompt strategy IDs. The public CorpusGen trainer uses one model name for model and tokenizer, so
the pins must be identical.

The worker uses `local_files_only=True`, `trust_remote_code=False`, and safetensors-only loading.
Snapshot verification covers deterministic contents before load, permits normal Hugging Face
snapshot-to-blob symlinks only inside the approved repository root, and rejects escaping links,
executable Python, unsafe pickle formats, and tokenizer/model auto-map declarations. Keep the
configured cache root read-only throughout verify and load; a digest alone cannot prevent a
writable-cache TOCTOU race.

Install the training image dependency profile with:

```bash
python -m pip install "corpuskit-app[worker-gpu-training]"
```

Requests bound target inventory, prompt count, steps, batch, generated tokens, learning rate, KL,
clip, gamma, lambda, value loss, activity deadline, and required seed. Production composition
accepts an exact allowlisted dynamic strategy such as `missing-units-v1`, which remains the demo
default, or an operator-enabled static prompt source. Static prompts use a canonical immutable
`prompt-set` artifact with schema `corpuskit.phon-rl-prompt-artifact.v1`, 1–10,000 nonblank UTF-8
prompts, a 4,000-character per-prompt limit, and an 8 MiB total limit. Upload the artifact in the
Artifact Manager or project artifact API, then place only its `artifact_id`, `content_sha256`, and
`prompt_count` in `prompt_source`. A user-supplied UUID or path is never tenant authority.

At submission and again immediately before execution, the parent requires an active artifact with
the exact kind and digest in the run's organization and project. It streams and re-hashes the
object, requires byte-for-byte canonical JSON, and writes it under a random, mode-restricted,
single-use token directory. The killable child atomically claims that exact run-kind/spec-bound
envelope and re-verifies the prompt bytes and count. Cleanup runs after success, timeout,
cancellation, and handled failure; startup removes old token-shaped crash orphans without
following symlinks. Prompt text, materialization paths, and the trusted envelope never enter the
durable run spec, response, event history, result summary, logs, or manifest. Arbitrary
Python/callback paths remain forbidden.

The trainer binding supports coverage reward only. Nonzero phonotactic or fluency training weights
fail closed until explicit server-owned scorer bindings exist. CorpusGen creates a frozen reference
model copy, so plan memory for at least two model copies. Third-party logging is disabled for the
isolated training interval and restored afterward so prompts and local cache paths do not reach
configured handlers.

## Checkpoint and staged result

Training returns a versioned manifest, bounded progress, normalized metrics, and an
integrity-checked checkpoint bundle. Compatibility metadata binds model/tokenizer pins and
digests, CorpusGen, Torch, Transformers, and optional PEFT versions. Weight files must be
safetensors; `.bin`, `.pt`, `.pth`, pickle, and related formats fail closed. Loading checks every
compatibility field against the active allowlist.

During a durable run, the training callback also emits sampled `preparing_training`, `training`,
`staging_result`, and `finished` events through the parent progress contract. Only completed and
total steps are public; rewards, losses, prompts, checkpoint bytes, and local paths remain inside
the killable child. Sampling preserves first/final observations and caps each activity attempt at
128 messages even when the requested training step count is 10,000.

Raw checkpoint bytes are capped at 60 MiB. Canonical base64 plus a reserved 20 MiB metadata margin
fits the 100 MiB result-envelope and artifact-store cap. Boundary tests prove that a DTO-valid
checkpoint can be staged without exceeding the declared enclosing result budget.

`TrainPhonRlDurableHandler` executes inline inside the platform's one outer killable
`ProcessExecutionRunner`; it starts no nested process or thread. The child writes content-addressed
bytes with `ConfiguredStagedArtifactWriter` and returns only:

```text
contract=corpuskit.staged-artifact-result.v1
staged_artifact_ref=staged-artifact://sha256/<digest>
schema_id=corpuskit.phon-rl-training-result.v1
artifact_type=run-result
media_type=application/json
```

No child field carries organization, project, run, user, path, or final-object authority.
Parent-side adoption streams and re-hashes the bytes, validates the full
`PhonRlTrainingResult`, writes/re-verifies the final content address, and atomically publishes
artifact metadata with success under the authoritative `RunWorkflowReference`. The parent parses
the strict training DTO and enforces the lesser of its requested deadline and
`CORPUSKIT_WORKER_ACTIVITY_DEADLINE_CAP_SECONDS`. With an immutable worker image digest configured,
execution facts also bind the model policy and prompt-source digest before computation.

The parent opens distinct worker and adoption database handles. Staging and production require a
credential-bearing `CORPUSKIT_ADOPTION_DATABASE_URL` different from `CORPUSKIT_DATABASE_URL`; the
training child receives neither connection nor credential.

## Worker profile and deployment

`build_profile_handler_registry(settings)` registers `TRAIN_PHON_RL` only for `gpu-training` and
only when:

- the policy allowlist is nonempty;
- configured cache-root IDs exactly equal policy root IDs and resolve to absolute directories;
- every static/dynamic/PEFT option is explicitly enabled by its server policy, and dynamic
  strategies are in the exact built-in allowlist; and
- the artifact store permits the complete 100 MiB result contract.

Every other profile rejects RL policy. `CORPUSKIT_TEMPORAL_TASK_QUEUE` must exactly equal
`gpu-training`; the dispatcher has no cross-profile fallback. Start the local deployment after
provisioning the read-only model bind mount and exact policy JSON:

```bash
docker compose --profile durable --profile gpu-training up --build
```

Set `CORPUSKIT_WORKER_IMAGE_DIGEST` to the deployed immutable OCI digest for parent-authored
manifest/replay facts. Staging and production require it; development without it does not fabricate
provenance.

## PEFT inference and release status

CorpusGen 0.1.7 `PhonRLStrategy.prepare()` does not assign the model returned by its adapter
loader. CorpusKit does not patch that object or mutate CorpusGen private fields. Instead, an
application-owned loader accepts only the two parent-materialized files `adapter_config.json` and
`adapter_model.safetensors` from a read-only root, verifies the complete base model, tokenizer,
snapshot, CorpusGen, Torch, Transformers, and PEFT compatibility tuple, and loads with
`PeftModel.from_pretrained(..., is_trainable=False, local_files_only=True)`. It then calls
`merge_and_unload(safe_merge=True)` and injects that application-owned model/tokenizer into a
small backend compatible with CorpusGen's public `GenerationLoop` constructor seam.

The backend uses CorpusGen's public prompt template and `PhonRLStrategy`, calls `prepare`, and
applies public `modify_logits` through a Transformers logits processor. The hook is intentionally
identity; the merged learned weights provide guidance. The normal local handler rejects adapter
requests without a parent-created trusted input. Adapter inference is default-deny per exact local
model policy and incompatible with quantization.

A generation spec identifies only the adopted successful training result's `artifact_id`, exact
result `artifact_sha256`, and nested `checkpoint_sha256`. The parent reauthorizes the successful
same-project `TRAIN_PHON_RL` lineage, parses the full strict result, matches installed versions and
base/tokenizer pins, materializes only the config and safetensors weights, and re-verifies every
file in the child. Result manifests record base and adapter digests but no local paths.

The base acceptance suite uses real eSpeak and real CPU Torch for rewards/PPO plus a deterministic
fake tokenizer/model training loop. It covers variable EOS/pad masks, GAE/logit/value alignment,
two-phase batch-G2P atomicity, sanitized OOM/failure, prompt confidentiality and one-use claims,
checkpoint integrity, canonical staging, outer-process cancellation, profile isolation, adoption,
and deadlines. A real offline tiny causal model plus a real PEFT LoRA safetensors adapter exercises
application-owned load, safe merge, public-loop generation, and digest-bearing output on CPU.

The manual `qualified-gpu.yml` workflow implements the qualified PEFT chain as two explicit,
fail-closed execution phases for the same exact candidate SHA. On Linux, `peft-train` runs in the
exact production `gpu-training` image, requests `use_peft=True` under an exact rank/alpha allowlist,
validates real LoRA safetensors, adopts the result, and proves cancellation cannot stage late
output. It persists only ephemeral mounted SQLite/object-store state and a bounded HMAC-bound
training receipt. A separate `peft-infer` invocation in the exact production `gpu-inference` image
authenticates that receipt, reopens and revalidates the adopted training state and lineage,
materializes the adapter read-only for one use, runs safe-merge generation on CUDA, and performs a
second parent adoption. Both containers run non-root with a read-only root filesystem and no
network access.

Schema-v3 evidence validation requires the two exact phase roles, both immutable image digests,
the same non-local 40-character source SHA, actual CUDA proof in each phase, receipt verification,
both adoptions, clean one-use materialization, matching result/checkpoint/adapter lineage, a safe
checkpoint layout, no sensitive durable payload, and complete cancellation. The Windows path runs
the same `peft-train` and `peft-infer` roles under one exact CUDA lock profile and validates that
shared profile/source identity across the receipt handoff. In either path, the workflow removes the
entire ephemeral state before upload and permits only the final JSON evidence file to be retained.

This is harness capability, not live qualification. The remaining release gate is a successful
dispatch on a labelled qualified GPU runner for the exact release-candidate SHA, followed by
retention and review of the uploaded `peft-chain.json`. Until that external record is attached,
`CK-RL-005..007` remain **Implemented**; `CK-RL-001..004` remain **Verified**.

The 30-day Actions artifact is only a transfer record. Before expiry, release operations must
archive the exact JSON in an access-controlled immutable/WORM evidence store and record its
SHA-256 plus permanent read-only permalink in the release record. Promotion reviewers must match
the candidate SHA and both training/inference image identities; missing archival infrastructure or
a mismatched digest blocks promotion.

Focused evidence:

- `tests/unit/test_phon_rl_domain_service_api.py`
- `tests/unit/test_phon_rl_adapter.py`
- `tests/unit/test_phon_rl_worker_registry.py`
- `tests/integration/test_phon_rl_process_runner.py`
- `tests/integration/test_phon_rl_trusted_inputs.py`
- `tests/integration/test_real_tiny_model_runtime.py`
- `tests/gpu/test_qualified_runtime_contract.py`
- `tests/unit/test_trusted_inputs.py`
- `tests/unit/test_worker_composition.py`
- `tests/integration/test_staged_artifact_adoption.py`
- `tests/unit/test_advanced_app_integration.py`
- `apps/web/src/components/advanced-workbench.test.tsx`
