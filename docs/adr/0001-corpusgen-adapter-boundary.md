# ADR-0001: Isolate CorpusGen behind a versioned adapter boundary

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

CorpusKit depends on CorpusGen for linguistic computation. CorpusGen 0.1.7 exposes a small
top-level API and a broader documented module-level API for advanced generation, scoring,
guidance, and training. It is currently an alpha package, so allowing its objects and import
paths to spread throughout the web product would make upgrades risky and application
contracts unstable.

Calling the CorpusGen CLI would add process, escaping, progress, error-classification, and
serialization problems while losing type information. Reimplementing the algorithms in
CorpusKit would create two sources of truth.

## Decision

Only `src/corpuskit/adapters/corpusgen/` may import `corpusgen`. The dependency is exact-
pinned in every worker profile. The adapter:

- accepts versioned CorpusKit request models;
- calls CorpusGen Python APIs directly;
- returns stable JSON-safe CorpusKit result models rather than CorpusGen dataclasses;
- converts callbacks into progress events and cancellation checks;
- records CorpusGen, PHOIBLE, eSpeak, optional dependency, and model versions;
- normalizes sets/orderings and non-finite values for deterministic persistence; and
- maps exceptions to an explicit application error taxonomy.

Separate adapter services cover inventory/G2P, evaluation, selection, generation,
guidance/training, and export, but share common version and error handling. Advanced
CorpusGen imports remain internal even when they are documented public APIs.

The boundary is enforced by an architecture test. CorpusGen upgrades require a dedicated
compatibility PR that runs golden fixtures for every capability and documents output or
schema changes. Generated, copy-only CorpusGen CLI previews are a reproducibility aid;
production execution does not shell out and the preview surface does not generate Python code.

## Consequences

### Positive

- Application contracts survive CorpusGen internal reorganization.
- Upgrades have one reviewable integration surface.
- Temporal workers receive serializable, versioned values.
- Testing can replace the engine through a typed fake without mocking the entire package.
- CorpusKit does not duplicate linguistic algorithms.

### Negative

- New CorpusGen features require deliberate adapter and schema work.
- Some data is copied during model conversion.
- The adapter must maintain compatibility logic while old runs remain readable.

## Rejected alternatives

- **Import CorpusGen throughout services:** quickest initially, but couples product behavior
  and persisted contracts to an alpha dependency.
- **Invoke the CLI:** weaker typing, progress, cancellation, error handling, and security.
- **Fork or reimplement CorpusGen:** duplicates scientific logic and fragments maintenance.
- **Expose raw CorpusGen dataclasses from the API:** creates accidental wire-contract
  commitments and fails for sets or implementation-specific metadata.

## Verification

- Static architecture test rejects disallowed imports.
- Golden tests compare adapter output with direct pinned CorpusGen output.
- Contract tests assert JSON-safe, schema-versioned results.
- A dependency-upgrade workflow fails if the pin changes without compatibility evidence.
