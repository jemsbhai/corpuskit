# CorpusKit web application

The Next.js application is an authenticated browser workbench over the CorpusKit API. It does
not run CorpusGen in the browser and never receives an OIDC access token. All `/api/v1/*`
requests pass through the same-origin BFF, which resolves the encrypted server-side session,
synthesizes the bearer header, and requires the session-bound CSRF value for mutations.

## User routes

| Route           | User workflow                                                                                                                               | Current execution boundary                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `/projects`     | Tenant project creation, bounded corpus import, immutable version/sentence inspection, deterministic export, and confirmed project deletion | Mounted tenant workspace API; selected project is shared globally                     |
| `/g2p`          | Single/batch IPA, phoneme, diphone, and triphone transcription; variants and exports                                                        | Mounted synchronous API; requires eSpeak                                              |
| `/inventory`    | PHOIBLE language/mapping search, source/best/all/union inventories, 38-feature filters, allophones, and provenance                          | Mounted synchronous API; requires provisioned PHOIBLE data                            |
| `/evaluate`     | Coverage, counts, distribution quality, missing units, and source-level provenance for bounded sentence rows                                | Mounted synchronous API; raw text requires eSpeak                                     |
| `/coverage`     | Target-size estimation, ordered tracking/provenance, weights, and canonical reports                                                         | Mounted bounded lab API                                                               |
| `/analysis`     | Distribution, text quality, WER/CER/PER/SER, and coverage trajectory with data tables                                                       | Mounted bounded analysis API                                                          |
| `/selection`    | Six selector configurations and all six real responses retained for one side-by-side comparison                                             | Core algorithms mounted; ILP and NSGA-II require the reported optimization capability |
| `/generation`   | Repository preview, composite scoring, n-gram scorer artifacts, phonotactic scoring, and readability                                        | Preview/scoring only; repository execution remains worker-only                        |
| `/advanced`     | Hosted/local validation and estimates, DATG and Phon-RL labs, CLI parity preview, and advanced durable-job submission                       | Mounted policy-gated APIs; execution runs only on isolated worker profiles            |
| `/jobs`         | Typed submission for six registered CPU run kinds, filtering, detail, monotonic polling, cancellation, retry, and final-artifact navigation | Mounted durable control plane                                                         |
| `/artifacts`    | Selected-project upload, direct run-artifact links, integrity metadata, verified download, presign, and confirmed delete                    | Mounted artifact API; public upload is `corpus-text` only                             |
| `/capabilities` | Refreshable backend checks, tenant quota usage/policy ceilings, and hash-linked audit events                                                | Mounted capability and authenticated platform read APIs                               |

The global context stores only selected project, corpus, and version UUIDs in `sessionStorage`.
It re-hydrates the corpus/version pair from project-scoped server listings before use and never
stores an organization identifier, token, corpus content, or result. For lineage-bearing jobs it
then fetches every ordered sentence page and verifies the version's declared count before
submission. Job and artifact views discard or hide organization fields and show only records
whose returned `project_id` matches the selected project.

Version-backed durable runs are atomic and never silently truncate: phonemize and evaluate accept
at most 500 sentences, while select accepts at most 2,000. The Job Center rejects a larger
selected version before paging its contents. Import an explicit bounded derived version for one
atomic run; executing the complete larger version requires a future chunked-job contract.

## Deliberately bounded controls

- `/advanced` mounts the non-executing hosted/local/Hugging Face/DATG/Phon-RL validation and
  estimate APIs, bounded read-only DATG and PPO/reward labs, and shell-safe CLI parity preview.
  It submits an unchanged validated specification through the durable job API; it never exposes
  a provider, dataset, or model execution route directly.
- Hosted, Hugging Face repository, local, DATG, and Phon-RL jobs are registered only on their
  exact isolated worker profile and remain default-deny until an operator provides immutable
  allowlists, cache attestations, deadlines, and separate worker/adoption identities. The
  browser has no credential field.
