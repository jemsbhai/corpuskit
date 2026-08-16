# Contributing

CorpusKit uses traceable requirements and test-first acceptance. Before implementing a
feature, add or update its row in `docs/product/capability-matrix.md` and identify the
automated acceptance scenario that proves it.

## Local checks

```bash
uv lock --check
uv run corpuskit-phoible provision --json
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run pytest tests/release/test_release_contract.py
docker run --rm -v "$PWD:/repo" -w /repo \
  rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
GH_TOKEN="$(gh auth token)" uvx --from zizmor==1.29.0 zizmor --pedantic .github/workflows
npm ci
npm run format:check
npm run lint
npm run typecheck
npm run test
npm run build
```

No required check may use `continue-on-error`. New production code must be typed, must
not introduce a new cross-tenant data path, and must include tests for its failure modes.

## Documentation examples

The [recipe cookbook](docs/recipes.md) is the task-oriented entry point and
[CorpusKit and CorpusGen](docs/corpusgen-relationship.md) owns the cross-project boundary. Mark a
copy/paste JSON body with `<!-- recipe-request:name -->` immediately before its fenced block and
add its real DTO validator to `tests/architecture/test_documentation_contract.py`. That test also
keeps recipe routes on mounted `/api/v1` paths, resolves relative links, and derives the documented
CorpusGen compatibility version from `pyproject.toml`.

Do not document router-local paths as public endpoints, promise a capability that the matrix does
not mark available, or make an example depend on a sibling CorpusGen checkout. Examples for
credentials, tokens, datasets, or models must use unmistakable non-secret placeholders and state
the policy or runtime profile required to execute them.

## CorpusGen boundary

Imports of `corpusgen` are permitted only below `src/corpuskit/adapters/corpusgen/`.
Upgrade the exact pin only in a dedicated compatibility change that runs the full adapter
contract suite and explicitly reviews golden output differences.

## Commits and reviews

Use focused Conventional Commit subjects. Pull requests must explain the user outcome,
security and data implications, acceptance evidence, and rollback plan. Release changes
require an independent review.

## Release and supply-chain changes

Changes to release workflows, `scripts/release/`, Docker base digests, dependency lockfiles, or
artifact verification are security-sensitive. GitHub Actions must use a full 40-character commit
SHA with its reviewed version in a comment; mutable tags and branches are not accepted. Do not
add long-lived registry, signing, or PyPI credentials.

Version changes update `pyproject.toml`, the root `package.json`, the web `package.json`, and the
dated changelog section together. Never rebuild, overwrite, or retag a partially published
version. Release managers follow the independent-review, signed-tag, immutable-release,
environment-approval, build-once promotion, and rollback process in
[`docs/operations/releases.md`](docs/operations/releases.md).
