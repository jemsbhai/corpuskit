# Production acceptance standard

This document defines the evidence required to call a CorpusKit change accepted. A
feature is not available merely because its UI is visible or its happy path works. It
must have a traced requirement, bounded behavior, automated acceptance coverage,
operational telemetry, user documentation, and a supported failure mode.

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. A release
manager may not waive a MUST silently; every waiver needs an owner, rationale, expiry,
and linked remediation issue in the release record.

## Acceptance profiles

| Profile                           | Trigger                                         | Required evidence                                                                                                                                                                                                                                                                                       | Target duration  |
| --------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| PR                                | Every pull request                              | Static checks, unit/property/component tests, CorpusGen contracts, Chromium smoke E2E, automated accessibility, security scans, docs and container smoke                                                                                                                                                | 15 minutes       |
| Automated nightly                 | Default branch, once per day or manual dispatch | Successful broad CI for the exact SHA; repeated full backend coverage; real eSpeak/PHOIBLE and selector acceptance; frontend static, focused coverage, production build, Chromium/Firefox/WebKit and axe; deterministic performance/memory; scoped mutation; source secret/IaC and mutation-image scans | 90 minutes       |
| External nightly/release evidence | Approved candidate environment                  | Budget-capped [provider canary](../operations/qualified-provider.md), qualified local-model/GPU evidence, service recovery, longer fuzz/load/migration rehearsal, DAST, and manual accessibility as applicable                                                                                              | No silent waiver |
| Release                           | Signed release candidate                        | Every worker image and optional capability, GPU DATG/RL, full E2E and manual accessibility, DAST, migration/restore/rollback, SBOM/provenance/signatures, 24-hour staging soak and canary                                                                                                               | No time waiver   |

All required jobs MUST fail closed. Required checks MUST NOT use `continue-on-error`,
hide test failures with retries, or replace reviewed snapshots automatically.

## Universal release gates

A release candidate is acceptable only when all of the following are true:

- The capability matrix maps each shipped requirement to an automated test, user
  documentation, telemetry, and an operational owner through the machine-checked
  [`capability-operations.md`](../product/capability-operations.md) companion. Exact executable
  test symbols are independently checked by
  [`acceptance-evidence.json`](../product/acceptance-evidence.json); a partial entry cannot back a
  Verified matrix row.
- Python and TypeScript strict type checks pass with no baseline suppressions added by
  the release.
- Lint and formatting pass with zero warnings.
- Backend coverage is at least 90% lines and 85% branches; frontend global coverage is
  at least 85% lines and 80% branches. Pull requests enforce at least 95% coverage of
  changed Python lines. The focused frontend workbench suite separately enforces, per
  measured file, at least 80% lines and 90% branches; it is not a Git-diff metric.
- The browser authentication record-cipher, security, service, and session-store modules
  have 100% branch coverage. CorpusGen adapter, backend authorization, quota,
  provider-secret, upload-validation, and job-state modules MUST each gain a dedicated
  100% branch report before GA. Overall or focused aggregate reports do not satisfy that
  requirement; until those reports exist, this remains an explicit GA release blocker.
- Mutation testing scores at least 75% across the reviewed core scope and 90% across
  its security-critical scope. The initial enforced wave covers corpus validation plus
  authentication and advanced-run admission; CorpusGen adapter and job-state waves
  MUST each reach 90% before GA rather than being inferred from this scoped result.
- All PR, nightly, and release suites pass against the exact artifacts being promoted.
- OpenAPI compatibility, database migration, clean install, and rollback checks pass.
- The production image passes real eSpeak G2P and checksum-verified PHOIBLE lookup.
- There are no known Critical or High security vulnerabilities. Medium findings need a
  named owner, documented mitigation, and remediation date within 30 days.
- There are no unresolved security findings of any severity in authentication,
  authorization, tenant isolation, secret handling, or arbitrary code/network access.
- Automated accessibility reports have zero serious or critical axe violations, and
  the manual WCAG 2.2 AA review is complete.
- Performance budgets and the SLO release criteria in
  [`../operations/slo.md`](../operations/slo.md) pass without an unexplained regression.
- There are zero open P0 or P1 defects. Any accepted P2 defect has explicit release-owner
  approval, documented user impact, workaround, and scheduled fix.
