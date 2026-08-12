# Service level objectives

This document defines CorpusKit service level indicators (SLIs), objectives (SLOs),
error budgets, and release/runtime response policy. Product acceptance thresholds are
defined in [`../quality/acceptance.md`](../quality/acceptance.md).

## Scope and measurement

The production service has two reliability surfaces:

1. The **control plane**: web, API, authentication callback, project metadata, job
   submission/status/cancellation, and artifact metadata.
2. The **compute plane**: CorpusGen CPU/GPU jobs and approved third-party provider calls.

SLIs are calculated from server-side request and workflow telemetry over rolling 30-day
windows, segmented by tenant class, route/capability, worker profile, region, and release
version. Synthetic probes validate the public path separately. Client cancellation,
invalid input, rejected quota, planned maintenance announced at least 72 hours in
advance, and confirmed third-party provider outages are excluded from platform-error
SLIs but are reported independently. Internal timeouts, capacity exhaustion, worker
crashes, and malformed application responses are not excluded.

## Objectives

| SLI | Objective | Window |
| --- | --- | --- |
| Control-plane availability | 99.9% successful eligible requests | Rolling 30 days |
| Control-plane non-validation errors | Fewer than 0.5% 5xx or malformed responses | Rolling 30 days |
| Cached inventory/search and job status | p95 ≤400 ms; p99 ≤1 s | Rolling 24 hours and 30 days |
| Job submission acknowledgement | p95 ≤300 ms; p99 ≤1 s | Rolling 24 hours and 30 days |
| CPU queue start delay | p95 ≤5 s under provisioned load | Rolling 24 hours |
| GPU queue start delay | p95 ≤30 s under provisioned load | Rolling 24 hours |
| Platform-induced terminal job failure | Less than 1% of accepted jobs | Rolling 30 days |
| Progress freshness for running jobs | 99% update at least once every 15 s | Rolling 24 hours |
| Artifact durability | 99.999999999% annual durability from storage provider; zero application-caused loss | Annual plus incident count |
| Backup recovery | RPO ≤15 min; RTO ≤60 min | Every recovery event and monthly drill |

Provider failures, refusals, rate limits, and latency are tracked per provider/model. A
provider outage may not violate platform job-success SLO if CorpusKit classifies it
correctly, stays within retry/cost limits, preserves job state, and returns an actionable
error. It still triggers provider health UI and operational alerts.

## Job deadlines and capacity protections

Every job has a queue deadline, execution deadline, heartbeat, cancellation path, and
server-enforced resource budget. Default tenant limits are:

- Three concurrent CPU jobs.
- One concurrent GPU or hosted-LLM job.
- Generation: no more than 100 accepted sentences, 500 iterations, or 15 minutes.
- ILP/NSGA-II: no more than 2,000 candidates until a higher-capacity profile is
  explicitly approved.
- Upload: 10 MB, 10,000 sentences, and 2,000 characters per sentence.
- Phon-RL: role-gated and limited by GPU minutes, wall time, input/output tokens,
  checkpoint size, and tenant storage quota.

Quota rejection MUST occur before expensive work starts and MUST return the applicable
limit and remediation. Workflows use bounded exponential backoff with jitter. Retries
must not create duplicate artifacts, audit events, charges recorded by CorpusKit, or
terminal state transitions.

## Performance budgets

The following release benchmarks run on documented, pinned reference hardware with the
versioned acceptance fixtures:

| Workload | Budget |
| --- | --- |
| Evaluate 100 typical sentences | p95 ≤10 s |
| Greedy-select 1,000 pre-phonemized candidates | p95 ≤15 s |
| Export 10,000 sentences | p95 ≤10 s |
| Web LCP | p75 ≤2.5 s |
| Web INP | p75 ≤200 ms |
| Web CLS | p75 ≤0.1 |
| Initial JavaScript | ≤250 KB gzip, excluding lazy chart/editor chunks |
| Repeated-job worker memory | Within 10% of post-warm baseline after 100 medium jobs; no monotonic growth |

No benchmark may regress more than 10% from its checked-in baseline without a written
capacity analysis, named owner, and release approval. Benchmark results always record
hardware, operating system, image digest, CorpusGen/eSpeak/PHOIBLE/model revisions,
fixture digest, and seed.

