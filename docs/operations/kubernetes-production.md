# Kubernetes production deployment

The Helm chart at `deploy/helm/corpuskit` is CorpusKit's hardened reference topology. It has been
validated by strict Helm rendering, negative policy tests, and Kubernetes/Prometheus Operator
schemas. It has **not** been proven on a live managed cluster in this repository. A staging
deployment, failure drills, load evidence, external-service conformance, and an alert exercise
remain mandatory promotion gates.

## Topology and trust boundaries

The chart deploys the web application, API, transactional-outbox dispatcher, and four distinct
Temporal worker deployments:

| Component | Queue | External access | Privileged data path |
| --- | --- | --- | --- |
| API | `batch-cpu` submissions | PostgreSQL, Temporal, S3, OIDC | tenant-scoped application DB role |
| Dispatcher | `batch-cpu` routing | PostgreSQL, Temporal | outbox dispatch DB role only |
| Batch worker | `batch-cpu` | PostgreSQL, Temporal, S3; writable DATG publication cache | worker DB plus separate artifact-adoption DB role |
| Provider worker | `external-provider` | worker dependencies plus allowlisted provider CIDRs | profile-specific provider secret and adoption role |
| GPU inference | `gpu-inference` | PostgreSQL, Temporal, S3; model caches stay offline | read-only model and DATG caches |
| GPU training | `gpu-training` | PostgreSQL, Temporal, S3; model caches stay offline | read-only model cache and bounded Phon-RL policy |
| Maintenance | n/a | PostgreSQL and S3 | dedicated maintenance DB/object-store roles |

Migration and PHOIBLE provisioning are blocking `pre-install`/`pre-upgrade` hooks. Maintenance is
a singleton `CronJob` with `concurrencyPolicy: Forbid`. PHOIBLE, model, and DATG caches are
pre-created PVCs. Model caches are read-only. The DATG claim mounts read-write only in the batch
parent at `/datg-index-publish`, and read-only in API and GPU inference at `/datg-indexes`; the
trusted parent is the only DATG writer. The provisioning hook is the only writer to the PHOIBLE
claim.

Every pod has its own ServiceAccount, disables API-token automount and service links, runs as a
non-root image user with RuntimeDefault seccomp, drops all Linux capabilities, forbids privilege
escalation, and uses a read-only root filesystem plus bounded memory-backed `/tmp`. Images are
rendered only as `repository@sha256:digest`. API/web use topology spreading, multi-replica PDBs,
and conservative HPAs. GPU pods require an explicit node selector, toleration, and equal nonzero
`nvidia.com/gpu` request/limit.

API and worker pods set `TMPDIR=/run/corpuskit-espeak` and mount a distinct 64 MiB memory
`emptyDir` there. Phonemizer needs that narrow writable/executable location because it copies and
loads the installed eSpeak shared library per wrapper; it must not redirect the rest of `/tmp` or
grant any additional capability. Kubernetes does not expose an `exec`/`noexec` switch for
`emptyDir`; enforce any node-runtime mount policy independently and keep this dedicated path
bounded and service-scoped. `XDG_CONFIG_HOME=/tmp/corpuskit-xdg` keeps optional Pulse state out of
the read-only image home.

Current release manifests are `linux/amd64` only. The chart applies the required
`kubernetes.io/os: linux` and `kubernetes.io/arch: amd64` node selectors to API, web, dispatcher,
all workers, telemetry, migration/PHOIBLE hooks, and maintenance. GPU selectors are additive and
cannot replace those platform constraints. Do not remove them until the release pipeline produces
and tests other platform manifests.

## External prerequisites

Use a dedicated namespace; the chart's namespace-wide default-deny policy must never share a
namespace with unrelated workloads. Provision these dependencies independently and record their
owner, SLO, recovery plan, endpoint, certificate
authority, and tested version:

- Kubernetes 1.35 or newer with a NetworkPolicy-enforcing CNI, metrics-server, Pod Security
  admission, an ingress controller, and a TLS certificate controller or pre-created certificate;
- PostgreSQL with point-in-time recovery and the role/grant model from
  `tenant-isolation-quotas-audit.md`;
