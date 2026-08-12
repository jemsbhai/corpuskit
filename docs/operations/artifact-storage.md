# Artifact storage and reproducibility manifests

Status: implemented storage foundation, authoritative parent-side result adoption, server-built
run manifests, and durable replay coordination. Deployment templates expose independent worker
and adoption database-secret handles for every worker profile, and CI exercises clean
PostgreSQL with distinct non-owner roles. A mandatory seeded manifest/replay gate through real
Temporal, PostgreSQL 17, and private MinIO runs in CI. Deployment-specific credential
provisioning and production object-store readiness, encryption, policy, and alert evidence remain
release-gated.

## Implemented boundary

CorpusKit stores immutable artifact metadata in PostgreSQL/SQLite and bytes in either a local
filesystem adapter (development/test) or a fixed S3-compatible endpoint. Object keys contain
only generated tenant/project/run identifiers, kind, and SHA-256; an upload filename is never
used as a path. Metadata queries include organization and project predicates, and inaccessible
IDs return the same not-found response as absent IDs.

The public API currently accepts only `corpus-text`, explicitly treating it as untrusted user
input. Writers cannot upload bytes labeled as manifests, exports, evaluation reports,
checkpoints, model adapters, or run results. A trusted durable parent can adopt a reviewed
`run-result` from an internal staging object after reloading the producing run; this path is not
available to an HTTP actor. Trusted exports, reports, checkpoints, and adapters remain closed
until their producing service can construct and commit them from durable execution facts. A
parent-only service can now record
immutable worker execution facts, build a canonical `RunManifest` after success, persist it as a
run-owned `run-manifest` artifact, and submit an idempotent replay run from that exact durable
recipe. No HTTP route can trigger trusted manifest construction. The mounted no-body replay router
can only submit the verified stored recipe and read its tenant-authorized comparison status.

Full authenticated downloads verify storage metadata, byte count, media type, and SHA-256 while
streaming. Byte ranges are rejected with 416 because a partial response cannot prove the full
object digest. S3 deployments may also issue an authorization-checked, SigV4, short-lived GET
URL; the endpoint, bucket, path style, response disposition, expiry, and signature fields are
server controlled.

## Staged worker-result adoption

An artifact-producing child has deliberately narrow authority. It writes strict result JSON bytes to
`staging/v1/sha256/<prefix>/<digest>` through `ConfiguredStagedArtifactWriter` and returns only the
versioned `StagedArtifactResult` envelope: contract, staged digest reference, reviewed schema ID,
`run-result` type, `application/json` media type, and byte size. Extra fields are rejected; in
particular, the child cannot name an organization, project, run, user, filename, final key, bucket,
endpoint, or timeout.

The parent then:

1. reloads the `RunWorkflowReference` and immutable run row, including organization, project,
   creator, kind, canonical specification, and specification digest;
2. derives the only allowed schema and per-run process deadline from strict server-owned DTOs;
3. streams the staged object with a fixed chunk bound, checking descriptor, exact size, SHA-256,
   media type, result DTO, and schema; selection adoption additionally binds ordered selected
   indices and text, unit, target mode, algorithm, selection limit, and explicit target coverage
   space to the immutable run spec;
4. writes and re-reads the final tenant/project/run content address; and
5. under a locked run row, inserts one run-owned artifact and commits the `succeeded` state/event
   in the same database transaction.

Redelivery first verifies the staged and final bytes and then returns the existing artifact. A
crash after final object write but before database commit leaves an idempotently adoptable object.
Cancellation is checked before staging read, after validation, and again in the locked terminal
transaction. If cancellation won, no artifact row or success event is committed; the final object
is an orphan eligible for later reconciliation. Child errors and storage/schema failures persist
only stable codes, never content, local paths, object keys, or exception text.

Current reviewed schemas cover full corpus selection, repository generation, hosted generation,
local generation, language-model analysis, DATG index/guided generation, and Phon-RL training.
The selection schema has a code-owned 4 MiB limit independent of a larger configured object limit;
over-budget results fail as `result_too_large` before a claim is returned and are never truncated.
This allowlist is necessary but not sufficient to enable a handler: the
worker profile must also have an exact dependency/policy composition. The shipped `batch-cpu`
worker registers the six core handlers and can add only policy-gated DATG index building.
`external-provider`, `gpu-inference`, and `gpu-training` compose only their reviewed hosted,
local/model-analysis/DATG, and Phon-RL handlers, respectively. Repository generation has a
bounded standalone callable but is not registered as a durable batch handler.

## Configuration

The relevant `CORPUSKIT_*` settings are:

| Setting | Purpose |
|---|---|
| `ARTIFACT_BACKEND` | `filesystem` locally or `s3`; staging/production require `s3` |
| `ARTIFACT_ROOT` | Local adapter root; never used as an S3 endpoint |
| `ARTIFACT_MAX_BYTES` | Per-object service limit, at most 100 MiB |
| `ARTIFACT_RETENTION_DAYS` | Tombstone retention, minimum 30 days |
| `ARTIFACT_ORPHAN_GRACE_SECONDS` | Minimum age before an unreferenced object may be removed |
| `ARTIFACT_S3_ENDPOINT` | Fixed credential-free HTTP(S) origin; HTTPS is mandatory in staging/production |
| `ARTIFACT_S3_BUCKET`, `ARTIFACT_S3_REGION` | Fixed server-owned routing; never accepted per request |
| `ARTIFACT_S3_PATH_STYLE` | Off by default; enable only for a compatible endpoint such as local MinIO |
| `ARTIFACT_S3_SSE` | `AES256` (SSE-S3) or `aws:kms`; mandatory in staging/production |
| `ARTIFACT_S3_KMS_KEY_ID` | Required with SSE-KMS and verified on stored objects |
| `ARTIFACT_S3_CONNECT_TIMEOUT_SECONDS`, `ARTIFACT_S3_READ_TIMEOUT_SECONDS` | Bounded SDK deadlines |
| `ARTIFACT_S3_MAX_ATTEMPTS` | Standard retry budget, one through five total attempts |
| `ARTIFACT_PRESIGN_SECONDS` | Maximum signed URL lifetime, 30 through 900 seconds |

