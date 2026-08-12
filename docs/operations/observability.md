# Observability operations

CorpusKit ships a hardened API telemetry foundation: bounded Prometheus HTTP metrics and
structured JSON access logs with recursive content and credential redaction. The production Helm
chart adds a protected ServiceMonitor, Prometheus rules, a Grafana dashboard, and an optional
OpenTelemetry Collector gateway. See [`telemetry-contract.md`](telemetry-contract.md) for the
precise boundary between emitted application metrics and required external normalized signals.
This is not a claim that the complete SLO instrumentation described in [`slo.md`](slo.md) has been
validated in a live environment.

## Prometheus endpoint

When `CORPUSKIT_METRICS_ENABLED=true`, the API serves `GET /internal/metrics`. The route is
not included in OpenAPI and is excluded from its own request counters, histograms, and access
logs. It emits:

- `corpuskit_http_requests_total{method,route,status_class}`;
- `corpuskit_http_request_duration_seconds{method,route}`;
- `corpuskit_http_requests_in_progress`; and
- `corpuskit_http_unhandled_exceptions_total`.

Only a conservative HTTP method, the matched route template, and status class are labels. Raw
paths, query strings, request bodies, tenant/project/run/user identifiers, corpus text, prompts,
phonemes, and credentials are never metric labels. Unmatched routes share the fixed
`unmatched` label, and unknown methods share `OTHER`.

Authenticated traffic-limit rejections are visible through
`corpuskit_http_requests_total{status_class="4xx"}` on the normalized route and through the
stable `rate_limited` response code. The limiter deliberately does not add organization or
subject labels. Alerting may use a bounded route-level 429 ratio, while per-tenant investigation
uses access-controlled database/audit evidence. Limiter persistence outages appear as bounded
503 responses before the application handler executes.

Development and test may omit a token for an isolated local scrape. Staging and production
fail startup unless metrics are enabled and `CORPUSKIT_METRICS_BEARER_TOKEN` contains 32-512
visible ASCII characters. Keep the endpoint on an operator-only network in addition to bearer
authentication. Store the token in the platform secret manager, mount it into the scraper as a
file, and do not place it in a command line or repository:

```yaml
scrape_configs:
  - job_name: corpuskit-api
    scheme: https
    metrics_path: /internal/metrics
    bearer_token_file: /run/secrets/corpuskit_metrics_token
    static_configs:
      - targets: [api.internal.example:443]
```

Rotate by updating the API and scraper secret during a coordinated rollout. The current API
accepts one token, so keep the old API replicas and old scraper configuration available until
new replicas are ready, switch the scraper, then drain the old replicas. A missing, malformed,
or incorrect credential always returns the same generic `401` contract with `Cache-Control:
no-store`; supplied credentials are never reflected.

## Structured logs

The `corpuskit-api` entry point configures one-event-per-line JSON logging. API access events
contain only method, matched route template, status code/class, duration, a fixed-length SHA-256
correlation digest derived from a validated bounded request ID, and a fixed outcome. They never
include the caller's raw request ID, raw URL, query, body, client address,
tenant, user, or exception text. The shared processor recursively redacts keys associated with
authorization, cookies, credentials, passwords, secrets, tokens, API keys, prompts, sentences,
corpora, content, and signed URLs; it also bounds depth, collection length, and string size and
normalizes non-JSON objects to type names.

Treat redaction as defense in depth. New log sites must use an allowlist of fields and must not
pass exception strings, provider payloads, model outputs, filesystem paths, or arbitrary DTO
dumps. CI canary tests assert that secret and corpus markers do not survive telemetry handling.

## Validation and incident checks

Before promoting an environment:

1. Confirm unauthenticated and incorrect-token scrapes return generic `401` responses.
2. Confirm an authenticated scrape succeeds and contains no tenant-, request-, or content-derived
   labels.
3. Exercise a known route, a `404`, and a controlled `5xx`; verify the counters and in-progress
   gauge converge correctly.
4. Search collected logs for the release canary strings used by security tests.
5. Confirm the operator network policy blocks public and tenant-workload access.
6. Confirm retention, access control, and export policy for telemetry match the data-classification
   policy.

If a credential or corpus canary appears in telemetry, stop promotion, restrict telemetry access,
rotate affected credentials, preserve the bounded evidence needed for incident response, and
follow the security incident process.

## Explicit gaps

The application metrics above do not yet emit Temporal queue age, outbox lag,
workflow/activity duration and retries, trusted job-heartbeat age, worker CPU/RSS/GPU saturation,
provider token/cost ledgers, object-store/database/Redis/OIDC readiness, or distributed traces.
The Helm dashboard and alert rules consume an explicit external metric contract and page when its
required series are absent; they do not manufacture those signals. The optional collector accepts
OTLP and forwards API metrics, but current application images do not include an OpenTelemetry SDK.
An external log-canary scanner, platform collectors/exporters, live Alertmanager delivery tests,
and staging failure exercises remain operator gates. Until those signals and exercises are
complete, CorpusKit does not satisfy the full observability or paging contract in `slo.md` and is
not generally available.
