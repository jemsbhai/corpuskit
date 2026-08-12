# Durable job operations

CorpusKit executes durable runs through one versioned Temporal workflow and four exact worker
queues. PostgreSQL remains the tenant-facing source of run state, events, and bounded result
summaries; Temporal is the execution authority. The default `batch-cpu` worker always provides the
six reviewed core operations and may add DATG index build. Hosted generation, local
generation/analysis/DATG, and Phon-RL are registered only by their separately configured profiles.
Repository generation is registered on `external-provider`; Hugging Face sources require an exact
immutable server policy, while local repository sources use the same bounded handler. The reserved
durable `export` kind has no handler and is rejected before persistence or quota reservation;
deterministic corpus exports remain available through the workspace API.

## Safety and history contract

- Submission atomically writes the immutable run, first event, and outbox start intent.
- `Idempotency-Key` is organization-scoped and replaying the same request returns the first run.
- The workflow ID is deterministically derived from organization and run UUIDs. Duplicate outbox
  delivery uses Temporal request deduplication and the existing workflow instead of starting work
  twice.
- Workflow, activity, signal, result, and heartbeat payloads contain only the organization UUID,
  run UUID, and canonical specification SHA-256. Run kind, specification, corpus text, provider
  input, credentials, and exception details do not enter Temporal history.
- Activities reload the tenant-scoped row and recompute its specification hash before work. A
  tenant mismatch is indistinguishable from a missing run; a changed spec fails closed as
  `spec_integrity_violation`.
- Specs are canonical JSON limited to 256 KiB, depth 20, and 20,000 nodes. Credential-shaped keys
  are rejected. Hosted specs contain only a public server-policy `connection_id`; secret values
  and `secret://` references remain worker configuration and never enter run history.
- Result summaries are JSON-only, at most 64 KiB, depth 10, and 5,000 nodes. They contain counts,
  rates, and coverage summaries rather than corpus bodies.

## State machine and crash behavior

The worker advances `queued -> provisioning -> running -> succeeded|failed`. Cancellation uses
`queued|provisioning|running -> cancelling -> cancelled`. Each transition updates the run and
appends its next event sequence in one database transaction using state-and-sequence compare and
swap. Terminal rows are never overwritten.

Activity redelivery is safe. If a process dies after computation but before the success commit, a
retry recomputes the pure core operation and races for the single terminal CAS. If a process dies
after the commit, the retry observes the terminal row and performs no engine work. Selection with
a stochastic or NSGA-II algorithm requires an explicit seed for replayable durable execution.

Cancellation is cooperative. The API first persists `cancelling` and an outbox signal intent. The
dispatcher signals the stable workflow, which cancels the active activity. Activities heartbeat,
poll the database cancellation projection, discard uncommitted computation, and acknowledge
`cancelled`. A cancellation that races with a result commit wins whenever `cancelling` was
persisted before the terminal CAS.

Each synchronous engine invocation runs in a fresh spawned child process behind bounded JSON IPC.
The activity keeps the tenant-scoped specification in local process memory; only the opaque
workflow reference is heartbeated or recorded by Temporal. Cancellation, worker shutdown, and the
hard execution deadline terminate the child, wait a short grace period, and then kill it if needed
before the activity acknowledges cancellation. A terminated child cannot later commit a result;
malformed responses, oversized summaries, and unsafe child error codes are rejected at the IPC
boundary.

Artifact-producing children return a bounded `staged-artifact://sha256/<digest>` envelope rather
than a result summary containing tenant authority or output bytes. The parent reloads the run,
validates the staged object and its kind-specific strict DTO, writes and re-verifies the final
content address, then atomically inserts artifact metadata with the success event. A retry after
the final object write converges on the same object and one artifact row. Cancellation is rechecked
in the final locked transaction and wins without publishing metadata or success.

When an immutable worker image digest is configured, the parent reloads the authoritative run
after `begin_execution`, validates the exact server policy and request DTO, and records immutable
`TrustedExecutionFacts` immediately before child computation. Facts include installed
CorpusKit/CorpusGen versions, exact worker profile/image/policy digest, available eSpeak/PHOIBLE
provenance, and required model provenance for model/DATG/RL operations. After successful adoption
or summary commit, the parent finalizes the canonical run manifest. Exact redelivery is idempotent;
mismatched facts fail closed. Staging and production require the image digest. Development without
one does not fabricate a manifest.