Access/secret/session credentials are secret-valued settings and excluded from settings
representations. In production, inject short-lived workload credentials rather than static
keys. Never put credentials, endpoints, buckets, ACLs, KMS keys, object keys, or response headers
in a user request.

## Local MinIO acceptance

Docker Compose pins MinIO server and client images by immutable manifest digest, creates the
private `corpuskit-artifacts` bucket through a one-shot initializer, and configures API/worker
S3 clients for explicit path-style access. The published ports bind to loopback only. Local
MinIO intentionally does not enable SSE because it has no production KMS; this is why the stack
runs in development mode. Do not copy that exception to staging or production.

```bash
docker compose up -d minio minio-init
CORPUSKIT_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
CORPUSKIT_TEST_S3_ACCESS_KEY=corpuskit-local \
CORPUSKIT_TEST_S3_SECRET_KEY=corpuskit-local-secret \
uv run pytest -o addopts='' tests/integration/test_s3_minio.py -q
```

CI also uploads `corpus-text` through the authenticated API, downloads it from MinIO, compares
SHA-256, and tombstones it. Anonymous bucket access remains disabled.

## Write failure, idempotency, and reconciliation

1. Authorization and project/run scope are checked before object I/O.
2. The service computes SHA-256 and rejects caller digest, size, media type, and filename
   violations.
3. The store uses `If-None-Match: *`; an existing content address is accepted only when its
   size, digest, and media type agree.
4. Metadata is inserted under a unique tenant/project/run/kind/digest constraint.
5. A failed metadata transaction deliberately does not immediately delete newly written bytes:
   a concurrent idempotent writer may already have adopted that same address.
6. Reconciliation queries metadata for the exact object keys returned by the current store page.
   Unreferenced objects younger than the configured grace window are deferred; older objects are
   deleted. A separate bounded audit reports missing or corrupt referenced objects.

Reconciliation is idempotent and safe to retry. The singleton maintenance command pages it with
a durable private cursor, advances that cursor only after a successful page, and resets it after
the final page so early young/referenced keys cannot starve later objects.

Staging cleanup is separate from final-object reconciliation. It lists at most 1,000 digest keys
per call, accepts only a cursor within the staging prefix, defers objects younger than the orphan
grace period, and treats a concurrently missing key as success. Delete/list failures are bounded
and retry-safe. The maintenance runner persists and compare-and-swaps this cursor internally; it
does not expose object keys in operator JSON. It restarts from the beginning after the final page
so deferred objects are revisited after their grace period.

## Deletion and recovery semantics

DELETE immediately tombstones metadata, so metadata, downloads, and new signed URLs become
tenant-inaccessible. Bytes are retained for at least 30 days. A purge deletes due object bytes
first and conditionally marks metadata deleted; failures remain retryable. Database rows retain
lineage. An already issued signed URL cannot be revoked by a tombstone and may work until its
maximum 15-minute expiry. Emergency revocation therefore requires bucket/KMS policy or
credential action. Backup expiry and legal hold policy remain deployment responsibilities.

## Production bucket policy checklist

- private bucket with account-level and bucket-level public access blocks;
- deny public ACLs and policies; the application never sends an ACL;
- TLS-only transport policy;
- require the configured SSE-S3 or exact SSE-KMS key;
- application identity limited to this bucket/prefix and required get/put/head/list/delete calls;
- access logging and object-store audit events without content or credentials;
- lifecycle/versioning aligned with database tombstone, legal hold, and backup policy;
- alarms for write/read/delete failures, missing/corrupt objects, and orphan growth; and
- restore/reconciliation drill across database and object backups.

## Known release gaps

- `/health/ready` does not yet probe bucket reachability, encryption, or policy.
- The default batch worker intentionally does not register repository or model generation.
  Least-privilege external-provider/GPU images and profile handlers are composed behind exact
  policies, but qualified deployment secrets, provider/model evidence, and hardware acceptance
  remain environment gates. Repository generation is not adapted to the durable handler contract.
- Orphan reconciliation, retention purge, and staged-result cleanup are exposed through the
  bounded [`corpuskit-maintenance` command](maintenance.md). A production scheduler and alert
  evidence remain deployment gates.
- Malware scanning, legal holds, backup/restore drills, and emergency signed-URL revocation
  automation remain platform gates. PostgreSQL RLS, transactional quota accounting, and chained
  audit evidence are implemented, but still require deployment-specific role/policy evidence.

The S3 calls follow the maintained Boto3 interfaces for
[`put_object`](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/put_object.html),
[`get_object`](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/get_object.html),
[`generate_presigned_url`](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/generate_presigned_url.html),
and bounded [Botocore configuration](https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html).
