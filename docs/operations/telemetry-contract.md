# Production telemetry contract

CorpusKit directly emits bounded, content-safe API HTTP metrics documented in `observability.md`.
The Helm chart adds a bearer-authenticated `ServiceMonitor`, SLO-oriented `PrometheusRule`, Grafana
dashboard, and an optional hardened OpenTelemetry Collector gateway. These resources define a
production contract; they do not fabricate signals the application or external platforms do not
yet emit.

## What the chart implements

Prometheus scrapes only the API's internal ClusterIP service at `/internal/metrics`. The metrics
bearer token is read from an existing Secret and is never present in the ServiceMonitor or
ConfigMap. NetworkPolicy permits the configured monitoring selector and optional collector, not
arbitrary namespace workloads. The rules cover scrape absence, 5xx rate, 99.9% error-budget fast
burn, p95 latency, unavailable deployments, crash loops, HPA saturation, dependency readiness,
outbox lag, Temporal queue age, stale job heartbeats, provider readiness, and provider budget.

The Grafana dashboard is provisioned as a labeled ConfigMap. It has panels for the same signals
and links to the operator-provided HTTPS runbook. Prometheus Operator and Grafana are external;
the chart does not create cluster-wide RBAC or assume a particular stack release beyond labels.

When enabled, the OpenTelemetry Collector:

- receives OTLP gRPC/HTTP only on a ClusterIP service;
- scrapes the protected API metrics using a mounted token;
- adds only environment, namespace, and service-namespace resource attributes;
- applies memory limiting, batching, bounded queues, and bounded retry; and
- exports over an operator-supplied HTTPS OTLP endpoint with a Secret-backed authorization value.

Current CorpusKit code does not include an OpenTelemetry SDK or automatic instrumentation, so the
collector does not by itself create application traces or worker metrics. OTLP trace/log receivers
are an integration point for a separately tested instrumented build. Do not claim distributed
trace coverage until correlation propagation and redaction tests pass end to end.

## Required normalized signals

An environment is not promotion-ready until trusted platform collectors export these exact,
low-cardinality series into the same Prometheus tenancy:

| Metric | Type/unit | Required labels | Source and meaning |
| --- | --- | --- | --- |
| `corpuskit_dependency_ready` | gauge, 0/1 | `namespace`, `dependency` | authenticated low-cost probes for `database`, `artifact-storage`, `temporal`, `redis`, and `oidc` |
| `corpuskit_outbox_oldest_pending_seconds` | gauge, seconds | `namespace` | age of the oldest dispatchable `PENDING`/expired-claim outbox row |
| `corpuskit_temporal_task_queue_age_seconds` | histogram, seconds | `namespace`, `worker_profile` | schedule-to-start age for the four bounded queues |
| `corpuskit_running_job_heartbeat_age_seconds` | gauge, seconds | `namespace`, `worker_profile` | age of the latest trusted heartbeat for running jobs |
| `corpuskit_provider_ready` | gauge, 0/1 | `namespace`, `provider` | non-billable provider configuration/connectivity canary |
| `corpuskit_provider_daily_budget_ratio` | gauge, ratio | `namespace`, `provider` | authoritative accrued/reserved daily spend divided by configured daily ceiling |

Use only bounded enums and deployment labels. Never attach organization, project, run, user,
corpus, prompt, sentence, phoneme, model output, credential, URL, object key, or raw provider error
as a label. Per-tenant cost and job investigation belongs in access-controlled audit data, not
global metric dimensions.

The outbox collector must use a read-only monitoring role with access only to the minimum aggregate
query or a security-definer view reviewed by the database owner. It must not export payload,
organization, run, claim-owner, or error-code values. Treat an empty healthy outbox as age zero;
distinguish query failure with the database dependency signal. Temporal queue and heartbeat data
must come from server/worker telemetry, not estimates from API request time. S3 readiness must use
a dedicated canary prefix and lifecycle, never a tenant object. Provider readiness must avoid paid
generation; budget comes from the authoritative ledger rather than provider response text.

The `*TelemetryMissing` alerts deliberately fire when these contract metrics are absent. Do not
silence them permanently. Keep a release in staging until the collectors are present, have stable
timestamps, and survive a collector restart and dependency failover.

## Alert exercise and routing

Route `severity=critical,service=corpuskit` to the staffed on-call path and warnings to the owned
operations queue. In staging, synthetically exercise each critical path:

1. deny an API scrape and confirm `CorpusKitApiScrapeMissing`;
2. inject bounded 5xx traffic and verify the high-error and fast-burn alerts;
3. pause the dispatcher and confirm outbox lag;
4. stop one poller profile and confirm queue/poller telemetry and workload alerts;
5. fail each dependency canary without exposing its endpoint or credential;
6. publish a stale synthetic heartbeat;
7. cross the provider budget threshold with a test ledger, without a paid call; and
8. confirm Alertmanager receipt, page delivery, acknowledgement, runbook link, and resolution.

Record alert start/delivery/acknowledgement/resolution times and dashboard screenshots as release
evidence. Check that alerts resolve after recovery and do not flap during normal rollout.

## Privacy, retention, and incident response

The optional collector and direct Prometheus scrape create two metric delivery paths. Use one as
the authoritative alert source to avoid duplicate pages. Configure TLS certificate validation,
tenant isolation, retention, and deletion in the external telemetry backend. Collector export
authorization must be independently rotatable; never reuse the API metrics token.

Continuously scan logs, metrics, and traces with canary corpus/secret markers. If any corpus text,
prompt, token, signed URL, provider payload, or tenant identifier appears, stop promotion, restrict
telemetry access, preserve bounded incident evidence, rotate affected credentials, and follow the
security incident process. A green dashboard is not proof of redaction or dependency correctness.
