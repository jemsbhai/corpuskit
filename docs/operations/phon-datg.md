# Phon-DATG index and guidance boundary

CorpusKit implements CorpusGen's bounded Phon-DATG index, token-set inspection, and local logit
guidance contracts. DATG handlers are composed only on their exact worker profiles. The trusted
parent applies the request-specific deadline, adopts the canonical staged result under its
authoritative `RunWorkflowReference`, and records execution provenance before a durable run can
report success. A successful build is now promoted into the shared content-addressed cache and a
tenant/project authorization catalog before the run reports success. Live pinned-model/CUDA
acceptance remains a release gate.

## Implemented behavior

- `BUILD_DATG_INDEX` (`build-datg-index`) and `GENERATE_DATG` (`generate-datg`) are distinct
  durable run kinds and do not collide with ordinary local generation.
- Index construction is bounded by vocabulary size, G2P batch size, and a whole-activity
  deadline. Guidance bounds target sizes, coverage history, candidates, generated tokens,
  deadline, and seed.
- Target, covered anti-target, and frequency anti-target token sets have bounded read-only
  inspection contracts for phoneme, diphone, and triphone units. Target lookup is an ANY union;
  covered mode requires all relevant token units to be covered; frequency mode requires every
  relevant count to be strictly greater than the threshold.
- Real CorpusGen `DATGStrategy` and `LogitModulator` behavior is covered by hand-computed clone and
  delta vectors in both anti-target modes. A bounded calculation-only preview returns strict
  before/delta/after matrices plus attribute and anti-attribute token classifications. Input
  logits remain unchanged, boosts are nonnegative, and penalties are nonpositive.
- Seed and exact pins are recorded, but GPU replay remains `best_effort` because deterministic
  kernels and a fixed hardware stack are not enforced.

Only `corpuskit.adapters.corpusgen.datg` imports CorpusGen. It copies the public
`AttributeWordIndex.unit_to_tokens` and `token_units` maps into versioned application DTOs. Replay
uses an application-owned facade implementing public lookup behavior; no CorpusGen private field
is copied or patched.

## Exact offline runtime policy

Clients submit a non-sensitive `runtime_id`. A server-owned default-deny allowlist maps it to the
exact model and tokenizer repositories, lowercase 40-character revisions, verified snapshot
SHA-256 digests, and permitted quantization. The current public `LocalBackend` loads its tokenizer
from the model name, so policy requires model and tokenizer to be the same snapshot.

The worker resolves only the configured cache root with `local_files_only=True`; it never downloads
a model or tokenizer. Snapshot verification uses the deterministic `corpuskit.snapshot.v1` digest
before load. Every resolved file must stay beneath the approved repository/cache root. Normal Hugging
Face snapshot symlinks into that repository's `blobs/` directory are allowed, while escaping links,
executable Python, pickle-style weights, missing safetensors, and unsafe auto-map metadata fail
closed. Keep the cache mounted read-only across verification and load to prevent a writable-cache
TOCTOU race.

Tokenizer loading forces exact `revision`, `local_files_only=True`, and
`trust_remote_code=False`; model loading also forces `use_safetensors=True`. Install the batch
index builder or GPU guidance profile with:

```bash
python -m pip install "corpuskit-app[worker-batch]"
python -m pip install "corpuskit-app[worker-gpu-inference]"
```

## Versioned cache artifact

`corpuskit.datg-index.v1` is bounded, canonical JSON. Its
`corpuskit.datg-index-cache-key.v1` identity binds:

- tokenizer repository, immutable revision, and verified snapshot digest;
- language and unit (`phoneme`, `diphone`, or `triphone`); and
- installed CorpusGen and eSpeak versions.

Changing any field changes the cache key. Validation reconstructs the sorted unit-to-token and
token-to-unit maps and rejects duplicate IDs, wrong unit levels, oversized data, inconsistent
identity, or content-digest mutation.

The trusted batch parent, never the child process, publishes the nested `DatgIndexArtifact` as
`<cache_key_sha256>.json`. It first validates the staged build result against the authoritative
run request and exact runtime/tokenizer policy. Publication uses canonical JSON, an fsynced
same-filesystem temporary file, and a no-replace hard link. Redelivery is idempotent when the full
artifact matches; an existing key with different content is an integrity failure and is never
overwritten.

After the cache file is verified, the adoption database transaction inserts an immutable
`datg_index_publications` row with organization, project, build run, creator, cache/content
digests, runtime, language, unit, counts, and byte size. The row is committed atomically with the
active result artifact and terminal run state. RLS permits tenant members to read it, the adoption
role to insert it, and the maintenance role to delete it. A cache file left unreferenced by a
failed database transaction grants no access: catalog listing, inspection, and generation all
require the tenant/project row.

## HTTP lab boundary

`app.py` mounts `datg_lab_router()` at these catalog/validation/inspection-only routes:

- `GET /api/v1/projects/{project_id}/datg/indexes`
- `POST /api/v1/projects/{project_id}/datg/index/inspect/targets`
- `POST /api/v1/projects/{project_id}/datg/index/inspect/anti/covered`
- `POST /api/v1/projects/{project_id}/datg/index/inspect/anti/frequency`
- `POST /api/v1/projects/{project_id}/datg/index/preview/logits`
- `POST /api/v1/datg/index/validate`
- `POST /api/v1/datg/generation/validate`

