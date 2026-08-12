# CorpusGen CLI parity lab

CorpusKit exposes `POST /api/v1/labs/cli/preview` for users who want to move between the
workbench and CorpusGen's command-line interface. The endpoint validates a typed request and
returns three equivalent representations:

- an exact `argv` array, which is the authoritative representation;
- a POSIX-shell command quoted with Python's `shlex` rules; and
- a PowerShell command with every argument single-quoted and embedded quotes doubled.

The preview does **not** start a process, read a caller path, contact a provider, or resolve a
secret. It is available to viewer roles because it is a pure serialization operation. Actual
CorpusKit workflows retain their normal editor/owner authorization and execution controls.
The accessible `/advanced` workbench provides typed workflow presets, POSIX/PowerShell display,
manual-copy fallback, parity warnings, and a copy button. It sends only the bounded preview DTO;
the browser and API never invoke the returned command.

## Supported workflows

The discriminator is `workflow` and accepts `inventory`, `evaluate`, `select`, or `generate`.
The generated arguments match CorpusGen 0.1.7, which is the exact application dependency pin.
Unknown fields are rejected.

- Inventory covers language/voice, optional PHOIBLE source, and text or JSON output.
- Evaluation covers inline sentences or a file placeholder, derived or PHOIBLE targets, all
  three coverage units, text/JSON/JSON-LD output, and all verbosity levels. CorpusGen's CLI does
  not accept an arbitrary explicit target list; the CorpusKit evaluation API does.
- Selection covers all six algorithms, derived or PHOIBLE targets, coverage and sentence limits,
  output files, and the distribution selector's JSON target distribution. Stochastic seeds,
  general weights, ILP timeouts, and NSGA-II population controls are API-only.
- Generation covers repository, LLM API, and local backends; finite stopping criteria; PHOIBLE
  source plus additive phonemes; weights; model/backend settings; DATG/RL guidance; phonotactic
  and perplexity scorers; and text/JSON output. API keys are intentionally absent from the schema.

Every preview enables `PYTHONUTF8=1`. This prevents IPA output from failing when a Windows or
legacy terminal uses a code page that cannot represent Unicode phonetic symbols.

## Security and reproducibility

Treat a preview as a command to review, not as a durable run manifest. CorpusKit returns explicit
warnings for every known parity gap:

- file contents are not hashed by the CorpusGen CLI;
- the dataset CLI does not expose an immutable config/revision pin;
- a local model identifier is not equivalent to CorpusKit's verified offline snapshot policy;
- external-provider output is not reproducible and credentials must come from the caller's
  environment; and
- the CLI does not provide CorpusKit tenancy, quotas, cancellation, artifact adoption, or durable
  workflow semantics.

Generation has additional, version-specific omissions. CorpusGen 0.1.7 does not expose an
immutable model revision or snapshot digest, a local-generation seed, top-p, or sampling-mode
switch. Its hosted command infers the provider from the model namespace instead of accepting the
separately validated provider, connection, pacing, retry, budget, and usage contracts enforced by
CorpusKit. Its JSON output has accepted text and aggregate loop metrics, but no provider/model
identity, per-candidate phonemes or source IDs, coverage gains, usage, or execution manifest.
Readability scoring and filtering are also API-only. The parity comparisons therefore normalize
only fields that both products actually expose; they do not manufacture equality for omitted
contracts.

Repository commands can be replayed only when their referenced files and PHOIBLE snapshot are
held immutable. Stochastic selectors and local generation are labeled best-effort. LLM output is
labeled as externally dependent.

Automated acceptance executes the generated inventory, evaluation, selection, repository
generation, hosted generation, and CPU-local generation argument vectors against the real pinned
CorpusGen CLI. Inventory, evaluation, selection, and every executable generation path compare the
shared JSON semantics with CorpusKit's normalized adapter results. The hosted run crosses a
deterministic fake provider seam with no network or credentials and additionally compares the
model, prompt, temperature, and token-limit call boundary. The CPU-local run uses generated tiny
safetensors weights and tokenizer files, forces both Hugging Face libraries offline, and executes
on CPU. Because the locked CLI has no seed or deterministic-decoding flag, the test seeds its
process immediately before invocation and discloses that copied CLI commands cannot preserve that
condition.

Live-provider parity, qualified CUDA 4-bit/8-bit local parity, and executable DATG, Phon-RL,
phonotactic, and perplexity CLI combinations remain environment-specific release gates. The locked
CLI has no equivalent DATG index publication contract and no readability CLI controls. Contract
tests separately reject shell-injection strings, control characters, unbounded generation,
ambiguous option combinations, and secret-shaped extra fields.
