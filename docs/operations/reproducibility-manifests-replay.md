# Reproducibility manifests and durable replay

Status: CK-REP-001/002 are **Verified**. In addition to focused SQLite and distinct-role
PostgreSQL contracts, mandatory CI executes a seeded stochastic selection and replay through
non-owner API, dispatcher, worker, and adoption roles on PostgreSQL 17, real Temporal, an exact
image-attested non-root worker, and private MinIO. The gate proves duplicate dispatcher delivery
converges, canonical manifests bind the complete normalized recipe and seed, and source/replay
selection artifacts are byte-identical with an exact comparison verdict.

## Trust boundary

Clients never submit manifest provenance. `TrustedExecutionFacts` is a strict, versioned,
server-authored DTO with no organization, project, run, user, object key, endpoint, credential, or
request-supplied authority. It records:

- installed CorpusKit and CorpusGen versions;
- an allowlisted worker profile, immutable worker image digest, and canonical worker-policy digest;
- optional eSpeak, PHOIBLE, model, dataset, input-artifact, and cache/snapshot attestations; and
- the declared `exact`, `best-effort`, or `nonreproducible` classification.

The trusted parent must build this DTO from its validated deployment policy and immutable runtime
facts immediately before child computation. It calls
`RunManifestService.record_execution(reference, facts)` through the worker database identity.
The service reloads the run from the signed workflow reference, recomputes the normalized
specification digest, resolves all corpus/artifact inputs under authoritative organization and
project scope, streams and re-hashes input objects, validates required model provenance, and then
inserts one immutable `run_execution_facts` row. Redelivery with byte-identical facts is a no-op;
different facts fail closed.

No child handler, request body, result summary, or staged-artifact envelope may populate these
fields. In particular, a child-reported image/model/version is not evidence.

For an allowlisted Hugging Face repository, dataset provenance records the namespaced dataset,
config, split, immutable commit revision, and a SHA-256 of that canonical selector. The separate
`content_sha256` remains null unless an execution path independently attests the complete loaded-row
snapshot; CorpusKit never labels a selector hash as a dataset-content hash.

## Manifest finalization

After authoritative result adoption has committed the run as `succeeded`, the parent calls
`RunManifestService.finalize(reference)` through the adoption identity. The service:

1. reloads the immutable run, execution facts, start/success events, corpus version, and active
   input/output artifact rows;
2. recomputes the normalized run-spec and corpus digests;
3. streams every referenced object and verifies key, media type, size, and SHA-256;
4. constructs strict `corpuskit.run-manifest.v1` canonical JSON with no NaN/Infinity;
5. writes the content-addressed manifest object and reuses an identical existing object safely;
6. in one locked database transaction consumes artifact quota, inserts the run-owned immutable
   artifact, binds it to the execution-fact row, appends chained audit evidence, and completes any
   replay comparison.

The database transaction is idempotent. A crash after the object write leaves an orphan that the
normal grace-delayed reconciler can inspect; redelivery converges on one metadata row. PostgreSQL
allows the adoption role to set the manifest binding once, while triggers prevent changes to the
recorded facts or completed binding. The API role cannot insert execution facts or update either
reproducibility table.

Provenance is conditional. eSpeak, PHOIBLE, model, and dataset fields are required only when the
normalized workflow actually depends on them. Missing required provenance fails finalization;
inventing an unused dependency is not necessary.

## Durable replay

`RunManifestService.submit_replay(actor, project_id, source_run_id, idempotency_key)` first reads
and fully verifies the canonical source manifest. Under the API transaction it then creates:

- a new queued run with the exact stored source kind, corpus version, normalized specification,
  specification digest, and parent-run lineage;
- one `run_replays` row bound to the verified source manifest digest;
- the initial replay event and dispatch outbox message;
- an idempotent per-tenant quota reservation; and
- allowlisted chained audit evidence.

The caller cannot replace parameters, inputs, provenance, classification, or source-manifest
identity. The idempotency key is organization-scoped; reusing it for different lineage fails.
Normal durable dispatch executes the replay. Its worker must record fresh authoritative execution
facts, and adoption finalizes the observed manifest. Comparison keeps replay inputs distinct from
outputs:

| Source classification | Result                                                                                                                                  |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `exact`               | Exact only when replay-critical inputs, exact classification, and ordered output digest/size/name records match; otherwise mismatch.    |
| `best-effort`         | Reports a best-effort match/divergence when inputs remain compatible; provenance drift or a nonreproducible observed run is a mismatch. |
| `nonreproducible`     | Always disclosed as nonreproducible; submission is permitted for investigation but never presented as deterministic reproduction.       |

External provider behavior, mutable upstream datasets, model registries, drivers, hardware kernels,
and nondeterministic libraries can still cause divergence. The manifest records identity evidence;
it does not make an uncontrolled dependency deterministic.

