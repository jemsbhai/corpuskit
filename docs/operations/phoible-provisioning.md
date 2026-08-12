# PHOIBLE snapshot provisioning

CorpusKit's `DATA` runtime profile uses the exact PHOIBLE snapshot supported by
`corpusgen==0.1.7`. The dataset is not bundled into the application image. Provision it with
an explicit release/init job; API requests and workers never download data implicitly.

## Pinned supply-chain identity

| Field | Pinned value |
|---|---|
| CorpusGen compatibility | `0.1.7` |
| PHOIBLE revision | `b92abff4f4ca2544eece4d9eff5c707f8d508d0c` |
| SHA-256 | `395e0977c3a5402af9cd5effd4ffdf0e47396336241fac534a4706e3cd8a7ecf` |
| Exact size | `24,578,868` bytes |
| Network origin | `https://raw.githubusercontent.com` |

The downloader accepts only the canonical HTTPS URL on port 443, without credentials,
query parameters, or a fragment. Redirects are rejected before they can be followed. It
streams at most the pinned byte count and requires both the exact size and SHA-256 digest.

CorpusGen 0.1.7's `PhoibleDataset.download()` was audited as the compatibility source: it
uses the same immutable revision and digest and atomically replaces its default cache after
verification. Its general-purpose `urllib.request.urlretrieve` path has no operator-bounded
timeout and follows redirects, while checksum enforcement during `load()` applies only to
CorpusGen's default cache. CorpusKit therefore keeps the exact data identity but supplies a
narrower streaming provisioner with timeout limits, pre-follow redirect rejection, exact
length enforcement, sanitized failures, offline installation, and deployment controls. An
automated contract test detects drift between the two pinned identities.

## Operator commands

Provision or verify the default CorpusGen cache:

```bash
uv run corpuskit-phoible provision --json
uv run corpuskit-phoible status --json
```

`provision` is idempotent: a verified snapshot returns `action=already_present` without a
network request. Use `--force` to fetch and reverify the pinned bytes even when the current
cache is healthy. `--timeout-seconds` is constrained to 1–300 seconds and defaults to 30.

For an air-gapped deployment, transfer the exact CSV through the organization's approved
artifact channel, then run:

```bash
uv run corpuskit-phoible provision --source-file /secure-staging/phoible.csv --json
```

The offline source must be a regular, non-symlink file and is subject to the same exact
size and checksum checks. CorpusKit never trusts a filename, HTTP header, or operator claim
in place of content verification.

Exit status `0` means the pinned snapshot is ready. Status `1` means it is missing, invalid,
or provisioning failed; status `2` is an argument error. Machine output and errors omit the
cache and source paths. Stable failure codes distinguish unsafe targets, redirects, size or
checksum failures, unreadable sources, and installation failures without echoing lower-level
exception details.

## Atomicity and least privilege

Provisioning writes a uniquely named temporary file in the destination directory, flushes
and fsyncs it, verifies the complete content, and only then performs an atomic replacement.
Directory fsync is used where supported. A failed, truncated, oversized, or corrupt transfer
cannot replace a previously valid cache, and temporary files are cleaned up best-effort.
Concurrent provisioners may duplicate a download, but they can only install the same pinned
bytes and each replacement remains atomic.

The Compose `provision-phoible` service runs once as UID 10001 with a read-only root
filesystem, no Linux capabilities, `no-new-privileges`, and a dedicated outbound-only
network. Its PHOIBLE volume is the sole writable persistent mount. API and batch-worker
containers depend on successful completion and mount that volume read-only.

```bash
docker compose up --build
docker compose ps --all provision-phoible
docker compose logs provision-phoible
docker compose run --rm --no-deps provision-phoible status --json
```

On a clean volume, the one-shot service downloads and verifies the snapshot before the API
starts. On later starts it verifies the cached snapshot and exits without network access.
Compose requires `phoible` as an API readiness capability; a missing or altered cache
therefore fails readiness closed.

For Kubernetes or another orchestrator, use the same container command as a separately
authorized release job or init container, attach an egress policy allowing only the pinned
origin, write a dedicated persistent volume, and mount that volume read-only into API and
worker pods. Do not grant API pods write access as a workaround for a failed job.

## Recovery and snapshot upgrades

If status reports `missing` or `invalid`, stop DATA-profile workers and rerun `provision`.
If external download is unavailable, use the verified offline path. A healthy existing file
remains usable after a forced refresh fails.

A future PHOIBLE upgrade is a reviewed compatibility change, not a mutable configuration:

1. update CorpusGen and the CorpusKit adapter together;
2. review the immutable upstream revision, URL, exact length, and digest;
3. update constants, this runbook, capability metadata, and the lockfile in one change;
4. run atomic-failure, real-load, inventory-statistics, and clean-volume Compose acceptance;
5. roll out the provision job before read-only consumers.

Never weaken the checksum, allow arbitrary URLs, enable redirects, or download from an HTTP
request handler to recover an environment.
