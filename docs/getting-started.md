# Getting started with CorpusKit

This guide takes a new contributor from a clean checkout to a working local CorpusKit website.
The recommended path uses Docker Compose and does not require Python, Node.js, npm, or eSpeak NG
on the host. A separate source-development path is included for people changing the application.
If you are deciding between the application and the underlying Python library, read
[CorpusKit and CorpusGen](corpusgen-relationship.md). After startup, use the
[recipe cookbook](recipes.md) for checked browser and API examples.

> **Local use only:** the Compose quick start uses deterministic development secrets and signs
> every browser in as the same demo owner. All published ports bind to `127.0.0.1`, but that is
> not authentication. Do not expose, forward, or tunnel this demo profile to other users. Use the
> production OIDC and deployment runbooks for any shared environment.

## Recommended: run the website with Docker

### 1. Install the prerequisites

You need:

- Git;
- Docker Desktop on Windows or macOS, or Docker Engine on Linux;
- Docker Compose v2 (`docker compose`, not the legacy `docker-compose` command);
- Linux containers and an internet connection for the first image build and PHOIBLE download.

The basic stack is qualified on Linux/AMD64. Allocate at least 2 CPUs, 4 GB of memory, and 10 GB
of free Docker disk space. Apple Silicon and other ARM64 hosts may work because the pinned base
images publish ARM64 manifests, but the complete application is not yet an ARM64 release gate.

Confirm that Docker is running:

```text
docker version
docker compose version
```

`docker version` must show both **Client** and **Server** sections; `docker compose version` only
needs to print a Compose v2 version. On Windows, switch Docker Desktop to Linux containers if it
is using Windows containers.

### 2. Clone and start CorpusKit

```text
git clone https://github.com/jemsbhai/corpuskit.git
cd corpuskit
docker compose --profile web up --build --detach --wait
```

No `.env` file is required for this isolated demo. On the first run, Docker downloads pinned base
images, builds the API and web images from the committed lockfiles, creates PostgreSQL and MinIO
volumes, applies database migrations, and downloads and verifies CorpusGen's pinned PHOIBLE
snapshot. A cold start can take several minutes.

Three one-shot services should finish with exit code `0`: `migrate`, `minio-init`, and
`provision-phoible`. Their exited state is expected. PostgreSQL, MinIO, the API, and the web app
should remain healthy and running.

### 3. Verify the startup

POSIX shell:

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/ready
docker compose --profile web ps --all
```

PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
docker compose --profile web ps --all
```

The readiness response must contain `"ready": true`. Then open:

- website: <http://127.0.0.1:3000>
- first live workspace: <http://127.0.0.1:3000/projects>
- development API documentation: <http://127.0.0.1:8000/docs>

There is no sign-up or password prompt in local demo mode. CorpusKit creates a `Demo user`, a
local organization, and an empty `Demo project` automatically.

### 4. Create your first corpus

1. Open <http://127.0.0.1:3000/projects> and select **Demo project**.
2. Under **Create an immutable corpus**, enter a corpus name and keep `en-us` as the language.
3. Either enter one sentence per line or choose **File import** and select
   [`apps/web/e2e/fixtures/demo-corpus.txt`](../apps/web/e2e/fixtures/demo-corpus.txt).
4. Select the new immutable version and inspect its ordered sentences and JSON, TXT, or CSV
   export.
