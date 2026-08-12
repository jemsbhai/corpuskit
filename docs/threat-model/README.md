# CorpusKit threat model

## Status and review triggers

- Initial model: 2026-08-11
- Scope: target production architecture described in
  [the architecture overview](../architecture/overview.md)
- Method: asset/trust-boundary analysis with STRIDE-style threat enumeration

Review this model before general availability and whenever identity, tenancy, upload limits,
provider integrations, model execution, worker profiles, data retention, public sharing, or
deployment boundaries change. Security-sensitive decisions must link to a test and an
operational owner.

## Security objectives

1. An organization cannot read, modify, execute against, or infer another organization's
   corpus, run, artifact, secret, model adapter, membership, or audit data.
2. An attacker cannot cause unbounded compute, provider spend, storage consumption, or queue
   starvation.
3. User-controlled text, files, prompts, model identifiers, and metadata cannot become code,
   commands, unauthorized network requests, or log instructions.
4. Credentials and tokens remain confidential and can be rotated and revoked.
5. Scientific results retain integrity, provenance, reproducibility, and clear indication of
   fixture versus live execution.
6. Expected component or provider failure does not corrupt state or publish duplicate
   results.

## Assets and sensitivity

| Asset                          | Primary concern               | Notes                                                            |
| ------------------------------ | ----------------------------- | ---------------------------------------------------------------- |
| Corpus text and uploads        | confidentiality, integrity    | may contain unpublished, licensed, personal, or clinical content |
| Generated text and prompts     | confidentiality, integrity    | may reveal source content or user intent                         |
| PHOIBLE/eSpeak/model metadata  | integrity                     | wrong versions invalidate reproducibility                        |
| Run specifications and results | integrity, availability       | scientific history must not be rewritten                         |
| Provider credentials           | confidentiality               | can incur external cost and data access                          |
| OIDC sessions and tokens       | confidentiality, authenticity | enable user and organization access                              |
| Model adapters/checkpoints     | confidentiality, integrity    | potentially expensive and user-derived IP                        |
| Audit records                  | integrity, availability       | required for investigation and accountability                    |
| Compute and provider budget    | availability, financial       | GPU/LLM abuse can be costly                                      |
| Signing/encryption keys        | confidentiality, integrity    | compromise affects the entire service                            |

Corpus and generated text are treated as sensitive by default. CorpusKit does not use user
content to train shared models. Logging content requires an explicit local-only diagnostic
setting and must never be available in production.

## Actors

- legitimate owners, administrators, editors, and viewers;
- guest demo users;
- an authenticated but malicious tenant;
- an unauthenticated internet attacker;
- a compromised browser or stolen session;
- a malicious upload, dataset, model, adapter, or prompt author;
- a compromised external identity/model/dataset provider;
- an operator with infrastructure access; and
- a compromised application dependency, image, or worker.

## Trust boundaries

1. Browser to web/API over the public internet.
2. Web/API to the external OIDC provider.
3. API to PostgreSQL, cache, object storage, secret manager, and Temporal.
4. Temporal to worker profiles.
5. Workers to organization-scoped data and artifacts.
6. External-provider workers to hosted LLM and dataset/model endpoints.
7. GPU workers to downloaded models and user-created adapters.
8. Operator and CI/CD access to production infrastructure.
9. Guest demo tenant to persistent authenticated tenants.

No identifier crossing a boundary proves ownership by itself. Authorization uses verified
identity, current membership, requested action, and resource organization.

## Principal threats and controls

### Identity and session spoofing

Threats include forged JWTs, algorithm confusion, issuer/audience mismatch, replay, stolen
cookies, login CSRF, account recovery abuse, and stale organization membership.

Controls:

- OIDC Authorization Code with PKCE, state, and nonce;
- strict issuer, audience, algorithm, signature, expiration, and clock-skew validation;
- secure HTTP-only same-site cookies, TLS, CSRF protection on state changes, and restrictive
  CORS;
- opaque `__Host-` browser sessions with all provider tokens retained server-side; the BFF
  discards browser authorization and synthesizes bearer credentials only after session lookup;
- AES-256-GCM encryption of Redis/Valkey login and session records with Redis-key associated
  data, an active-plus-old application key ring, TLS credentials, bounded store operations, and
  no production memory fallback;
- one-time login transactions, exact return-path allowlisting, session-ID rotation at login,
  absolute and idle expiry, early token refresh, refresh-token rotation, and distributed
  per-session locking to prevent concurrent stale-token overwrite;
- same-origin HTTPS/443 OIDC discovery/token/JWKS/revocation egress without redirects or endpoint
  query strings, plus streaming caps on identity-provider and BFF request/response bodies;
