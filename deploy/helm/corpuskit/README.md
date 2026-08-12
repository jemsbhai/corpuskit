# CorpusKit production chart

This chart renders the CorpusKit control plane, four isolated compute profiles, migration and
PHOIBLE provisioning hooks, bounded maintenance, ingress, network policy, autoscaling, disruption
budgets, and the monitoring contract. PostgreSQL, Temporal, Redis/Valkey, S3-compatible storage,
OIDC, ingress, Prometheus Operator, and GPU device support are external platform dependencies.
The chart never installs demo copies of them.

The default values intentionally do not render. A deployment operator must provide immutable
image digests, existing Secret/PVC references, exact HTTPS identity and storage endpoints, TLS
ingress, explicit egress CIDRs, and a runbook URL. Use `ci/production-values.yaml` only for static
validation: its TEST-NET addresses, example domains, and synthetic digests cannot run.

## Validate

Use Helm 4.2.3 and kubeconform 0.8.0, pinned with release-asset checksums in
`.github/workflows/deployment.yml`:

```text
helm lint deploy/helm/corpuskit --strict \
  --values /secure/path/production-values.yaml
helm template corpuskit deploy/helm/corpuskit \
  --namespace corpuskit --include-crds \
  --values /secure/path/production-values.yaml > rendered.yaml
kubeconform -strict -summary -kubernetes-version 1.35.0 \
  -schema-location default \
  -schema-location '<pinned CRD catalog URL>' rendered.yaml
```

`values.schema.json` rejects unknown keys. Template validation additionally rejects shared
database, Temporal, object-store, or service-account identities; mutable or placeholder image
references; incomplete network allowlists; mismatched GPU resources; empty advanced runtime
policies; unsafe provider environment names; and provider credentials that are not in an exact
one-to-one relationship with hosted-model policy references. Hosted policy also binds each model
namespace to its exact LiteLLM provider and accepts only a bounded, server-owned request delay
(zero by default). Hosted and immutable Hugging Face
repository policies are exposed to the API for admission and only to the external-provider worker
for execution. Custom ingress annotations and pod metadata cannot replace chart-owned TLS,
selector, profile, rollout-checksum, or Helm hook keys.
Artifact, OIDC, and OTLP endpoints are limited to HTTPS port 443, and the port embedded in the
Temporal address must exactly match the port rendered into its egress policy.

`api.rateLimit` is mandatory and cannot be disabled. Its fixed-window size, read/write ceilings,
and retention are rendered into the API, while the same window and retention contract is rendered
into the maintenance CronJob so cleanup interprets stored window epochs identically. Keep edge
connection limiting in front of the chart as well; the application limit starts only after a
request has authenticated.

Release images are currently `linux/amd64`; the chart therefore requires
the `kubernetes.io/os: linux` and `kubernetes.io/arch: amd64` global selectors on every Deployment,
hook Job, and CronJob. GPU profile selectors are additive and may not override a global selector.
Remove these constraints only after the release workflow publishes and verifies other platforms.

## Install

Create and validate every external Secret and PVC first. The DATG claim must support concurrent
batch publication and API/GPU reads (normally `ReadWriteMany`): the chart mounts it writable only
in `worker-batch` and read-only in both consumers. Configure identical, nonempty DATG runtime
policy lists for batch and GPU inference. Then run the migration and PHOIBLE hooks as part of an
atomic release:

```text
helm upgrade --install corpuskit deploy/helm/corpuskit \
  --namespace corpuskit --create-namespace \
  --values /secure/path/production-values.yaml \
  --atomic --wait --timeout 20m
```

See `docs/operations/kubernetes-production.md` for identity, rollout, rollback, network, and
acceptance requirements. See `docs/operations/telemetry-contract.md` before enabling paging.
