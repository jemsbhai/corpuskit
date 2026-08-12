# Maintenance scheduler

CorpusKit exposes one bounded, idempotent maintenance batch through
`corpuskit-maintenance run-once`. It is designed for a Kubernetes CronJob, systemd timer, or
equivalent singleton scheduler. It does not run inside the public API process.

Each batch uses one UTC cutoff and performs, in order:

1. expire stale quota reservations, terminalizing their non-terminal runs before releasing
   capacity and appending the maintenance audit events;
2. purge artifact bytes whose tombstone retention period has elapsed, then conditionally mark
   their metadata deleted and release storage quota;
3. audit final artifact objects for missing/corrupt references and delete sufficiently old
   unreferenced objects;
4. page through old unowned staged-result objects and delete them after the configured orphan
   grace period;
5. purge due deletion-pending projects only after their artifact metadata and exact object prefix
   are empty, then release corpus quota while preserving audit evidence; and
6. delete one bounded page of expired authenticated API rate-limit windows.

The command accepts only fixed operational bounds:

```bash
export CORPUSKIT_RUNTIME_ROLE=maintenance
corpuskit-maintenance run-once \
  --limit 500 \
  --max-reconciliation-pages 10 \
  --max-staging-pages 10
```

`--limit` must be 1-1,000 and both page budgets must be 1-20. The scheduler persists opaque
continuation state in `maintenance_cursors`, keyed by operation and a SHA-256 fingerprint of the
configured object-store namespace. It advances that state only after a successful page and
resets it after reaching the end, so later objects cannot starve across bounded invocations.
The table is forced-RLS and accessible only to the maintenance database role. Cursors never
appear in process arguments, output, metrics, or logs; malformed or non-advancing stored/service
cursors fail closed.

For an isolated local stack, run the same one-shot image with:

```bash
docker compose --profile maintenance run --rm maintenance
```

The base Compose stack uses its schema-owner demo credential and therefore is not RLS evidence.
Production must replace it with the dedicated maintenance login described below.

## Identity and concurrency

Use a dedicated PostgreSQL login that inherits only the `corpuskit_maintenance` NOLOGIN policy
role described in
[`tenant-isolation-quotas-audit.md`](tenant-isolation-quotas-audit.md). Use separate object-store
credentials restricted to the CorpusKit bucket/prefix and only the exact list, metadata, and
delete operations required by the artifact runbook. Do not share API, migration-owner,
dispatcher, worker, or platform-admin credentials with this process.

On PostgreSQL, the command holds a fixed session advisory lock for the batch. A competing
invocation emits `{"status":"already_running"}` and exits successfully without work. The lock
acquisition transaction is committed immediately, so the long object-store phase does not hold
an idle database transaction or snapshot. The lock is explicitly released and is also released
by PostgreSQL if the process/connection dies. SQLite has no cross-process singleton lock and is
supported only for isolated local development.

Configure a scheduler with `concurrencyPolicy: Forbid`, a finite active deadline, a read-only
root filesystem, no privilege escalation, dropped Linux capabilities, a non-root UID, the
maintenance database identity, and the same private object-store network policy used by trusted
artifact services. Start with a five-minute cadence; adjust only from measured stale-lease and
object-growth evidence.

## Output and alerts

Successful work emits one compact JSON `corpuskit.maintenance-report.v1` document containing
aggregate counts, page counts, `more_available` flags, and timestamps only. It never emits
tenant IDs, run IDs, continuation values, object keys, database URLs, or filesystem paths. Exit
status is:

- `0`: healthy batch or another singleton already owns the lock;
- `1`: configuration, database, object-store, or internal failure; details are redacted; and
- `2`: the batch completed but detected deletion failures, missing/corrupt referenced objects,
  staging cleanup failures, or project purge failures.

Page on repeated status `1`, any missing/corrupt referenced object, or stale reservations that
continue to grow. Create a ticket for isolated retryable deletion failures. Preserve the JSON
report as bounded operational evidence and correlate by scheduler execution ID rather than
adding tenant/object identifiers to metrics.

`rate_limit_windows_deleted` is the aggregate cleanup count. It contains no organization,
subject, route, or window identifier.

## Failure and recovery

All mutations are individually idempotent and guarded by current state. A crash may leave a
subset completed; rerun the command. Object deletion precedes the conditional database transition
for retained artifacts, so a database failure after delete is visible as a retryable missing
object and never resurrects access. Staged/final orphan deletion observes the configured grace
window to avoid racing adoption or idempotent writers. Project purge runs after reconciliation
and independently refuses to remove metadata while any object remains under the project's exact
prefix. This protects the artifact upload race described in the
[project deletion runbook](project-deletion.md).

If referenced objects are missing or corrupt, stop destructive cleanup, preserve database and
object-versioning evidence, restore the exact content-addressed bytes from backup, verify their
SHA-256 and metadata, then rerun reconciliation. Never repair metadata by hand without a reviewed,
audited incident procedure.

## Current limitations

The command is implemented and fully branch-tested, but its container scheduler deployment and
alert rules still need environment-specific release evidence. Malware scanning, legal holds,
externally anchored audit evidence, and backup restore are separate controls.
