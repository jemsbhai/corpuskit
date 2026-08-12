# OIDC authentication operations

CorpusKit delegates production identity to an OpenID Connect provider and accepts only
signed `RS256` access tokens. The API derives the organization and role from verified token
claims; callers cannot select a tenant through a request parameter or header.

## Security contract

- `demo` mode is accepted only in `development` and `test`. Startup fails in staging or
  production when demo authentication is selected.
- Production requires an HTTPS issuer, exact audience, exact discovery-document issuer,
  `RS256` signature, `sub`, `iss`, `aud`, and `exp`. `nbf` is enforced when present. The
  default clock skew is zero and can be raised only to 60 seconds.
- Discovery and JWKS requests have a five-second timeout, do not follow redirects, accept at
  most 1 MiB, and cache at most 100 signing keys.
- API discovery and JWKS targets must use HTTPS on the default port 443; the discovered
  `jwks_uri` must share the issuer origin. Browser-client discovery, authorization, token,
  revocation, and JWKS endpoints likewise stay on the configured issuer origin, use HTTPS/443,
  contain no credentials, query, or fragment, and never follow redirects.
- The default JWKS cache lifetime is five minutes. An unknown `kid` can trigger rotation
  refresh, limited to once per ten seconds to prevent an attacker from turning random key IDs
  into identity-provider traffic. Once a cached key expires, refresh failure fails closed with
  a stable `503 authentication_unavailable` response.
- Authentication responses never include tokens, token claims, key IDs, provider URLs,
  exception text, or requested paths. Do not add request-header/body logging around these
  routes.
- Caller-supplied `X-Request-ID` values are accepted only as 1–128 character ASCII tokens
  using letters, digits, `.`, `_`, `:`, or `-`; malformed values are replaced with a UUID
  and are never reflected.
- API responses keep `default-src 'none'`. When development docs are enabled, only `/docs`
  and its OAuth redirect receive the narrowly scoped jsDelivr/FastAPI asset policy needed by
  Swagger UI. Production disables both docs and OpenAPI routes.
- Browser access and refresh tokens exist only in the Next.js server process and encrypted
  session records. They never enter JavaScript-visible storage, cookies, HTML, response bodies,
  URLs, or logs. The browser receives only an opaque `__Host-corpuskit_session` cookie marked
  `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`.
- Mutating BFF and logout requests require a session-bound CSRF value. Browser-supplied
  `Authorization` is discarded; the BFF synthesizes the only bearer header from server-held
  session state.
- Rendered pages receive a fresh request nonce and a production `script-src` policy without
  `unsafe-inline`; API and auth responses use a deny-all non-HTML policy. Production TLS remains
  mandatory. The HTTP-only test harness disables only `upgrade-insecure-requests` so WebKit can
  exercise the otherwise production-built application locally.

## Provider registration

Register the web tier as a confidential OIDC client with exactly one production redirect URI:
`https://<public-web-origin>/auth/callback`. Enable Authorization Code, S256 PKCE, ID tokens,
refresh-token rotation, and revocation. Disable implicit, password, device, wildcard redirect,
and plain-PKCE grants. Grant only `openid profile offline_access` unless another reviewed scope
is required. The ID token audience must contain the web client ID; the API access-token audience
must equal `CORPUSKIT_OIDC_AUDIENCE`.

Register the API as a resource server, not a public client. Configure the provider to issue:

| Claim    | Required value                                                           |
| -------- | ------------------------------------------------------------------------ |
| `iss`    | Exact configured issuer URL                                              |
| `aud`    | Configured API audience (default deployment convention: `corpuskit-api`) |
| `sub`    | Stable provider subject, at most 255 characters                          |
| `exp`    | Short-lived expiration; 5–15 minutes is recommended                      |
| `nbf`    | Optional; enforced when issued                                           |
| `org_id` | CorpusKit organization UUID                                              |
| `role`   | One of `owner`, `admin`, `editor`, or `viewer`                           |
| `name`   | Optional display name, at most 160 characters                            |

Custom organization and role claim names can be configured, but their values retain the
same strict formats. Organization membership remains a CorpusKit authorization concern;
production provisioning must map provider identity to an active membership before expanding
the current claim-only boundary.

## Configuration

Inject settings as runtime secrets/configuration; never bake tokens or client secrets into an
image or `.env` committed to source control.

