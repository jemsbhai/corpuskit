# Database migration runbook

CorpusKit uses a linear Alembic history for PostgreSQL. Application processes never
create production tables with SQLAlchemy `create_all`; a separate deployment job applies
the migration before new application and worker versions receive traffic.

## Safety contract

- The migration process requires `CORPUSKIT_DATABASE_URL`; it has no implicit database
  fallback.
- Supported URLs use `postgresql+asyncpg` in deployed environments and
  `sqlite+aiosqlite` only for tests or isolated local demos.
- `corpuskit-db upgrade` always targets `head`. It does not accept an arbitrary revision.
- The operator CLI deliberately provides no downgrade command.
- PostgreSQL migration and drift-check processes take the same advisory lock and fail
  rather than running concurrently.
- Online revisions are grouped into one command transaction on transactional-DDL backends.
  Compatibility preflights that must also protect SQLite run before any revision is applied.
- SQL echo is disabled and bound values are hidden. CLI failures report an exception
  class, not a connection URL or driver message. Never enable SQLAlchemy echo in a
  migration job.
- Each revision is immutable after release. Correct it with a new revision.
- Migration `0004_tenant_controls` requires the six `corpuskit_*` non-login policy roles.
  Pre-provision them with the attributes in the tenant-control runbook on managed databases,
  or grant the migration owner `CREATEROLE` for the migration transaction. Runtime logins must
  be separate non-owner, non-superuser, non-`BYPASSRLS` principals.
- Migration `0005_reproducibility` adds forced-RLS execution-fact/replay lineage tables and
  one-way PostgreSQL immutability triggers. Worker may insert facts, adoption may bind final
  manifests/comparisons, and API may insert replay lineage only through its tenant transaction;
  do not broaden those grants to make integration convenient.

## Deployment procedure

1. Verify the exact candidate image digest, migration head, and expected current
   production revision.
2. Confirm a successful PostgreSQL backup or recovery point within the 15-minute RPO.
3. Confirm the previous application image and rollback procedure remain available.
4. Inject the schema-owner `CORPUSKIT_DATABASE_URL` from the deployment secret manager. Do not pass a
   credential-bearing URL as a command-line argument or paste it into a shell history.
5. Check the current revision:

   ```console
   corpuskit-db current
   ```

6. Put the application into the compatibility state documented by the release. Follow
   expand/migrate/contract sequencing for a zero-downtime change.
7. Apply the one allowed forward operation:

   ```console
   corpuskit-db upgrade
   ```

8. Prove the upgraded schema matches SQLAlchemy metadata:

   ```console
   corpuskit-db check
   ```

9. Run the release database smoke tests, then admit canary traffic and monitor database
   errors, lock waits, latency, and job failures.
10. Record the before/after revisions, candidate digest, backup identifier, CI evidence,
    start/end time, and operator in the release record. Never record the URL.

`check` exits nonzero when the database is behind, the migration has drifted from model
metadata, or the connection fails. Treat every nonzero result as release-blocking.

## Local and CI validation

The repository-level `alembic.ini` intentionally contains no URL. With an injected local
URL, maintainers may run the underlying Alembic commands for revision development:

```console
uv run alembic current
uv run alembic check
uv run alembic upgrade head
```

CI starts an empty PostgreSQL database, provisions separate policy/login roles, upgrades it to
`head`, and immediately performs the empty-database downgrade/reapply round trip before granting
runtime access or running any data-bearing application tests. It then runs the autogenerate drift
check, exercises real non-owner RLS/service-role/quota/audit contracts, and proves a populated
artifact-identity collision refuses rollback without changing the PostgreSQL revision, catalog,
policies, or data. SQLite tests provide a fast empty-database round trip, populated
downgrade-safety checks, and application-predicate semantics but do not replace PostgreSQL CI.

When adding a model field or constraint:

1. Generate a candidate revision against a disposable, fully upgraded database.
2. Review the operations manually, including names, locks, table rewrites, indexes,
   defaults, backfill cost, reversibility, and tenant isolation.
3. Add upgrade, downgrade, drift, and representative data-preservation tests.
4. Test against a production-sized staging copy when the operation can rewrite or lock a
   table.
5. Do not merge until `corpuskit-db check` reports no new operations after the upgrade.

## Rollback and recovery

Prefer rolling the application back while leaving a backward-compatible expanded schema
in place. Schema downgrade is an exceptional, independently approved recovery action:

- stop all application and worker writes;
- capture another backup or recovery point;
- verify the target revision is explicitly documented as data-preserving;
- run the reviewed underlying `alembic downgrade <revision>` command from the matching
  release image;
- validate tenant row counts, constraints, artifact references, and application smoke
  tests before restoring traffic.

If a revision is destructive or downgrade cannot preserve data, restore PostgreSQL from
the verified recovery point instead. Follow the RPO/RTO and incident process in
[`slo.md`](slo.md).

Revision `0003_artifact_integrity` permits the same `(organization_id, sha256, kind)` identity in
different project scopes, while revision `0002_durable_job_outbox` does not. Before any downgrade
command whose path crosses from `0003` to `0002`, the migration environment checks for
legacy-identity collisions before applying any revision; revision `0003` repeats the check before
its own DDL. If collisions exist, the command refuses the entire downgrade without deleting,
merging, or identifying any rows and reports that the data cannot be represented by the legacy
constraint. Keep the database at revision `0003` or later and perform an independently reviewed,
data-preserving remediation, or restore the verified recovery point; do not delete artifacts
merely to force a schema rollback.

## Failure handling

- **Another migration holds the lock:** verify the active deployment job. Do not kill it
  until its transaction and database activity are understood.
- **Database is not at the expected revision:** stop promotion and reconcile the release
  history; do not stamp the database merely to silence the check.
- **Drift is detected:** generate and review a new migration. Never edit an already
  released revision.
- **Connection or authentication failure:** inspect secret injection and network policy.
  The CLI intentionally redacts driver details, so use restricted platform diagnostics
  that also preserve credential redaction.
- **Partially applied migration:** PostgreSQL DDL is run transactionally where supported.
  Keep traffic stopped, inspect the revision table and database transaction state, and
  choose forward repair or restore with the database owner.

See [tenant isolation, quotas, and audit operations](tenant-isolation-quotas-audit.md) for the
role/grant matrix, transaction context, and post-migration RLS verification.
