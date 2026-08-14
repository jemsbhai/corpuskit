# Changelog

All notable changes to CorpusKit will be documented here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and Semantic Versioning.

## [Unreleased]

### Added

- Append-only corpus version creation from manual sentences or bounded UTF-8 TXT, CSV, and JSON
  imports, with parent lineage, atomic quota accounting, audit evidence, API contracts, and an
  accessible project-workbench flow.

## [0.1.0-alpha.1] - 2026-08-12

### Added

- Standalone CorpusKit repository and production architecture contract.
- A 75-item traceability matrix covering every CorpusGen 0.1.7 capability.
- Pinned CorpusGen adapter with typed G2P, evaluation, and inventory contracts.
- Capability-aware API health and readiness endpoints.
- Multi-tenant persistence foundation, immutable corpus imports, and durable run states.
- Confidential OIDC Authorization Code + PKCE authentication through a server-side BFF,
  encrypted Redis/Valkey sessions, opaque host-only cookies, CSRF protection, and
  tenant/project role enforcement. Live vendor-IdP acceptance remains an environment gate.
- Accessible project, G2P, inventory, evaluation, analysis, selection, generation/scoring,
  advanced-runtime, and durable-job workbenches with bounded inputs and explicit capability
  disclosures.
- Transactional outbox dispatch and bounded Temporal workflows with idempotent submission,
  monotonic events, lease recovery, cancellation, retry, process isolation, and exact
  profile/queue routing.
- Forced PostgreSQL row-level security under distinct API, dispatcher, worker, adoption,
  maintenance, and platform roles, plus transactional quotas and chained audit evidence;
  clean real-PostgreSQL CI exercises the non-owner role boundaries and race behavior.
- Immutable content-addressed artifacts, authoritative staged-result adoption, execution facts,
  canonical manifests, and exact/best-effort/nonreproducible replay comparison. Worker and
  adoption database handles are independently wired for each deployable worker profile.
- Owner/admin-confirmed project deletion, byte-first retained-artifact purge, bounded
  reconciliation and maintenance cursors, exact quota release, and auditable recovery behavior.
- Profile-scoped batch, external-provider, GPU-inference, and GPU-training workers with hosted
  and local generation, model analysis, Phon-DATG, and Phon-RL policy boundaries. Qualified
  CUDA/model and live-provider runs remain release acceptance gates.
- Credential-redacting PostgreSQL backup, offline verification, and destructive-isolated restore
  drills, with managed point-in-time recovery and coordinated object-store recovery left to the
  deployment platform.
- Strict backend tests, typing, lint, security, deployment, and acceptance standards.
- Initial accessible Next.js application and local container foundation.
- Checksum-verified, atomic PHOIBLE provisioning CLI and one-shot Compose job with an
  air-gapped path, fail-closed readiness, read-only consumers, CI acceptance, and an
  operator runbook.
- Build-once immutable release, digest-only promotion, and approval-gated PyPI Trusted
  Publishing workflows with signed provenance, dual-format SBOMs, and verification
  runbooks.
