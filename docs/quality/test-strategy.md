# Test strategy

CorpusKit tests the application as a multi-tenant service and tests CorpusGen as an
untrusted, version-pinned computational dependency. Coverage numbers are necessary but
not sufficient: contracts, invariants, failure recovery, real system dependencies, and
user-observable behavior all have independent gates.

The normative product thresholds are in
[`acceptance.md`](acceptance.md). Operational indicators and error budgets are in
[`../operations/slo.md`](../operations/slo.md).

## Principles

- Tests MUST be deterministic by default. Record seeds, pin assets by digest/revision,
  freeze clocks where appropriate, and avoid assertions on wall-clock time outside
  designated performance suites.
- PR tests MUST NOT depend on public networks or paid providers.
- A mocked integration proves our protocol behavior; a live canary separately proves
  that an approved external integration remains usable.
- Every job test covers success, validation failure, dependency failure, timeout,
  cancellation, retry, duplicate delivery, and worker interruption where applicable.
- Tests MUST assert public outputs and durable state, not private implementation details.
- Snapshot and golden changes require human review. Commands MUST NOT update them in CI.
- Production regressions receive a failing test at the lowest useful layer before the
  fix is merged.

## Suite taxonomy

### Static and architecture checks

Run Ruff formatting/linting, strict mypy, frontend formatting, ESLint with zero
warnings, strict TypeScript, unused-code detection, OpenAPI compatibility, docs/link
checks, secret detection, SAST, dependency audit, and license policy.

An architecture test MUST reject any `corpusgen` import outside
`src/corpuskit/adapters/corpusgen/`. Similar tests enforce direction between API,
application, domain, persistence, and worker layers and ensure the web package cannot
import server secrets.

### Unit and property tests

Unit tests isolate domain rules, parsing, serialization, authorization decisions,
quotas, redaction, job transitions, adapter normalization, and UI state. Hypothesis or
equivalent property tests cover boundary values and Unicode.

Required scientific/domain invariants include:

- Coverage is finite and in `[0, 1]`; covered and missing sets partition targets.
- Coverage trajectories never decrease.
- Selection indices are unique, in range, and within the sentence budget.
- Re-evaluating selected/generated sentences agrees with reported coverage.
- Greedy and CELF return the same deterministic fixture result.
- A fixed seed reproduces stochastic and NSGA-II results.
- Empty and identity inputs have defined finite metrics; identity WER/CER/PER/SER is
  zero.
- JSON and JSON-LD preserve IPA and multilingual Unicode and contain no NaN/Infinity.
- Redaction is idempotent and removes corpus text, prompts, credentials, authorization
  headers, and signed URLs.
- Job state transitions reject illegal, cross-tenant, late, or duplicate mutations.
- Run manifests are canonical non-NaN JSON built only from immutable execution, run, corpus, and
  artifact facts; any digest, size, schema, timestamp, or provenance tamper fails closed.
- Replay submission copies the verified source recipe exactly, reserves quota once, and comparison
  distinguishes exact, best-effort, nonreproducible, and input-drift outcomes.

Coverage gates are 90% line/85% branch backend and 85% line/80% branch frontend.
Pull requests enforce 95% coverage of changed Python lines. The focused frontend
workbench suite instead applies per-file 80% line/90% branch thresholds and is not a
Git-diff gate. Browser authentication record-cipher, security, service, and
session-store modules enforce 100% branches. Dedicated 100% branch reports for the
CorpusGen adapter and backend authorization, quotas, secrets, uploads, and job
transitions are still required before GA; aggregate coverage does not prove them.
Mutation gates are 75% overall and 90% for the currently configured critical scope.

### CorpusGen adapter contract tests

These tests install and use the pinned wheel `corpusgen==0.1.7`; they MUST NOT rely on a
local editable CorpusGen checkout. Contract fixtures cover:

- inventory lookup, source selection, union, search, features, and missing-data errors;
- G2P batch/single behavior, language errors, Unicode, and empty strings;
- phoneme, diphone, and triphone evaluation plus all report fields and JSON/JSON-LD;
- distribution, text-quality, trajectory, WER/CER/PER/SER, and perplexity DTOs;
- all six selectors, all weight strategies, budgets, distributions, metadata, and
  seeded behavior;