- a fresh unpredictable CSP nonce on every dynamically rendered page, `strict-dynamic`, and no
  production `script-src 'unsafe-inline'` or `unsafe-eval`;
- short session lifetime, revocation/rotation support, and reauthentication for secrets,
  membership, and destructive actions;
- current application membership checked on each request; and
- production builds refuse test authentication.

### Broken tenant authorization and IDOR

Threats include swapping project/artifact/job IDs, signed URL reuse, worker queries without
tenant scope, indirect inference through errors or timing, and cross-tenant cache keys.

Controls:

- authorization at the service boundary for every object and action;
- non-null `organization_id` on tenant-owned rows and forced PostgreSQL RLS;
- validated transaction-local organization/user/service context plus distinct non-owner,
  non-`BYPASSRLS` policy roles; forged API-side service GUCs do not satisfy both checks;
- pooled connections clear context after commit/rollback, and real PostgreSQL tests enumerate
  every current resource table under hostile tenant IDs;
- organization-scoped repository interfaces, cache keys, workflow/activity inputs, and
  artifact metadata;
- opaque IDs, uniform not-found responses, short-lived signed URLs, and private buckets;
- no client-selected organization or Temporal queue without verified membership/policy; and
- exhaustive negative authorization tests across all roles and resources.

### Malicious uploads and parser exhaustion

Threats include oversized files, compression bombs, malformed Unicode/CSV/JSON, formula
injection in exports, path traversal filenames, content-type spoofing, parser complexity,
and stored malicious HTML.

Controls:

- streaming upload limits, maximum expanded size/row/line/field lengths, timeouts, and quotas;
- server-generated object keys and filenames retained only as escaped metadata;
- allowlisted formats with parsing in a sandboxed batch worker;
- Unicode normalization recorded as part of an immutable import policy;
- output encoding and Content-Disposition enforcement; spreadsheet exports neutralize formula
  prefixes;
- UI treats all text as data and does not render user HTML; and
- malware scanning remains a deployment-specific release gate where the risk classification
  requires it; it is not implemented by the current application.

### Prompt injection and untrusted generated content

Corpus text may contain instructions aimed at hosted models or operators. Generated text may
contain unsafe, misleading, or executable-looking content.

Controls:

- prompts clearly delimit data and instructions, with no tool use enabled by default;
- provider workers expose no general application credentials or arbitrary tools;
- generated content is untrusted data, escaped in UI and exports;
- provider/model identity, prompt template hash, warnings, and moderation policy are recorded;
- no generated command is executed automatically; and
- output filters, cost limits, and user review are explicit product controls, not a claim
  that injection can be eliminated.

### SSRF and arbitrary network access

Threats include user-supplied dataset/model URLs targeting instance metadata or internal
services, provider redirects, and model code fetching additional resources.

Controls:

- accept registry IDs rather than arbitrary URLs where possible;
- HTTPS/domain allowlists, DNS/IP validation, redirect revalidation, and blocked private,
  loopback, link-local, and metadata address ranges;
- network policy permits public egress only from dedicated external-provider workers;
- proxy-level request size/time limits and response type validation; and
- local model workers default to offline/cached revisions with `trust_remote_code=False`.

### Unsafe model and adapter loading

Threats include remote-code execution, malicious pickle/deserialization, dependency confusion,
GPU denial of service, and adapters incompatible with a base model.

Controls:

- allowlisted immutable model revisions and safetensors-style formats where supported;
- `trust_remote_code=False` unless a revision completes an explicit security review;
- signed worker images, locked dependencies, SBOMs, vulnerability scanning, and no runtime
  package installation;
- model loading inside least-privileged GPU workers with read-only roots and constrained
  service accounts;
- validate adapter format, size, provenance, base model, and organization ownership; and
- hard memory/time/token limits with worker termination and clean rescheduling on exhaustion.

### Credential disclosure

Threats include keys in form telemetry, browser storage, database rows, Temporal history,
exceptions, logs, traces, support tools, or provider request dumps.

Controls:

- secret manager storage with envelope encryption and opaque references;
- plaintext resolution only inside the scoped provider activity;
- structured allowlist logging and centralized redaction of headers, credentials, corpus text,
  prompts, and generated content;
- credentials never returned after creation or placed in URLs/local storage;
- per-provider scope, rotation, deletion, and use audit; and
- automated canary-secret and log/workflow-history scanning.

### Job tampering, replay, and duplicate effects

Threats include unauthorized cancellation/retry, changed inputs after queueing, replayed
provider calls, forged progress, double result publication, and workflow/version mismatch.