## Error budget and change policy

A 99.9% monthly availability objective permits approximately 43 minutes 50 seconds of
unavailable time in a 30.44-day month. The availability and job-success budgets are
managed independently.

- At 25% budget burn in 7 days: create an operational review and assign an owner.
- At 50% budget burn in 7 days: pause nonessential risky releases and prioritize
  reliability work.
- At 75% budget burn in 30 days or a projected budget exhaustion: freeze feature
  releases; only incident, security, and demonstrated reliability fixes may deploy.
- At 100% budget exhaustion: incident review and explicit service-owner approval are
  required before the release freeze ends.

Security incidents, cross-tenant exposure, data loss, and secret disclosure always stop
promotion regardless of remaining error budget.

## Alerting

Page the on-call engineer for:

- control-plane availability below 99% for 5 minutes;
- fast burn of at least 14.4 times the 30-day error budget over 1 hour, confirmed over 5
  minutes;
- slow burn of at least 6 times the budget over 6 hours, confirmed over 30 minutes;
- p95 job submission above 1 second for 10 minutes;
- CPU queue p95 above 30 seconds or GPU queue p95 above 5 minutes for 10 minutes;
- platform terminal-job failure above 5% for 10 minutes with at least 20 jobs;
- no heartbeat from a running job for twice its expected heartbeat interval;
- cross-tenant authorization denial anomalies, suspected secret exposure, integrity
  failure, data loss, or backup failure;
- provider spend exceeding 80% of the daily budget or an unbounded retry signal.

Create a ticket rather than a page for isolated canary failures, performance regressions
under 10%, approaching storage quotas, certificate/credential expiry beyond 14 days, and
nonurgent dependency findings.

## Telemetry contract

Metrics MUST include request count/error/latency, queue age, job start and duration,
retries, cancellation latency, stop reason, worker CPU/RSS/GPU memory, artifact sizes,
provider latency/tokens/cost, capability readiness, and backup/restore outcomes. Traces
propagate a correlation ID from browser to API, Temporal workflow/activity, adapter, and
approved provider call.

Telemetry dimensions use opaque tenant/project/job identifiers and bounded-cardinality
enums. Logs and traces MUST NOT contain corpus text, prompts, generated sentences,
credentials, authorization headers, signed URLs, or raw provider payloads. Redaction is
tested continuously with canary secrets.

Each enabled capability publishes a readiness state and last successful synthetic test.
Public readiness verifies required low-cost dependencies without starting models or
calling paid providers. Detailed capability diagnostics are authenticated.

## Continuity and recovery

- Database point-in-time recovery supports an RPO of 15 minutes or less.
- Configuration and metadata restore plus service restart meets an RTO of 60 minutes.
- Content-addressed artifacts use versioning and retention protection appropriate to
  their classification.
- Restore tests run monthly into an isolated environment and verify tenant ownership,
  artifact digests, job/result linkage, and audit history.
- Database migrations run as a separate one-shot job and follow expand/migrate/contract
  sequencing when zero-downtime compatibility is required.
- Rollback instructions identify the last compatible API, worker, web, and schema
  versions. A release may not depend on irreversible destructive migration without a
  verified restore procedure and explicit approval.

## Release and canary policy

The release candidate must pass the complete release profile, then soak in staging for
at least 24 hours. Production begins with 5% traffic for at least 30 minutes using the
same signed image digests.

Automatic rollback occurs when, compared with the established baseline:

- 5xx rate exceeds 2% for 5 minutes;
- p95 control-plane latency doubles for 10 minutes or breaches 1 second for eligible
  cached/status routes;
- platform job failure exceeds 5% with at least 20 jobs;
- queue age breaches the paging threshold;
- worker crash-loop, memory, or GPU-memory exhaustion occurs repeatedly;
- migration, authorization, integrity, or capability-readiness checks fail.

An on-call owner, rollback operator, dashboards, and incident channel MUST be active
before canary traffic starts. Promotion evidence records the candidate digest, soak and
canary windows, SLI comparison, alerts, rollback readiness, and final decision.
