# ADR-0005: Use PostgreSQL plus object storage with tenant keys and RLS

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

CorpusKit needs transactional metadata, memberships, immutable lineage, queryable sentence
indexes, large uploads and exports, detailed result files, and model adapters. A single
storage technology is not well suited to both relational authorization and large immutable
artifacts. The application must also isolate organizations without making early operations
depend on a database per customer.

## Decision

Use PostgreSQL for organizations, memberships, projects, corpus/version metadata, bounded
sentence indexes, inventory snapshots, run specifications, jobs/events, result summaries,
artifact metadata, model/adapter metadata, secret references, and audit events. Use
S3-compatible object storage for source uploads, normalized snapshots, detailed JSONL or
Parquet results, exports, and model adapters.

Every tenant-owned relational row has a non-null `organization_id`. Repository methods
require an organization scope, and PostgreSQL row-level security provides defense in depth.
Workers set an authenticated organization context on every transaction. Cross-organization
foreign keys are prevented through composite constraints or validated repository operations.

Every object key uses an opaque identifier rather than a user filename. Metadata records its
organization, SHA-256, media type, byte size, producer, retention state, and lineage. Signed
downloads are short-lived and issued only after authorization. Object-store buckets are
private, encrypted, versioned where required, and protected from public ACLs.

Physical deduplication by hash may share bytes only below the authorization layer. Artifact
ownership and deletion records remain per organization. A documented v1 import limit bounds
the relational sentence index; larger detailed outputs stay in columnar artifacts.

## Consequences

### Positive

- Transactions protect identity, lineage, job, and audit state.
- Object storage scales large immutable artifacts economically.
- Shared infrastructure supports early multi-tenancy with defense-in-depth isolation.
- Content hashes enable integrity checks, idempotency, and reproducible exports.

### Negative

- Cross-store lifecycle and deletion need reconciliation.
- RLS policies and worker transaction context are security-critical.
- Large interactive sentence queries need explicit limits or specialized indexing later.
- Shared infrastructure cannot satisfy every future data-residency requirement.

## Rejected alternatives

- **Store all files in PostgreSQL:** backup size, I/O behavior, and large-object operations are
  poor for datasets and model artifacts.
- **Store everything in object storage:** weak transactional relationships, authorization,
  filtering, and job projections.
- **Database per organization:** strong isolation but excessive provisioning, migrations,
  pooling, and cost for the initial product.
- **Tenant prefixes without database RLS:** one application bug could expose another tenant.

## Verification

- Automated RLS tests exercise every tenant-owned table with hostile cross-tenant IDs.
- Integration tests confirm signed URL scope, expiry, private ACLs, and hash verification.
- Deletion tests cover database rows, object tombstones, lineage, audit, retries, and legal
  retention exceptions.
- Backup/restore drills include both PostgreSQL and object metadata consistency.
- Storage reconciliation reports orphaned, missing, size-mismatched, or hash-invalid objects.

## Current implementation note

The v0.1 foundation implements tenant-scoped metadata, content-addressed local/S3 adapters,
integrity-verified authenticated downloads, short-lived presigning, tombstone/purge services,
and grace-period key-targeted orphan reconciliation. Public creation is limited to untrusted
corpus text. Migration `0004_tenant_controls` now forces PostgreSQL RLS under distinct
non-owner API/service roles, while application predicates remain mandatory. SQLite does not
provide that defense and is rejected outside local demo/test use. Scheduled cleanup,
production role provisioning, authoritative manifests, object-store readiness/policy probes,
and backup/restore drills remain release gates; the verification bullets above are not a claim
that every operational control is already deployed.