- production Temporal with TLS and six independently revocable API credentials;
- authenticated TLS Redis/Valkey for web sessions;
- TLS S3-compatible storage with versioning, retention controls, and SSE-S3 or SSE-KMS;
- an OIDC provider configured exactly as described in `oidc-authentication.md`;
- Prometheus Operator CRDs, kube-state-metrics, Alertmanager, and Grafana;
- an NVIDIA device plugin/operator and qualified GPU nodes when GPU profiles are enabled; and
- ReadWriteMany storage (or an equivalent immutable snapshot distribution mechanism) for the
  PHOIBLE cache and for immediate batch-to-API/GPU DATG publication visibility. Model snapshots
  remain immutable; DATG files are content-addressed, created without replacement, and authorized
  through tenant/project catalog rows.

`compose.yaml` contains local demonstration dependencies and start-dev Temporal. They are not a
source of production configuration, identity, durability, or availability evidence.

## Secret and identity contract

The chart creates no `Secret`. Supply secret references through an external secret controller or
pre-create them with encryption at rest and audited access. Do not commit a rendered Secret,
database URL, API key, state key, encryption key, KMS identifier, or provider credential.

Database Secret values are credential-bearing `postgresql+asyncpg://` URLs. The chart requires
twelve distinct Secret names: API, dispatcher, four workers, four adoption identities, migrator,
and maintenance. Each worker process receives only its profile DB and adoption URLs. The migrator
gets only its owner/migration URL. PostgreSQL grants—not merely different Secret names—must also be
distinct and verified after every migration.

Temporal API-key Secrets are distinct for API, dispatcher, and all four worker profiles. S3
credentials are distinct for API, each worker, and maintenance. Prefer short-lived workload
identity credentials where the S3 implementation supports them; this chart currently models
static Secret references so their exposure is explicit. Dispatcher, web, migrator, and PHOIBLE do
not receive S3 keys. Only API receives the Prometheus bearer token. Only web receives OIDC client,
Redis session, state-HMAC, and session-encryption secrets. Only the provider worker receives
`CORPUSKIT_PROVIDER_*` keys, and template validation requires an exact bijection between those
environment names and `secret://env/<name>` hosted-policy references.

Rotate one role at a time with credential overlap: create the new external secret/version, update
values, complete a rolling deployment, verify old credentials are unused, then revoke them.
Session-key and OIDC rotation must also follow `oidc-authentication.md`. Never rotate migration and
runtime DB roles simultaneously with a schema change.

## Network policy

The chart installs namespace-wide default-deny ingress and egress, then grants only the paths each
component needs. Kubernetes NetworkPolicy does not understand FQDNs. `networkPolicy.cidrs` must
therefore contain current, explicit external CIDRs or stable addresses for a controlled egress
gateway. Do not use `0.0.0.0/0` or `::/0`. DNS is limited to the configured kube-dns selector.

Provider CIDRs are granted only to the external-provider worker. OIDC is granted to API and web;
Redis only to web; PostgreSQL only to DB clients; S3 only to API, workers, and maintenance. The
offline PHOIBLE source-PVC path needs no internet. If the provisioning hook downloads its pinned
snapshot instead, explicitly maintain `phoibleSource` CIDRs and treat provider address changes as
a reviewed deployment. NetworkPolicy does not replace a provider allowlist, TLS verification,
cloud firewall, private endpoint, or egress proxy audit.

The provider allowlist must cover every approved hosted-model endpoint and each Hugging Face Hub,
dataset, and backing-download endpoint needed by an immutable repository policy. Prefer routing
these changing provider ranges through a controlled egress gateway with stable CIDRs. Repository
imports run only on the external-provider worker; the API receives the non-secret policy JSON for
admission but receives neither provider credentials nor provider-network egress.

The artifact-storage endpoint, OIDC issuer/callback, and OTLP exporter must use the default TLS
port, either implicitly or as explicit `:443`; chart validation rejects other ports because their
egress rules allow only TCP 443. The port embedded in `temporal.address` must exactly match
`temporal.port`, which is the value rendered into every Temporal client policy. Custom ingress
annotations and global pod metadata cannot replace the chart-owned TLS redirects, proxy bounds,
selector/profile labels, rollout checksums, or Helm hook annotations.

Helm executes migration and PHOIBLE hooks before regular release resources exist. The chart
therefore creates two hook-scoped ingress/egress-deny policies at weight `-25`, before either Job.
They permit only the migrator's database path and, when no offline source PVC is present, the
PHOIBLE download path. Successful hook policies intentionally remain until the next hook run so
Helm cannot remove protection before the Jobs start; deleting the dedicated namespace removes them
on uninstall. Do not install this release into a namespace that will be retained for other apps.

