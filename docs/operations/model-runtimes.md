# Hosted and local model runtimes

## Delivery state

CorpusKit has worker-side contracts for targeted hosted-LLM generation, pinned local causal-LM
generation, perplexity fluency scoring, and corpus perplexity. Hosted adapter behavior is verified
with a deterministic fake provider. Exact worker-profile composition, authoritative parent-side
staged-result adoption, bounded per-run deadlines, and parent-authored execution facts are wired.
The offline tiny-model CPU acceptance is verified; live-provider and qualified CUDA/quantization
acceptance remain explicit release gates.

These operations are not mounted as synchronous execution endpoints. Operation-specific HTTP
routes expose only pure validation and conservative cost estimation. Approved execution requests
use the authenticated generic `POST /api/v1/runs` route and are dispatched durably; the API process
does not call a provider or load a model. Returning from a job handler means one activity function
completed; it does not claim that a Temporal workflow or persisted run completed.

## Exact application integration

`build_worker()` calls `build_profile_handler_registry(settings)` and refuses cross-profile
policy. `CORPUSKIT_TEMPORAL_TASK_QUEUE` must equal `CORPUSKIT_WORKER_PROFILE`; the dispatcher owns
the complete run-kind-to-queue map and has no fallback queue.

| Worker profile      | Registered model operations                  | Network posture                                      |
| ------------------- | -------------------------------------------- | ---------------------------------------------------- |
| `external-provider` | `generate-llm` and `generate-repository`     | selected-provider or allowlisted Hugging Face egress |
| `gpu-inference`     | configured `generate-local` and `perplexity` | offline/no egress                                    |
| `batch-cpu`         | no model-runtime operation                   | backend only                                         |
| `gpu-training`      | no model-runtime operation                   | offline/no egress                                    |

Configure matching exact JSON policy DTOs in the API admission process and relevant worker.
Empty policy arrays are default-deny. The API uses the policy only for pure DTO validation,
authorization, cost estimation, and a redacted selector catalog; it never resolves a credential
reference. The hosted worker policy contains a public `connection_id` and a server-only
`secret://env/NAME` reference, while the public run spec and catalog contain only the connection
ID. Supply the referenced secret environment variable only to the `external-provider` worker.
The local policy contains the exact repository, 40-character revision, snapshot digest, devices,
and quantizations. The configured cache root must be an existing absolute read-only mount.

`app.py` mounts the operation-specific validation/estimate routers and the generic durable run
router. Capability catalog and bounded read-only lab calls allow viewer access; model/runtime policy
validation and durable submission require owner, admin, or editor. There is no synchronous HTTP
provider call, model load, index build, generation, or training route.

`CoreRunActivities` uses one killable `ProcessExecutionRunner`. It parses the complete request DTO
deadline under `CORPUSKIT_WORKER_ACTIVITY_DEADLINE_CAP_SECONDS`; registered handlers do not create
a nested process or thread. Hosted generation, local generation, and analysis stage their full
strict result DTO for parent adoption before terminal success.

The trusted parent uses separate worker and adoption database sessions. Deployed workers require
`CORPUSKIT_ADOPTION_DATABASE_URL` with credentials distinct from `CORPUSKIT_DATABASE_URL`; the
adoption secret is never supplied to HTTP, the spawned child, or a model handler.

The child receives only a `ModelResultArtifactStager`. The production implementation is
`ConfiguredStagedArtifactWriter`, which writes unowned bytes under their digest. The child returns
the exact strict `StagedArtifactResult` envelope and cannot create organization, project, run,
user, filename, final object, or artifact metadata. The parent reloads those facts from the
immutable run, streams and re-hashes staged and final content, validates the kind-specific result
DTO, and commits the artifact row with the success event. Cancellation wins in the locked commit;
failed/cancelled attempts leave only grace-delayed reconcilable objects.

When `CORPUSKIT_WORKER_IMAGE_DIGEST` is configured, the parent records immutable execution facts
after the authoritative run enters `RUNNING` and before child computation, then finalizes the
canonical run manifest only after adoption succeeds. Staging and production workers require this
immutable OCI digest. Local development may omit it, in which case manifest recording is
deliberately unavailable rather than populated with a fabricated identity.

The child-handler return proves only bounded computation and staging; durable success still
requires the parent adoption transaction. `ProcessModelActivityDeadlineExecutor` is not used in a
Temporal worker because it would create an invalid nested process boundary.

The mounted control-plane routes are:

- `GET /api/v1/advanced/capabilities`
- `POST /api/v1/runs` (generic durable submission)
- `POST /api/v1/model-runtime/hosted/validate`
- `POST /api/v1/model-runtime/hosted/estimate`
- `POST /api/v1/model-runtime/local/validate`
- `POST /api/v1/model-runtime/analysis/validate`
- `POST /api/v1/model-runtime/analysis/estimate`

