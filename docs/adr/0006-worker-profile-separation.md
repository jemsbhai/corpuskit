# ADR-0006: Separate CPU, external-provider, GPU inference, and GPU training workers

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

CorpusGen capabilities have materially different dependencies, cost, resource demands, and
network trust. Inventory/G2P and most analysis are CPU tasks. Hosted generation requires
outbound network access and provider credentials. Local inference needs model caches and may
need a GPU. Phon-RL training is long-lived, checkpoint-heavy, and competes with inference for
GPU memory. A universal worker image would be large, privileged, slow to patch, difficult to
autoscale, and able to access more data and secrets than necessary.

## Decision

Build and operate distinct immutable worker profiles:

| Profile | Temporal queue | Main capabilities | Network/secret posture |
| --- | --- | --- | --- |
| Interactive CPU | none (HTTP lab) | bounded G2P, inventory, analysis | no durable activity or provider secret |
| Batch CPU | `batch-cpu` | core analysis/selection; optional DATG index build | backend only; read-only model/PHOIBLE caches |
| External provider | `external-provider` | hosted LLM and immutable repository import | provider/Hub egress; scoped secret resolution |
| GPU inference | `gpu-inference` | local generation, perplexity, Phon-DATG | read-only model cache; no provider secrets by default |
| GPU training | `gpu-training` | Phon-RL training and checkpoint publication | isolated GPU nodes; scoped artifact read/write |

Each image installs only its declared CorpusGen extras and system packages. A capability
registry reports worker availability to the API so unavailable features can be disabled
honestly. Workflows select a queue from a server-side policy, never a browser-provided queue
name.

Kubernetes applies separate service accounts, network policies, resource limits, node
selectors, taints/tolerations, autoscaling rules, and pod security settings. Model revisions
are allowlisted and `trust_remote_code` is always false in these runtime adapters. GPU training
cannot starve inference because it uses a separate queue, quota, and node policy.

The dispatcher owns a total, one-to-one `RunKind` mapping and publishes no queue from user input.
Repository generation is assigned to `external-provider` and uses exact immutable Hub policies,
one killable child, and parent-owned result adoption. The reserved durable export kind is rejected
before persistence; workspace exports remain synchronous and bounded. Exact batch, external-provider, GPU-inference, and
GPU-training image targets share a hardened base while installing only their declared optional
dependency profile. Advanced registries are default-deny until their server policy is nonempty.

The parent worker process holds separate database pools for worker and adoption service accounts.
Worker authority may read/transition runs and insert execution facts; adoption authority alone may
publish result metadata and finalize manifest bindings. Deployed configuration rejects missing or
equal credentials. Adoption credentials never cross the spawned-child IPC boundary.

## Consequences

### Positive

- Smaller images and security blast radius.
- Independent scaling, quotas, patching, and service objectives.
- Provider credentials and public egress are absent from workers that do not need them.
- GPU inference latency is insulated from long training workloads.

### Negative

- More images, manifests, queues, and compatibility combinations.
- Model/artifact handoff between profiles must be explicit.
- Underused specialized capacity can cost more than a shared pool.
- End-to-end tests need a profile matrix.

## Rejected alternatives

- **One full worker image:** maximal dependency, credential, egress, supply-chain, and resource
  blast radius.
- **CPU/GPU split only:** hosted providers still expose secrets and egress to unrelated work;
  training can starve inference.
- **Client-selected queue:** permits cost and policy bypass.
- **Dynamic package installation in jobs:** undermines reproducibility and supply-chain
  controls.

## Verification

- Unit tests prove total/unique server routing, no cross-profile fallback, exact handler sets,
  default-deny allowlists, cache-root boundaries, and spawn-serializable registries.
- CI builds and scans separate batch and external-provider images, runs them read-only/non-root and
  offline, and asserts unrelated extras are absent. GPU Dockerfile targets are statically checked;
  qualified CUDA execution remains an explicit release gate.
- Compose validation keeps only `external-provider` on `provider-egress`; GPU and batch workers are
  backend-only. A deployed Kubernetes network-policy acceptance test remains outstanding.
- Parent-authored manifests record exact profile, immutable image and policy digests, installed
  CorpusKit/CorpusGen/eSpeak/PHOIBLE facts, and required model provenance. Staging/production fail
  when the image digest is absent.
