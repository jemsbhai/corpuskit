# Security Policy

## Supported versions

CorpusKit has not reached its first stable release. Security fixes are applied to the
default branch and the most recent published prerelease.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting for the
`jemsbhai/corpuskit` repository once the remote is available. Until then, contact the
maintainer directly at `jemsbhai@gmail.com` with the subject `CorpusKit security report`.

Include the affected version, reproduction steps, impact, and any suggested mitigation.
Please avoid accessing data that is not yours, degrading shared services, or publishing
details before a coordinated fix is available.

## Security promises

- Production authentication is delegated to OIDC; CorpusKit does not store passwords.
- Tenant authorization is enforced at both service and persistence boundaries.
- Provider credentials are never returned to clients or written to logs.
- Model, dataset, and PHOIBLE revisions are pinned and recorded.
- Remote model code is disabled, uploads are bounded, and generated text is untrusted.
- Critical or high findings block a release.

## Release artifact integrity

Official candidates are published only by the tag-triggered immutable-release workflow. Python
distributions and both SPDX/CycloneDX SBOM formats have SHA-256 checksums, GitHub build/SBOM
attestations, and keyless Sigstore bundles. Container images are signed and promoted only by OCI
digest. Every third-party GitHub Action is pinned to a full reviewed commit SHA.

CI also pins its PostgreSQL and Temporal service images by digest and verifies the downloaded
PGDG repository key's full OpenPGP primary fingerprint before placing it in APT's trust path.
Short key IDs and transport security alone are not accepted as signing-key identity.

Do not trust a version tag, filename, checksum without its signature, or a mutable container tag.
Follow [`docs/operations/releases.md`](docs/operations/releases.md) to verify the GitHub immutable
release, release assets, Cosign certificate identity, provenance, SBOM predicates, and image
digest. PyPI publishing must use the separately approval-protected Trusted Publisher workflow;
long-lived PyPI tokens are forbidden.

Base-image digest changes, release workflow/action changes, and lockfile changes are supply-chain
security changes requiring independent review. Automated vulnerability results are
point-in-time: report newly disclosed issues through the private vulnerability process even when
the original release scan was green.