The catalog exposes only non-secret provider/model/connection selectors, opaque prompt-template
IDs, exact immutable local
model identities, runtime IDs, allowed devices/quantizations, and availability flags. It omits
credential references, artifact digests, and filesystem paths. New advanced durable runs and
replays are parsed into their exact kind-specific DTO and authorized against the same server
allowlist before a run, quota reservation, or outbox message is created. Revoking a policy blocks
new submissions and retries without breaking retrieval of an already-created idempotent request.

The accessible `/advanced` workbench presents the redacted gates, non-secret request templates,
validation/estimates, durable submission linked to `/jobs`, bounded DATG/Phon-RL labs, and a
non-executing CorpusGen CLI preview. Viewer sessions remain inspection-only in the UI.

## Hosted-provider security and budgets

A request carries a non-sensitive `selection.connection_id`, never an API key or secret reference.
The server-owned `HostedModelPolicy` maps that exact connection/provider/model triple to one
credential `SecretReference`; only the worker resolves it. Its bounded `request_delay_seconds`
setting is operator-owned, defaults to zero, and cannot be overridden in a durable request. The
redacted capability catalog, validation response, conservative estimate, and execution manifest
all report the effective value. Optional custom prompts are separate
`HostedPromptTemplatePolicy` entries containing an opaque ID, a worker-only secret reference, an
exact UTF-8 size and SHA-256 digest, and a maximum rendered-byte ceiling. Requests contain only the
opaque ID. The API authorizes that ID and estimates from the declared ceiling without loading prompt
text. Worker startup resolves and integrity-checks every configured prompt, and execution rechecks
its digest, size, allowed `{target_units}`/`{language}`/`{k}` fields, and rendered ceiling. Credential
and prompt secrets must be distinct. The default resolver accepts only
`secret://env/NAME` with an uppercase bounded environment-variable grammar. Durable specs, public
run responses, results, and manifests contain no `secret://` reference or credential value. Raw
credential-shaped run-spec keys remain rejected; results expose only
`credential_mode="server_secret_reference"`.

`external_processing_confirmed=true` is a required request field. It records the user's explicit
acknowledgement that prompts and target data leave CorpusKit for the selected provider. The manifest
records that confirmation and `processing_boundary="external_provider"`; absence is a hard schema
failure.

Every request supplies finite limits for provider requests, input tokens, output tokens, USD cost,
per-request timeout, retries, generation iterations, accepted sentences, loop time, and the whole
activity. Before every attempt, CorpusKit reserves a conservative UTF-8-byte input ceiling and the
full output-token allowance. An attempt that would cross any budget is rejected before the client
is called. Provider-reported usage must fit the reservation. Retries are classified,
exponentially bounded, counted as requests, and constrained by the same deadline. The server-owned
request delay runs before every provider attempt, including retries, and fails before sleeping or
contacting the provider when that delay would consume the whole-activity deadline.

The built-in LiteLLM client requires the model namespace to exactly match the allowlisted provider,
passes that provider as LiteLLM's `custom_llm_provider`, calls `completion` rather than `Router`,
passes `num_retries=0`, and rejects a
worker process with global success/failure/observability callbacks or verbose logging enabled. This
keeps all provider attempts inside CorpusKit's budget ledger and prevents inherited prompt logging.
Run LiteLLM in a dedicated model worker with callbacks and fallback routers disabled. Provider
failures, invalid usage, and empty responses are sanitized hard failures; none become an empty
successful run.

The hosted manifest records provider/model, temperature, output limit, server request delay, retry
and budget policy,
whole-activity deadline, external-processing confirmation, whether the prompt template was custom,
its opaque template ID, and its SHA-256 digest. It never stores prompt text, connection deployment
details, or a secret reference. Provider seeding is explicitly reported as unsupported by this
adapter contract.

## Local-model supply chain and lifecycle

Local requests accept only a namespaced Hub model ID and lowercase 40-character commit revision
that exactly match `LocalModelPolicy`. Paths, URLs, branches, arbitrary revisions, devices, and
quantization modes are rejected. The loader always sets:

- `revision=<exact commit>`
- `local_files_only=True`
- `trust_remote_code=False`
- `use_safetensors=True` for model weights

Therefore HTTP cannot download a model, and a worker fails if the exact snapshot was not provisioned
in its cache. CPU is unquantized. Four-bit and eight-bit modes require an allowed CUDA selection and
a `LOCAL_GPU` worker profile. The manual qualified-GPU baseline fail-closes unless both modes load
real bitsandbytes modules (`Linear4bit` and `Linear8bitLt` respectively), keep model parameters on
CUDA, execute target-covering CorpusKit generation, and record the matching public result-manifest
mode. This harness still requires a retained exact-candidate workflow run before the capability is
called qualified.

The default loader is wrapped by a two-entry process-local LRU keyed by model, revision, device,
quantization, and artifact digest. It reuses a bundle within one long-lived process and invokes an
available cleanup hook on eviction/`clear()`. The current durable `ProcessExecutionRunner` uses one
child per run, so CorpusKit does not claim cross-run cache reuse. Language-model analysis still
shares one loaded identity between fluency and perplexity inside its run.