5. Try the same sentences in **Evaluate**, **Selection**, or **G2P**. **Jobs** can submit a run and
   show its persisted history; in the basic profile it remains queued until you enable the
   [durable profile](#optional-use-durable-local-jobs).

The landing page's Riverbend values are illustrative. Results produced in the workbenches above
come from the running API.

Continue with the [recipe cookbook](recipes.md) to create and append immutable corpus versions,
call G2P, inspect PHOIBLE, evaluate and select sentences, preview local repository generation,
submit a persisted run, generate a CorpusGen CLI preview, and run the multilingual smoke test.

## What the basic Compose profile runs

| Component           | Purpose                                   | Host address            |
| ------------------- | ----------------------------------------- | ----------------------- |
| `web`               | Next.js website and authenticated BFF     | <http://127.0.0.1:3000> |
| `api`               | FastAPI and CorpusGen adapter             | <http://127.0.0.1:8000> |
| `postgres`          | Persistent local application database     | `127.0.0.1:5432`        |
| `minio`             | Persistent S3-compatible artifact storage | `127.0.0.1:9000`        |
| MinIO console       | Local storage inspection                  | <http://127.0.0.1:9001> |
| `migrate`           | One-shot Alembic database upgrade         | exits `0`               |
| `minio-init`        | One-shot private bucket initialization    | exits `0`               |
| `provision-phoible` | One-shot pinned PHOIBLE provisioning      | exits `0`               |

The basic profile persists run submissions and history but has no dispatcher or worker, so it does
not execute queued runs. It deliberately omits Temporal, hosted-provider access, local-model
caches, and GPU execution.

## Stop, restart, update, and reset

Stop the containers while preserving all local data:

```text
docker compose --profile web down
```

Start the same data again:

```text
docker compose --profile web up --detach --wait
```

After pulling application changes, rebuild and run the migrations again:

```text
git pull --ff-only
docker compose --profile web up --build --detach --wait
```

PostgreSQL data, MinIO objects, artifacts, and the PHOIBLE snapshot live in named Docker volumes.
`docker compose down` preserves them.

To perform a completely clean reset, first make sure that the Compose project shown by
`docker compose ls` is the local CorpusKit demo. Then run:

```text
docker compose --profile web down --volumes --remove-orphans
```

> **Destructive reset:** `--volumes` permanently removes the local CorpusKit database, corpora,
> artifacts, and downloaded PHOIBLE snapshot for this Compose project. The next startup downloads
> and provisions everything again.

## Troubleshooting

### Docker cannot connect to the daemon

Start Docker Desktop or the Docker Engine service, then rerun `docker version`. Seeing only the
client section is not enough; a server section must be present.

### A host port is already in use

The application containers communicate over their private Compose network, so changing a host
port does not change internal service configuration. Set only the conflicting mappings before
starting.

POSIX example:

```bash
CORPUSKIT_POSTGRES_PORT=55432 \
CORPUSKIT_MINIO_PORT=19000 \
CORPUSKIT_MINIO_CONSOLE_PORT=19001 \
CORPUSKIT_API_PORT=18000 \
CORPUSKIT_WEB_PORT=13000 \
docker compose --profile web up --build --detach --wait
```

PowerShell example:

```powershell
$env:CORPUSKIT_POSTGRES_PORT = "55432"
$env:CORPUSKIT_MINIO_PORT = "19000"
$env:CORPUSKIT_MINIO_CONSOLE_PORT = "19001"
$env:CORPUSKIT_API_PORT = "18000"
$env:CORPUSKIT_WEB_PORT = "13000"
docker compose --profile web up --build --detach --wait
```

With those example values, use <http://127.0.0.1:13000> for the website and
<http://127.0.0.1:18000/api/v1/health/ready> for readiness. Environment changes apply only to the
shell in which they were set. Remove them or open a new shell to restore the defaults.

### Startup does not become healthy

Inspect the resolved services and recent logs:

```text
docker compose --profile web config --services
docker compose --profile web ps --all
docker compose --profile web logs --since=15m postgres migrate minio minio-init provision-phoible api web
```

Common causes are an older Compose version without `--wait` support, insufficient Docker disk or
memory, blocked registry/PyPI/npm access during the image build, or blocked GitHub access during
PHOIBLE provisioning. Fix the cause and rerun the same `up --build --detach --wait` command; the
one-shot setup operations are idempotent.

### PHOIBLE is unavailable

The API never downloads PHOIBLE during a request. Check the one-shot provisioner explicitly:

```text
docker compose logs provision-phoible
docker compose run --rm --no-deps provision-phoible status --json
```

See the [PHOIBLE provisioning runbook](operations/phoible-provisioning.md) for checksum failures,
air-gapped installation, and recovery.

### ARM64 build or startup fails

The current release path is qualified on Linux/AMD64. Docker Desktop users on ARM64 can try its
AMD64 emulation by setting `DOCKER_DEFAULT_PLATFORM=linux/amd64` before building. Treat a native
ARM64 run as development-only until the repository adds an ARM64 acceptance gate.

## Optional: use durable local jobs

The basic demo is enough for first use. To add the local Temporal server, dispatcher, and CPU
worker, stop the basic stack and start both profiles with the Temporal backend.

POSIX shell:

```bash
docker compose --profile web down
CORPUSKIT_JOB_BACKEND=temporal \
docker compose --profile web --profile durable up --build --detach --wait
```

PowerShell:

```powershell
docker compose --profile web down
$env:CORPUSKIT_JOB_BACKEND = "temporal"
docker compose --profile web --profile durable up --build --detach --wait
```

The Temporal UI is then available at <http://127.0.0.1:8233>. Read the
[durable-jobs runbook](operations/durable-jobs.md) before enabling hosted, local-model, or GPU
profiles; those profiles require explicit allowlists, credentials, caches, or qualified hardware.

## Develop directly from source

Use this path only when changing Python or web code. The Docker quick start above remains the
simplest way to evaluate the application.

### Host requirements

- Python `>=3.12,<3.13`;
- [`uv`](https://docs.astral.sh/uv/);
- Node.js `>=24.18.1,<25` (the repository includes [`.nvmrc`](../.nvmrc));
- npm `11.16.0`;
- eSpeak NG available on `PATH` or installed through the platform's supported mechanism.

Install eSpeak NG with the OS package manager on Linux/macOS or a current Windows build from the
[eSpeak NG releases](https://github.com/espeak-ng/espeak-ng/releases). Verify it with
`espeak-ng --version`.

### One-time setup

POSIX shell:

```bash
uv sync --frozen --all-groups
npm ci
cp .env.example .env
cp apps/web/.env.example apps/web/.env.local
mkdir -p data artifacts
uv run corpuskit-phoible provision --json
uv run corpuskit-phoible status --json
CORPUSKIT_DATABASE_URL=sqlite+aiosqlite:///./data/corpuskit.db \
uv run corpuskit-db upgrade
```

PowerShell:

```powershell
uv sync --frozen --all-groups
npm ci
Copy-Item .env.example .env
Copy-Item apps/web/.env.example apps/web/.env.local
New-Item -ItemType Directory -Force data, artifacts
uv run corpuskit-phoible provision --json
uv run corpuskit-phoible status --json
$env:CORPUSKIT_DATABASE_URL = "sqlite+aiosqlite:///./data/corpuskit.db"
uv run corpuskit-db upgrade
```

The copied files contain deterministic demo-only settings and are ignored by Git. The root
`.env` configures the Python API. Next.js loads `apps/web/.env.local`; it does not load the root
file when the workspace script runs. The migration CLI deliberately reads only the process
environment, which is why the commands above set `CORPUSKIT_DATABASE_URL` explicitly.

### Start two development terminals

Terminal 1, from the repository root:

```text
uv run corpuskit-api
```

Terminal 2, also from the repository root:

```text
npm run dev
```

Keep both processes running. Verify the API at
<http://127.0.0.1:8000/api/v1/health/ready>, then open <http://127.0.0.1:3000/projects>.

The direct-development defaults use SQLite, filesystem artifacts, in-process sessions, and demo
identity. The two-process setup can persist run submissions but does not execute them; use the
optional durable Compose profile for execution. It does not provide PostgreSQL RLS, shared
sessions, or production worker recovery and is not a production deployment.

## Run the live acceptance walkthrough

Starting the website does not require host Node.js or Playwright. If you want to execute the
fixed-input browser acceptance suite, install the exact Node/npm versions above and follow the
[15-minute live demo](product/15-minute-demo.md). That guide adds `npm ci`, a Chromium install,
and the live Playwright command to an already understood local stack.

## Requirements and container inventory

CorpusKit intentionally does not use a hand-maintained `requirements.txt`:

- [`pyproject.toml`](../pyproject.toml) declares Python packages and optional worker groups;
- [`uv.lock`](../uv.lock) freezes the complete Python dependency graph;
- [`package.json`](../package.json), [`apps/web/package.json`](../apps/web/package.json), and
  [`package-lock.json`](../package-lock.json) define and lock Node dependencies;
- [`docker/api.Dockerfile`](../docker/api.Dockerfile) builds the API, migration, provisioning,
  dispatcher, and maintenance runtime;
- [`docker/web.Dockerfile`](../docker/web.Dockerfile) builds the Next.js server;
- [`docker/worker.Dockerfile`](../docker/worker.Dockerfile) contains the batch, external-provider,
  GPU-inference, and GPU-training worker targets;
- [`docker/mutation.Dockerfile`](../docker/mutation.Dockerfile) is test-only;
- [`compose.yaml`](../compose.yaml) wires the local profiles, health checks, one-shot setup, and
  persistent volumes together.

Do not generate a second requirements file from the lock. Use `uv sync --frozen` or the
Dockerfiles so local and CI dependency resolution stays identical.

## Shared and production deployments

Docker Compose is an isolated development/demo topology, not a public hosting recipe. A shared
deployment requires external OIDC, encrypted Redis/Valkey browser sessions, PostgreSQL roles and
RLS, private S3 storage, TLS Temporal, separate dispatcher/worker identities, secret management,
maintenance scheduling, ingress, and monitoring. Start with the
[Kubernetes production runbook](operations/kubernetes-production.md) and
[OIDC authentication runbook](operations/oidc-authentication.md). The repository is still an
alpha and does not claim completed production promotion evidence.