- Release artifacts are built once, content-addressed, SBOM-attested, signed, and
  promoted unchanged from staging to production.
- Changelog, version, upgrade notes, user guides, API documentation, security notes,
  operations runbooks, and rollback instructions describe the shipped behavior.

## CorpusGen compatibility acceptance

CorpusKit pins `corpusgen==0.1.7`. Only code under
`src/corpuskit/adapters/corpusgen/` may import CorpusGen. An architecture test MUST
enforce this boundary.

Every durable run manifest that crosses the worker/adoption boundary MUST record:

- CorpusKit and CorpusGen versions;
- eSpeak version and selected voice;
- PHOIBLE revision and SHA-256 when used;
- model provider, model name, immutable revision, and dataset revision when used;
- random seed and complete normalized parameters;
- start/end timestamps, stop reason, input and output content digests, and worker image
  digest.

An update to CorpusGen MUST be isolated in a dependency PR. That PR MUST run the full
contract suite against the installed wheel, explicitly review every golden-output
change, and demonstrate that disabled optional capabilities still fail with a typed,
actionable `capability_unavailable` response.

## Security and privacy acceptance

CorpusKit targets OWASP ASVS Level 2 and the OWASP API Security Top 10. Release evidence
MUST include the current threat model and these controls:

- Production identity uses OIDC/OAuth with PKCE; CorpusKit does not store passwords.
  Administrators use MFA.
- Cookie sessions are `Secure`, `HttpOnly`, and `SameSite=Lax` or stricter. Mutating
  cookie-authenticated requests have CSRF protection.
- Every resource operation performs tenant and user authorization. End-to-end tests
  attempt cross-tenant read, mutation, export, job control, and artifact access for every
  resource type.
- CORS uses an exact origin allowlist. Responses set HSTS, a CSP without
  `unsafe-eval`, `nosniff`, frame protection, and a restrictive permissions policy.
- Uploaded, corpus, and model-generated text is always treated as untrusted. HTML is
  escaped or sanitized, downloads have safe content disposition, and spreadsheet
  exports neutralize formula injection.
- Uploads are checked by content and declared type and default to at most 10 MB, 10,000
  sentences, and 2,000 characters per sentence. Archive expansion and decompression
  ratios are bounded.
- Arbitrary provider, dataset, or model URLs are forbidden. Approved providers,
  datasets, and model revisions are allowlisted; private, loopback, link-local, and
  cloud-metadata destinations are blocked at validation and egress layers.
- Hugging Face assets use immutable revisions, `trust_remote_code=False`, and
  `safetensors`. Runtime request handling MUST NOT initiate an unapproved asset
  download.
- LLM/local-model output receives no tool execution authority. Compute workers have no
  access to cloud metadata, unrelated tenant artifacts, or application secrets.
- Provider credentials are ephemeral by default. Persisted credentials use KMS-backed
  envelope encryption, are never returned after creation, and are never logged.
- Corpus text, prompts, generated text, credentials, authorization headers, and signed
  artifact URLs are excluded from logs by default.
- Users are told when content will leave CorpusKit for an external provider and must
  confirm transmission. Product documentation states retention and model-provider data
  handling.
- Data is encrypted in transit and at rest. Users can export and delete projects and
  artifacts; deletion and the default 30-day retention policy have automated tests.
- Rate, concurrency, compute-time, provider-token, provider-cost, artifact-size, and
  job-deadline limits are enforced server-side and tested for bypass.

Default production limits are three concurrent CPU jobs and one concurrent GPU or LLM
job per tenant. Generation is capped at 100 accepted sentences, 500 iterations, and 15
minutes. ILP and NSGA-II are capped initially at 2,000 candidates. Phon-RL requires an
explicitly enabled role and hard GPU, wall-time, token, checkpoint, and storage quotas.

## Accessibility acceptance

The shipped product MUST conform to WCAG 2.2 AA across primary and failure workflows.
Acceptance requires:

- Zero serious or critical axe findings on all release E2E pages.
- Complete keyboard operation with logical focus order, visible focus, skip links, no
  traps, and focus restoration after dialogs and navigation.
