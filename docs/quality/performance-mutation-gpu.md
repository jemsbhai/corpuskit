# Performance, mutation, and qualified GPU evidence

This document describes three slow acceptance waves which are deliberately separate
from pull-request CI. They run on a schedule or by explicit dispatch, retain machine-
readable reports, and do not call a paid model provider.

## Performance contract

`tests/fixtures/performance/v1.json` is the canonical public-data fixture. Validation
fails unless it expands to exactly 100 evaluation sentences, 1,000 pre-phonemized
selection candidates, 10,000 JSON export sentences, and 100 worker-memory jobs. The
runner measures:

- cached, checksum-verified PHOIBLE inventory search and loaded-status HTTP paths;
- job status and submission through in-process ASGI/auth/serialization seams (these
  two cases are explicitly named `no_persistence` and are not PostgreSQL latency);
- real CorpusKit/CorpusGen/eSpeak evaluation of 100 sentences;
- the public CorpusGen greedy selector over 1,000 pre-phonemized candidates;
- deterministic CorpusKit JSON encoding of 10,000 sentences; and
- long-lived parent-worker RSS around 100 real evaluation jobs, each isolated through
  the production killable child-process runner.

Every comparable timing uses three untimed warmups and 20 timed samples. Reports retain
nearest-rank median, p95, p99, minimum, and maximum values. Absolute limits come from
`acceptance.md`; memory must finish within 10% of the post-warm RSS and must not show
material uninterrupted growth.

The comparator rejects unsupported schemas, weak sampling, fixture drift, suite drift,
environment drift, dirty source state, and missing exact Git provenance. Relative
regression is enforced only when profile ID, OS and release, architecture, CPU
model/count, Python implementation/version, and fixture digest all match exactly. A
non-comparable profile is reported without failing the relative comparison; the prior
self-comparison still enforces absolute SLO and memory contracts. A change of exactly
10% passes; a change greater than 10% fails. The CLI refuses to use either input path
as its output, so a verification run cannot overwrite approved evidence.

Run a diagnostic observation with:

```powershell
uv run python -m scripts.performance.run_benchmarks `
  --output artifacts/performance/candidate.json `
  --profile-id windows-x64-rtx4090-laptop-local `
  --samples 20 --warmups 3