```text
CORPUSKIT_ENVIRONMENT=production
CORPUSKIT_AUTH_MODE=oidc
CORPUSKIT_API_RATE_LIMIT_ENABLED=true
CORPUSKIT_OIDC_ISSUER=https://identity.example.com/realms/corpuskit
CORPUSKIT_OIDC_AUDIENCE=corpuskit-api
CORPUSKIT_OIDC_ALGORITHMS=["RS256"]
CORPUSKIT_OIDC_JWKS_CACHE_SECONDS=300
CORPUSKIT_OIDC_REFRESH_COOLDOWN_SECONDS=10
CORPUSKIT_OIDC_HTTP_TIMEOUT_SECONDS=5
CORPUSKIT_OIDC_CLOCK_SKEW_SECONDS=0
CORPUSKIT_OIDC_ORGANIZATION_CLAIM=org_id
CORPUSKIT_OIDC_ROLE_CLAIM=role
CORPUSKIT_API_INTERNAL_URL=http://api:8000/api/v1
CORPUSKIT_WEB_AUTH_MODE=oidc
CORPUSKIT_WEB_OIDC_CLIENT_ID=corpuskit-web
CORPUSKIT_WEB_OIDC_CLIENT_SECRET=<secret-manager reference or injected value>
CORPUSKIT_WEB_OIDC_REDIRECT_URI=https://corpuskit.example/auth/callback
CORPUSKIT_WEB_OIDC_SCOPES=openid profile offline_access
CORPUSKIT_WEB_STATE_SECRET=<independent base64url secret, at least 32 bytes>
CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS=["<active 32-byte base64url key>"]
CORPUSKIT_WEB_ALLOWED_RETURN_PATHS=["/","/projects","/evaluate","/analysis","/capabilities","/g2p","/inventory","/coverage","/selection","/generation","/advanced","/jobs","/artifacts"]
CORPUSKIT_WEB_SESSION_REDIS_URL=rediss://corpuskit:<password>@cache.example:6379/0
CORPUSKIT_WEB_REDIS_PREFIX=corpuskit:web
CORPUSKIT_WEB_REDIS_COMMAND_TIMEOUT_MS=2000
CORPUSKIT_WEB_SESSION_ABSOLUTE_SECONDS=28800
CORPUSKIT_WEB_SESSION_IDLE_SECONDS=1800
CORPUSKIT_WEB_TOKEN_REFRESH_BEFORE_SECONDS=60
CORPUSKIT_WEB_LOGIN_TRANSACTION_SECONDS=600
CORPUSKIT_WEB_OIDC_TIMEOUT_SECONDS=5
```

Production also requires Temporal and disabled API documentation, as described in the main
runtime configuration.

Generate the state and session-encryption values independently. One portable Node.js command
is `node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"`;
run it once per key. Never reuse either value as the OIDC client secret, Redis password, API
signing material, or each other. The encryption setting is a JSON array whose first entry is
active for writes and whose remaining entries are decrypt-only rotation keys (maximum four).

Every production/staging web replica must receive identical auth settings, have a synchronized
clock, share the same Redis/Valkey deployment, and reach only the issuer over HTTPS/443 and the
fixed internal API base. `CORPUSKIT_API_INTERNAL_URL` must contain exactly an HTTP(S) origin and
`/api/v1`, with no credentials, query, or fragment. Redis must use `rediss://` and credentials;
the client disables offline queuing and reconnect loops and bounds connection, command, and lock
waits. An unavailable or corrupt store yields a generic 503. Production never falls back to an
in-memory session store, and an auth configuration error never enables demo/test identity.

The deterministic in-memory store and local subjects exist only when
`CORPUSKIT_ENVIRONMENT=test` with `CORPUSKIT_WEB_AUTH_MODE=test`, or explicit development/demo
configuration. They are not production migration or outage fallbacks.

The repository's base Compose `web` profile explicitly injects deterministic demo-only state and
session keys, the exact return-path list, and `CORPUSKIT_WEB_AUTH_MODE=demo`; it is not a
production auth deployment. Override both keys for any shared development environment.
Staging/production deployment manifests must instead inject every OIDC and Redis/Valkey value
above and enforce the API/provider network policy. Merely placing values in a Compose
substitution `.env` does not pass undeclared variables into a container; verify the effective
container environment without printing secret values.

## Route policy

- `/api/v1/health/live`, `/api/v1/health/ready`, `/api/v1/version`, and
  `/api/v1/capabilities` are public operational endpoints and expose no identity data.
- `/api/v1/auth/me` requires any authenticated organization role and returns the derived
  principal context.
- Current workflow POST routes require `owner`, `admin`, or `editor`; `viewer` receives a
  stable `403 forbidden` response.

