# ADR-0003: Use Temporal for durable asynchronous jobs

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

CorpusKit operations range from sub-second reads to hours-long model training. Generation
depends on iterative callbacks and provider calls; local inference and Phon-RL use scarce GPU
resources. Production jobs must survive API/worker restarts, report progress, honor
cancellation, classify retries, and avoid duplicate results.

Executing work in HTTP handlers cannot meet these needs. Redis-only queues and Celery are
familiar, but durable state machines, cancellation, workflow history, heartbeats, and
recovery would still need substantial application code.

## Decision

Use Temporal workflows for all potentially expensive or externally dependent operations.
Activities call the CorpusGen adapter and external infrastructure. PostgreSQL remains the
user-facing source of run/job projections; Temporal is the execution authority.

Job creation writes the immutable run specification, job row, and outbox event in one
database transaction. A dispatcher starts an idempotently named workflow and acknowledges
the outbox event. Reconciliation safely restarts dispatch when either side is interrupted.

Workflows and activity inputs contain stable IDs, hashes, and secret references. They do not
contain corpus bodies or plaintext credentials unless a bounded value is explicitly judged
safe for Temporal history. Activities heartbeat progress and cancellation state. Outputs
are first written to content-addressed temporary artifacts and committed through an
idempotent database operation.

Retry policies are per activity and apply only to classified transient errors. Application
validation, unsupported languages/models, quota exhaustion, and insufficient resources are
non-retryable. Cancellation is cooperative and remains `cancelling` until activities stop
safely. Workflow definitions follow Temporal determinism constraints and use versioning for
in-flight compatibility.

## Consequences

### Positive

- Jobs survive process restarts and transient infrastructure failures.
- Durable cancellation, retries, timers, and progress are standardized.
- CPU, provider, GPU inference, and training queues scale independently.
- Execution history supports operations and audit investigations.

### Negative

- Temporal adds infrastructure and an additional operational skill set.
- Workflow code must remain deterministic and version-compatible.
- Sensitive or large values require discipline because workflow history is durable.
- PostgreSQL projections and Temporal history require reconciliation monitoring.

## Rejected alternatives

- **Synchronous API execution:** incompatible with long-running or restart-safe work.
- **In-process background tasks:** lost on deploy/restart and cannot coordinate GPU pools.
- **Celery/Redis:** suitable for simpler tasks, but lifecycle correctness would be custom.
- **PostgreSQL polling alone:** fewer services but weak workflow semantics and inefficient
  long-running coordination.

## Verification

- Fault tests restart API, dispatcher, and workers at each lifecycle boundary.
- Idempotency tests prove replay and activity retry do not duplicate committed artifacts.
- Cancellation tests cover queued, provisioning, generation, inference, and training states.
- Workflow replay tests run in CI before workflow changes are released.
- Alerts cover outbox lag, workflow failures, heartbeat timeout, retry storms, and queue lag.