- Correct page titles, landmarks, headings, names, descriptions, form labels, inline
  validation, and error summaries.
- Job submission, progress, cancellation, success, and failure changes announced
  without stealing focus.
- Charts provide an equivalent accessible data table and do not encode meaning by color
  alone. Text and non-text contrast meet AA thresholds.
- IPA symbols render using an approved font stack. Language and direction attributes
  are correct for multilingual and bidirectional content.
- Layout works at 200% browser zoom and 320 CSS pixels without loss of content or
  functionality.
- Manual NVDA with Chrome and VoiceOver with Safari evidence for each release.

## Performance acceptance

On the pinned reference hardware and fixture set:

- Cached inventory/search and job-status endpoints: p95 at most 400 ms and p99 at most
  1 second.
- Job submission: p95 at most 300 ms.
- CPU queue start: p95 at most 5 seconds under provisioned load; GPU queue start: p95 at
  most 30 seconds.
- Evaluate 100 typical sentences: p95 at most 10 seconds.
- Greedy-select 1,000 pre-phonemized candidates: p95 at most 15 seconds.
- Export 10,000 sentences: p95 at most 10 seconds.
- Core Web Vitals at p75: LCP at most 2.5 seconds, INP at most 200 ms, and CLS at most
  0.1.
- Initial application JavaScript is at most 250 KB gzip, excluding lazy-loaded
  chart/editor chunks.
- No benchmark regresses more than 10% from the checked-in baseline without an approved
  performance note and capacity assessment.
- Worker resident memory after 100 repeated medium jobs returns to within 10% of the
  post-warm baseline and shows no monotonic growth.

## Demo and GA acceptance checklist

The release candidate MUST complete this checklist in a clean tenant using the signed
candidate images. The guided demonstration MUST finish in under 15 minutes with
pre-warmed assets; long-running DATG/RL work may display a pre-completed reproducible
job but must also submit and execute the bounded smoke described below.

1. Sign in, create a project, import manual text plus CSV and JSON, and show validation
   errors without losing valid data.
2. Browse and search PHOIBLE languages, choose a source and union inventory, inspect
   segment features, and show the pinned revision/checksum.
3. Run real G2P fixtures covering Latin, Cyrillic, Arabic, Devanagari, and CJK scripts;
   inspect IPA and tokenized phonemes.
4. Evaluate one corpus for phoneme, diphone, and triphone coverage.
5. Inspect counts, missing units, per-sentence provenance, distribution quality, text
   quality, and a monotonic coverage trajectory.
6. Compute known-answer WER, CER, PER, SER, and corpus perplexity results.
7. Run greedy, CELF, stochastic, distribution-aware, ILP, and NSGA-II selection. Apply
   uniform, inverse-frequency, and linguistic-class weights, budgets, target coverage,
   target distribution, and deterministic seeds; compare results and Pareto metadata.
8. Generate from an uploaded repository pool, one supported live LLM provider, and one
   pinned local model.
9. Demonstrate n-gram phonotactic, perplexity-fluency, readability, and composite
   phonetic scoring and filtering.
10. Run a deterministic bounded Phon-DATG smoke and display guidance/reproducibility
    metadata.
11. On the exact candidate SHA, run `peft-train` for at least two PPO steps with
    `use_peft=True` in the exact GPU-training image, persisting only ephemeral mounted
    SQLite/object-store state plus its bounded HMAC-bound receipt. Run `peft-infer` separately
    in the exact GPU-inference image: authenticate the receipt, reopen and revalidate the adopted
    training result and lineage, one-use materialize its read-only safetensors adapter, generate
    on CUDA, and perform the second parent adoption. Validate both phase roles, image digests,
    candidate SHAs and CUDA proofs, and cancel a second training job with no live child or late
    staging. The Windows alternative MUST execute the same two roles under one exact lock profile.
    Remove all ephemeral state before uploading only the fail-closed schema-v3 `peft-chain.json`;
    a local or contract-test-only artifact is insufficient.
12. Stream job progress, refresh/reconnect, cancel, retry a safe failure, and display an
    actionable error for a deliberately disabled capability.
13. Export the corpus, parameters, reproducibility manifest, evaluation JSON, and
    JSON-LD; verify digests and re-import supported formats.
