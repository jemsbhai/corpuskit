# Project deletion and retention

CorpusKit implements project deletion as a two-phase, retention-safe lifecycle. The public API
only schedules deletion; it never hard-deletes project rows or object bytes in the request. The
dedicated maintenance process performs eventual physical removal after all preconditions hold.

## User contract

Only an organization owner or admin may request deletion. The `/projects` workbench obtains the
current server-verified organization role and presents the control only to those roles; the API
independently enforces the same policy. The user must type the exact, case-sensitive phrase
`DELETE <persisted project name>`. There is no browser-only confirmation or client-supplied
retention override.

The API contract is:

```http
DELETE /api/v1/projects/{project_id}
Content-Type: application/json

{"confirmation":"DELETE Example project"}
```

A successful request returns `202 Accepted` with `deletion_pending`, `requested_at`, and
`retention_until`. A retry with the same exact confirmation returns the persisted lifecycle
snapshot and does not append another audit event. Foreign-tenant, absent, and unauthorized
identifiers use the non-enumerating not-found contract. A nonterminal run or an active quota
reservation returns a conflict; deletion never silently cancels user work.

Deletion requests cannot currently be withdrawn. Operators must not promise recovery after the
request. The retention interval protects cleanup correctness and compliance workflows; it is not
a user-visible recycle bin.

## Transactional logical deletion

The request transaction locks the project, then:

1. confirms there is no draft, queued, provisioning, running, or cancelling run and no active
   reservation;
2. records the exact number of corpus sentences used for final quota reconciliation;
3. locks every active or tombstoned project artifact, tombstones it, and extends its retention to
   the project deadline when necessary;
4. changes the project from `active` to `deletion_pending`; and
5. appends one allowlisted `project.deletion_requested` audit event containing only artifact and
   sentence counts plus the UTC retention deadline.

PostgreSQL additionally uses one transaction-scoped advisory lock derived from the project UUID
across deletion, replay/fact/finalization writes, and physical purge. This preserves a single
lifecycle order without granting worker or maintenance identities `UPDATE` on projects. Every
participant re-reads authoritative active/due state after taking that lock.

After commit, every project-scoped workspace, job, artifact, manifest, and replay lookup requires
an active project. The pending project disappears from lists, and direct identifiers fail closed.
New corpus, run, artifact, execution-fact, finalization, and replay writes also recheck or lock the
active project so deletion wins deterministically.

Artifact and corpus quota remains charged during retention. Releasing it early would permit a
tenant to exceed retained-storage or sentence limits while the bytes and rows still exist.

## Retention and physical purge

`CORPUSKIT_ARTIFACT_RETENTION_DAYS` also sets project deletion retention and is constrained to 30
through 3,650 days. Lower values fail configuration/service validation. At or after the deadline,
`corpuskit-maintenance run-once` performs artifact retention purge and final-object reconciliation
before project purge.

For each due project, the maintenance identity reopens a tenant-scoped transaction and verifies:

- the project remains deletion-pending and due;
- no nondeleted artifact, nonterminal run, or active reservation exists;
- no object remains under the exact organization/project artifact prefix; and
- the authoritative sentence count still matches the count captured at deletion request.

Only then does one transaction delete artifact metadata, replay/execution facts, terminal run
history, corpora and sentences; release the exact corpus-sentence quota; append
`project.purged`; and remove the project row. Artifact purge has already released artifact count
and byte quota after successfully deleting each object. Audit rows and their organization chain
are deliberately preserved after project metadata is removed.

Object deletion or listing failures, database inconsistency, new conflicting state, or quota-count
mismatch defer or fail that candidate. Metadata is never removed while an object is known to
remain. Retrying maintenance is safe and idempotent.

## In-flight artifact race

An artifact upload writes its content-addressed object outside the database transaction, then
locks and reauthorizes the active project before inserting metadata. Deletion may commit during
that object-store write. In that case the second authorization rejects metadata creation, leaving
an unreferenced object rather than an accessible artifact. The minimum 30-day project retention
allows the write to settle; final-object reconciliation removes the orphan after its grace period.
The project prefix check independently defers physical project purge until reconciliation has
actually removed it. Keep maintenance ordering and both checks intact.

## Operations and evidence

Monitor the count-only `project_purge` section in `corpuskit.maintenance-report.v1`:

- `eligible`: due candidates examined;
- `deleted`: fully purged projects;
- `deferred`: candidates with safe retryable preconditions still outstanding; and
- `failed`: storage, database, or accounting failures needing investigation.

Repeated `deferred` counts require checking artifact purge/reconciliation health and active-state
invariants. Any `failed` count produces degraded maintenance status. Do not delete rows, rewrite
quota counters, or shorten retention manually. Preserve database/object-version evidence, repair
the underlying object or accounting fault through a reviewed incident procedure, verify the audit
chain, and rerun the same bounded command.

Automated evidence lives in:

- `tests/integration/test_project_deletion.py` for SQLite lifecycle, authorization, retention,
  audit/quota exactness, object-store failure, and deterministic upload/deletion races;
- `tests/integration/test_postgres_tenant_controls.py` for opt-in real PostgreSQL RLS,
  concurrent idempotency, cross-tenant non-enumeration, and maintenance-only purge;
- `tests/integration/test_reproducibility.py` for pending-project manifest/replay fail-closed
  behavior; and
- `apps/web/src/components/project-workbench.test.tsx` for role-aware, accessible exact
  confirmation.

Production backup expiry/erasure drills, legal holds, external provider copies, externally
anchored/WORM audit retention, deployment scheduling, and full-stack browser acceptance remain
separate release gates. Database deletion does not claim immediate removal from immutable backups;
the continuity policy must define and evidence their expiry.