Result publication uses two database authorities in the trusted parent. Worker reads, lifecycle
transitions, cancellation, and execution-fact insertion use `CORPUSKIT_DATABASE_URL`; artifact
commit and manifest/replay binding use `CORPUSKIT_ADOPTION_DATABASE_URL`. The latter is a
worker-only secret and must be a credential-bearing PostgreSQL asyncpg URL. Staging and production
reject a missing adoption URL or credentials equal to the worker URL. Never inject adoption
credentials into the API, dispatcher, child process, run spec, or history. Local Compose uses the
same development owner only as an explicitly non-production convenience.

## Atomic input and corpus-lineage bounds

Core run specifications are intentionally atomic. `PHONEMIZE` and `EVALUATE` accept at most 500
ordered input rows; `SELECT` accepts at most 2,000. When a request links an immutable corpus
version, admission reconstructs the canonical normalized payload and requires its language,
sentence count, and SHA-256 to match that exact version. The Job Center also pages and verifies
every expected ordinal before submission and refuses an over-limit version before fetching its
contents. Neither layer truncates to manufacture matching lineage. Import an explicit bounded
derived version for one atomic run; complete execution of a larger version requires a future
chunked-job contract with explicit per-chunk lineage.

Successful `SELECT` runs stage the complete deterministic `corpuskit.corpus-selection.v1`
semantic result and return only its digest claim through child IPC and Temporal. The artifact
preserves ordered indices/sentences, coverage sets, unit, target mode, algorithm, iterations, and
algorithm metadata; wall-clock `elapsed_seconds` is intentionally omitted so exact replay has a
stable digest. The trusted parent verifies selected text and indices against the immutable run
spec before publishing the tenant/project/run-owned artifact. Selection results have an explicit
4 MiB artifact budget. This budget covers the near-limit Unicode and maximum configured NSGA-II
acceptance shape but does not promise that every pathological G2P expansion will fit; excess
output fails non-retryably as `result_too_large` and is never truncated.

## Fixed retry and deadline policy

The versioned `corpuskit.core-run.v1` workflow applies server-controlled policy:

| Boundary                            | Deadline / policy                                                                       |
| ----------------------------------- | --------------------------------------------------------------------------------------- |
| Provision/finalize activity         | 30 second start-to-close; five bounded retries                                          |
| Execution activity Temporal ceiling | 25 hour start-to-close; 73 hour schedule-to-close                                       |
| Spawned child process               | Validated per-run DTO value, server-capped at 5 minutes by default and 24 hours maximum |
| Activity heartbeat                  | 15 second timeout; worker emits every 0.5–10 seconds                                    |
| Core execution retries              | Three attempts, 2x backoff capped at 10 seconds                                         |
| Workflow                            | 74 hour run and execution deadline                                                      |
| Worker shutdown                     | 1–300 second configurable graceful drain, default 30 seconds                            |

`engine_unavailable`, `internal_error`, worker shutdown, persistence availability,
`artifact_store_unavailable`, and `manifest_storage_unavailable` are retryable. Invalid specs,
unsupported kinds, missing installed dependencies/data, unsupported languages, inventory misses,
engine contract violations, `result_too_large`, immutable-facts conflicts, missing runs, and
spec-integrity failures are non-retryable. Database projections receive only stable codes; exception messages are not
logged, stored in events, or sent to Temporal as application-error details.

Repository, hosted, local, model-analysis, DATG-build, DATG-generation, and Phon-RL deadlines are
accepted only after full validation of their reviewed request DTO. Timeout-looking child output
and unknown fields cannot alter the deadline. Core run kinds without such a field receive the
server cap. Timeout or cancellation is
acknowledged only after terminate, bounded join, kill if necessary, and final join complete, so a
child cannot produce a late staging write after the parent records cancellation.

## Live progress contract

Repository generation and Phon-RL training use the optional
`execute_with_progress(spec, emit)` handler extension. Each child message is a closed,
versioned projection containing only an allowlisted phase, a per-attempt sequence below 128,
optional completed/total counters capped at 10,000, optional coverage, and optional accepted
count. Every encoded message is capped at 512 bytes. Prompt or sentence text, source IDs, model
output, reward/loss values, paths, credentials, and arbitrary metadata have no field in this
schema and fail validation if supplied.