```

No approved baseline exists at initial repository creation because there is no clean
source commit to identify. `benchmarks/baselines/README.md` defines the required two-
commit approval process. Scheduled automation permits that absence only before `HEAD`
exists or on the root source commit; every later scheduled run and every release fails
without an approved schema-valid exact-profile baseline from an ancestor commit. CI
never updates a baseline: it writes a separate comparison file and fails relative
regression only on a comparable exact profile. Queue latency, PostgreSQL endpoint
latency, Web Vitals, JavaScript size, and the 24-hour soak remain separate unmeasured
release evidence and are listed in each report.

## Mutation contract

Mutmut 3.7.0 runs in a digest-pinned Python/uv image as a fixed non-root user, with
networking disabled and a 45-minute outer timeout. Its focused test selection currently
generates 155 real mutants across:

- `auth/dependencies.py` (critical authentication dependency),
- `services/run_admission.py` (critical fail-closed run admission), and
- `domain/corpora.py` (core normalization, validation, and digest behavior).

The score is `(killed + caught_by_type_check) / (total - skipped)`. Survivors, no-test,
unchecked, interrupted, suspicious, segfault, and timeout verdicts all count as misses;
missing metadata fails closed. A minimum of 50 overall and 25 critical mutants prevents
a token smoke. The committed gates are at least 75% overall and at least 90% critical.

A fresh isolated Linux run of the hardened non-root image killed 127 of 155 overall
(81.94%) and 72 of 79 critical mutants (91.14%), with no no-test, timeout, or unchecked
verdicts. The checkout was read-only and mutation output used ephemeral storage. This
is implementation evidence, not a clean-commit release record. The scheduled workflow
reruns from scratch and uploads its JSON. CorpusGen adapter and job-state mutation
waves remain mandatory before GA as stated in `acceptance.md`; the current scoped
score does not imply those future gates passed.

## Qualified CUDA contract

`qualified-gpu.yml` is manual-only and requires an explicitly labelled self-hosted GPU
runner. The Linux profile builds the exact production inference and training image
targets, confirms their non-root user, disables container networking, mounts a read-only
acceptance program, and records the exact image ID. The Windows alternative provisions
`windows-cu132-v1.lock.txt` into a dedicated Python 3.12.12 environment. That lock pins
81 distributions, the exact official PyTorch CUDA wheel URL plus SHA-256 fragment, its
published size/ETag, the pristine wheel RECORD digest, and a canonical installed-RECORD
digest. The latter removes only the five known path-dependent rows added by the pinned uv
installer; all original wheel manifest rows remain attested.

The Windows Torch pin was independently derived from the official PyTorch `cu132` index and
artifact on 2026-08-12. The downloaded 1,917,946,849-byte wheel matched the published
`aae695147d9f3c9a62f5d4e684569b73-229` ETag and hashes to
`0bcf7ae00b2e20ef2b53af2e764a4fd8646b913bfaaeba2b9c975e672e8c7902`; its pristine
`torch-2.13.0+cu132.dist-info/RECORD` hashes to
`f8b0f86cacb13585da12fec801316550b82f45863b80117de148593c9f02d8d1`. Exact uv 0.12.3
adds `INSTALLER`, `REQUESTED`, `direct_url.json`, `torchrun.exe`, and `torchfrtrace.exe`
rows at install time; the launcher hashes include the environment path, so hashing the raw
installed RECORD is not reproducible across qualified runners. CorpusKit requires those five
rows and verifies their uv/direct-URL provenance, removes exactly those rows, sorts and
canonicalizes the remaining CSV rows, and requires
`bcca40a4130fe52ab0acdbdd96498217d6acb7f3a948455fd4172df401ca7907`. An added or
changed wheel-manifest row therefore still changes the attestation. A fresh Python 3.12.12
environment installed through the hash-fragment lock and passed this canonical check; the
previous raw installed-RECORD value did not match the locked installation mechanism.

Both paths create the tiny GPT-2-shaped tokenizer/model locally and serialize only
safetensors. Acceptance records source identity, runtime/profile digest, driver, CUDA,
Torch, cuDNN, GPU model/capability/memory, and the generated model snapshot digest. It
proves an actual CUDA tensor and model parameter device in each applicable phase. The
baseline inference invocation continues to run in the exact production inference runtime
and covers:

1. local generation with target coverage;
2. separate real bitsandbytes 4-bit and 8-bit loads and target-covering generation, with
   the exact quantized module type, CUDA parameter placement and result-manifest mode attested;
3. shared-model finite perplexity;
4. DATG index construction and guided generation.

The PEFT acceptance chain is a separate two-phase boundary, not a combined process in one
image:

1. `peft-train` runs exactly two bounded PPO steps with `use_peft=True` under an exact
   rank/alpha allowlist in the killable handler of the exact production `gpu-training`
   image. It validates real LoRA tensors, parent-adopts the result, exercises cancellation,
   and writes only mounted ephemeral SQLite/object-store state plus a bounded HMAC-bound
   receipt.
2. `peft-infer` runs as a separate process in the exact production `gpu-inference` image.
   It authenticates the receipt, reopens the durable state, revalidates the successful
   same-project training run, result, compatibility and digest lineage, one-use materializes
   only read-only `adapter_config.json` and `adapter_model.safetensors`, safe-merges, generates
   target-covering text on CUDA, and parent-adopts the generation result.

Both Linux phase containers are non-root, have read-only root filesystems, and use
`--network none`; only the ephemeral state mount is writable between them. Schema-v3
validation rejects anything other than the exact `gpu-training` then `gpu-inference` roles,
their immutable image digests, one shared non-local 40-character candidate SHA, actual CUDA
proof in both phases, an authenticated receipt bound to the training phase, both parent
adoptions, clean one-use materialization, matching lineage digests, safe checkpoint layout,
or complete cancellation. The Windows alternative invokes the same two roles sequentially
under one exact `windows-cu132-v1` lock/profile identity and validates that identity and the
same candidate SHA across the receipt boundary. The harness does not turn either operation
into an HTTP route.

After inference validation, the workflow deletes the complete SQLite/object-store/receipt
state before the artifact-upload step and uploads only JSON (`peft-chain.json`, alongside the
separate baseline JSON where applicable). The ephemeral handoff cannot enter retained CI
artifacts. Contract tests cover phase ordering, image/profile and source identity, receipt
tampering, lineage, cleanup ordering, and the JSON-only upload boundary.

On the local RTX 4090 Laptop host, both exact production images built, but Docker/WSL's
injected driver returned CUDA error 500 from raw `cuInit(0)`; `nvidia-smi` alone was not
accepted as proof. A later isolated Windows feasibility probe used Python 3.12.12, driver
595.79, Torch 2.13.0+cu132 and the checked 81-package lock: real 4-bit `Linear4bit` and 8-bit
`Linear8bitLt` generation each ran on `cuda:0` with full target coverage. That probe also
found and fail-closed on a stale installed-RECORD pin, which this contract replaces with the
independently verified wheel hash plus canonical manifest pin. No probe output or checkpoint
state is retained, and editing the contract changes the candidate SHA, so this is diagnostic
evidence only. The remaining external gate is a successful manual `qualified-gpu.yml` dispatch
for the new exact release-candidate SHA on a labelled qualified runner, with both JSON artifacts
retained and linked from the release record. Until that happens, no qualified PEFT/CUDA or
quantized-generation evidence is claimed.
