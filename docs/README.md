# CorpusKit documentation

Start with the path that matches what you are trying to do.

## Use CorpusKit

| Goal | Start here |
| --- | --- |
| Run the local website from a clean checkout | [Getting started](getting-started.md) |
| Complete common browser and API tasks | [Recipe cookbook](recipes.md) |
| Decide between CorpusKit and CorpusGen | [CorpusKit and CorpusGen](corpusgen-relationship.md) |
| Demonstrate the real local stack with fixed input | [15-minute live demo](product/15-minute-demo.md) |
| Exercise five writing systems | [Multilingual demo](product/multilingual-demo.md) |
| Understand immutable projects, corpora, and versions | [Project workspaces](product/project-workspaces.md) |

The [capability matrix](product/capability-matrix.md) is the source of truth for what is
implemented, verified, or still gated. The [capability operations map](product/capability-operations.md)
connects every capability family to its documentation, telemetry, permissions, and failure
procedure.

## Understand and extend the system

- [Architecture overview](architecture/overview.md)
- [Architecture decisions](adr/)
- [CorpusGen CLI parity](product/cli-parity.md)
- [Test strategy](quality/test-strategy.md)
- [Acceptance contract](quality/acceptance.md)
- [Contribution guide](../CONTRIBUTING.md)

CorpusKit keeps its public HTTP contract in [`contracts/openapi.json`](../contracts/openapi.json).
Development mode also serves interactive API documentation at <http://127.0.0.1:8000/docs>.

## Operate CorpusKit

| Concern | Runbook |
| --- | --- |
| Production topology and rollout | [Kubernetes production](operations/kubernetes-production.md) |
| Authentication and browser sessions | [OIDC authentication](operations/oidc-authentication.md) |
| Durable execution and worker routing | [Durable jobs](operations/durable-jobs.md) |
| Database changes | [Database migrations](operations/database-migrations.md) |
| Object storage and result adoption | [Artifact storage](operations/artifact-storage.md) |
| PHOIBLE data lifecycle | [PHOIBLE provisioning](operations/phoible-provisioning.md) |
| Manifests and replay | [Reproducibility](operations/reproducibility-manifests-replay.md) |
| Metrics, logs, alerts, and SLOs | [Observability](operations/observability.md) and [SLOs](operations/slo.md) |
| Backup and restore | [PostgreSQL continuity](operations/postgresql-continuity.md) |
| Release, promotion, and rollback | [Releases](operations/releases.md) |

Advanced runtime runbooks cover [model execution](operations/model-runtimes.md),
[repository generation and scoring](operations/repository-generation-and-scoring.md),
[Phon-DATG](operations/phon-datg.md), and [Phon-RL](operations/phon-rl.md). Each one distinguishes
implemented behavior from deployment-specific acceptance gates.
