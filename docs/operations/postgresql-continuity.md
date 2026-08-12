# PostgreSQL backup and restore-drill runbook

CorpusKit's continuity command creates private PostgreSQL custom-format archives, proves them
offline, and can restore a proven archive only into an empty, explicitly named drill database.
It is a logical-backup control for release and recovery evidence. It does not replace managed
PostgreSQL point-in-time recovery, multi-zone replication, WAL archiving, or object-store backup.

## Safety boundary

- Connection material is accepted only through libpq `PG*` environment variables. The CLI has no
  database URL, user, host, or password argument and never writes connection values to output.
- Production should inject `PGPASSWORD` or, preferably, a short-lived `PGPASSFILE` from the secret
  manager. Do not place either value in shell history, a unit file, a CronJob manifest, or CI logs.
- The executable invokes absolute `pg_dump`, `pg_restore`, and `psql` paths without a shell. Every
  child process has a deadline and receives only an allowlist of system and libpq variables.
- PostgreSQL clients must report a parseable supported major (16, 17, or 18); all three local tools
  must have the same major. Verification and restore additionally require the archive's `pg_dump`
  major to equal the local `pg_restore` major. The release images should pin PostgreSQL 17 clients
  to the deployment's PostgreSQL 17 line.
- Backup and verification accept only an absolute, existing, non-symlink directory. On POSIX it
  must be owned by the process and not writable by group or other users. Bundle identifiers, file
  names, file types, sizes, and directory membership are strict.
- A backup is built in a mode-0700 partial directory. Its archive, canonical manifest, and detached
  manifest digest are flushed before the entire directory is renamed into view on the same
  filesystem. Failed partial bundles are removed and a published bundle is never overwritten.
- The digest detects accidental or unauthorized changes only when the manifest or its enclosing
  object is trusted. It is not a signature. Copy the complete bundle to encrypted, authenticated,
  immutable storage and retain the storage-provider checksum, version, retention lock, and audit
  record.
- Restore requires the exact phrase `RESTORE <bundle> INTO EMPTY <PGDATABASE>`. The target name
  must match `corpuskit_restore_drill_*`, and a live preflight must find zero user relations. The
  restore command deliberately omits `--clean` and `--create`, uses one transaction, strips owner
  and privilege commands, and stops on the first error.
- A drill-name prefix is a last-line software guard, not proof of infrastructure isolation. Network
  policy, a separate non-production cluster/account, ephemeral credentials, and an empty database
  created for this drill are mandatory. Never rename or alias a production database to bypass the
  guard.
- The CLI deliberately has no command to drop a database and no general-purpose production restore
  command. Destructive cleanup and actual disaster recovery remain separately approved platform
  actions.

## Backup bundle

Each successful invocation publishes exactly one directory:

```text
ckpg_20260811T221530123456Z_<archive-digest-prefix>_<nonce>/
  database.dump
  manifest.json
  manifest.sha256
```

`manifest.json` is canonical UTF-8 JSON and records only the bundle identifier, UTC creation time,
archive format, SHA-256, byte size, table-of-contents entry count, client versions, and the fact that
owner and privilege commands were excluded. It never records a host, port, database, user,
password, URL, tenant, or corpus content. `manifest.sha256` protects the exact canonical manifest
bytes. The archive itself contains database data and must be classified and encrypted accordingly.

## Scheduled backup procedure

1. Confirm PostgreSQL health, the current Alembic revision, free local staging space, immutable
   destination availability, and the corresponding object-store recovery point. A database-only
   restore can leave artifact metadata inconsistent with object data.
2. Use a dedicated backup role with only the privileges required by `pg_dump`. Configure TLS and
   certificate validation through libpq. Inject its short-lived secret into the process environment;
   do not paste it into a command shown below.
3. Create a local same-filesystem staging root once and protect it:

   ```console
   install -d -m 0700 /var/lib/corpuskit/postgres-backups
   ```

4. Configure the scheduler with the absolute pinned PostgreSQL client directory, then pass that
   directory with `--pg-bin-dir` to the installed entry point. If running from a checkout, use
   `uv run python -m corpuskit.operations.continuity_cli` in place of `corpuskit-continuity`.

   ```console
   corpuskit-continuity backup \
     --root /var/lib/corpuskit/postgres-backups \
     --pg-bin-dir /usr/lib/postgresql/17/bin \
     --timeout-seconds 1800
   ```

5. Capture the one-line JSON result in the restricted backup ledger. It contains a bundle ID,
   archive and manifest digests, size, and creation time—never connection details.
6. Upload the whole published directory as one immutable storage object or transactional set.
   Verify the remote object checksum and retention lock, then remove local staging only under the
   reviewed retention policy. Never upload a `.partial` directory.
7. Alert if no successful backup and immutable copy complete within the 15-minute RPO in
   [`slo.md`](slo.md). Logical dump duration alone does not satisfy the RPO.

The command uses custom format, `--no-owner`, `--no-privileges`, `--no-password`, and a consistent
`pg_dump` snapshot. It never captures subprocess stderr because PostgreSQL diagnostics can include
connection and schema details. Investigate failures using restricted platform telemetry, not by
turning on command tracing or printing the environment.

## Offline verification

Run verification from a separate recovery worker after download. No `PG*` variables or live
database are needed. The verifier checks the exact three-member bundle, canonical manifest,
detached manifest digest, archive size and SHA-256, client-major compatibility, and a non-empty
`pg_restore --list` table of contents. It does not execute archive SQL.

