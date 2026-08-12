# ADR-0002: Make corpus versions and run specifications immutable

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

Coverage, selection, generation, and training results are meaningful only relative to exact
inputs and parameters. Editing a corpus or run configuration in place would make prior
reports irreproducible, break audit history, and make retries ambiguous. A mutable document
model appears convenient but is inappropriate for scientific lineage and production job
recovery.

## Decision

A `corpus` is a logical container. Its `corpus_version` records an immutable normalized
sentence collection, content hash, source artifact, normalization policy, language metadata,
creator, and parent version. Editing creates a new version.

A `run_spec` is an immutable, schema-versioned normalized configuration. It identifies its
input corpus versions and inventories and records every effective value, including preset
defaults, seeds, budgets, stopping criteria, dependency versions, model/provider revision,
and scoring weights. A run attempt references one specification. Retry creates a new attempt
and preserves the failed attempt.

Successful selection and generation runs may publish a new corpus version. The new version
records the producing run and its input parents. Artifacts are content-addressed by SHA-256;
equal content may be deduplicated physically without merging authorization or lineage.

Corrections use supersession records rather than update/delete of scientific history. Hard
deletion is reserved for authorized retention/privacy workflows and produces an audit event.

## Consequences

### Positive

- Every report and export has stable provenance.
- Compare, clone, retry, cache, and deduplication semantics are clear.
- Jobs can commit idempotently after worker or network failure.
- Users can reproduce an execution from its manifest.

### Negative

- Storage grows as users iterate.
- Product flows must distinguish a logical corpus from a version.
- Retention and deletion must traverse lineage safely.
- Schema migrations must preserve readability of historical specifications.

## Rejected alternatives

- **Mutable corpus rows:** destroys the relationship between an input and historical result.
- **Snapshot only on export:** failures and intermediate comparisons remain untraceable.
- **Store only user-provided parameters:** changing defaults silently changes reproduction.
- **Overwrite a failed run on retry:** loses diagnostics and can conceal partial side effects.

## Verification

- Database permissions and repository tests reject in-place changes to immutable fields.
- Property tests prove a content hash changes whenever normalized content changes.
- Acceptance tests clone and reproduce a golden run from its manifest.
- Retry and idempotency tests prove that one attempt cannot overwrite another or publish the
  same logical result twice.