Before rollout, resolve every endpoint from inside the target network, compare it with the
allowlist, exercise DNS failover, and confirm denied traffic with a disposable canary pod. A CNI
that ignores NetworkPolicy is a hard deployment failure.

## Prepare and install

1. Build the API with the optimization extra and build each worker from its exact Docker target.
   Scan, sign, and publish images; record immutable digests in release evidence.
2. Restore a recent production backup into isolation and run `corpuskit-db upgrade` plus
   `corpuskit-db check`. Verify expand/contract compatibility with both old and candidate images.
3. Pre-populate immutable model PVCs and pre-create the empty DATG publication claim. Put the
   pinned PHOIBLE source on a read-only source PVC whenever possible; the hook verifies and
   atomically installs it into the cache claim. Configure identical nonempty DATG runtime policies
   on batch and GPU inference so built keys remain generation-authorized.
4. Create all Secrets/PVCs and validate role grants. Never put secret values in a Helm values file.
5. Copy `values.yaml` to a private deployment repository and fill every required reference,
   endpoint, digest, runtime policy, CIDR, ingress field, and runbook URL.
6. Use the workflow-pinned Helm 4.2.3 and kubeconform 0.8.0 to run the exact strict lint,
   negative tests, render, and Kubernetes 1.35 schema-validation commands from the deployment
   workflow. Review the rendered secret references and NetworkPolicies.
7. Install with `helm upgrade --install --atomic --wait --timeout 20m`. Inspect hook logs before
   they age out. A migration or PHOIBLE failure must abort promotion.

Do not use `ci/production-values.yaml`: its digests are synthetic and its TEST-NET CIDRs and
example hostnames are deliberately non-routable.

## Readiness and promotion

The API liveness route checks process health. Its current readiness route verifies required
CorpusGen/PHOIBLE capabilities but does not prove PostgreSQL, Temporal, S3, Redis, OIDC, provider,
or worker readiness. Worker/dispatcher exec probes only detect a live PID; Temporal poller health
must come from Temporal and synthetic telemetry. The Prometheus contract intentionally pages on
missing dependency/outbox/workflow/provider signals. Install and verify the collectors described
in `telemetry-contract.md` before routing traffic.

Promotion requires all of the following evidence:

- migration and model/schema drift checks, PHOIBLE checksum, all six Temporal pollers/clients, and
  a create/evaluate/select/export artifact round trip;
- web OIDC login/refresh/revocation through TLS Redis and role/tenant authorization checks;
- S3 write/read/digest/delete/reconciliation using each intended workload identity;
- default-deny and positive egress tests, ingress TLS/HSTS, external certificate validation, and
  no public API/metrics/OTLP service;
- PDB drain, HPA scale-up/down, dispatcher failover, worker termination/cancellation, GPU
  scheduling, and maintenance overlap tests;
- dashboards populated, every required alert synthetically fired and delivered, and no corpus,
  prompt, token, signed URL, or tenant identifier in telemetry; and
- the SLO load profile, a 24-hour staging soak, a 5% canary for at least 30 minutes, a named
  on-call owner, and recorded rollback approval.

## Upgrade and rollback

Capture current Helm values, all image digests, schema revision, external-service versions,
dashboard snapshots, and backup/PITR markers before an upgrade. Keep old images and credentials
available through the canary. Stop automatically when migration, authorization, integrity,
dependency readiness, error rate, latency, queue age, crash-loop, or GPU saturation gates fail.

For an application regression with a compatible schema, remove traffic from the candidate and run
`helm rollback <release> <revision> --wait --timeout 20m`; then verify API readiness, Temporal
pollers, outbox drain, and artifact integrity. Never assume Helm rollback reverses data. If a
migration is incompatible, keep traffic stopped and follow the database recovery plan; restore or
forward-fix only from an explicitly rehearsed decision. If object integrity or tenant isolation is
suspect, freeze writes and follow the security/continuity runbook rather than rolling through it.

## Known external acceptance gaps

Repository validation cannot prove cloud IAM, private endpoints, DNS/CIDR stability, CNI policy
enforcement, ingress behavior, certificate rotation, Prometheus/Alertmanager delivery, Temporal
HA, database PITR, object-store retention, Redis failover, GPU drivers, provider budgets, or a
real cluster rollout. Keep the release in staging until those named gates have evidence.
