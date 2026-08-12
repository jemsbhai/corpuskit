# ADR-0004: Delegate identity to OIDC and pass only secret references

- Status: Accepted
- Date: 2026-08-11
- Owners: CorpusKit maintainers

## Context

CorpusKit is a multi-user application holding potentially sensitive corpus text, model
outputs, and provider credentials. Implementing passwords, MFA, account recovery, session
revocation, and organization identity internally would create high-risk security work that
is not part of the product's linguistic purpose. Long-running workflows also make it unsafe
to place plaintext provider keys in task payloads or durable workflow histories.

## Decision

Production authentication uses a standards-compliant external OIDC provider with
Authorization Code flow and PKCE. The web tier uses secure, HTTP-only, same-site cookies;
the API validates issuer, audience, signature, expiration, nonce/state where applicable,
and required claims. Local and self-hosted development uses Keycloak with the same claim
contract. Test-only authentication is unavailable in production builds.

Organization membership and application roles (`owner`, `admin`, `editor`, `viewer`) are
owned by CorpusKit. The API derives organization scope from verified identity and
membership. It never trusts an arbitrary tenant ID supplied by the browser.

Provider credentials are session-only or stored under envelope encryption in an approved
secret manager. PostgreSQL stores only metadata and an opaque `secret_ref`. Temporal inputs
and histories contain only that reference. The narrowly scoped provider activity resolves
the secret at execution time. Secret reads and changes are audited; plaintext is never
logged, returned after creation, placed in URLs, or stored in browser local storage.

## Consequences

### Positive

- Password/MFA/recovery security is delegated to a specialized system.
- Enterprise federation can be supported without changing application authentication.
- Provider keys do not persist in databases, workflow histories, or logs.
- Authentication and application authorization remain separable and testable.

### Negative

- Sign-in availability depends on the configured identity provider.
- Claim mapping and key rotation require operational monitoring.
- Local development needs a real OIDC service or explicit test harness.
- A secret manager increases per-environment setup and cost.

## Rejected alternatives

- **Application-managed passwords:** unnecessary security and compliance surface.
- **Provider access token as application authorization:** providers do not model CorpusKit
  organizations or resources.
- **Plaintext encrypted column for all secrets:** makes the API/database path able to decrypt
  every provider key and encourages accidental task serialization.
- **Keys in Temporal workflow input:** workflow history is durable and broadly replicated.

## Verification

- Negative tests cover issuer/audience mismatch, expiry, algorithm confusion, missing claims,
  revoked membership, and cross-organization resource IDs.
- Production configuration refuses test authentication and insecure cookie settings.
- Secret-scanning tests inspect logs, events, workflow payloads, and error responses.
- Rotation tests prove signing keys and stored provider keys can change without downtime.
- Audit tests cover secret creation, use, rotation, deletion, and denied access.