Controls:

- immutable run specifications and content-addressed inputs;
- authorized cancellation/retry endpoints with audit events;
- transactional outbox, idempotent workflow IDs, activity idempotency keys, and atomic result
  publication;
- monotonic event sequence numbers and server-authored state transitions;
- Temporal deterministic workflow versioning and replay tests; and
- provider retries only where safe, under token/cost ceilings.

### Resource and financial denial of service

Threats include huge target combinations, pathological edit-distance inputs, repeated ILP or
NSGA-II jobs, unlimited LLM tokens, model-download churn, GPU queue starvation, and guest
tenant abuse.

Implemented controls:

- atomic per-tenant CPU/expensive job reservations plus artifact-byte/count and corpus-sentence
  accounting under a locked usage row;
- typed per-run accepted-sentence, iteration, activity-deadline, provider token/cost, RL
  step/token, and checkpoint-size admission ceilings;
- explicit fail-closed classification for every run kind, idempotent release, and stale-run
  terminalization before expired capacity can be reused; and
- atomic PostgreSQL-backed authenticated rate limits keyed by tenant, opaque subject/route
  digests, method, and fixed window, with independent read/write ceilings;
- a non-enumerating HTTP 429 with bounded server-owned `Retry-After`.
- fail-closed HTTP 503 responses that do not reflect persistence details when the limiter's
  database authority is unavailable.

Unauthenticated edge connection limiting, accumulated billing/provider ledgers, GPU-minute
accounting, fair-share scheduling, circuit breakers, global emergency stops, guest-tenant
auto-deletion, and complete spend/queue alerting remain release gates.

### Data tampering and provenance fraud

Threats include modified uploads/results, stale PHOIBLE/model revisions, altered manifests,
fixture output presented as live, and operator edits to history.

Controls:

- SHA-256 for source and output artifacts with verification on read/commit;
- immutable versions/specifications and PostgreSQL-immutable audit events;
- a strict parent-only service records immutable execution facts, verifies authoritative
  run/corpus/artifact state, and constructs canonical run-owned manifests; clients cannot submit
  provenance fields;
- durable replay verifies the source manifest, clones only its stored recipe, atomically reserves
  quota/outbox/lineage/audit state, and distinguishes exact, best-effort, and nonreproducible
  outcomes;
- clearly labeled fixture/demo results isolated from live runs; and
- restricted migration/operator roles with audited break-glass access.

Artifact storage additionally uses fixed server-configured endpoints/buckets, generated
content-addressed keys, conditional idempotent writes, S3 checksum metadata, bounded full-object
verification, and private ACL defaults. Public uploads can only be labeled as untrusted corpus
text; writers cannot mint manifests, exports, evaluation reports, checkpoints, or model
adapters. Orphan deletion has a grace period and rechecks exact returned keys so it cannot race
another writer adopting the same content address. Signed URLs are capability tokens that cannot
be revoked after issuance, so their maximum lifetime is 15 minutes and emergency revocation is
an object-store/KMS policy action.

Artifact-producing child processes are treated as untrusted for authorization and provenance.
Their versioned return envelope cannot contain tenant, project, run, user, endpoint, key, filename,
or timeout authority. The parent derives those facts from a tenant-scoped immutable run row,
streams and re-hashes staged and final bytes, validates an allowlisted strict result schema, and
locks the run for the combined artifact/success commit. Cancellation is checked at each boundary
and in that final transaction. Staging cleanup is cursor-bounded and grace-delayed so retrying or
concurrent runs cannot lose a shared content address.

### Repudiation and insufficient audit

Threats include denying a secret change, membership update, export, deletion, training run, or
costly provider request.

Implemented controls cover project/corpus creation, project deletion request/final purge, run
submit/cancel/retry/terminal state,
artifact create/tombstone/purge/adoption/manifest publication, replay submit/comparison,
reservation expiry, and privileged quota-policy changes. Each mutation appends in the same
transaction to an organization-scoped monotonic
sequence/SHA-256 chain. Metadata is action-specific, allowlisted, and bounded; it never records
secret values, prompts, specifications, storage paths, signed URLs, or corpus content.
PostgreSQL rejects audit-event update/delete, and only current owner/admin members can page the
chain. Identity/membership/secret/export audit actions, externally anchored or WORM evidence,
long-term audit retention, and automated integrity monitoring remain release gates.

### Supply-chain and deployment compromise

Threats include malicious packages/actions/images, dependency confusion, leaked CI secrets,
unsigned deployment, overly broad Kubernetes service accounts, and vulnerable native tools.