- repository, LLM, and local generation backends; stop reasons and progress events;
- phonotactic, fluency, readability, and composite scoring/filtering;
- DATG indexing/guidance and Phon-RL reward, policy, training config, result, and
  checkpoint compatibility;
- missing optional extras, missing eSpeak, corrupt/missing PHOIBLE, provider failure,
  model OOM, invalid parameters, and deadline/cancellation mapping.

Golden DTOs exclude elapsed time and other nondeterministic values. Every golden stores
the CorpusGen/eSpeak/PHOIBLE/model revisions that produced it.

### Service integration tests

Run the API and worker with real PostgreSQL, Temporal, and S3-compatible object storage.
Test transactions, object digests, presigned URL scope, workflow retries, heartbeats,
cancellation, idempotency, stale-job recovery, audit records, and tenant policy. Run
every supported migration from the previous release and verify rollback for reversible
migrations.

The mandatory combined replay gate uses PostgreSQL 17 with distinct non-owner API, dispatcher,
worker, and adoption roles; a real Temporal server and external exact-image worker; and a private
MinIO bucket. It submits and replays a seeded stochastic selection, injects duplicate dispatcher
delivery, verifies canonical manifests and anonymous-access denial, and requires byte-identical
source/replay selection artifacts plus an exact comparison verdict.

Real eSpeak and checksum-verified PHOIBLE are mandatory in nightly and release images.
PR tests may use small deterministic adapter fakes in addition to focused real-container
smokes, but a fake never replaces the real nightly suite.

Third-party calls use deterministic HTTP doubles in automated PR and scheduled workflows.
Separately recorded live-provider canaries use a budget-capped approved LLM model and a
small pinned Hugging Face dataset. Provider outages produce an explicit canary result and
incident signal; they never cause the application to silently return an empty success.

### Browser end-to-end tests

Playwright exercises authentication, project/import workflows, every product
capability, progress/reconnect/cancel/retry, export/delete, authorization failures, and
responsive layouts. Chromium is required on PRs; Chromium, Firefox, and WebKit are
required nightly and for release.

Each test fails on unexpected console errors, page errors, unhandled promise
rejections, failed hidden API calls, or serious/critical axe findings. Visual snapshots
cover stable high-value pages and require explicit review when changed.

### Security, accessibility, and fault tests

Security suites include SAST, dependency/container/IaC scanning, secret scanning, DAST,
cross-tenant authorization tests, malicious uploads, XSS and CSV-formula payloads,
Unicode/bidi cases, SSRF destinations, oversized inputs, decompression bombs, token and
cost-limit bypass, and provider-key leakage checks.

Accessibility combines automated axe/component tests with keyboard scripts, 200% zoom
and 320 CSS pixel layouts, chart-table equivalence, progress announcements, and manual
NVDA/Chrome and VoiceOver/Safari release review.

Fault tests inject database, Temporal, object-store, provider, model, eSpeak, PHOIBLE,
worker, network, disk, and memory failures. They verify bounded retries, no duplicate
durable effects, no cross-tenant leakage, actionable errors, and eventual cleanup.

### Performance and reliability tests

Benchmarks use a versioned fixture set and documented reference hardware. Tests measure
endpoint percentiles, queue delay, job duration, throughput, memory/GPU memory, artifact
size, frontend Core Web Vitals, and JavaScript size. A checked-in baseline is compared
on like-for-like hardware; a regression over 10% blocks release absent an approved
capacity assessment.

Load tests exercise normal and peak tenant mixes. Soak tests run for at least 24 hours
before release. Workers must return to within 10% of post-warm resident memory after 100
medium jobs and show no monotonic growth.

## Execution profiles

### Pull request profile

Required on every PR and targeted to complete within 15 minutes:

1. Lockfile, generated-file, and migration consistency.
2. Backend/frontend format, lint, types, architecture, and docs checks.
3. Unit, property, component, adapter-contract, and coverage checks. Scoped mutation
   runs in scheduled quality rather than as a pull-request gate.
4. Deterministic mocked service/provider integrations.
5. Chromium happy/failure-path smoke with axe.
6. Secret, SAST, dependency, and license scans.
7. Build all changed images and run non-root/read-only image smoke.
8. OpenAPI backward-compatibility and frontend production build/size budget.

