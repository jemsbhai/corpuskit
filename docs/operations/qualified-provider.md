# Qualified live-provider runbook

`qualified-provider.yml` is the only release qualification path that intentionally spends
provider credits. It is manual-only, runs one fixed low-cost CorpusKit/CorpusGen generation
fixture, and retains a schema-validated redacted JSON record. No credentialed run has been
performed for the initial repository state; the workflow and fake-client tests are implementation
evidence only.

The retained JSON schema, adversarial checks, and interpretation limits are specified in the
[qualified provider evidence contract](../quality/qualified-provider-evidence.md).

## One-time repository and runner setup

Create a GitHub environment named `qualified-provider` with all of these controls:

- required reviewers who can verify the candidate, selected provider/model, and current public
  input/output prices;
- deployment branch/tag restrictions matching the release process;
- administrator bypass disabled where the GitHub plan supports it; and
- one environment secret named `QUALIFIED_PROVIDER_API_KEY`.

Use a dedicated, least-privilege canary credential with a provider-side spend limit and no access
to production data. Do not use a personal or production application key. The workflow never
accepts a credential as a dispatch input or command-line argument.

The self-hosted Linux runner must carry the labels `corpuskit-qualified` and `provider-egress`.
Provision a Docker network named `corpuskit-qualified-provider-egress`, give it the label
`corpuskit.egress-policy=qualified-provider`, and enforce DNS/IP/TLS egress outside the workflow so
containers on that network can reach only the reviewed provider endpoints. A Docker label is an
identity check, not a firewall; the runner owner must implement and audit the actual network rule.
The runner also needs Docker, GNU `timeout`, and enough resources to build the exact
`worker-external-provider` production target.

Disable provider SDK debug logging and organization-wide LiteLLM callbacks on this runner. The
adapter rejects active LiteLLM success, failure, async, or verbose callback state, but runner and
provider account logging remain operator responsibilities.

## Review and dispatch

Reviewers must verify each input before approving the protected environment deployment:

- `candidate_sha`: the full lowercase 40-character reviewed release-candidate commit;
- `provider`: the exact LiteLLM provider namespace;
- `model`: the exact `provider/model` identifier, whose prefix must equal `provider`;
- `input_cost_per_million_usd`: the current non-zero input rate for that exact model; and
- `output_cost_per_million_usd`: the current non-zero output rate for that exact model.

Dispatch from GitHub Actions or, after substituting reviewed values, with:

```bash
gh workflow run qualified-provider.yml \
  -f candidate_sha=<full-candidate-sha> \
  -f provider=<provider> \
  -f model=<provider/model> \
  -f input_cost_per_million_usd=<reviewed-rate> \
  -f output_cost_per_million_usd=<reviewed-rate>
```

The workflow checks out that exact SHA, rejects a dirty or different checkout, builds the production
provider-worker target, records its immutable local image ID, and proves its `10001:10001` runtime
identity. The live container has a read-only root filesystem, dropped capabilities, no-new-
privileges, bounded CPU/memory/PIDs, ephemeral home/temp filesystems, and only the pre-provisioned
provider-egress network. eSpeak receives a separate small executable tmpfs because its library
loader cannot run from the general no-exec temp mount.

## Fixed spend and time boundary

The fixture cannot be expanded by workflow inputs. It uses the public
`CorpusgenModelRuntimeAdapter` and `HostedModelPolicy` with:

- one fixed non-sensitive `/p/` target, the built-in prompt template, English, temperature zero,
  and one candidate per iteration;
- at most two provider requests, 2,048 conservatively reserved input units, 96 output tokens total,
  and 48 output tokens per request;
- zero retries, one accepted sentence, two generation iterations, and a 20-second loop limit;
- a 12-second per-request timeout and 30-second whole-activity deadline; and
- a USD ceiling of `$0.05`, computed from the reviewer-supplied non-zero rates before every call.

The shell additionally kills the complete live container after 40 seconds (TERM, then KILL after
five seconds). This outer deadline covers an SDK or network stack that ignores its inner timeout.
Provider-side spend controls remain mandatory because no client-side process can revoke a request
already accepted by a provider.

## Evidence review and release use

A successful run uploads only `provider.json` as
`qualified-provider-<candidate-sha>` for 30 days. A second container revalidates it with networking
disabled and without the provider credential before upload. The evidence records exact source and
worker-image identity, runtime package versions, fixture/prompt/target digests, provider/model and
reviewed rates, fixed limits, reported usage/cost, target coverage, and privacy assertions. It does
not retain prompt text, generated text, candidate IDs, a credential reference, or a credential
value.

Download and independently revalidate a retained artifact with the same candidate identity:

```bash
uv run --extra worker-external-provider python \
  scripts/provider/qualified_provider_acceptance.py verify \
  --candidate-sha <full-candidate-sha> \
  --worker-image-digest <sha256:image-id> \
  --provider <provider> \
  --model <provider/model> \
  --input-cost-per-million-usd <reviewed-rate> \
  --output-cost-per-million-usd <reviewed-rate> \
  --input provider.json
```

Link the successful workflow run and its artifact from the release record. Do not copy the artifact
into the repository, edit it, or treat a run for another commit, image, provider, or model as
transferable.

## What remains external

The exact remaining gate is a successful protected-environment dispatch for the new clean
release-candidate SHA using a real credential for the selected provider/model, followed by offline
verification and retention/linking of `provider.json`. Until that occurs, CorpusKit makes no live
provider-qualification claim.

The 30-day Actions artifact is a transfer mechanism, not the release archive. Before it expires,
release operations must copy the exact bytes to an access-controlled immutable/WORM evidence
store, record the file SHA-256 and permanent read-only permalink in the release record, and have
promotion reviewers match its candidate SHA, worker image, provider, and model. Missing durable
archival infrastructure or a mismatched digest blocks promotion.

Even a passing canary proves only live availability, provider-reported usage, bounded cost under the
reviewed rates, output parseability, and `/p/` target coverage for that invocation. It does not prove
the provider's eventual invoice, independently confirm that a reviewer entered the latest price,
guarantee general output quality, or close Temporal redelivery/provider idempotency and duplicate-
charge semantics. Those billing and failure-boundary checks remain separate release/production
controls.