The policy and manifest bind an operator-provisioned snapshot digest. Before Transformers sees the
snapshot, the loader recomputes `corpuskit.snapshot.v1`: sorted logical relative path, byte size,
and SHA-256 of every file, with a required safetensors file and unsafe pickle-weight suffixes
rejected. The snapshot and every resolved file must remain under the approved repository cache
root. Normal Hugging Face snapshot symlinks into that repository's `blobs/` directory are allowed;
links escaping the root are rejected. Mount the approved cache root read-only for the whole
verify-and-load operation—an immutable Hub revision cannot prevent local mutation or close a
verify/load race on a writable mount.

Local generation accepts a bounded seed, applies it through Transformers immediately before the
CorpusGen loop, and records it with sampling mode in the manifest. It remains `best_effort` even
when sampling is disabled: a seed and exact revision do not guarantee bitwise determinism across
kernels, drivers, devices, or library versions. The manifest records that deterministic algorithms
were not enforced.

Nonzero local-generation fluency weight binds `PerplexityFluencyScorer.from_model` to that same
authorized bundle before the CorpusGen loop. It is never constructed for the zero-weight default.
The request must cap candidate fluency evaluations at 250 (`max_iterations` times candidates per
iteration), the scorer memoizes rank/commit calls, and the whole worker process remains subject to
the parent-owned activity deadline. Hosted and repository generation reject nonzero fluency because
they do not carry an exact local-model selection.

## Shared fluency and perplexity lifecycle

Language-model analysis loads one bounded bundle. CorpusKit passes those application-owned objects
to the public `PerplexityFluencyScorer.from_model(model, tokenizer)` constructor and to
`compute_corpus_perplexity(model=..., tokenizer=...)`. It never reads the scorer's private model or
tokenizer fields. The result preserves every input source ID for fluency and emits an ordered
`scored` or `skipped_too_short` source mapping for perplexity, plus corpus summary, token-count, and
negative-log-likelihood metrics.

An analysis request may also include a bounded composite-scoring specification whose source IDs and
texts exactly match the analysis rows and whose fluency weight is nonzero. The worker ranks those
pre-phonemized candidates with the already-computed fluency scores; it does not call the model a
second time. The no-I/O analysis estimate exposes sentence, token, scorer-call, profile, and
whole-activity bounds before durable submission. Exact model/revision, device, quantization, and
snapshot digest still come only from immutable `LocalModelPolicy`; clients cannot supply a path,
URL, branch, artifact digest, or secret.

## Runtime requirements and acceptance gaps

- External-provider workers: install `corpuskit-app[worker-external-provider]`, provide eSpeak for
  generated-text G2P, configure exact hosted-model and/or Hugging Face repository allowlists, and
  allow only the selected provider or allowlisted repository egress.
- Local CPU workers: install `corpuskit-app[local]`, provision the exact safetensors snapshot and eSpeak,
  and size RAM for the selected model.
- Local GPU workers: install `corpuskit-app[worker-gpu-inference]` and pin/qualify CUDA, PyTorch,
  transformers, bitsandbytes, driver, GPU, and quantization combinations.
- No live provider request, downloaded model, or GPU smoke is part of the base test run. Those remain
  explicit release-profile gates; fakes cannot establish model quality, provider availability, real
  cost, hardware compatibility, or performance.

The local Compose worker profiles are opt-in. Start hosted execution together with the durable
control plane using `docker compose --profile durable --profile hosted up --build`. Start local
GPU inference with `--profile durable --profile gpu-inference`; pre-create and provision the host
model/index cache directories before mounting them read-only. Both advanced workers fail startup
when their exact policy array is empty. Set `CORPUSKIT_WORKER_IMAGE_DIGEST` to the deployed image's
immutable digest when testing manifest/replay behavior.

Provider attempts are counted and retried only inside the bounded hosted runner. A hard worker
loss after a provider accepted a request but before durable result adoption can still cause a
Temporal activity redelivery and a second provider charge because no provider-wide idempotency
standard or durable per-attempt cost ledger exists. Keep hosted rows below `Verified` until a live
release gate exercises this failure boundary with the selected provider.

Focused acceptance is in:

- `tests/unit/test_model_runtime_domain_service_api.py`
- `tests/unit/test_model_runtime_adapter.py`
- `tests/unit/test_model_runtime_handlers.py`
- `tests/unit/test_worker_composition.py`
- `tests/integration/test_staged_artifact_adoption.py`
- `tests/unit/test_advanced_app_integration.py`
- `tests/integration/test_jobs.py`
- `apps/web/src/components/advanced-workbench.test.tsx`
- `apps/web/e2e/workbenches.spec.ts`

The focused suite includes deterministic retry, timeout, budget, redaction, explicit consent,
duplicate and mutation handling, seed replay plumbing, symlink/cache-boundary and tamper detection,
offline loading, cache lifecycle, shared identity, real CorpusGen backend construction, HTTP
no-network behavior, default-adapter spawn/pickle, single-boundary durable registries, artifact
integrity, child-process deadlines, and invalid-message tests. The CorpusGen model-runtime adapter
itself is above 90% statement/branch coverage.