- There is intentionally no direct HTTP training endpoint. The advanced workbench exposes a
  bounded non-secret JSON configuration editor whose templates and server-side validation remain
  allowlisted; it rejects credentials and cannot execute shell commands.
  The Artifact Manager uploads exact canonical `prompt-set` JSON for operator-enabled static
  Phon-RL training; durable specs carry only the artifact UUID, digest, and prompt count. Local
  PEFT generation similarly selects an adopted successful training result by result/checkpoint
  digests. The trusted parent authorizes and ephemerally materializes both inputs; prompt text,
  adapter paths, and trusted envelopes never enter browser-visible run state.
- Synchronous repository preview accepts bounded raw or pre-phonemized rows. Durable repository
  generation runs on the external-provider profile; remote Hugging Face input additionally
  requires an exact dataset/config/split/revision allowlist and the profile's restricted Hub
  egress policy.

## Deployment prerequisites

Use the complete production configuration and rotation procedure in
[`docs/operations/oidc-authentication.md`](../../docs/operations/oidc-authentication.md). In
particular:

1. Register the exact HTTPS callback and configure Authorization Code, S256 PKCE, ID tokens,
   rotating refresh tokens, and revocation.
2. Allow OIDC discovery, authorization, token, JWKS, and revocation only on the configured
   issuer's HTTPS/443 origin, without credentials, endpoint query strings, fragments, or
   redirects. The API independently requires its discovery/JWKS URL on HTTPS/443 and the
   discovered `jwks_uri` on the issuer origin.
3. Set a fixed `CORPUSKIT_API_INTERNAL_URL` ending in `/api/v1`, with no credentials, query, or
   fragment. Production does not default to localhost.
4. Provide authenticated `rediss://` Redis/Valkey, bounded command/lock timeouts, an independent
   state HMAC secret, and an application-layer AES-256-GCM session key ring. Store failure is a
   generic 503; production never falls back to memory or test identity.
5. Set the exact return-path allowlist to:

   ```json
   [
     "/",
     "/projects",
     "/evaluate",
     "/analysis",
     "/capabilities",
     "/g2p",
     "/inventory",
     "/coverage",
     "/selection",
     "/generation",
     "/advanced",
     "/jobs",
     "/artifacts"
   ]
   ```

6. Rehearse session encryption rotation as `[new, old]`, retain the old key through the absolute
   session lifetime plus rollout skew, then remove it. Rotate the state HMAC only after draining
   login transactions; rotate OIDC and Redis credentials with provider/ACL overlap.

The base Compose profile uses explicit local demo identity and deterministic demo keys. It is not
a production identity deployment and cannot be enabled when `CORPUSKIT_ENVIRONMENT=production`.
The executable, fixed-fixture, no-request-mock acceptance flow is documented in
[`docs/product/15-minute-demo.md`](../../docs/product/15-minute-demo.md); run it only against an
already-started isolated demo or staging web origin.

## Acceptance gates

From the repository root:

```text
npm run format:check --workspace @corpuskit/web
npm run lint --workspace @corpuskit/web
npm run typecheck --workspace @corpuskit/web
npm test --workspace @corpuskit/web
npm run test:workbenches --workspace @corpuskit/web
npm run build --workspace @corpuskit/web
npm run test:e2e --workspace @corpuskit/web
CORPUSKIT_LIVE_BASE_URL=http://127.0.0.1:3000 npm run test:e2e:live --workspace @corpuskit/web
```

The scoped workbench configuration enforces at least 90% branch coverage per new state/transport
module. Playwright runs every workbench route through Chromium, Firefox, and WebKit with axe,
console/page/request failure checks, keyboard reachability, and a 320-pixel/200%-text layout
check. Live provider, TLS Redis/Valkey, revocation, and multi-replica refresh-lock validation
remain staging gates because the deterministic browser harness cannot prove external policy.