14. Prove cross-tenant isolation, delete the project and artifacts, and verify audit and
    retention behavior.
15. Complete the flow with no browser console errors, failed hidden requests, uncaught
    exceptions, accessibility violations, secret exposure, or manual database edits.

Item 14 has automated service evidence for owner/admin authorization, non-enumerating tenant
isolation, exact confirmation, concurrent idempotency, immediate logical denial, 30-day minimum
retention, byte-first artifact deletion, orphan-write reconciliation, exact quota release, and a
preserved audit chain. The signed-candidate live-stack browser demonstration and production
backup-erasure evidence remain required before the checklist itself is complete.

## Release record

The immutable release record MUST link the requirement matrix, CI runs, coverage and
mutation reports, security scans, accessibility evidence, performance report, data
migration and restore evidence, SBOM and signatures, staging soak dashboard, canary
decision, known defects, waivers, and named release approvers.

## Automated artifact-integrity evidence

The release-candidate automation in `.github/workflows/release.yml` implements the mechanical
artifact-integrity portion of this standard. It requires a clean default-branch commit, a
GitHub-verified annotated SemVer tag, successful exact-SHA CI and scheduled-quality workflows,
version/changelog agreement, immutable GitHub releases, and independent `release` environment
approval. It builds the Python distribution and all six exact container profiles once, validates
installed CLI/resources and non-root read-only execution, provisions the checksum-pinned PHOIBLE
snapshot and then smoke-tests real eSpeak G2P and PHOIBLE lookup offline from every exact Python
image digest, scans the exact digests, emits SPDX and CycloneDX SBOMs, keylessly signs
files/images, creates build and SBOM attestations, and verifies all evidence before publishing.

Standard CI uploads backend JUnit/coverage and frontend coverage/browser artifacts for 14 days.
`.github/workflows/quality-scheduled.yml` runs daily or by manual dispatch and uploads repeated
backend/linguistic, frontend/three-browser, performance, and mutation evidence for 14 days. It
fails unless broad `ci.yml` already succeeded for the same SHA, and the release gate accepts only
a successful scheduled run for the exact tagged commit. This is the broad automated nightly
slice; paid-provider, qualified GPU, production DAST, manual accessibility, and operational
release evidence remain external gates.

The scheduled performance job may omit an approved baseline only before the repository has a
`HEAD` or while `HEAD` is the single root commit. Every later scheduled run and every release
requires a schema-valid exact-profile baseline whose clean source revision is an ancestor of the
candidate. Verification writes separate reports and never overwrites that baseline.

`verify-promotion.yml` consumes only immutable release assets and image digests and records a
reviewed staging/production plan; `publish-pypi.yml` consumes only the already verified wheel and
sdist through an environment-bound PyPI Trusted Publisher. The normative procedure and consumer
commands are in [`../operations/releases.md`](../operations/releases.md).

This automation is necessary but not sufficient for GA. A local workflow/schema test is not
evidence of GHCR, Sigstore, PyPI, or environment execution. Qualified GPU and live-provider runs,
vendor OIDC/TLS Redis, manual accessibility, DAST, performance/SLO results beyond the scheduled
absolute benchmark, complete continuity
and rollback drills, 24-hour staging soak, canary, on-call readiness, and named approvals remain
external release gates and must be attached to the immutable release record.

The supply-chain slice was locally checked on 2026-08-11 with the pinned Actionlint container,
Zizmor 1.29.0 in online pedantic mode, strict YAML loading, Ruff, mypy, the release-contract and
packaging tests, Compose model validation, upstream action-SHA resolution, and live resolution of
the pinned OCI indexes. A real wheel and sdist passed archive validation; a fresh Python 3.12.13
environment loaded all seven console entry points, found the typed package resource, and exercised
the four argument-parsing CLI help paths. The PGDG verifier accepted the published full
fingerprint and rejected the intentional near-match in an isolated Linux container. These are
local implementation checks, not registry, Sigstore, PyPI, staging, GPU, provider, or IdP evidence.

The reproducible slow-wave commands, exact-profile comparison rules, mutation scoring
denominator, baseline governance, and qualified CUDA profiles are documented in
[`performance-mutation-gpu.md`](performance-mutation-gpu.md).