The canonical API document lives at `contracts/openapi.json`. CI regenerates the test-profile
document in memory and requires an exact match; on pull requests it also compares the committed
candidate to the default-branch snapshot with
`python -m scripts.quality.openapi_contract compare`. The conservative comparator rejects removed
paths, operations, parameters, responses, media types, schemas or properties, narrowed enums and
tighter input bounds. Intentional breaking changes therefore require a separately versioned API,
not merely an updated snapshot in the same pull request.

Use test sharding and caches, but a cache miss MUST produce the same result. Flaky test
reruns may collect diagnostic evidence only; the original failure keeps the check red.

### Automated nightly profile

The daily/manual `quality-scheduled.yml` workflow first requires a successful broad
`ci.yml` run for the exact same SHA, then repeats or extends it with:

- real production-image eSpeak and checksum-verified PHOIBLE across multilingual
  fixtures;
- all six selectors, advanced metrics, optional CPU capabilities, and clean-wheel
  CorpusGen contracts;
- the full non-provider/non-GPU backend suite with independently checked 90% line and
  85% branch coverage;
- Firefox and WebKit E2E in addition to Chromium, plus automated axe checks;
- frontend static checks, global and focused coverage, and a production build;
- deterministic repeated performance/load/memory fixtures with strict baseline
  lifecycle policy; and
- a fresh scoped mutation wave plus source secret/IaC and mutation-image scans.

Budget-capped live-provider canaries, qualified GPU/local-model evidence, production
service-recovery and migration drills, longer fuzz campaigns, and manual accessibility
remain separately recorded nightly or release evidence. The automated workflow does
not call a paid provider and must not be cited as proof of those external gates.

A nightly failure opens or updates a tracked defect and pages the owner when it consumes
an SLO error budget. The default branch is not releasable while a product-owned nightly
gate is red.

### Release profile

Release runs against signed candidate artifacts and includes:

- every CPU and GPU image and every enabled capability;
- real GPU local generation, deterministic DATG, and at least two bounded Phon-RL PPO
  steps followed by checkpoint reload;
- the full three-browser E2E suite and manual accessibility review;
- DAST, image and IaC policy scans, SBOM/provenance/signature verification;
- migration, backup restore, rollback, graceful shutdown, and disaster-recovery drills;
- the full demo/GA checklist in `acceptance.md`;
- a 24-hour staging soak followed by a 5% production canary for at least 30 minutes.

Production promotion uses the exact tested digest. Automatic rollback triggers and an
on-call owner MUST be active before canary traffic begins.

## Test data and environment policy

- Test corpora are synthetic, public-domain, or explicitly licensed and contain no
  production user data or secrets.
- Fixtures include empty, maximum-size, malformed, duplicated, mixed-normalization,
  emoji, IPA, right-to-left, and multilingual text.
- Golden linguistic fixtures pin the PHOIBLE snapshot and expected linguistic output. Run
  manifests record the observed eSpeak NG version, but Linux builds install eSpeak NG from the
  selected OS repository rather than pinning one package build. A platform change that alters
  output requires scientific review rather than a blind snapshot update.
- Paid-provider fixtures use a dedicated constrained account, allowlisted models,
  minimum token limits, and automatic spend alerts.
- GPU/model tests use preapproved immutable assets and isolated caches. Tests never
  enable remote model code.
- Timeouts exist at test, request, activity, workflow, and provider layers. CI kills and
  reports orphan processes and workflows.

## Failure ownership and flaky-test policy

Quarantine is allowed only for a non-security, non-authorization test with a linked
owner and seven-day expiry. The affected capability is not releaseable while its only
acceptance coverage is quarantined. A test failing at least twice in 50 runs is treated
as flaky; fix or replace it rather than adding retries or timing sleeps.

Standard CI uploads backend JUnit/coverage evidence and frontend coverage/browser evidence for
14 days. Scheduled quality uploads its repeated backend/linguistic, frontend/three-browser,
performance, and mutation evidence for 14 days. Security results that are not uploaded remain
workflow-check evidence; release image digests, SBOMs, signatures, and attestations are retained
in the immutable release record.