```console
env -u PGHOST -u PGPORT -u PGDATABASE -u PGUSER -u PGPASSWORD \
  corpuskit-continuity verify \
  --root /var/lib/corpuskit/postgres-backups \
  --bundle ckpg_20260811T221530123456Z_0123456789ab_abcdef012345 \
  --pg-bin-dir /usr/lib/postgresql/17/bin \
  --timeout-seconds 300
```

Retain the verification JSON beside the storage version and scheduler run ID. A successful offline
verification proves readability and internal integrity, not restorability, referential consistency
with object storage, extension availability, role availability, or application correctness. Those
are covered by the restore drill.

## Isolated restore drill

Run at least quarterly, before a production release that changes persistence, and after backup,
PostgreSQL, extension, or encryption-key changes.

1. Provision a separate ephemeral PostgreSQL cluster or an equivalently isolated non-production
   instance with the same PostgreSQL major, extensions, locale, encoding, and encryption posture.
   Block application and worker identities from reaching it.
2. Pre-provision the six CorpusKit policy roles described in
   [`tenant-isolation-quotas-audit.md`](tenant-isolation-quotas-audit.md). RLS policies in the dump
   refer to these roles even though ownership and grants are excluded.
3. Create a new empty database with a lowercase name such as
   `corpuskit_restore_drill_2026q3a`. Confirm independently that it has no user relations. Never
   reuse a prior drill database.
4. Download an immutable bundle and run the offline verification above. Compare its digest to the
   independently retained backup ledger and storage-provider checksum.
5. Inject a target-only libpq credential. It must not have access to production. Construct the exact
   confirmation phrase from the already verified bundle ID and the exact `PGDATABASE` value.
6. Run:

   ```console
   corpuskit-continuity restore-drill \
     --root /var/lib/corpuskit/postgres-backups \
     --bundle ckpg_20260811T221530123456Z_0123456789ab_abcdef012345 \
     --pg-bin-dir /usr/lib/postgresql/17/bin \
     --timeout-seconds 1800 \
     --confirm "RESTORE ckpg_20260811T221530123456Z_0123456789ab_abcdef012345 INTO EMPTY corpuskit_restore_drill_2026q3a"
   ```

7. The command repeats offline verification, proves the target is empty, restores in one
   transaction, confirms at least one user relation, and reads a strictly formed Alembic revision.
   Retain its credential-free JSON report.
8. Run application smoke tests with read-only drill credentials: tenant-scoped row counts, run/event
   and artifact-reference counts, audit-chain validation, representative corpus reads, and a sample
   manifest replay. Reconcile every referenced object against the matching object-store recovery
   point. The continuity command does not claim these application checks.
9. Record elapsed time against the 60-minute RTO, backup/storage versions, PostgreSQL and CorpusKit
   versions, row-count evidence, reconciliation result, operator/reviewer, and every exception.
10. Revoke target credentials and remove the exact ephemeral database through the platform's
    independently approved cleanup workflow. Re-check its generated name before deletion.

## CI acceptance

The real acceptance test is intentionally opt-in and guarded. It accepts only a loopback owner URL
whose database is named `corpuskit_migrations` or `corpuskit_continuity_ci`; it creates a unique
`corpuskit_restore_drill_<24 hex>` database, inserts a unique marker into the CI source, performs the
real `pg_dump`/offline verification/`pg_restore` round trip, proves the marker and Alembic revision,
and drops only the generated target in `finally`.

CI must install matching PostgreSQL 17 client tools and set:

```text
CORPUSKIT_RUN_POSTGRES_CONTINUITY_ACCEPTANCE=1
CORPUSKIT_TEST_POSTGRES_OWNER_URL=<secret-injected loopback CI URL>
```

Run only the isolated gate with:

```console
uv run pytest tests/integration/test_postgres_continuity_acceptance.py -q --no-cov
```

The URL is an environment secret and must be masked. Never pass it to pytest, the continuity CLI,
or a diagnostic command as an argument.

## Failure handling

- **`tool_unavailable` / `tool_version`:** stop. Install the pinned client set and confirm all three
  tools resolve from the reviewed absolute directory. Do not substitute an older `pg_restore`.
- **`process_timeout`:** treat the bundle as unpublished or the drill as failed. Investigate locks,
  I/O, network, and sizing with restricted database telemetry. Do not increase the four-hour hard
  ceiling without a reviewed code change.
- **`process_failed`:** the public error is intentionally redacted. Inspect server-side logs under
  the incident process. Never retry with `set -x`, environment dumps, or verbose client output.
- **`backup_integrity`:** quarantine the bundle and retain it for incident analysis. Do not edit its
  manifest or recalculate digests to make verification pass; create a new backup.
- **`restore_target_not_empty` / `unsafe_restore_target`:** stop and provision a new isolated empty
  drill target. Do not add `--clean`, rename production, or weaken the prefix check.
- **post-restore validation failure:** keep the isolated target for restricted diagnosis, mark the
  drill failed, and do not infer recoverability from `pg_restore` exit status alone.

For an actual production recovery, follow the managed database recovery procedure and incident
approval chain. Restore PostgreSQL and the versioned object store to coordinated recovery points,
validate tenant isolation and audit integrity before admitting any traffic, and preserve all
recovery evidence.
