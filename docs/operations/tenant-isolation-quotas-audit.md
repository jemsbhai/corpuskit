# Tenant isolation, quotas, and audit operations

This runbook covers migrations `0004_tenant_controls` through
`0007_project_deletion`, the PostgreSQL role boundary, atomic tenant quota accounting, the
immutable audit chain, and private global maintenance progress. These controls are defense in depth:
every service query still carries an explicit organization predicate and performs membership
and role authorization.

## Deployment contract

Staging and production must use PostgreSQL through separate credentials for migrations, the
API, dispatcher, worker, adoption, maintenance, and platform administration. The migration
credential owns the schema; no runtime credential may be a superuser, own an application
table, have `BYPASSRLS`, or inherit the migration role. `Settings` rejects SQLite in staging
and production. SQLite remains supported for local demos and fast tests, where application
predicates are the only tenant boundary and PostgreSQL RLS is absent.

Migration `0004_tenant_controls` installs and forces RLS on:

- organizations, users, memberships, projects, corpora, corpus versions, and sentences;
- runs, run events, and transactional outbox messages;
- artifacts; and
- quota policies/usages/reservations and audit heads/events.

Migration `0005_reproducibility` extends the same forced-RLS boundary to execution facts and
replay lineage. Migration `0006_maintenance_cursors` adds a global, forced-RLS continuation
table available only to the maintenance role; it contains opaque storage-scan progress scoped
by a non-secret backend fingerprint and never tenant content.

Migration `0007_project_deletion` adds constrained project lifecycle, retention, and accounting
fields; permits the API to schedule deletion; and grants the maintenance role only the select and
delete operations needed to discover due projects and remove tenant-scoped descendants. Runtime
audit rows remain undeletable.

It also installs a narrowly scoped `SECURITY DEFINER` membership predicate with a fixed
`search_path`, revokes public execution, and grants execution only to CorpusKit policy roles.
The function owner must remain the migration owner. Do not grant `CREATE` on `public` to
runtime roles.

## Database roles