The parent rejects malformed, oversized, duplicate, or decreasing child sequences, then writes a
`run.progress` event only while the authoritative run is `running`. Event payloads include the
Temporal activity-attempt number so a safe retry may restart its child sequence at zero. Duplicate
or stale delivery for the same attempt is idempotently ignored. The database event sequence
remains globally increasing, so `GET /api/v1/runs/{run_id}/events?after=<cursor>` can reconnect
without gaps or reordering. Cancellation terminates and joins the child before acknowledging the
run and no progress or result is accepted after `running` ends. Progress is an observational
projection only; it is not added to Temporal workflow history and does not affect replay output.

## Local durable stack

Each deployable Python entry point requires its exact non-secret runtime posture. This scopes
production validation and prevents one service from silently starting with another service's
credential set:

| Entry point             | Required setting                     |
| ----------------------- | ------------------------------------ |
| `corpuskit-api`         | `CORPUSKIT_RUNTIME_ROLE=api`         |
| `corpuskit-dispatcher`  | `CORPUSKIT_RUNTIME_ROLE=dispatcher`  |
| `corpuskit-worker`      | `CORPUSKIT_RUNTIME_ROLE=worker`      |
| `corpuskit-maintenance` | `CORPUSKIT_RUNTIME_ROLE=maintenance` |

`api` is the configuration default for local compatibility. Every other entry point rejects that
default and any mismatched role before it connects to Temporal, PostgreSQL, or object storage.
Compose and the Helm chart set these values explicitly; operators invoking installed commands
directly must do the same.

Set the API to durable mode and start the opt-in profile:

```text
CORPUSKIT_JOB_BACKEND=temporal docker compose --profile durable up --build
```

The profile starts:

- `temporal`, with gRPC and UI bound to loopback only;
- `dispatcher`, which leases the transactional outbox and starts/signals workflows; and
- `worker-batch`, the non-root `batch-cpu` worker on the server-controlled `batch-cpu` queue.

The dispatcher ignores the process's single queue value and uses the complete immutable routing
map below. A run kind must occur in exactly one profile; unknown/duplicate mappings fail before
Temporal publication and there is no fallback.

| Queue/profile       | Registered operations                                      |
| ------------------- | ---------------------------------------------------------- |
| `batch-cpu`         | six core operations; optional DATG index build             |
| `external-provider` | hosted generation and repository generation/import         |
| `gpu-inference`     | configured local generation, perplexity, and DATG guidance |
| `gpu-training`      | Phon-RL training only                                      |

The advanced Compose services are separate opt-in profiles and default-deny when their policy JSON
is empty:

```text
CORPUSKIT_JOB_BACKEND=temporal docker compose --profile durable --profile hosted up --build
CORPUSKIT_JOB_BACKEND=temporal docker compose --profile durable --profile gpu-inference up --build
CORPUSKIT_JOB_BACKEND=temporal docker compose --profile durable --profile gpu-training up --build
```

The API admission process must receive matching non-secret selectors and opaque secret references
for the advanced run kinds. It parses and authorizes `generate-repository`, `generate-llm`, `generate-local`,
`perplexity`, `build-datg-index`, `generate-datg`, and `train-phon-rl` before inserting a run,
reserving quota, or writing an outbox message. Empty or mismatched API policy remains default-deny;
workers repeat policy enforcement at execution. Actual credential values stay worker-only.

The external-provider worker is the only worker attached to `provider-egress`. It receives only
hosted-model and Hugging Face repository policies; provider credentials are needed only for
hosted calls. GPU workers have backend-only networking
and force offline Hugging Face/Transformers mode. Model caches and API/GPU DATG cache views are
read-only. The batch parent alone receives the writable DATG publication view, and only after
strict result/policy validation; create and provision their host directories before starting the
service. Production deployment
overrides must pass `CORPUSKIT_WORKER_IMAGE_DIGEST=sha256:<digest>` into each worker.
They must also inject distinct worker/adoption service URLs; the base local Compose values are not
a production role-separation example.

PostgreSQL must be healthy, the `migrate` job must reach Alembic head, and the PHOIBLE provisioner
must complete before dependent application services start. Dispatcher and worker containers are
read-only, capability-free, and backend-only. Do not scale the local fixed dispatcher identity;
production replicas must set a unique `CORPUSKIT_DISPATCHER_ID` per process.

