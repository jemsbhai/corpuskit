# Qualified provider evidence contract

The manual live-provider qualifier produces `corpuskit.qualified-provider-acceptance.v1`. Its
Pydantic model is both the generating schema and the offline validation boundary; unknown fields,
non-finite numbers, loose identities, changed fixed limits, and incomplete observations fail
closed. The workflow validates the serialized bytes in the live process and again in a separate
credential-free, network-disabled container.

## Acceptance conditions

An artifact is accepted only when all of these facts agree:

| Boundary | Required evidence |
| --- | --- |
| Source | Non-zero full candidate SHA equals the reviewed workflow input. |
| Runtime | Non-zero `sha256:` production worker image ID and exact CorpusKit, CorpusGen, LiteLLM, and Python versions. |
| Selection | Safe provider/model grammar and a model namespace exactly equal to the provider. |
| Fixture | Exact fixed-fixture, built-in prompt-template, and target digests. No prompt or generated text. |
| Admission | Two requests, no retries, fixed token/iteration/time limits, and a `$0.05` ceiling. |
| Usage | Positive provider-reported input/output usage within reservations; actual and reserved costs exactly recompute from the retained reviewed rates. |
| Quality | Exactly one accepted result, complete target coverage, no missing target, and `target_coverage` as the stop reason. |
| Privacy | No credential value/reference, prompt text, generated text, or callback output is retained. |

The file is canonical compact JSON, at most 32 KiB, created atomically with exclusive permissions,
and never overwrites a prior artifact. Verification rejects symlinks, oversized files, schema
changes, identity substitutions, rate/cost mutations, weakened privacy assertions, and additional
fields such as `raw_output`.

## Adversarial test boundary

`tests/provider/test_qualified_provider_acceptance.py` drives a fake provider through the public
`CorpusgenModelRuntimeAdapter` and the real CorpusGen hosted backend. It covers exact provider/model
arguments, environment-only secret resolution, generated-output and credential redaction, positive
usage/cost computation, target coverage, schema strictness, source/image mismatch, namespace
confusion, zero/non-finite pricing, reservation exhaustion before contact, over-reported usage,
blank output, provider errors, missing coverage, missing secret/network policy, stale-output
overwrite attempts, oversized artifacts, sanitized CLI failures, and workflow hardening.

These tests deliberately do not count as live evidence. A fake can establish budget, redaction,
schema, and control-flow behavior, but not provider availability, SDK compatibility, real output,
reported billing usage, current pricing, or invoice behavior.

Run the offline contract suite with:

```bash
uv run pytest -o addopts= -q tests/provider/test_qualified_provider_acceptance.py
uv run ruff check scripts/provider tests/provider
uv run mypy scripts/provider/qualified_provider_acceptance.py \
  tests/provider/test_qualified_provider_acceptance.py
```

Run Actionlint and Zizmor against `.github/workflows/qualified-provider.yml` before changing its
permissions, action pins, expressions, or secret boundary. The workflow intentionally has no
`pull_request`, `pull_request_target`, `schedule`, repository write permission, retry, or automatic
spend trigger.

## Interpretation limits

The retained price fields are reviewer-approved inputs, not a provider-signed rate card. The cost
fields use provider-reported token usage and CorpusKit's exact decimal calculation, not an eventual
invoice. No output text is retained, so the artifact proves the fixed automated phonetic gate but
cannot support later subjective content review. Max retries is zero, and this direct canary does not
exercise Temporal activity redelivery; a successful artifact therefore cannot establish provider-
wide idempotency or absence of duplicate charges after worker loss.

No credentialed workflow run or live provider result is claimed for the initial implementation.
Qualification begins only when a successful exact-candidate artifact is retained and linked under
the operational runbook.

