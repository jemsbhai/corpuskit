# CorpusKit recipe cookbook

These recipes continue from the [local Docker quick start](getting-started.md). They use real
CorpusKit routes and fixed, non-sensitive input; no response is mocked. Each marked JSON request
is parsed against the corresponding application contract in CI.

The examples target the isolated development demo at `http://127.0.0.1:8000/api/v1`. Demo mode
accepts requests as the fixed local owner without a token. A shared deployment is different: use
its HTTPS URL and a real OIDC bearer token, never copy development secrets, and follow the
[OIDC runbook](operations/oidc-authentication.md).

## How to run a request

Start the stack and set the API root:

```bash
docker compose --profile web up --build --detach --wait
API=http://127.0.0.1:8000/api/v1
```

For a request with a JSON body, save the displayed body as `request.json`, then use its recipe's
path:

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/g2p"
```

PowerShell equivalent:

```powershell
$Api = "http://127.0.0.1:8000/api/v1"
$Body = Get-Content .\request.json -Raw
Invoke-RestMethod -Method Post -ContentType "application/json" -Body $Body -Uri "$Api/g2p"
```

You can instead paste the same body into the development Swagger UI at
<http://127.0.0.1:8000/docs>. Change the path in the command for each recipe. Response values
depend on the installed engine and pinned data; the checks below describe stable fields instead
of inventing exact linguistic output.

## Recipe index

| Goal | Browser | Public API |
| --- | --- | --- |
| Confirm the exact engine and runtime | `/capabilities` | `GET /api/v1/version`, `GET /api/v1/capabilities` |
| Create and export an immutable corpus | `/projects` | `POST /api/v1/projects`, project corpus/version routes |
| Transcribe text | `/g2p` | `POST /api/v1/g2p` |
| Inspect a PHOIBLE inventory | `/inventory` | `GET /api/v1/phonology/inventories/{identifier}` |
| Evaluate phonetic coverage | `/evaluate` | `POST /api/v1/evaluations` |
| Choose a compact sentence set | `/selection` | `POST /api/v1/selections` |
| Preview generation from a local pool | `/generation` | `POST /api/v1/generation/preview` |
| Submit and follow persisted work | `/jobs` | `POST /api/v1/runs`, run and event routes |
| Reproduce a core operation with CorpusGen | `/advanced` | `POST /api/v1/labs/cli/preview` |
| Smoke-test five writing systems | `/advanced` | `POST /api/v1/labs/demos/multilingual` |

The [CorpusKit and CorpusGen guide](corpusgen-relationship.md) explains which surface to choose
and what guarantees are added at each boundary.

## Check the deployed engine and capabilities

```bash
curl --fail-with-body --silent --show-error "$API/version"
curl --fail-with-body --silent --show-error "$API/capabilities"
```

`/version` reports the CorpusKit package version and its exact `corpusgen_contract`. In the
capability report, find the `corpusgen-core`, `espeak-g2p`, and `phoible` checks before running
core linguistic recipes. Optional optimization, repository, hosted, local-model, and GPU checks
describe this deployment; they are not claims about every CorpusKit installation.

Use `/health/ready` for an automated readiness gate. A `503` response includes the same bounded
capability report and means a required dependency is unavailable.

## Create, version, and export an immutable corpus

The browser path is the shortest: open <http://127.0.0.1:3000/projects>, select **Demo project**,
and create a corpus from
[`apps/web/e2e/fixtures/demo-corpus.txt`](../apps/web/e2e/fixtures/demo-corpus.txt).

The API path first creates a separate project.

<!-- recipe-request:project-create -->
```json
{
  "name": "English coverage cookbook",
  "description": "Immutable input used by the documentation recipes."
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/projects"
```

Copy the response `id` into `PROJECT_ID`. Create version 1 of a corpus inside that project:

<!-- recipe-request:corpus-create -->
```json
{
  "name": "Pangram candidates",
  "language": "en-us",
  "sentences": [
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog.",
    "Sphinx of black quartz, judge my vow.",
    "How vexingly quick daft zebras jump!"
  ]
}
```

```bash
PROJECT_ID=replace-with-project-id
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/projects/$PROJECT_ID/corpora"
```

The response has separate `corpus` and `version` objects. Record `corpus.id`, `version.id`,
`version.version_number`, and `version.content_sha256`. CorpusKit normalizes whitespace,
de-duplicates identical normalized rows, preserves original text, and returns the stored count.

Append a successor rather than editing version 1:

<!-- recipe-request:corpus-append -->
```json
{
  "language": "en-us",
  "sentences": [
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog.",
    "Sphinx of black quartz, judge my vow.",
    "How vexingly quick daft zebras jump!",
    "Waltz, bad nymph, for quick jigs vex."
  ]
}
```

```bash
CORPUS_ID=replace-with-corpus-id
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/projects/$PROJECT_ID/corpora/$CORPUS_ID/versions"
```

The new response must have `version_number: 2` and a `parent_version_id` equal to version 1. The
old version remains readable. Export either exact version as deterministic text:

```bash
VERSION_ID=replace-with-version-id
curl --fail-with-body --silent --show-error --dump-header export-headers.txt \
  --output pangrams.txt \
  "$API/projects/$PROJECT_ID/corpora/$CORPUS_ID/versions/$VERSION_ID/export?format=txt"
```

Keep the response's `ETag`, `Content-Digest`, and `X-Content-SHA256` headers with an exported
artifact when integrity matters. Use `format=json` or `format=csv` for the other deterministic
encodings. File import limits and CSV/JSON shapes are documented in
[project workspaces](product/project-workspaces.md).

## Transcribe a sentence with G2P

Open <http://127.0.0.1:3000/g2p> or send:

<!-- recipe-request:g2p -->
```json
{
  "text": "Sphinx of black quartz, judge my vow.",
  "language": "en-us"
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/g2p"
```

Verify that `text` and `language` match the request, `ipa` is non-empty, and `phonemes` contains
the tokenized transcription. `diphones`, `triphones`, `phoneme_count`, and `unique_phonemes` are
derived from that same result. Use `POST /api/v1/g2p/batch` with a `texts` array for up to 500
items; do not issue one request per sentence when a bounded batch fits.

## Inspect a PHOIBLE inventory

```bash
curl --fail-with-body --silent --show-error \
  "$API/phonology/inventories/en-us"
```

The identifier may be an eSpeak voice such as `en-us`, an ISO 639-3 code such as `eng`, or a
Glottocode. The result separates consonants, vowels, tones, marginal phonemes, allophones, and
distinctive features. If several PHOIBLE sources exist, first call:

```bash
curl --fail-with-body --silent --show-error \
  "$API/phonology/inventories/eng/sources"
```

Then add `?source=<returned-source>` to the inventory request. An unknown identifier returns
`404`; an invalid source returns `422`. The API never downloads PHOIBLE during a request.

## Evaluate a corpus against PHOIBLE

Open <http://127.0.0.1:3000/evaluate> or send:

<!-- recipe-request:evaluate -->
```json
{
  "sentences": [
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog.",
    "Sphinx of black quartz, judge my vow.",
    "How vexingly quick daft zebras jump!"
  ],
  "language": "en-us",
  "unit": "phoneme",
  "target": {
    "mode": "phoible",
    "phonemes": []
  }
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/evaluations"
```

`coverage` is a fraction from `0` to `1`. Inspect `covered_units`, `missing_units`,
`unit_counts`, `sentence_details`, and `unit_sources` before accepting a headline percentage.
The same response includes distribution and text-quality summaries when available.

Change `unit` to `diphone` or `triphone` to evaluate larger target spaces. Use `derived` target
mode to measure only units discovered in the supplied text, or `explicit` with a non-empty,
unique `phonemes` array for a curated target. Synchronous evaluation accepts at most 500
sentences; use a persisted run when you need job history, cancellation, or a manifest.

## Select a compact sentence set

Open <http://127.0.0.1:3000/selection> or send the same candidate pool to the CELF selector:

<!-- recipe-request:select -->
```json
{
  "candidates": [
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog.",
    "Sphinx of black quartz, judge my vow.",
    "How vexingly quick daft zebras jump!",
    "Waltz, bad nymph, for quick jigs vex.",
    "Glib jocks quiz nymph to vex dwarf."
  ],
  "language": "en-us",
  "unit": "phoneme",
  "target": {
    "mode": "phoible",
    "phonemes": []
  },
  "options": {
    "algorithm": "celf",
    "max_sentences": 4,
    "target_coverage": 0.85
  }
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/selections"
```

Review `selected_indices` and `selected_sentences` together; indices refer to the original
candidate order. Also inspect `missing_units`, `iterations`, and algorithm-specific `metadata`.
The sentence cap or available pool may stop the run below the requested coverage.

The core deployment supports greedy, CELF, stochastic, and distribution-aware selection.
ILP and NSGA-II require the optimization capability. Stochastic and NSGA-II durable runs require
an explicit `seed`; the synchronous API accepts one in `options.seed` when reproducibility
matters. Distribution-aware selection additionally requires a non-empty `target_distribution`.
The synchronous candidate limit is 2,000.

## Preview generation from a local repository

This bounded preview selects from caller-supplied, pre-phonemized candidates. It performs no
provider call, model load, or dataset download:

<!-- recipe-request:repository-preview -->
```json
{
  "source": {
    "kind": "prephonemized",
    "entries": [
      {
        "source_id": "pat",
        "text": "Pat tapped.",
        "phonemes": ["p", "a", "t"]
      },
      {
        "source_id": "bad",
        "text": "Bad dad.",
        "phonemes": ["b", "a", "d"]
      },
      {
        "source_id": "cab",
        "text": "A cab.",
        "phonemes": ["k", "a", "b"]
      }
    ]
  },
  "target": {
    "phonemes": ["p", "b", "t", "d"],
    "unit": "phoneme"
  },
  "stopping": {
    "target_coverage": 1.0,
    "max_sentences": 2,
    "max_iterations": 3,
    "timeout_seconds": 2.0
  },
  "candidates_per_iteration": 3,
  "activity_timeout_seconds": 10.0
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/generation/preview"
```

The response's `execution_mode` must be `"synchronous_preview"` and its `source_kind` must be
`"prephonemized"`. Inspect the ordered `accepted` rows, `coverage`, `missing_units`, iteration
count, and `stop_reason`; reaching a finite sentence or iteration cap below full coverage is a
valid outcome. Source IDs must be unique safe identifiers, and each `phonemes` array must already
contain tokenized units.

Raw local text is also supported with source kind `raw_text` and a language, in which case the
API uses eSpeak. Hugging Face sources are rejected by the preview route and require allowlisted,
immutable, durable repository generation. Hosted and local model generation is worker-only. See
[repository generation and scoring](operations/repository-generation-and-scoring.md) and
[model runtimes](operations/model-runtimes.md).

## Submit and follow a durable evaluation

`POST /api/v1/runs` persists an idempotent run request, but the basic `web` Compose profile without
`durable` has no dispatcher or worker and therefore does not execute queued runs. Before submitting
this recipe, start the [durable profile](getting-started.md#optional-use-durable-local-jobs) with
`CORPUSKIT_JOB_BACKEND=temporal`; it adds Temporal, the dispatcher, and the batch CPU worker. This
example uses the seeded local Demo project. Replace its `project_id` with your project ID if
desired.

<!-- recipe-request:durable-evaluate -->
```json
{
  "project_id": "00000000-0000-4000-8000-000000000003",
  "kind": "evaluate",
  "spec": {
    "sentences": [
      "Pack my box with five dozen liquor jugs.",
      "The quick brown fox jumps over the lazy dog."
    ],
    "language": "en-us",
    "unit": "phoneme",
    "target": {
      "mode": "phoible",
      "phonemes": []
    }
  }
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: cookbook-evaluate-v1' \
  --data @request.json \
  "$API/runs"
```

The first accepted submission returns `201`; replaying the identical request with the same key
returns `200` and the original run. Record its `id`, `state`, `spec_sha256`, and `outbox_state`.
Do not reuse an idempotency key for a different logical operation.

```bash
RUN_ID=replace-with-run-id
curl --fail-with-body --silent --show-error "$API/runs/$RUN_ID"
curl --fail-with-body --silent --show-error \
  "$API/runs/$RUN_ID/events?after=0&limit=100"
```

Advance `after` to the highest sequence you have processed. Events are ordered and bounded; poll
the run until it reaches `succeeded`, `failed`, or `cancelled`. Cancellation is
`POST /api/v1/runs/$RUN_ID/cancellation`. A terminal retry is
`POST /api/v1/runs/$RUN_ID/retries` with a new `Idempotency-Key` header.

If you add `corpus_version_id`, the inline sentences and language must normalize to the exact
stored version; CorpusKit rejects a decorative or mismatched lineage link. See the
[durable jobs runbook](operations/durable-jobs.md) for queue routing, deadlines, atomic input
limits, and crash semantics.

## Generate a CorpusGen CLI equivalent

This request validates an evaluation and returns a copy-only command preview:

<!-- recipe-request:cli-preview-evaluate -->
```json
{
  "workflow": "evaluate",
  "language": "en-us",
  "sentences": [
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog."
  ],
  "target": "phoible",
  "unit": "phoneme",
  "output_format": "json",
  "verbosity": "normal"
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/labs/cli/preview"
```

Treat `argv` as authoritative. `posix_command` and `powershell_command` are quoted display forms,
and `environment` includes the required UTF-8 setting. Read `reproducibility` and every warning
before running the command yourself. A preview performs no process, file, model, dataset, or
provider access and does not generate Python code. Direct CorpusGen CLI output is not a
CorpusKit run or artifact. See [CLI parity](product/cli-parity.md) for the exact gaps.

## Run the curated multilingual smoke test

The deployed Linux image is the normative runtime for this fixed catalogue:

<!-- recipe-request:multilingual-demo -->
```json
{
  "cases": [
    "latin-english",
    "arabic",
    "indic-devanagari",
    "cjk-mandarin",
    "tonal-vietnamese"
  ]
}
```

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data @request.json \
  "$API/labs/demos/multilingual"
```

Check top-level `passed`, `case_count`, and `passed_count`, then inspect every case. A failed case
has a stable `failure_code`, no coverage claim, and does not erase successful evidence from the
others. The request accepts only catalogue IDs, never arbitrary text. See the
[multilingual demo contract](product/multilingual-demo.md).

## Continue into advanced workflows

Advanced examples require deployment policy, immutable external inputs, or qualified hardware;
there is no safe universal credential or model placeholder that makes them runnable everywhere.
Use these runbooks after the core recipes pass:

- [Repository generation and deterministic scoring](operations/repository-generation-and-scoring.md)
- [Hosted and local model runtimes](operations/model-runtimes.md)
- [Phon-DATG indexing and generation](operations/phon-datg.md)
- [Phon-RL reward and training](operations/phon-rl.md)
- [Artifacts, manifests, and replay](operations/reproducibility-manifests-replay.md)
- [Qualified live-provider acceptance](operations/qualified-provider.md)

These documents distinguish validation-only HTTP routes from durable execution and identify the
worker profile, allowlist, secret reference, immutable revision, budget, and evidence required.

## Interpret errors consistently

- `401` means the shared deployment needs a valid bearer token; local demo mode supplies its
  fixed identity internally.
- `403` means the authenticated role cannot perform the operation.
- `404` means the resource is absent or outside the caller's tenant scope.
- `409` means immutable state or an idempotency key conflicts with the request.
- `413` means the body or upload exceeded the server limit.
- `422` means the typed request is invalid; correct the request rather than retrying it unchanged.
- `429` means a rate, quota, or budget limit was reached; honor the bounded `Retry-After` header
  when present.
- `502` means the pinned engine returned an incompatible result; treat it as a server compatibility
  defect rather than retrying the unchanged request.
- `503` means a required engine, data, storage, provider, or runtime capability is unavailable.

Send an `X-Request-ID` containing a short, non-sensitive identifier when correlating a request
with sanitized logs. Never put corpus text, prompts, tokens, credentials, model paths, or tenant
identifiers in that header.