The API and worker profiles retain a general-purpose, bounded, `noexec` `/tmp`. Phonemizer copies
the installed eSpeak shared library for each isolated wrapper and must load that copy. Compose
therefore sets `TMPDIR=/run/corpuskit-espeak` only for those execution-capable services and mounts
that path as a separate 64 MiB `exec,nodev,nosuid`, UID/GID 10001, mode `0700` tmpfs. Do not make
all of `/tmp` executable, enlarge this exception without measurement, or add it to services that
cannot invoke eSpeak. `XDG_CONFIG_HOME=/tmp/corpuskit-xdg` keeps eSpeak's optional Pulse
configuration out of the read-only home on the ordinary non-executable tmpfs.

Useful checks:

```text
docker compose --profile durable ps --all
docker compose --profile durable logs temporal dispatcher worker-batch
docker compose exec postgres psql -U corpuskit -d corpuskit -c \
  "select state, count(*) from runs group by state order by state"
```

Staging and production require PostgreSQL, Temporal TLS, external OIDC, exact HTTPS origins, and
API docs disabled. When Temporal Cloud/API-key authentication is used, provide
`CORPUSKIT_TEMPORAL_API_KEY` through the runtime secret store and set
`CORPUSKIT_TEMPORAL_TLS=true`; never place it in Compose source or a run spec.

## Submission and result polling

- `POST /api/v1/runs` — persisted submission; 201 new or 200 idempotent replay.
- `GET /api/v1/runs` and `GET /api/v1/runs/{id}` — tenant-scoped state, safe failure code, and
  bounded result summary.
- `GET /api/v1/runs/{id}/events?after=N` — ordered append-only polling.
- `POST /api/v1/runs/{id}/cancellation` — cooperative cancellation request.
- `POST /api/v1/runs/{id}/retries` — immutable child attempt for failed/cancelled runs.

Clients persist the greatest event sequence they have observed and poll with `after`. Empty pages
are normal. `outbox_state=published` means Temporal durably accepted the intent; it does not imply
that the run reached `running`.

## Monitoring, recovery, and reconciliation

Alert on oldest pending/claimed outbox age, publish failures, expired-lease reclaims, queued age,
workflow/activity failure rate, heartbeat timeouts, retry storms, and task-queue lag. Never use
organization, subject, run spec, idempotency key, or corpus text as metric labels.

During a Temporal outage:

1. leave committed outbox rows pending and stop repeatedly unhealthy dispatchers;
2. restore TLS/DNS/service connectivity without editing outbox payloads;
3. start one dispatcher and observe expired leases drain;
4. compare non-terminal PostgreSQL runs with their deterministic Temporal workflow IDs; and
5. never infer `running` or `succeeded` from `outbox_state=published` alone.

Database restore must preserve runs, events, and outbox rows at the same recovery point. Expired
claims are reclaimable after restore. A closed workflow cancellation is acknowledged only after a
tenant-scoped database probe confirms the run is terminal.

## Handler extension protocol

New durable capabilities implement `DurableRunHandler` with one fixed `RunKind` and an
`execute(spec)` method returning a bounded summary. Long-running handlers may additionally
implement the strict `execute_with_progress` extension described above. Registration is explicit through
`HandlerRegistry.extended`; duplicate kinds fail worker startup. A release adding a handler must:

1. define a strict extra-forbid DTO and resource bounds;
2. keep the Temporal reference unchanged—IDs and hash only;
3. classify retryable and non-retryable failures into stable codes;
4. use a provider idempotency key when the provider contract supports one; otherwise disclose and
   gate the at-least-once side-effect/cost risk;
5. keep the handler and its unopened dependencies spawn-serializable (no live sockets, locks, or
   anonymous callables in the registered object graph);
6. assign a server-side worker profile/queue and least-privilege image; and
7. add workflow replay, crash/redelivery, cancellation, tenant-isolation, and real-Temporal tests.

`build_worker()` now delegates to exact profile composition. Hosted, repository, local-analysis,
DATG, and Phon-RL handlers are enabled only by matching server policy on their reviewed queues and
always use canonical parent adoption. Repository generation uses the same single outer process
boundary. Export has no handler and is deliberately rejected before persistence. Live Hub,
provider, and CUDA gates remain separate from structural worker registration.