Controls:

- committed lockfiles, hash-verified installs where available, dependency review, SBOMs,
  artifact/image signing, provenance attestations, and vulnerability scanning;
- protected branches/environments, least-privileged short-lived CI identity, and two-person
  review for deployment/security changes;
- immutable images promoted through environments; no production build from a mutable branch;
- least-privileged service accounts, pod security, network policy, read-only filesystems, and
  regular base image/eSpeak/model runtime patching; and
- production deployment verifies signatures and expected image digests.

### Availability and third-party failure

Threats include OIDC/provider/model registry outage, database/object-store interruption,
worker crash, and poison jobs repeatedly failing.

Controls:

- bounded timeouts, retry budgets, circuit breakers, dead-letter/operator review, and honest
  capability status;
- durable Temporal workflows and idempotent output commits;
- graceful degradation keeps local core features usable when external providers fail;
- database/object-store backups, restore drills, and multi-zone operation are required
  deployment controls but are not yet evidenced by this repository; and
- poison-job isolation prevents one tenant or input blocking a queue.

## Privacy and retention

- Collect only the identity, content, configuration, and telemetry required to deliver the
  service.
- Project deletion immediately removes logical access, tombstones project artifacts, retains data
  for a configurable minimum of 30 days, and permits only a dedicated maintenance identity to
  remove rows after object bytes are gone. Corpus quota is released exactly at final purge while
  audit evidence remains.
- Deletion serialization and a second artifact-write authorization prevent active artifacts from
  committing after the request. A surviving unreferenced object blocks project purge until
  grace-delayed reconciliation removes it.
- Define retention for events, audit records, provider traces, and guest tenants. Legal holds,
  external-provider deletion, and backup expiry/erasure evidence remain release gates.
- Backups follow a documented expiry policy. Deletion documentation distinguishes immediate
  logical unavailability from eventual backup expiry.
- Provider requests disclose which data leaves the deployment before execution. A user must
  opt in to external processing and choose/save credentials deliberately.
- Do not use user content for shared model training or product analytics.

## Security validation requirements

Before general availability:

- threat scenarios map to named automated tests or a documented manual control;
- authorization tests cover every role/action/resource pair and hostile cross-tenant IDs;
- SAST, dependency, secret, container, IaC, and SBOM scans have no unresolved critical or high
  findings;
- upload fuzzing and property tests cover parser limits and Unicode edge cases;
- SSRF tests include redirects, DNS rebinding defenses, IPv4/IPv6 private ranges, and metadata
  endpoints;
- workflow fault tests cover replay, cancellation, retry, worker death, and duplicate delivery;
- logs, traces, workflow histories, error reports, and browser storage pass sensitive-data
  inspection;
- backup restore and tenant deletion are demonstrated in staging;
- an independent penetration test covers authentication, tenancy, signed URLs, uploads,
  provider integration, and job control; and
- residual high risks require an explicit owner, mitigation date, and release approval.

## Incident response hooks

Before GA, operators must be able to revoke sessions, disable an identity/provider integration,
rotate signing and provider keys, suspend an organization, stop a task queue, cancel a workflow,
quarantine an artifact/model, disable external egress, and preserve safe audit evidence. These
hooks are not all implemented. An already issued S3 signed URL cannot be individually revoked;
emergency invalidation requires object/KMS/bucket-policy action until its maximum 15-minute
expiry. Runbooks must state who may use each control and how the action is reviewed.

## Explicit non-goals and residual risk

- CorpusKit cannot guarantee that user or model-generated text is factually correct, safe, or
  free of copyrighted or personal material; review and policy controls remain necessary.
- Prompt injection cannot be eliminated. The architecture limits its authority and blast
  radius.
- Hosted providers receive opted-in data under their own security and retention terms.
- Shared-database RLS materially reduces, but does not equal, physical tenant isolation.
  Dedicated deployments may be required for regulated users.
- Side-channel attacks on shared CPU/GPU infrastructure are not fully eliminated; high-
  assurance deployments require dedicated nodes or environments.
- Denial of service can be bounded and detected, not completely prevented.
- Unauthenticated edge/WAF limiting, billing/cost reconciliation, malware scanning,
  backup/restore and tenant-erasure drills, externally anchored audit retention, and complete
  production observability remain release gates and are not claimed by this slice.
- Object-store readiness/policy probing, advanced production worker-profile fact composition, and
  independently signed image/policy attestation remain release gates. Mandatory combined
  Temporal/PostgreSQL/private-MinIO acceptance verifies the local image identity and replay path;
  server-built canonical serialization by itself is not proof that the declared environment ran.
