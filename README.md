# CorpusKit

CorpusKit is a production-oriented, multi-user corpus design workbench powered by
[`corpusgen`](https://pypi.org/project/corpusgen/). It turns CorpusGen's linguistic
evaluation, optimization, generation, guidance, and training APIs into traceable,
reproducible user workflows.

> **Current status:** the repository is under active implementation. It is not yet a
> production release. A capability is only considered available after its requirement,
> automated acceptance test, operational runbook, and user documentation are complete.
> The repository contains a hardened release-candidate pipeline, but no registry publication or
> production promotion is claimed until its external acceptance gates are recorded.

## Product scope

CorpusKit is designed for TTS/ASR dataset builders, speech researchers, clinical
linguistics teams, language documentation projects, and engineers building repeatable
corpus-design pipelines. The current alpha application surfaces:

- PHOIBLE inventory exploration and eSpeak G2P inspection;
- phoneme, diphone, and triphone evaluation with distribution and text-quality metrics;
- WER, CER, PER, SER, perplexity, and coverage-trajectory analysis;
- greedy, CELF, stochastic, ILP, distribution-aware, and NSGA-II selection;
- repository, Hugging Face, hosted LLM, and local-model generation;
- phonotactic, fluency, readability, Phon-DATG, and Phon-RL workflows;
- immutable corpus versions, reproducibility manifests, reports, and exports.

The complete traceability matrix is maintained in
[`docs/product/capability-matrix.md`](https://github.com/jemsbhai/corpuskit/blob/main/docs/product/capability-matrix.md).
Its documentation, telemetry, owner-role, and failure-mode mapping is maintained in the
[`capability operational ownership map`](https://github.com/jemsbhai/corpuskit/blob/main/docs/product/capability-operations.md).
For a fixed-input, no-mock walkthrough of the real local stack, use the
[`15-minute live demo`](https://github.com/jemsbhai/corpuskit/blob/main/docs/product/15-minute-demo.md).

## Architecture

- **Web:** Next.js 16, React 19, strict TypeScript, accessible server/client components.
- **API:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, and PostgreSQL.
- **Compute:** a pinned `corpusgen==0.1.7` adapter running in bounded Temporal jobs.
- **Artifacts:** immutable, content-addressed files in S3-compatible storage.
- **Identity:** external OIDC; development/test demo mode uses a fixed isolated tenant and
  is rejected in staging and production. Provider tokens remain in encrypted server-side
  sessions behind an opaque `__Host-` cookie and CSRF-protected BFF.
- **Operations:** bounded Prometheus metrics and structured JSON logs today, Docker Compose
  locally, and independently scalable CPU/GPU workers in production; correlated tracing and
  the complete alert suite remain release gates.

The artifact foundation now provides tenant/project/run-scoped immutable metadata,
content-addressed filesystem and private S3-compatible adapters, verified full-object downloads,
30-day tombstones, bounded SigV4 download links, orphan reconciliation, and a canonical
versioned manifest DTO. Public uploads are deliberately limited to untrusted `corpus-text`;
artifact-producing child processes can return only unowned content-addressed staging references,
which the durable parent revalidates and adopts under authoritative run scope. A parent-only
service now records immutable execution facts, constructs canonical run-owned manifests, submits
idempotent replay runs, and compares exact/best-effort/nonreproducible outcomes. The API mounts
only the no-body replay submit/read routes; trusted manifest finalization remains parent-only. A
reviewed worker facts factory is composed for image-attested profiles. Every deployable worker
profile has independent worker and adoption database-secret handles, and CI exercises the
non-owner worker/adoption boundary against clean PostgreSQL. Mandatory CI also executes a seeded
selection and replay through real Temporal, PostgreSQL 17, an exact image-attested non-root worker,
and private MinIO, including duplicate dispatch convergence and byte-identical result comparison.
Deployment-specific secret provisioning and production object-store policy remain release gates.
See the [artifact storage runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/artifact-storage.md) for MinIO setup,
production bucket policy, retention behavior, and explicit release gaps.
See the
[manifest and replay runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/reproducibility-manifests-replay.md) for the trust
boundary, replay semantics, database roles, integration contract, and remaining acceptance gates.

The platform persistence layer now keeps explicit application predicates and adds forced
PostgreSQL RLS under separate API/dispatcher/worker/adoption/maintenance/platform roles.
Per-tenant job, artifact, corpus, generation, provider, and RL admission ceilings are
server-owned and transactionally accounted. Security-relevant project/corpus/run/artifact/quota
mutations append allowlisted, per-tenant SHA-256-chained audit evidence in the same transaction;
only owners/admins can page it. SQLite remains a local-demo/test backend without RLS, and
staging/production reject it. See the
[tenant isolation, quota, and audit runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/tenant-isolation-quotas-audit.md)
for role provisioning, defaults, verification, and explicit remaining gates.

Owners/admins can request project deletion only with an exact explicit confirmation. Access ends
transactionally, artifacts are tombstoned for at least 30 days, and the maintenance identity
removes project metadata only after all object bytes are gone, then releases exact corpus quota
while preserving the audit chain. See the
[project deletion runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/project-deletion.md) for race handling, recovery, and
remaining backup/legal-hold gates.

The API now exposes low-cardinality Prometheus request metrics at the operator-only
`/internal/metrics` endpoint and emits bounded structured access logs with recursive secret and
corpus-content redaction. Staging and production fail startup unless the metrics endpoint is
enabled and protected by a strong opaque bearer token. Workflow, worker, provider, storage,
database, tracing, dashboard, and alert-rule coverage remain release gates; see the
[observability runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/observability.md) for the exact implemented contract and
explicit gaps. The separate
[telemetry contract](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/telemetry-contract.md) defines the versioned metric,
label-cardinality, alert, dashboard, and external-collector obligations that deployments must
satisfy before promotion.

Expired job leases, artifact retention purge, final-object reconciliation, staged-result cleanup,
and due project purge are exposed through a bounded `corpuskit-maintenance run-once` operator
command. It uses
the dedicated maintenance identity, a PostgreSQL advisory singleton lock, and private forced-RLS
cursor state so bounded scans continue without exposing storage keys. Cleanup or integrity
findings produce machine-readable evidence and a nonzero degraded status. Deployment scheduling
and alert evidence remain environment gates; see the
[maintenance runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/maintenance.md).

Database continuity is exercised through credential-redacting PostgreSQL custom-format backups,
offline verification, and destructive-isolated restore drills. The
[`corpuskit-continuity` runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/postgresql-continuity.md) defines the exact
toolchain pin, immutable-storage handoff, restore confirmation, and recovery evidence required;
it complements rather than replaces managed point-in-time recovery and coordinated object-store
backups.

Only `src/corpuskit/adapters/corpusgen/` may import CorpusGen. This boundary is enforced
by an architecture test because CorpusGen 0.1.7 is an alpha dependency with a small
stable top-level API and a wider module-level advanced API.

See [`docs/architecture/overview.md`](https://github.com/jemsbhai/corpuskit/blob/main/docs/architecture/overview.md) and the ADRs in
[`docs/adr/`](https://github.com/jemsbhai/corpuskit/tree/main/docs/adr). The hardened production topology, immutable image and secret inputs,
network isolation, upgrade/rollback procedure, and validation commands are in the
[Kubernetes production runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/kubernetes-production.md).

## Python package versus full application

The planned PyPI distribution is the Python control plane, adapters, workers, migrations, and
operator CLIs. It does **not** contain the Next.js browser application. No package publication is
claimed for this source candidate. From a checkout, use the locked environment:

```bash
uv sync --frozen
uv run corpuskit-phoible provision
uv run corpuskit-db upgrade
uv run corpuskit-api
```

After the signed release and Trusted Publisher workflow have completed, the equivalent bounded
API install is:

```bash
python -m pip install "corpuskit-app==0.1.0a1"
corpuskit-phoible provision
corpuskit-db upgrade
corpuskit-api
```

`corpuskit-api` binds to `127.0.0.1` by default and starts in the development-only demo posture;
it is not a shared or production deployment. Optional worker dependencies are explicit:

```bash
python -m pip install "corpuskit-app[optimization]==0.1.0a1"  # ILP / NSGA-II
python -m pip install "corpuskit-app[repository]==0.1.0a1"    # Hugging Face datasets
python -m pip install "corpuskit-app[llm]==0.1.0a1"           # hosted providers
python -m pip install "corpuskit-app[local]==0.1.0a1"         # local model workflows
```

Use the signed, digest-pinned container release or Helm chart for the complete multi-user web
application. Those deployment paths add the web image, PostgreSQL, Temporal, object storage,
OIDC/Redis sessions, role-separated credentials, network policy, and profile-specific workers;
the [release](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/releases.md) and
[Kubernetes](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/kubernetes-production.md) runbooks define the required verification.

## Development prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 24 LTS and npm 11
- eSpeak NG for real G2P workflows
- Docker with Compose for the full local stack

Install locked dependencies after lockfiles have been generated:

```bash
uv sync --all-groups
npm ci
```

Provision CorpusGen's pinned, checksum-verified PHOIBLE snapshot before using inventory or
PHOIBLE-targeted workflows. This is an explicit operator action; serving HTTP requests never
downloads it:

```bash
uv run corpuskit-phoible provision --json
uv run corpuskit-phoible status --json
```

Run the API and web application:

```bash
mkdir -p data
export CORPUSKIT_DATABASE_URL=sqlite+aiosqlite:///./data/corpuskit.db
uv run corpuskit-phoible provision
uv run corpuskit-db upgrade
uv run corpuskit-api
npm run dev
```

Every Python service also requires an exact process posture. `corpuskit-api` uses the default
`CORPUSKIT_RUNTIME_ROLE=api`; direct dispatcher, worker, and maintenance processes must set
`dispatcher`, `worker`, and `maintenance`, respectively. A mismatched entry point fails before
opening service connections. See the [durable jobs runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/durable-jobs.md) for
the complete role/command map. Direct API installs bind only to `127.0.0.1` by default. Containers
set `CORPUSKIT_API_BIND_HOST=0.0.0.0` explicitly behind the Compose loopback publication or the
Helm Service, ingress, and NetworkPolicy boundary.

PowerShell uses `$env:CORPUSKIT_DATABASE_URL =
"sqlite+aiosqlite:///./data/corpuskit.db"` after `New-Item -ItemType Directory -Force data`.
The migration CLI deliberately requires the database URL in the process environment; it
does not infer a production target. Run migrations explicitly before every direct API
startup after pulling schema changes. Docker Compose performs the same development-only
ordering through its one-shot `migrate` and `provision-phoible` services. The latter owns
write access to the shared PHOIBLE volume; API and worker consumers mount it read-only. See
the [PHOIBLE provisioning runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/phoible-provisioning.md) for air-gapped
installation, verification, recovery, and release-job guidance.

The landing page's fixed, explicitly illustrative walkthrough is served at
`http://localhost:3000`; use the [15-minute live demo](https://github.com/jemsbhai/corpuskit/blob/main/docs/product/15-minute-demo.md)
for executable API-backed acceptance. API documentation is available at
`http://localhost:8000/docs` in development mode. Production disables API docs. See the
[`OIDC operations runbook`](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/oidc-authentication.md) before configuring a
non-demo environment.

Running the web process directly requires explicit `CORPUSKIT_WEB_*` settings; the
development-only examples are listed in [`.env.example`](https://github.com/jemsbhai/corpuskit/blob/main/.env.example). Docker Compose supplies
the same deterministic demo-only defaults so the isolated local walkthrough starts cleanly, and
operators must override both keys for any shared environment. Staging/production also require a
confidential OIDC client, a TLS-authenticated shared Redis/Valkey session store, independent state
and session-encryption keys, and an explicit fixed internal API URL. Missing or unavailable
auth/session infrastructure fails closed and never enables an in-memory fallback.

The `/projects` workbench now demonstrates tenant-scoped project creation, bounded manual or
UTF-8 TXT/CSV/JSON corpus import, append-only immutable corpus versions with explicit parent
lineage, version inspection, deterministic exports, and owner/admin-confirmed project deletion.
The API router is integrated with the durable control plane through one application-owned
database lifecycle; see [`docs/product/project-workspaces.md`](https://github.com/jemsbhai/corpuskit/blob/main/docs/product/project-workspaces.md).
Project/corpus metadata update and individual corpus deletion are not presented as available.

The application also includes bounded G2P, PHOIBLE inventory, evaluation, distribution/text
quality/error-rate/trajectory analysis, six-algorithm selection comparison, repository
generation and scoring, advanced-runtime validation, and durable-job workbenches. The advanced
lab discloses configured policy without sending provider secrets or model paths to the browser;
hosted calls, local model loading, DATG work, and Phon-RL training run only in their designated
durable worker profiles. Browser, API, and real-engine acceptance cover the implemented slices;
qualified CUDA/model, live-provider, and vendor-IdP evidence still gates production promotion.

Run submission is committed with its transactional outbox record and dispatched to an exact
Temporal task queue. The durable parent enforces idempotency, monotonic events, cancellation,
retry, leases, parent-owned deadlines, and canonical result adoption. See the
[durable jobs runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/durable-jobs.md) for the implemented failure and recovery
contract.

Hosted-LLM and pinned local-model execution are isolated to worker job handlers; HTTP exposes only
allowlist validation and hosted-cost estimation. See the
[model runtime runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/model-runtimes.md) for server-owned connection mapping,
budgets, verified offline model provisioning, hardware gates, exact integration steps, and the
live-provider/model acceptance gaps that remain before release. The manual, budget-capped
[qualified provider runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/qualified-provider.md)
defines the only credentialed canary path and the redacted evidence it must retain.

The `/advanced` workbench exposes a redacted runtime catalog, non-secret validation/estimate
forms, durable Hugging Face repository/hosted/local/DATG/Phon-RL job submission and status links, project-scoped read-only DATG and
bounded Phon-RL laboratories, and copy-only CorpusGen CLI previews. It has no HTTP route that
performs synchronous provider/dataset calls, loads a model, builds an index, generates text, or
starts training; execution occurs only after durable submission on the designated worker. Empty
server allowlists and a project with no parent-published DATG index are shown as unavailable and
fail closed. Successful DATG builds publish a content-verified reusable key before reporting
success; inspection and generation still require that key's tenant/project catalog authorization.

Phon-DATG indexing/guidance and Phon-RL reward/training use default-deny, immutable model policy,
offline cache confinement, bounded worker execution, and canonical staged-result adoption. Their
exact supported profiles and remaining qualified-hardware/model gates are documented in the
[Phon-DATG](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/phon-datg.md) and [Phon-RL](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/phon-rl.md) runbooks.

## Quality contract

The required checks include strict Python and TypeScript typing, lint with zero warnings,
unit/property/contract/integration/E2E tests, 90% overall backend coverage, 95% coverage of
changed Python lines on pull requests, WCAG 2.2 AA gates, dependency and container scanning,
build-once digest-verified image
promotion, and real eSpeak/PHOIBLE integration tests. See
[`docs/quality/acceptance.md`](https://github.com/jemsbhai/corpuskit/blob/main/docs/quality/acceptance.md).

Release candidates are built once from a GitHub-verified signed SemVer tag. The pipeline emits a
validated wheel/sdist, six non-root images, SPDX and CycloneDX SBOMs, checksums, keyless Sigstore
signatures, and GitHub provenance/SBOM attestations; promotion verifies the same recorded digests
without rebuilding. PyPI remains a separate manual Trusted Publisher operation behind a protected
environment. See the [release and promotion runbook](https://github.com/jemsbhai/corpuskit/blob/main/docs/operations/releases.md) for repository
prerequisites, consumer verification, rollback, and the GPU/provider/IdP/staging evidence that
still blocks production promotion.

## Security

Do not report vulnerabilities through a public issue. Follow
[`SECURITY.md`](https://github.com/jemsbhai/corpuskit/blob/main/SECURITY.md). Corpus text, generated text, prompts, credentials, and
authorization headers are excluded from logs by default.

## License

Apache-2.0. See [`LICENSE`](https://github.com/jemsbhai/corpuskit/blob/main/LICENSE).
