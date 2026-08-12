# Capability operational ownership

This companion to the [capability traceability matrix](capability-matrix.md) supplies the
operational fields required by the production acceptance standard. Every requirement inherits
the row whose scope is the requirement ID prefix. The matrix remains the source for the exact
CorpusGen symbol, execution class, runtime, acceptance test, status, and limitation.

The metrics named below are the bounded signals CorpusKit actually emits or requires from the
deployment telemetry contract. `corpuskit_http_requests_total` and
`corpuskit_http_request_duration_seconds` use only method, normalized route template, and status
class labels; they never contain tenant, project, run, corpus, prompt, model output, or secret
values. Durable status, progress, audit, and replay records are access-controlled operational
evidence rather than Prometheus labels. See the [observability runbook](../operations/observability.md)
and [production telemetry contract](../operations/telemetry-contract.md).

The owner entries are accountable team roles, not a silent waiver mechanism. Before promotion,
the release record must resolve every applicable role to a named primary/on-call owner. A missing
assignee blocks the release.

| Requirement scope | User and operator documentation | Telemetry/evidence contract | Operational owner | Supported failure mode |
| --- | --- | --- | --- | --- |
| `CK-OPS-*` | [Architecture overview](../architecture/overview.md), [durable jobs](../operations/durable-jobs.md), [Kubernetes production](../operations/kubernetes-production.md) | `corpuskit_http_*` request/error/latency series; readiness; dependency, outbox, queue-age, heartbeat and deployment signals; durable run events | Platform Operations | Readiness and submission fail closed with bounded 4xx/503 responses; queued work remains recoverable and unregistered kinds are rejected before persistence. |
| `CK-INV-*` | [15-minute demo](15-minute-demo.md), [PHOIBLE provisioning](../operations/phoible-provisioning.md), [multilingual demo](multilingual-demo.md) | `corpuskit_http_*` request/error/latency series, readiness status, provisioner digest/size evidence and content-safe access logs | Linguistic Data Operations | Missing, corrupt or unprovisioned data returns a bounded unavailable response; load and provision operations never expose cache paths or partially replace the pinned snapshot. |
| `CK-G2P-*` | [15-minute demo](15-minute-demo.md), [multilingual demo](multilingual-demo.md) | `corpuskit_http_*` request/error/latency series, eSpeak readiness evidence and content-safe access logs | Linguistic Runtime | Unsupported languages and unavailable eSpeak fail explicitly; bounded requests never silently switch backend or return fabricated success. |
| `CK-COV-*` | [15-minute demo](15-minute-demo.md), [multilingual demo](multilingual-demo.md) | `corpuskit_http_*` request/error/latency series and content-safe access logs | Corpus Workflow | Invalid or oversized target spaces return 422; state is request-local and deterministic, with no partially persisted tracker. |
| `CK-EVAL-*` | [15-minute demo](15-minute-demo.md), [multilingual demo](multilingual-demo.md), [artifact storage](../operations/artifact-storage.md) | `corpuskit_http_*` request/error/latency series; durable event/progress records when submitted asynchronously; artifact integrity evidence | Evaluation and Analysis | Invalid targets are rejected; dependency failures are explicit; durable attempts preserve terminal failure/cancellation and cannot publish late results. |
| `CK-WGT-*` | [15-minute demo](15-minute-demo.md), [multilingual demo](multilingual-demo.md) | `corpuskit_http_*` request/error/latency series and content-safe access logs | Evaluation and Analysis | Non-finite, negative, incompatible or oversized weighting inputs return 422 without mutating caller state. |
| `CK-SEL-*` | [15-minute demo](15-minute-demo.md), [multilingual demo](multilingual-demo.md), [durable jobs](../operations/durable-jobs.md) | `corpuskit_http_*` request/error/latency series; durable event/progress records; queue-age and heartbeat signals for asynchronous selectors | Optimization Runtime | Missing optional solvers are reported as unavailable; bounded previews fail atomically and durable cancellation prevents late selection publication. |
| `CK-GEN-*` | [Repository generation and scoring](../operations/repository-generation-and-scoring.md), [model runtimes](../operations/model-runtimes.md), [qualified provider](../operations/qualified-provider.md), [durable jobs](../operations/durable-jobs.md) | `corpuskit_http_*` request/error/latency series; durable event/progress records; queue, heartbeat, provider readiness/budget, artifact digest and audit evidence | Generation and Model Runtime | Policy, consent, budget, credential, pin, timeout and output-contract failures are sanitized and terminal; cancellation kills compute before parent-side artifact adoption. |
| `CK-SCR-*` | [Repository generation and scoring](../operations/repository-generation-and-scoring.md), [15-minute demo](15-minute-demo.md) | `corpuskit_http_*` request/error/latency series, content-safe access logs and result artifact digests for durable consumers | Evaluation and Analysis | Invalid weights or scorer artifacts fail before commit; preview/rank is non-mutating and atomic score-and-commit does not expose partial state. |
| `CK-DATG-*` | [Phonetic DATG](../operations/phon-datg.md), [model runtimes](../operations/model-runtimes.md) | `corpuskit_http_*` request/error/latency series; durable event/progress records; queue/heartbeat, cache publication digest and tenant catalog evidence | Generation and Model Runtime | Unapproved models, revisions, cache keys or runtime provenance fail closed; only parent-verified, atomically published tenant-scoped indices become usable. |
| `CK-RL-*` | [Phon-RL](../operations/phon-rl.md), [model runtimes](../operations/model-runtimes.md) | `corpuskit_http_*` request/error/latency series; bounded durable step progress; queue/heartbeat, checkpoint digest and cancellation evidence | Training Runtime | Untrusted prompts/checkpoints, unsupported policies, non-finite rewards, timeout and cancellation fail closed; temporary inputs are one-use and late checkpoints cannot publish. |
| `CK-CLI-*` | [CLI parity](cli-parity.md), [release operations](../operations/releases.md) | Exit status, sanitized CLI diagnostics, matching HTTP/durable evidence for delegated operations and release smoke reports | Release Engineering | Missing dependencies/configuration fail with nonzero sanitized exits; preview does not perform network/model execution and durable commands preserve server policy. |
| `CK-REP-*` | [Reproducibility and replay](../operations/reproducibility-manifests-replay.md), [artifact storage](../operations/artifact-storage.md) | Manifest/artifact digests, access-controlled replay comparison, audit records, `corpuskit_http_*` request/error/latency and object-store dependency evidence | Reproducibility and Storage | Missing/tampered inputs, cross-tenant references and unavailable storage fail closed; replay remains classified unavailable until authoritative artifacts can be verified. |

This map does not claim that the external deployment signals are already present in every local
environment. The telemetry contract deliberately alerts when required production collectors are
missing, and that missing evidence blocks promotion rather than being inferred from unit tests.
