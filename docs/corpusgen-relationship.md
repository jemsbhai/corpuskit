# CorpusKit and CorpusGen

CorpusGen is the linguistic engine. CorpusKit is the multi-user application and control plane
that turns that engine into authenticated, versioned, durable, and auditable workflows. They are
separate packages and repositories; CorpusKit depends on CorpusGen, never the reverse.

CorpusKit currently supports exactly `corpusgen==0.1.7`. Repository CI and images resolve the
hash-locked PyPI artifact from `uv.lock`, not a sibling checkout or an editable path. See the
[`pyproject.toml` pin](../pyproject.toml), the [`uv.lock` artifact hashes](../uv.lock),
[CorpusGen 0.1.7 on PyPI](https://pypi.org/project/corpusgen/0.1.7/), and the
[CorpusGen v0.1.7 source](https://github.com/jemsbhai/corpusgen/tree/v0.1.7).

## Which project should I use?

| Need | Use | Why |
| --- | --- | --- |
| Call phonetic algorithms from one Python process | [CorpusGen](https://github.com/jemsbhai/corpusgen) | It is the direct Python library and CLI. |
| Explore or optimize a corpus from a terminal | CorpusGen | Its CLI is the shortest single-user path. |
| Work through a browser with projects and immutable corpus versions | CorpusKit | It owns the web experience and persisted workspace model. |
| Share work across users or organizations | CorpusKit | It owns OIDC, roles, tenant isolation, quotas, and audit evidence. |
| Run retryable or cancellable provider/model work | CorpusKit | It owns Temporal orchestration, worker isolation, and result adoption. |
| Embed CorpusKit as a general-purpose Python SDK | CorpusGen or the CorpusKit HTTP API | `corpuskit-app` is the application control plane, not a replacement algorithm SDK. |
| Add or change a linguistic algorithm | CorpusGen first, then CorpusKit | CorpusKit adapts reviewed CorpusGen releases instead of forking the scientific logic. |
| Add a product workflow, API, policy, or storage feature | CorpusKit | Those concerns deliberately stay outside CorpusGen. |

Installing CorpusKit already installs its exact CorpusGen dependency. You do not need to clone
both repositories to use or develop CorpusKit.

## The call and ownership boundary

The dependency direction is intentionally one-way, with two execution paths after CorpusKit
authentication and request validation:

```text
browser or API client
  -> versioned CorpusKit request
  -> CorpusKit authentication and validation
       |-> bounded synchronous service
       |     -> src/corpuskit/adapters/corpusgen/
       |     -> corpusgen==0.1.7
       |     -> normalized HTTP result
       |
       `-> durable submission, policy, quota, and transactional outbox
             -> Temporal and a profile-specific worker
             -> src/corpuskit/adapters/corpusgen/
             -> corpusgen==0.1.7
             -> run and events; adopted artifacts and manifests where supported
```

Both paths may use eSpeak, the pinned PHOIBLE snapshot, or an approved optional runtime according
to the operation. A bounded synchronous call does not automatically create a run, artifact, audit
event, or reproducibility manifest.

Only [`src/corpuskit/adapters/corpusgen/`](../src/corpuskit/adapters/corpusgen/) may import
`corpusgen`. The architecture test rejects imports elsewhere. The adapter calls CorpusGen's
Python APIs directly and converts CorpusGen dataclasses, sets, exceptions, and callbacks into
stable CorpusKit contracts. CorpusGen objects do not cross into HTTP responses or persisted
application state.

The detailed rationale is in
[ADR-0001](adr/0001-corpusgen-adapter-boundary.md). The
[architecture overview](architecture/overview.md) describes the surrounding service and worker
boundaries.

## Responsibility split

| Concern | CorpusGen | CorpusKit |
| --- | --- | --- |
| G2P, inventories, coverage, selection, scoring, generation, DATG, and Phon-RL algorithms | Implements | Adapts and exposes |
| Public Python algorithm API and standalone CLI | Owns | Provides reviewed CLI command previews for supported parity cases |
| Browser and versioned HTTP API | Does not provide | Owns |
| Projects, corpus lineage, immutable versions, and exports | Does not persist | Owns |
| Identity, organization roles, RLS, quotas, rate limits, and audit events | Does not provide | Owns |
| Durable retries, deadlines, cancellation, and worker routing | Callback/process primitives only | Owns with Temporal and isolated workers |
| Provider credentials and model allowlists | Accepts backend configuration | Keeps secrets server-side and enforces policy before execution |
| PHOIBLE access | Supplies the loader and mapping | Pins, verifies, provisions, and records the accepted snapshot |
| Result and dependency provenance | Returns algorithm results | Normalizes transport results; durable workflows record supported engine/data/model/image versions and hashes |

This split means that the same algorithm can be used directly through CorpusGen or through a
CorpusKit workflow, but the operational guarantees are different. A standalone CorpusGen call
does not create a CorpusKit project, immutable corpus version, tenant-scoped artifact, audit row,
or durable replay record.

## Capability crosswalk

| Workflow family | CorpusKit surface | Adapter module | CorpusGen surface | Runtime profile |
| --- | --- | --- | --- | --- |
| Inventory and G2P | `/inventory`, `/g2p`, `/api/v1/phonology/*`, `/api/v1/g2p` | `inventory.py`, `client.py` | `get_inventory`, `G2PManager` | core/data |
| Evaluation and analysis | `/evaluate`, `/analysis`, `/api/v1/evaluations`, `/api/v1/analyses/*` | `client.py`, `analysis.py` | `evaluate` and evaluation modules | core/data |
| Sentence selection | `/selection`, `/api/v1/selections` | `client.py` | `select_sentences` and six selectors | core or optimization |
| Repository generation and scoring | `/generation`, generation/scoring API routes | `generation.py`, `scoring.py` | repository backend and scorer modules | core/data for local preview and scoring; external-provider plus the repository extra for durable Hugging Face work |
| Hosted and local generation | `/advanced`, durable `/api/v1/runs` | `model_runtime.py` | LLM and local backends | external-provider or GPU inference |
| Phon-DATG | `/advanced`, DATG lab and durable routes | `datg.py` | DATG index and logit modulation modules | batch/GPU inference |
| Phon-RL | `/advanced`, Phon-RL lab and durable routes | `phon_rl.py` | reward, PPO trainer, value head, and policy modules | GPU training/inference |

The [capability matrix](product/capability-matrix.md) is the authoritative, per-symbol mapping and
status record. Optional CorpusGen extras are installed only in the process images and deployment
profiles that need them; a feature appearing in CorpusGen does not imply that every CorpusKit
deployment can run it.

## The same evaluation in both projects

Use CorpusGen directly when you want an in-process result and own all surrounding state. Before
running this example, install eSpeak NG and provision the pinned PHOIBLE snapshot using the
[versioned CorpusGen setup instructions](https://github.com/jemsbhai/corpusgen/blob/v0.1.7/README.md):

```python
import corpusgen

sentences = [
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog.",
]
report = corpusgen.evaluate(
    sentences,
    language="en-us",
    unit="phoneme",
    target_phonemes="phoible",
)
print(report.coverage)
print(sorted(report.missing_phonemes))
```

Use `POST /api/v1/evaluations` in CorpusKit when you want the bounded, normalized HTTP contract,
or submit the same specification to `POST /api/v1/runs` when you need job history and durable
execution. The complete copy/paste requests are in the
[recipe cookbook](recipes.md#evaluate-a-corpus-against-phoible) and
[durable-run recipe](recipes.md#submit-and-follow-a-durable-evaluation).

CorpusKit can also produce a reviewed CorpusGen CLI preview at `POST /api/v1/labs/cli/preview`.
The response contains an authoritative `argv` array plus quoted POSIX and PowerShell commands. It
does not execute the command and does not generate a Python program. Read the returned warnings:
direct CLI execution does not inherit CorpusKit's immutable input hashes, tenant controls,
provider policy, job history, or artifact adoption.

## Version and upgrade policy

- [`pyproject.toml`](../pyproject.toml) exact-pins the core package and every optional extra.
- [`uv.lock`](../uv.lock) locks the source distribution and wheel hashes used by CI and images.
- `GET /api/v1/version` reports the CorpusGen contract version; `GET /api/v1/capabilities`
  verifies the installed engine and available optional profiles.
- Compatibility tests use the pinned wheel and compare normalized adapter results with direct
  CorpusGen calls. They do not rely on a developer's neighboring clone.
- A CorpusGen update is a dedicated compatibility change: update every pin and the lock, run the
  golden/CLI/runtime matrix, review result and schema differences, and update the capability
  matrix and changelog.

Never make a release or CI result depend on an editable CorpusGen checkout. During coordinated
development, prove the upstream change in CorpusGen, release a version, and then update the exact
CorpusKit dependency through the compatibility process.

For the upstream API and runnable library examples, use the
[versioned CorpusGen README](https://github.com/jemsbhai/corpusgen/blob/v0.1.7/README.md) and
[v0.1.7 examples](https://github.com/jemsbhai/corpusgen/tree/v0.1.7/examples). For CorpusKit
availability and deployment truth, use this repository's capability matrix and runbooks rather
than assuming that every upstream feature is enabled.

## Where to report a change

- Report algorithm behavior, direct Python API, or standalone CLI issues to
  [CorpusGen](https://github.com/jemsbhai/corpusgen/issues).
- Report CorpusKit UI, HTTP contract, adapter normalization, persistence, job, policy, or
  deployment issues to [CorpusKit](https://github.com/jemsbhai/corpuskit/issues).
- For a suspected compatibility regression, include the CorpusKit revision, the output of
  `/api/v1/version`, the relevant capability check, and synthetic or public input. Never attach
  private corpus text, provider credentials, tokens, or private model paths to a public issue.
