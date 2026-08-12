# ADR-0007: Enforce tenant context, atomic quotas, and chained audit evidence

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

Application-level organization predicates are necessary but one omitted predicate can expose
a shared-database tenant. Concurrent submissions and uploads also need a single serialization
point or they can all observe capacity and exceed a limit. Security-relevant mutations must
leave durable evidence without a second write that can be lost after the business transaction
commits.

## Decision

Use PostgreSQL `FORCE ROW LEVEL SECURITY` as defense in depth on every organization-owned
table and the user/membership tables used to establish access. Every runtime transaction sets
validated organization, identity, and actor GUCs with transaction-local `set_config`. Policies
require both those values and membership in a distinct non-login database policy role. Keep
explicit service predicates and authorization checks.

Give dispatcher the only global update policy, limited to the transactional outbox. Global
maintenance queries discover bounded candidates, then mutate each record in an
organization-scoped transaction. Worker, adoption, and platform contexts always require an
authoritative organization. Migrations run under a distinct schema owner; runtime logins
neither own tables nor bypass RLS.

Store one server-owned quota policy and locked usage row per organization. Job submission
atomically increments CPU/expensive usage and creates a unique run reservation. Terminal
transitions release it idempotently. Expiry terminalizes abandoned work before releasing its
capacity. Artifact and corpus counters change in their metadata transaction. Typed run specs
are bounded by generation, deadline, provider, and RL ceilings, and every run kind is
classified without a fallback.

Append a small allowlisted audit event in the business transaction. Serialize each tenant's
events through a locked head and link canonical event hashes. PostgreSQL rejects audit-event
updates/deletes. Expose deterministic cursor reads only to current owner/admin members.

## Consequences

### Positive

- A missed application predicate does not automatically become cross-tenant access.
- Login role and transaction context must both agree, so forged service GUCs fail closed.
- Concurrent quota admission has one row-lock serialization point and idempotent reservations.
- Mutation and audit evidence commit or roll back together.
- Hash/sequence verification exposes deletion, reordering, and modification of audit history.

### Negative

- PostgreSQL policy and role changes become security-critical migration work.
- Runtime processes need separate database credentials and narrowly maintained grants.
- Tenant onboarding must create policy/usage/head rows through the platform identity.
- Shared-table RLS is not physical isolation and does not prevent every timing side channel.
- The hash chain is not independently anchored; a schema owner can rewrite data and chain.

## Rejected alternatives

- **Application predicates only:** a single repository regression can expose another tenant.
- **One privileged runtime login:** table ownership or `BYPASSRLS` silently defeats policies.
- **Count active rows without reservations:** concurrent admission can exceed limits, and
  retries are difficult to account idempotently.
- **Fixed reservation expiry:** a permitted long run could lose capacity while still active.
- **Audit in a later transaction or message consumer:** crashes create mutations with no
  evidence.
- **One global audit chain:** unrelated tenants contend and pagination leaks global activity.

## Verification

- Clean PostgreSQL migration/drift/downgrade/reapply CI.
- Non-owner/non-superuser tests cover every protected table, direct SQL forgery, pooled
  connection reuse, isolated service roles, dispatcher update, and immutable audit rows.
- Concurrent PostgreSQL tests prove the N+1 job is rejected while another tenant remains
  independent.
- SQLite semantic tests cover rollback, idempotent release, long deadlines, stale-run
  terminalization, quota counters, authorization, and audit-chain tamper detection.

See the
[tenant isolation, quotas, and audit runbook](../operations/tenant-isolation-quotas-audit.md).