There is no HTTP build or generation endpoint. Validation performs policy arithmetic only;
inspection reads an explicitly configured, existing, absolute application cache whose mount is
operator-attested as read-only. Listing returns only physically present entries cataloged for the
authenticated tenant and active project. Every inspection authorizes the supplied key before
opening it. CorpusKit checks the declaration, root, regular-file boundary, artifact identity, and
catalog content digest; deployment enforces the read-only mount. Missing, malformed, oversized,
symlinked, escaping, or identity-mismatched entries fail with a typed redacted error. Neither path
can load a model or access the network. Viewers may list and inspect their project's cache;
allowlist validation and durable submission require owner, admin, or editor. `GENERATE_DATG`
submission also requires a catalog row in that same tenant/project whose runtime, language, and
unit match the immutable run request, so possession or guessing of a SHA-256 key grants nothing.

The preview request accepts only an authorized cache key, bounded targets and coverage history,
bounded guidance options, and finite logits of at most 8 rows by 2,048 token columns. It never
accepts an index artifact from the browser. `DatgIndexCatalogService` re-authorizes the active
tenant/project/key, revalidates the immutable cache artifact and catalog content digest, constructs
the internal artifact-bearing adapter request, and calls only `CorpusgenDatgAdapter.preview_logits`.
The response binds the original matrix and cache key, gives an explicit exact delta matrix, and
attests that no generation, model load, or network use occurred.

The `/advanced` workbench loads real keys from the selected project's catalog, supplies bounded
inspection and logit-preview templates, and queues build/generation only after the unchanged exact
run spec passes server validation. The preview renders an accessible table of batch row, token ID,
attribute/anti-attribute classification, before value, delta, and after value. With no available
index it shows a build-first state and disables DATG inspection/generation; it never substitutes an
all-zero or otherwise usable fake digest.

## Durable worker composition

`build_profile_handler_registry(settings)` is fail closed:

- `batch-cpu` adds only `BUILD_DATG_INDEX` when an exact allowlist and read-only model cache are
  configured;
- `gpu-inference` adds only `GENERATE_DATG` when the exact allowlist, model cache, and
  content-addressed index cache are configured; and
- every other profile rejects DATG policy at startup.

Both handlers execute inline within the one outer `ProcessExecutionRunner`; they start no nested
process or thread. The parent parses the strict DATG DTO and selects the lesser of its deadline and
`CORPUSKIT_WORKER_ACTIVITY_DEADLINE_CAP_SECONDS`. The child stages the complete
`DatgIndexBuildResult` or `DatgGuidedGenerationResult` and returns only exact
`StagedArtifactResult` metadata using `staged-artifact://sha256/...`. Parent adoption streams and
re-hashes staged/final bytes, validates the exact schema, and atomically creates tenant/run-owned
metadata with success. With an immutable worker image digest configured, parent facts also attest
the worker policy and, for guidance, the exact cached index digest.

Publication uses a distinct adoption-role database session configured by
`CORPUSKIT_ADOPTION_DATABASE_URL`; worker reads and execution facts continue through
`CORPUSKIT_DATABASE_URL`. Deployed credentials must differ, and neither reaches the child.

Set `CORPUSKIT_WORKER_DATG_INDEX_PUBLISH_ROOT` only on the `batch-cpu` worker, pointing at its
writable view of the shared cache. Set `CORPUSKIT_WORKER_DATG_INDEX_CACHE_ROOT` on API and
`gpu-inference`, pointing at read-only views of the same storage, and attest those consumer mounts
with `CORPUSKIT_WORKER_DATG_CACHE_MOUNT_READ_ONLY=true`. Startup rejects a publication root on any
other worker profile and rejects a configured batch build policy without an absolute,
pre-provisioned publication directory.

The Helm chart mounts `workers.common.datgIndexCacheClaim` read-write only at
`/datg-index-publish` in `batch-cpu`, and read-only at `/datg-indexes` in API and GPU inference.
The external claim therefore needs `ReadWriteMany` access across nodes, or an equivalent storage
topology that provides immediate read-only visibility to consumers. Batch and GPU DATG policy
lists must be nonempty and identical, binding a built index to an actually authorized generation
runtime. Compose uses the same host directory, `CORPUSKIT_DATG_INDEX_CACHE_ROOT`, for all three
views; create it before starting the profiles.

Start the local deployment after provisioning bind-mounted caches and a nonempty policy:

```bash
docker compose --profile durable --profile gpu-inference up --build
```

The local acceptance suite creates a real offline Transformers `PreTrainedTokenizerFast`, saves it
under an immutable Hugging Face snapshot layout with a computed exact snapshot digest, loads it
through `TransformersTokenizerLoader`, builds the real eSpeak/CorpusGen index, then carries that
artifact through parent publication, tenant catalog inspection, and the additive preview. This
closes `CK-DATG-001` as **Verified**. `CK-DATG-003` remains **Implemented** because local
calculation and visualization do not replace a qualified immutable-model CUDA guided-generation
run.

Focused evidence:

- `tests/unit/test_datg_domain_service_api.py`
- `tests/unit/test_datg_adapter.py`
- `tests/unit/test_datg_worker_handler.py`
- `tests/unit/test_worker_composition.py`
- `tests/integration/test_staged_artifact_adoption.py`
- `tests/integration/test_datg_publication.py`
- `tests/integration/test_postgres_tenant_controls.py`
- `tests/deployment/test_helm_contract.py`
- `tests/unit/test_advanced_app_integration.py`
- `apps/web/src/components/advanced-workbench.test.tsx`
