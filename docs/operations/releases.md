# Release, signing, and promotion

CorpusKit has a build-once release-candidate pipeline. It does **not** mean that a production
release, GHCR publication, PyPI publication, or staging deployment has occurred. Those actions
require the external repository controls and acceptance evidence in this runbook.

The release boundary is an annotated, GitHub-verified SemVer tag. The supported tag forms are
`vMAJOR.MINOR.PATCH` and `vMAJOR.MINOR.PATCH-(alpha|beta|rc).N`. A release workflow can run only
on a tag push; it has no manual-dispatch or branch trigger and refuses an existing GitHub release
or GHCR version tag. Workflow reruns are rejected so a failed run that published any image
reserves that version permanently.
Do not delete or overwrite it to retry; fix the fault and cut a new version.

## Published candidate set

`.github/workflows/release.yml` builds the Python distributions and these Linux/AMD64 images
once from the same commit:

| Component                | GHCR package                                       | Dockerfile target               |
| ------------------------ | -------------------------------------------------- | ------------------------------- |
| API                      | `ghcr.io/OWNER/corpuskit-api`                      | `docker/api.Dockerfile:runtime` |
| Web                      | `ghcr.io/OWNER/corpuskit-web`                      | `docker/web.Dockerfile:runtime` |
| Batch CPU worker         | `ghcr.io/OWNER/corpuskit-worker-batch`             | `worker-batch`                  |
| External-provider worker | `ghcr.io/OWNER/corpuskit-worker-external-provider` | `worker-external-provider`      |
| GPU inference worker     | `ghcr.io/OWNER/corpuskit-worker-gpu-inference`     | `worker-gpu-inference`          |
| GPU training worker      | `ghcr.io/OWNER/corpuskit-worker-gpu-training`      | `worker-gpu-training`           |

The release assets contain one wheel, one sdist, SPDX JSON and CycloneDX JSON SBOMs for the
distribution set and every image, Cosign bundles for file signatures, GitHub/Sigstore
attestation bundles, `SHA256SUMS`, and `release-manifest.json`. The manifest is the authority for
image promotion. Deployment configuration must consume its `image@sha256:...` references and
must never substitute a mutable channel or version tag.

All Docker base-image references are pinned to OCI index digests in the Dockerfiles. Python
images use a package-aware Ubuntu 24.04 base and pin the installed Python 3.12, eSpeak NG,
certificate, and account-tool package revisions. The build still consumes Ubuntu package
repositories, so a later rebuild is not claimed to be bit-for-bit reproducible. The control is
to build once, record provenance, scan and test the resulting digest, and promote that digest
unchanged. The Python distribution build remains pinned separately to Python 3.12.13; container
runtimes use Ubuntu's supported, security-backported Python 3.12 package. Upstream Ubuntu and
Node official images currently have no CorpusKit-owned Sigstore identity policy in this
repository; approving new base digests or runtime package revisions remains an independently
reviewed dependency change. The web build verifies the pinned npm release in its build stage;
the standalone production runtime contains Node but removes npm and npx because it does not
install packages at runtime.

## One-time GitHub configuration

An administrator must complete and record all of these controls before creating a tag:

1. Enable GitHub **immutable releases**, have a repository administrator verify the setting, and
   set the repository variable `IMMUTABLE_RELEASES_CONFIGURED=true`. GitHub's read endpoint for
   this setting requires repository-administration access, which the least-privileged workflow
   token intentionally does not have. The variable is a pre-build acknowledgement; the workflow's
   authoritative check is `gh release verify` immediately after publication. Immutable
   publication locks the tag and assets and creates GitHub's release attestation.
2. Add an active tag ruleset for `v*` that restricts creation, updates, and deletion to release
   managers. Require signed commits where the repository policy permits it and block force
   pushes. Immutable releases lock a tag only after release publication; the ruleset protects
   the build window.