The browser starts at `/auth/login?returnTo=<exact-allowlisted-path>`. `/auth/callback` consumes a
one-time, encrypted server-side transaction bound to signed state, nonce, S256 PKCE, and an opaque
correlation cookie; it invalidates any prior session before issuing a new opaque session ID.
`/auth/session` returns only display identity, expiry, and a CSRF value. `/auth/logout` verifies
CSRF, deletes local state first, then makes best-effort access/refresh revocation calls. The BFF
under `/api/v1/*` resolves and refreshes tokens under a distributed per-session lock, forwards
only allowlisted headers, and applies hard request/query/response caps. Do not place tokens in
browser storage, cookies, query strings, workflow payloads, Temporal history, or logs.

## Key rotation

1. Publish the new signing key in JWKS while the old key remains present.
2. Wait at least one CorpusKit JWKS cache lifetime.
3. Begin issuing tokens with the new `kid`. Unknown key IDs cause a bounded immediate refresh
   after the refresh cooldown.
4. Keep the old public key available for at least the maximum token lifetime plus the cache
   lifetime.
5. Remove the old key and verify authentication with a newly issued token.

Emergency rotation can temporarily produce `401` for an unknown key during the ten-second
anti-amplification cooldown. Do not lower the cooldown below one second. A planned overlap
avoids this window.

Rotate application-held secrets as follows:

1. **Session encryption:** deploy `[new, old]`. New and refreshed records are written with
   `new`; both keys can decrypt during the overlap. Keep `old` for at least the eight-hour
   absolute session lifetime plus deployment skew, then remove it. For emergency compromise,
   remove the old key immediately and accept that sessions encrypted only by it fail closed.
2. **State HMAC:** it intentionally has no multi-key acceptance window. Stop or drain new login
   starts, wait the configured login-transaction lifetime (ten minutes by default), deploy the
   independent replacement to every replica, then re-enable login. Existing sessions continue.
3. **OIDC client secret:** create an overlapping provider credential, deploy it to all web
   replicas, verify code exchange and refresh, then revoke the old credential. If the provider
   cannot overlap credentials, drain login/refresh traffic and expect reauthentication.
4. **Redis credentials:** add a new ACL credential, roll every web replica, verify encrypted
   session read/write and lock acquisition, then revoke the old credential. Do not change the
   encryption key merely because the transport credential changes.

## Monitoring and response

Monitor aggregate counts only; never label metrics with subject, organization, token, `kid`,
issuer path, or error text.

- Alert when `authentication_unavailable` exceeds 1% of authenticated requests for five
  minutes. Check provider discovery/JWKS reachability, TLS, DNS, and provider status.
- Investigate a sustained rise in `invalid_authentication`, but treat individual failures as
  expected client events and do not log bearer values.
- For a compromised signing key, disable it at the provider, publish a replacement, preserve
  forensic logs that contain request IDs only, and rotate any provider client credentials.
- If the issuer or audience changes, deploy configuration and provider overlap deliberately;
  validation is exact and there is no multi-issuer fallback.

## Acceptance checks

Before production promotion:

1. Confirm startup rejects demo mode and missing issuer/audience.
2. Verify a real short-lived token reaches `/api/v1/auth/me` with the expected organization
   and least-privileged role.
3. Verify wrong issuer, wrong audience, `alg=none`, expired, not-yet-valid, unknown-key, and
   malformed-tenant tokens all fail.
4. Exercise overlapping signing-key rotation without downtime.
5. Block provider network access after cache expiry and confirm a generic `503` with no URL,
   header, token, path, or exception disclosure.
6. Confirm a viewer cannot call workflow POST routes and an editor can.
7. Search application logs and traces for the test bearer token and assert zero matches.
8. Run login/callback/session/logout in Chromium, Firefox, and WebKit; confirm both cookies have
   the required `__Host-` flags, a fixed prior cookie cannot survive login, open returns fail,
   mutation without CSRF fails, and refresh-token rotation survives concurrent BFF requests.
9. Exercise a TLS-authenticated Redis/Valkey outage and lock contention. Confirm generic 503
   responses, no memory fallback, no plaintext token in stored values, and successful recovery.
10. Inspect the deployed web environment and network policy for the exact internal API base,
    issuer HTTPS/443-only egress, distinct state/encryption secrets, and the active-plus-old key
    ring during a rehearsed session-key rotation.

The repository's Playwright flow deterministically verifies the local cookie/CSRF boundary; it
does not emulate a vendor's real login UI or prove provider policy. Staging promotion still
requires the live-provider, TLS Redis/Valkey, revocation, credential-rotation, and log-search
checks above. Provider-wide single sign-out is not implemented: logout removes the CorpusKit
session and revokes tokens where the provider exposes revocation, but it does not terminate the
user's unrelated provider sessions.
