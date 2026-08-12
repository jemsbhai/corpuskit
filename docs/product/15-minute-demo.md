# CorpusKit 15-minute live demo

This runbook demonstrates the real CorpusKit web, API, PostgreSQL, MinIO, eSpeak, PHOIBLE,
selection, scoring, job-control, quota, and audit surfaces. The automated acceptance path uses no
Playwright request interception and fails on any API 5xx response. Hosted providers, GPU models,
DATG, and Phon-RL remain deployment-gated and are not required for this CPU demo.

## Before the clock starts

Use Docker with Compose, Node.js 24 LTS, npm 11, and a clean checkout. Cold image builds and the
pinned PHOIBLE download can take longer than 15 minutes; pre-pull/build when presenting live.
The Compose posture is an isolated local demo identity, not a shared or production deployment.

```bash
npm ci
npx playwright install chromium
docker compose --profile web build
```

The fixed input is
[`apps/web/e2e/fixtures/demo-corpus.txt`](../../apps/web/e2e/fixtures/demo-corpus.txt). Do not
substitute ad hoc text during acceptance; identical input makes screenshots and result review
comparable between runs.

## Start and verify (minutes 0–2)

POSIX:

```bash
docker compose --profile web up -d --wait
curl --fail http://127.0.0.1:8000/api/v1/health/ready
CORPUSKIT_LIVE_BASE_URL=http://127.0.0.1:3000 npm run test:e2e:live --workspace @corpuskit/web
```

PowerShell:

```powershell
docker compose --profile web up -d --wait
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
$env:CORPUSKIT_LIVE_BASE_URL = "http://127.0.0.1:3000"
npm run test:e2e:live --workspace @corpuskit/web
```

The automated run is the executable acceptance record. It creates uniquely named demo data and
leaves it visible for the guided review below. A passing run proves that requests reached the
external stack; it does not prove live provider, qualified GPU, vendor IdP, TLS Redis, or
multi-replica behavior.

## Guided review (minutes 2–15)

1. **Project and immutable source (2 minutes).** Open <http://127.0.0.1:3000/projects>. Select the
   newest `CorpusKit demo ...` project. Show the fixed corpus, immutable version number, sentence
   order, content SHA-256, and JSON/TXT/CSV exports. Change pages and point out that the global
   project picker keeps the same project.
2. **Evaluation evidence (2 minutes).** Open `/evaluate`, paste the fixed fixture, keep `en-us`,
   phoneme, and derived target, then choose **Evaluate corpus**. Review live coverage, counts,
   missing units, distribution quality, and source-sentence evidence. The landing-page Riverbend
   numbers are explicitly fixed orientation data; this table is the live result.
3. **Selection comparison (2 minutes).** Open `/selection`, paste the same fixed fixture, and run
   GREEDY, CELF, STOCHASTIC, and DISTRIBUTION without changing candidates or target. When
   Optimization is reported available, also run ILP and NSGA-II. The tray retains all six
   responses and labels optional algorithms as gated when the deployment lacks them.
4. **Scoring controls (2 minutes).** Open `/generation` → **Composite scoring**. Set coverage
   weight `2`, readability weight `1`, and readability target `40–85`; rank candidates. Review the
   returned composite, coverage-gain, phonotactic, fluency, and readability columns. Fluency is
   zero in this deterministic no-model surface; the **Advanced** fluency/perplexity run demonstrates
   exact-policy offline model-backed composite ranking when a local worker is configured.
5. **Durable control plane and artifacts (2 minutes).** Open `/jobs`, wait for all ordered rows of
   the selected immutable version to be verified, submit the default typed phonemize run, refresh,
   and inspect its version lineage plus state/events. If a terminal run exposes an artifact link,
   follow it: `/artifacts?artifact=<uuid>` fetches that exact artifact even when it is outside the
   current list page. The core demo uses the inline backend; use the durable profile runbook when
   demonstrating Temporal process recovery.

   The fixture has six rows and fits every atomic run contract. CorpusKit will not silently
   truncate a larger selected version: phonemize/evaluate stop at 500 rows and select at 2,000.
   Import an explicit bounded derived version for one atomic run; a complete larger version needs
   a future chunked-job contract.

6. **Provenance and deployment truth (2 minutes).** Open `/inventory` and show the exact PHOIBLE
   revision plus SHA-256. Open `/capabilities`, refresh checks, and distinguish API-process
   dependency detection from worker health. As the demo owner, show current usage against
   server-owned quota and ordered, hash-linked audit events.
7. **Close on boundaries (1 minute).** In `/advanced`, show that hosted consent begins unchecked
   and resets after configuration changes, the browser accepts no credentials, empty DATG
   catalogs synthesize no cache key, and only validated requests can enter the durable control
   plane.

## Pass/fail record

Pass only when the live Playwright command exits zero and the guided review shows:

- one shared active project across routes and one immutable fixed corpus digest;
- live evaluation plus at least the four core selectors on identical input;
- explicit scoring weights with a backend result table;
- a real submitted CPU run, PHOIBLE revision/digest, refreshed capability checks, quota usage,
  and audit rows;
- no intercepted/mock API responses, browser accessibility violations, or API 5xx responses.

Record the git revision, container image digests, command output, and Playwright trace/report with
the release candidate. Remove the isolated demo data through the exact project-deletion control
when it is no longer needed; its retention and audit record are intentional.

For failures:

```bash
docker compose --profile web ps --all
docker compose --profile web logs --since=15m api web postgres minio provision-phoible
```

Do not replace a failed live run with the mocked cross-browser suite. The two gates establish
different evidence.