3. Create `release`, `staging`, `production`, and `pypi` GitHub environments. Each must have a
   required reviewer, prevent self-review, disallow administrator bypass, and restrict deployment
   tags to the release pattern. The workflows reject a missing environment or a reviewer rule
   that permits self-review.
4. Ensure the GitHub plan supports artifact attestations for the repository visibility. Public
   repositories use Sigstore's public-good instance and transparency log; private/internal
   repositories require GitHub Enterprise Cloud and use GitHub's private Sigstore instance.
5. Permit the repository `GITHUB_TOKEN` to write the repository-linked GHCR packages and
   attestations. Do not add a PAT, registry password, signing key, or PyPI token.
6. Protect `.github/workflows/release.yml`, `quality-scheduled.yml`, `publish-pypi.yml`,
   `verify-promotion.yml`, Dockerfiles, lockfiles, and `scripts/release/` with CODEOWNERS and
   independent review.
7. Make `ci.yml` a required check and keep manual dispatch enabled for `quality-scheduled.yml`.
   A tag is accepted only when both workflows have a successful run for the exact tagged commit.

GitHub documents [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
[artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations),
and [protected environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

## Pinned release toolchain

Every action reference is a full 40-character commit SHA. The adjacent version comment is for
review and update automation only. These versions and their release commits were checked against
the maintainers' GitHub releases on 2026-08-11:

| Tool/action                              | Reviewed version      |
| ---------------------------------------- | --------------------- |
| `actions/checkout`                       | 7.0.1                 |
| `actions/setup-python`                   | 7.0.0                 |
| `actions/setup-node`                     | 7.0.0                 |
| `actions/upload-artifact`                | 7.0.1                 |
| `actions/download-artifact`              | 8.0.1                 |
| `actions/attest`                         | 4.2.2                 |
| `actions/dependency-review-action`       | 5.0.0                 |
| `github/codeql-action`                   | 4.37.6                |
| `astral-sh/setup-uv` / uv                | 9.0.0 / 0.12.3        |
| Docker login / setup-buildx / build-push | 4.6.0 / 4.2.0 / 7.3.0 |
| Buildx                                   | 0.36.1                |
| Anchore SBOM action / Syft               | 0.24.0 / 1.51.0       |
| Cosign installer / Cosign                | 4.1.2 / 3.1.3         |
| Trivy action / Trivy                     | 0.36.0 / 0.73.0       |
| PyPA publish action                      | 1.14.2                |

When updating an action, verify the release in the upstream repository, resolve its tag to a
commit, review its action metadata and transitive actions, update the full SHA and version
comment together, and run `tests/release/test_release_contract.py`. A tag, branch, shortened SHA,
or unreviewed action is release-blocking.

The required CI workflow applies the same full-SHA rule. Its PostgreSQL 17.9 and Temporal 1.8.2
service images are pinned to OCI index digests. Before the PGDG signing key can enter APT trust,
CI downloads it to a temporary path, requires exactly one primary key with full fingerprint
`B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8`, runs a wrong-fingerprint negative control, and only
then installs the key. The identity is the one in PostgreSQL's
[repository announcement](https://www.postgresql.org/message-id/m2ip8ep30p.fsf%402ndQuadrant.fr);
TLS or the short `ACCC4CF8` key ID alone is not treated as identity proof.

## Preparing a candidate

1. Update all three versions together:
   `pyproject.toml` uses PEP 440 (`0.1.0a1`), while `package.json` and
   `apps/web/package.json` use SemVer (`0.1.0-alpha.1`).
2. Move shipped entries from `Unreleased` into a dated
   `## [0.1.0-alpha.1] - YYYY-MM-DD` changelog section. The workflow rejects a missing section.
3. Regenerate and review `uv.lock` and `package-lock.json`; run every required CI, nightly, and
   applicable release-profile test. Record manual accessibility, GPU, provider, migration,
   restore, performance, DAST, and operational evidence separately. The artifact workflow does
   not manufacture or waive that evidence.
4. Run the local packaging contract:

   ```bash
   uv lock --check
   build_environment=.venv-build
   UV_PROJECT_ENVIRONMENT="${build_environment}" \
     uv sync --frozen --only-group build --no-install-project
   source "${build_environment}/bin/activate"
   python -c \
     'import importlib.metadata as metadata; assert metadata.version("hatchling") == "1.32.0"'
   uv build --no-sources --no-build-isolation --no-index --out-dir dist
   deactivate
   python scripts/release/release_contract.py versions --tag v0.1.0-alpha.1
   python scripts/release/release_contract.py distributions \
     --directory dist --tag v0.1.0-alpha.1
   uv run pytest tests/release/test_release_contract.py
   ```

   The exact Hatchling version is duplicated in `[build-system].requires` for standard
   PEP 517 consumers and in the locked `build` dependency group for release construction.
   Release builds first sync only that frozen group into a disposable, dedicated environment,
   explicitly activate and verify it, then disable PEP 517 build isolation and the package
   index; they must never resolve a build backend or its transitive dependencies from the
   network.

5. Merge the independently approved version change to the protected default branch and wait for
   exact-SHA `ci.yml` and `quality-scheduled.yml` success. If the scheduled run does not cover the
   candidate commit, manually dispatch the quality workflow while that commit is the selected
   default-branch revision; do not create the tag until both exact-SHA runs are green. The
   scheduled run itself requires successful broad CI for that SHA and repeats the backend,
   linguistic, frontend/three-browser, performance, mutation, and scheduled security gates.
   Release preflight also requires the reviewed
   `benchmarks/baselines/github-actions-ubuntu-24.04-x64.v1.json`: it must have a valid schema,
   the exact expected profile, clean Git provenance, and a source revision ancestral to the
   candidate. There is no release bootstrap exception.
6. Create and push an annotated signed tag. Confirm GitHub displays it as Verified before
   approving the `release` environment:

   ```bash
   git tag -s v0.1.0-alpha.1 -m "CorpusKit v0.1.0-alpha.1"
   git push origin v0.1.0-alpha.1
   ```

The workflow checks a clean source tree, default-branch ancestry, exact version agreement,
GitHub's tag verification record, exact-SHA CI and scheduled-quality runs, package archive
integrity, console entry points, installed resources, image tag-to-digest identity, non-root
read-only container execution, offline real eSpeak/checksum-verified PHOIBLE behavior from each
exact Python digest, and Critical/High findings in the exact built images. It creates both SBOM
formats, keylessly signs files and images with GitHub OIDC/Sigstore, creates build-provenance and
SBOM attestations, and verifies every signature and attestation before publishing a draft release.
Publishing the draft is the final operation; immutable-release and asset verification then fail
closed if repository immutability is not active.

Trivy and GitHub advisory results are point-in-time security observations, not deterministic
proof that a future advisory database remains unchanged. SBOM license fields are evidence for
review; this repository does not yet claim a complete automated license allow/deny policy.
Source secret and IaC/misconfiguration checks run in exact-SHA CI and are repeated by the
required scheduled-quality workflow, while CodeQL runs in exact-SHA CI. Dependency review
remains pull-request evidence. These scans are not duplicated under the signing-capable release
job.

## Consumer verification

Use a current GitHub CLI and Cosign. Never verify with an identity wildcard.

```bash
tag=v0.1.0-alpha.1
repo=jemsbhai/corpuskit
gh release verify "$tag" --repo "$repo"
mkdir "corpuskit-$tag" && cd "corpuskit-$tag"
gh release download "$tag" --repo "$repo"
for asset in *; do gh release verify-asset "$tag" "$asset" --repo "$repo"; done
sha256sum --check SHA256SUMS
cosign verify-blob SHA256SUMS \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity "https://github.com/$repo/.github/workflows/release.yml@refs/tags/$tag" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify corpuskit_app-*.whl --repo "$repo"
```

Read each image reference from `release-manifest.json`, then verify it:

```bash
jq -r '.images[].reference' release-manifest.json | while read -r image; do
  cosign verify "$image" \
    --certificate-identity "https://github.com/$repo/.github/workflows/release.yml@refs/tags/$tag" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  gh attestation verify "oci://$image" --repo "$repo"
  gh attestation verify "oci://$image" --repo "$repo" \
    --predicate-type https://spdx.dev/Document/v2.3
done
```

## Promotion and rollback

`verify-promotion.yml` is a verification and approval record, not a cloud-specific deployment
adapter. It never invokes a Docker or Python build and never creates a new OCI manifest. Supply
the immutable tag, target environment, non-secret HTTPS acceptance/change permalinks (standard
port, with no userinfo, query, or fragment), and a lower rollback
tag for production. After environment approval it verifies the GitHub immutable release, every
asset, checksum, blob and image signature, build/SBOM attestations, and every version tag against
the recorded digest. It emits a retained `promotion-plan.json` containing only digest references.

The deployment system must consume that plan unchanged. Staging must complete the full release
profile and 24-hour soak before production approval. Production uses a 5% canary for at least 30
minutes and the automatic rollback thresholds in `slo.md`. A rollback selects the already signed
lower-version manifest; it does not rebuild or retag. Confirm database backward compatibility
and follow `database-migrations.md` before rolling application images back. Never reverse an
irreversible data migration by replacing containers.

## PyPI Trusted Publishing

Python publication is separate and manual. `release.yml` never uploads to PyPI.
Before setting `PYPI_TRUSTED_PUBLISHER_CONFIGURED=true` as a repository variable:

1. Create or claim the `corpuskit-app` PyPI project.
2. Configure its GitHub Trusted Publisher with owner `jemsbhai`, repository `corpuskit`, workflow
   filename `publish-pypi.yml`, and environment `pypi`.
3. Protect that environment as described above, require an independent reviewer, prevent
   self-review, and disallow bypass.
4. Confirm no `PYPI_TOKEN`, username, password, or long-lived publishing secret exists.

Then manually run **Publish verified Python distributions**, provide the immutable tag, and type
the requested exact confirmation. Its preparation job downloads the GitHub release, verifies
immutability, checksums, Cosign signatures, GitHub provenance, and package contents, then passes
only the wheel and sdist to the approval-protected job. The OIDC-enabled job has exactly two
steps: download the already verified workflow artifact and invoke the pinned PyPA action. It does
not check out source or rebuild. PyPA generates and publishes package-index attestations by
default. PyPI documents the [Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
and its [security model](https://docs.pypi.org/trusted-publishers/security-model/).

## Evidence and remaining external gates

Local validation proves workflow syntax, pins, package contents, version normalization, base
digest presence, and build/promotion separation. It does not prove that GitHub environments,
GHCR, Sigstore, PyPI, staging, or production are configured. Before production promotion attach:

- the successful immutable release run plus exact-SHA CI, the daily/manual automated-nightly
  scheduled-quality run, and the separately recorded external release-profile evidence;
- real qualified CUDA DATG/RL, the bounded live-provider evidence defined by
  [`qualified-provider.md`](qualified-provider.md), and vendor OIDC/TLS Redis evidence;
- full PostgreSQL/MinIO/Temporal migration, backup, restore, and rollback results;
- DAST, manual NVDA/VoiceOver, performance, SLO, dashboard, alert, and 24-hour soak evidence;
- open-defect/waiver review, named approvers, canary plan, and on-call owner; and
- confirmation that every production deployment reference equals a manifest digest.

Actions artifacts are time-limited transfer records. Qualified provider/GPU evidence must also be
copied byte-for-byte to an access-controlled immutable/WORM archive; the release record retains
each SHA-256 and permanent read-only permalink, and promotion reviewers re-check the embedded
candidate and image identities. This archive is an external prerequisite, not a repository-local
claim.

Until those records exist, the result is a verifiable release candidate, not a production-ready
or PyPI-published release.
