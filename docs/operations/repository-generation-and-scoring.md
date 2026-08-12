# Repository generation and scoring

## Delivery state

CorpusKit provides a bounded repository-generation activity, a smaller synchronous preview,
and deterministic candidate-scoring APIs. This slice covers pre-phonemized rows, raw text via
local eSpeak, and an optional Hugging Face dataset import. Hosted/local model generation and
model-based fluency use a separate worker-only boundary documented in
[`model-runtimes.md`](model-runtimes.md). Durable repository generation is registered on the
`external-provider` queue. Local raw/pre-phonemized inputs use the same handler without network;
immutable Hugging Face imports require the separately controlled Hub egress path.

`RepositoryGenerationResult.execution_mode` distinguishes a synchronous preview from a worker
activity. A returned worker-activity result means that the pure activity function finished; it
does not claim that an external Temporal workflow, outbox event, or persisted run completed.

## Application integration

The router is deliberately standalone. The application factory can wire it after constructing
the adapters and services:

```python
from corpuskit.adapters.corpusgen import (
    CorpusgenGenerationAdapter,
    CorpusgenScoringAdapter,
)
from corpuskit.api.generation_scoring import generation_scoring_router
from corpuskit.services.generation_scoring import (
    GenerationCoordinator,
    GenerationPreviewService,
    ScoringService,
)

coordinator = GenerationCoordinator(CorpusgenGenerationAdapter())
router = generation_scoring_router(
    GenerationPreviewService(coordinator),
    ScoringService(CorpusgenScoringAdapter()),
)
app.include_router(router, prefix="/api/v1", tags=["generation-scoring"])
```

The host application attaches its existing editor-role authorization dependency to write routes.
The preview service rejects Hugging Face sources before the CorpusGen adapter or dataset loader
can run, so HTTP requests cannot trigger remote downloads. The validation route authorizes an
exact durable request without opening a dataset, running eSpeak, or contacting the network.

The standalone routes are:

- `POST /generation/preview`
- `POST /generation/repository/validate`
- `POST /scoring/composite`
- `POST /scoring/ngram/scorers`
- `POST /scoring/ngram/constraints`
- `POST /scoring/phonotactics`
- `POST /scoring/readability`

## Worker integration and dataset policy

Install the optional dataset runtime only in the external-provider worker image:

```text
pip install "corpuskit-app[repository]"
```

Hugging Face access is default-deny. Configure exact server-owned policies, never selectors
supplied by the requester as authorization:

```python
from corpuskit.adapters.corpusgen import CorpusgenGenerationAdapter
from corpuskit.domain.generation import HuggingFaceRepositorySpec
from corpuskit.services.generation_scoring import GenerationCoordinator

allowed_sources = (
    HuggingFaceRepositorySpec(
        dataset="organization/dataset",
        config="clean",
        split="train",
        text_column="text",
        revision="0123456789abcdef0123456789abcdef01234567",
        language="en-us",
        max_samples=1000,
    ),
)
coordinator = GenerationCoordinator(
    CorpusgenGenerationAdapter(),
    allowed_huggingface_sources=allowed_sources,
)
```

The request must also name a config, split, string text column, and lowercase 40-character commit
revision. CorpusKit always calls the upstream loader with `trust_remote_code=False`. Dataset,
config, split, text column, revision, and language must match one exact policy. A request may lower
but never raise the configured sample cap.

`RepositoryGenerationDurableHandler` executes inline inside the platform's single spawned child;
it never starts a nested process. The parent `ProcessExecutionRunner` owns the hard
`activity_timeout_seconds`, cancellation, terminate/kill cleanup, and no-late-write guarantee.
The child returns a canonical `StagedArtifactResult`; the trusted parent re-hashes and adopts the
object under authoritative tenant/project/run scope before committing success and finalizing the
manifest. Child code cannot mutate the durable database or mint an authoritative artifact.

## Bounds

| Input or operation | Limit |
|---|---:|
| Synchronous repository rows | 250 |
| Worker repository rows / Hub samples | 1,000 |
| Sentence characters | 4,000 |
| Phonemes per sentence | 1,000 |
| Total repository text | 1 MiB UTF-8 |
| Target phonemes | 64 |
| Expanded target units | 4,096 |
| Candidates per iteration | 32 |
| Generation iterations | 100 |
| CorpusGen loop timeout | 30 seconds |
| Whole worker activity timeout | 300 seconds |
| Phonotactic artifact | 1 MiB canonical JSON |

At least one sentence, iteration, or loop-time safety stop is mandatory. The default request sets
all three. All public floats reject NaN and infinity.

## Scoring and artifacts

Composite preview and ranking use immutable input state. A commit produces a new `ScoringState`;
it never mutates the caller's artifact. Accepted source IDs are unique and cannot be committed
again. CorpusKit currently enables coverage, n-gram phonotactics, and Latin-script readability on
the synchronous route. A nonzero fluency weight there fails closed because no trusted model scorer
is bound to that process. The durable local-model analysis path can inject the existing
`PerplexityFluencyScorer` from an exact server-authorized immutable bundle and return the composite
ranking; zero remains the default, and synchronous scoring never loads or downloads a model.

The inventory-derived scorer and corpus-trained constraint model are distinct upstream APIs.
CorpusKit therefore emits distinct artifact type identifiers. Each artifact has schema version 1,
a canonical JSON payload, a one-MiB cap, and a SHA-256 integrity digest. Artifacts are application
manifests, not model checkpoints.

Readability responses use `status="unavailable"` with null scores for blank or non-Latin text.
They do not turn an unsupported script into a misleading score of zero.

## Verification and limitations

The acceptance suite covers the registered durable handler, exact server admission, canonical
staging contracts, a real CorpusGen repository loop, real local eSpeak G2P, golden
composite/n-gram vectors, artifact roundtrips, commit failure atomicity, error sanitization, an HTTP
no-network assertion, and a real killable-process deadline. The Hub loader contract uses a fake
loader; CI does not contact the live Hub.

Run the focused gate with:

```text
uv run --locked pytest -q -o addopts="" \
  tests/unit/test_generation_domain_service.py \
  tests/unit/test_generation_adapter.py \
  tests/unit/test_scoring_adapter.py \
  tests/unit/test_generation_scoring_api.py \
  --cov=corpuskit.domain.generation \
  --cov=corpuskit.adapters.corpusgen.generation \
  --cov=corpuskit.adapters.corpusgen.scoring \
  --cov=corpuskit.services.generation_scoring \
  --cov=corpuskit.api.generation_scoring \
  --cov=corpuskit.worker.generation_handler \
  --cov-branch --cov-fail-under=90
```

The Hub loader contract uses a deterministic fake loader in CI; a live immutable Hub revision,
DNS/egress enforcement, outage behavior, and rate-limit evidence remain staging gates. Durable
repository runs persist bounded phase, sampled acceptance-count, iteration, and coverage progress
as monotonic `run.progress` events before terminal publication. Candidate source IDs and text are
intentionally excluded. The child stream is capped at 128 messages and the Job Center reconnects
through the existing event cursor. See `durable-jobs.md` for cancellation and retry semantics.