## API-ready contract

The isolated `reproducibility_router(service)` defines these authenticated routes:

- writer (`owner`, `admin`, `editor`)
  `POST /projects/{project_id}/runs/{source_run_id}/replays` with `Idempotency-Key`; and
- any authenticated member `GET /replays/{replay_run_id}`.

The POST route requires an empty body. A new resource returns 201 and an idempotent replay returns 200. Inaccessible tenant/project/run IDs are indistinguishable from absent IDs. The router maps
internal integrity codes to bounded application errors and does not return paths, object keys,
content, secrets, or exception text. There is deliberately no public manifest-creation endpoint:
publication is an adoption-role terminalization responsibility, and users read the resulting
artifact through the normal tenant-authorized artifact API.

`api/app.py` mounts this reduced router with one application-owned
`RunManifestService(api_database, object_store, settings)` and externally managed lifecycles. Its
HTTP methods need only the API database identity and object-store client. Worker/adoption
processes must use their role-specific database handles; the HTTP process must never receive
either credential. Mounting replay does not authorize clients to create facts or manifests.

## Job Center workflow

The selected run in `/jobs` exposes **Replay this terminal run** only after it reaches a terminal
state. Submission is enabled only for a succeeded run because failed and cancelled runs have no
finalized source manifest, and only an `owner`, `admin`, or `editor` may invoke it. Viewers retain
read access but the action fails closed. The browser sends an empty-body POST through the
same-origin authenticated BFF; CSRF/session handling remains centralized there.

One generated `Idempotency-Key` is retained for the selected source run and reused after an
uncertain request failure. This prevents a user retry from silently creating two replay runs. The
client accepts only the strict replay DTO, polls `GET /replays/{replay_run_id}` until `compared` or
`unavailable`, and displays:

- source and replay run identity, source-manifest identity, classification, lifecycle, and expected
  manifest digest;
- source and observed manifest links through the normal tenant-authorized Artifact Manager;
- exact/best-effort/nonreproducible verdict, input compatibility, ordered-output equality, and
  bounded differing manifest-field names; and
- bounded authentication, authorization, conflict, contract, and availability errors without
  reflecting server exception text.

The UI never accepts replacement parameters, provenance, object keys, or a manifest request body.
Unit contract tests cover malformed projections and empty-body/idempotency transport. Component
tests cover writer/viewer behavior, polling/comparison, and same-key recovery; mocked Playwright
acceptance covers the complete user action and artifact links. The separate combined runtime gate
provides the real PostgreSQL/Temporal/private-object-store execution evidence.

## Database deployment

Migration `0005_reproducibility` creates `run_execution_facts` and `run_replays`, constraints,
indexes, forced RLS policies, least-privilege grants, and PostgreSQL immutability triggers. Apply
it through the schema-owner migration job. Runtime principals must remain non-owner,
non-superuser, and non-`BYPASSRLS` members of exactly the documented API/worker/adoption role.

The worker role can insert facts only for its transaction-local organization context. Adoption
can bind a completed manifest and comparison for that organization. API members can create replay
lineage through the service transaction but cannot mint worker evidence. SQLite retains explicit
application predicates for isolated development/tests and is rejected in staging/production.

## Verification and incident response

Automated evidence covers canonical/secret/nonfinite validation, conditional provenance,
full-object integrity, corpus/input/output binding, duplicate fact/finalization/replay delivery,
source and observed tampering, exact/best-effort/nonreproducible comparison, quota/audit atomicity,
cross-tenant enumeration, PostgreSQL role/trigger enforcement, and the typed Job Center replay
workflow. Mandatory combined acceptance adds a real external worker, Temporal redelivery,
PostgreSQL role separation, private MinIO storage, and a byte-identical seeded selection replay.
The focused domain/service/API suite exceeds 90% branch-inclusive coverage.

Treat these as integrity incidents and stop replay publication:

- execution fact/spec/corpus/artifact digest conflict;
- missing or malformed source manifest;
- completed-binding or replay-comparison mutation attempt;
- manifest object missing/corrupt or noncanonical JSON; or
- a runtime version different from the server's installed expected version.

Preserve the database audit chain and object metadata, disable the affected worker profile, and
reconcile from independently verified artifacts. Never “repair” a manifest by editing its row or
object; rerun under a reviewed version and preserve lineage.

## Remaining release gates

- Provision the independent worker/adoption deployment secret handles with genuinely distinct
  non-owner credentials in each environment; never weaken RLS or reuse an API/worker login for
  manifest binding.
- Add object-store readiness/policy checks and production alert evidence.
- Prove exact dependency snapshots for each advanced worker profile and clearly label hosted/GPU
  best-effort or nonreproducible behavior.

A canonical manifest alone is not proof that the declared worker environment executed the run;
preserve the image-attested combined runtime gate and deployment-specific supply-chain evidence.