Provision these cluster-level `NOLOGIN`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`,
`NOREPLICATION`, and `NOBYPASSRLS` policy roles before the migration when the managed
database does not grant `CREATEROLE` to the migration principal:

| Policy role | Login/process | Scope |
| --- | --- | --- |
| `corpuskit_api` | API login | verified user and current organization membership |
| `corpuskit_dispatcher` | outbox dispatcher login | global outbox select/update only |
| `corpuskit_worker` | durable parent worker login | one authoritative organization |
| `corpuskit_adoption` | result adoption login | one authoritative organization |
| `corpuskit_maintenance` | cleanup login | bounded global discovery and private cursor state; per-organization mutation |
| `corpuskit_platform` | onboarding/policy login | one operator-selected organization |

Create one `LOGIN ... INHERIT` principal per deployed process, grant it exactly one policy
role, then grant only the tables/actions that process uses. The CI migration job is the
executable grant reference. In particular, dispatcher receives only `SELECT, UPDATE` on
`outbox_messages`; audit events receive no runtime `UPDATE` or `DELETE`; and quota-policy
writes belong only to the platform role. A login must never be a member of multiple policy
roles.

The migration creates missing policy roles on self-managed PostgreSQL and fails closed when
an existing policy role can log in or has elevated database attributes. It deliberately does
not create production login principals or passwords. Cluster policy roles persist across an
Alembic downgrade; remove them only after confirming that no deployed database uses them.

## Transaction context

Each PostgreSQL application transaction must use `Database.session(TenantContext(...))`.
CorpusKit validates the context and sets these values with parameterized, transaction-local
`set_config(..., true)` calls:

- `corpuskit.organization_id`;
- `corpuskit.identity`; and
- `corpuskit.actor_id`.

User organization and subject values come from the verified principal, never a request body,
run summary, staged artifact, or URL parameter. Worker/adoption/platform contexts require an
organization. Dispatcher is global and cannot accept one. Maintenance may discover a bounded
global batch, but reopens each mutation under that row's organization. Session cleanup and
transaction-local GUCs prevent pooled-connection bleed after commit and rollback.

RLS also verifies the login's policy-role membership. Setting a service-looking GUC through
an API connection does not grant service access. PostgreSQL acceptance tests execute as real
non-owner logins and prove every protected table is `FORCE ROW LEVEL SECURITY`, the login owns
no protected table, cross-tenant IDs are invisible, forged service identities fail, and a
reused connection starts with no tenant context.

## Quota policy and admission

Quota policy is server-controlled; request DTOs cannot override it. Default per-organization
limits are:

| Resource | Default |
| --- | ---: |
| concurrent CPU jobs | 3 |
| concurrent expensive LLM/GPU/RL jobs | 1 |
| retained artifact bytes | 10 GiB |
| retained artifact records | 10,000 |
| corpus sentences | 1,000,000 |
| accepted generated sentences per run | 100 |
| generation iterations per run | 500 |
| activity deadline per run | 300 seconds |
| provider input/output tokens per run | 1,000,000 / 100,000 |
| provider cost ceiling per run | USD 10.00 |
| RL steps/tokens per run | 10,000 / 10,000,000 |
| checkpoint or model-adapter size | 100 MiB |

Every `RunKind` has an explicit CPU or expensive classification; adding an enum value without
classification fails at import and migration backfill. Typed generation/model/DATG/RL specs
are validated against policy at submission. Invalid or over-limit work is rejected before a
run or reservation commits. The API returns the stable `quota_exceeded` error as HTTP 429 with
a bounded server-owned `Retry-After`; it does not reveal usage from another organization.

Submission locks the tenant usage row and creates one unique reservation in the same
transaction as the run, initial event, outbox message, and audit event. Idempotent replay does
not reserve twice. Artifact and corpus counters are changed in the transaction that persists
their metadata. A failed transaction changes neither resource nor counter.

The initial reservation lease is the validated run deadline (or the 300-second default) plus
a five-minute termination grace. Transition to running renews it. Terminal success, failure,
or cancellation releases it idempotently. The bounded maintenance reaper locks both run and
reservation; an expired queued/running run is first made terminal with a stable failure or
cancellation event and only then releases capacity. It never leaves an executable active run
without a reservation.

Provider token and cost controls are admission ceilings declared by the typed run spec. They
do not yet provide billing, provider-ledger reconciliation, or organization-wide accumulated
spend accounting. Every authenticated API request is counted in a PostgreSQL-backed fixed
window keyed by the verified organization, a SHA-256 subject digest, HTTP method, and
low-cardinality route template. Read and write ceilings are independent, increments are
atomic across replicas, and exhaustion returns a stable HTTP 429 with `Retry-After`. The
maintenance batch deletes expired windows in bounded pages. Raw subjects, paths, tokens, and
request bodies are never stored. A rate-limit persistence failure rejects the authenticated
request with a stable, non-reflective HTTP 503 before its handler runs. Edge/WAF controls remain required for unauthenticated
connection floods, and fair-share scheduling remains a separate release gate.

## Audit evidence

Project creation, project deletion request/final purge, corpus creation, run
submit/cancel/retry/terminal transitions, artifact
creation/tombstone/purge/adoption, expired reservations, and privileged quota-policy changes
append an audit event in the same database transaction as the mutation. Metadata is
action-specific, allowlisted, canonical JSON capped at 2 KiB. It excludes corpus text,
prompts, run specifications, credentials, headers, signed URLs, and storage paths.

Each organization has a monotonically sequenced SHA-256 chain. PostgreSQL constraints enforce
sequence/hash shape and uniqueness, and a trigger rejects every `audit_events` update/delete,
including from a runtime role that later receives an accidental table grant. The head row is
locked while appending, so concurrent writes serialize. The chain detects database-history
tampering but is not externally anchored or WORM storage.

Only current owners/admins can read `/api/v1/platform/quota` and the cursor-paginated
`/api/v1/platform/audit-events`. Audit filters include bounded page size, UTC-aware time range,
action, and resource type. Editor/viewer/cross-tenant access is returned as the same
non-enumerating not-found response.

## Verification and incident procedure

Before promotion:

1. Run `corpuskit-db upgrade` and `corpuskit-db check` with the schema owner.
2. Connect as every runtime login and verify `rolsuper = false`, `rolbypassrls = false`, and
   that no protected table is owned by the login.
3. Run `tests/integration/test_postgres_tenant_controls.py` and
   `tests/integration/test_postgres_maintenance_state.py` with seven distinct URLs.
4. Confirm all protected tables report both `relrowsecurity` and `relforcerowsecurity`.
5. Exercise one accepted and one rejected concurrent submission in two organizations.
6. Verify an audit chain and attempt an audit update/delete as the API login.
7. Confirm production configuration rejects SQLite.
8. Concurrently request one project deletion as the real API login, prove a foreign tenant sees
   not-found, and confirm only the maintenance login can physically purge it after retention.

If tenant leakage is suspected, stop affected API/worker credentials, preserve PostgreSQL
logs and audit rows read-only, rotate the login, and compare the organization chain from the
genesis hash. Do not repair an audit chain in place. If counters diverge, stop new admissions,
reconcile usage from authoritative active reservations/artifacts/sentences under the platform
role, append an operator-reviewed correction migration, and restore admission only after the
cross-tenant and concurrency suite passes.

## Explicit remaining gates

The Compose demo intentionally uses its schema-owner database login and is not production RLS
evidence. Production role provisioning is infrastructure-owned. Billing/cost ledgers,
malware scanning, backup/restore and tenant-erasure drills, externally
anchored audit/WORM retention, and complete observability/alerting remain subsequent gates.
Readiness validates production database type through configuration but does not yet probe RLS
policy/role drift at runtime.
