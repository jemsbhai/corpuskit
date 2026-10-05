# Braces security patch

Upstream: braces 3.0.3, MIT license (included in the archive).
Advisory: https://github.com/advisories/GHSA-vfj7-8cjw-p6xm (no patched release).

The checked-in archive is named @corpuskit/braces 3.0.3-corpuskit.1 and installed
under the braces alias through the root npm override. This is an actual source
patch, not an audit exception. Parse rejects nesting beyond 128 stack entries;
compile, expand, and stringify reject AST traversal deeper than 128 levels,
including caller-supplied trees. Normal brace expansion remains compatible.

The patch is preserved in nesting-guard.patch. Reproduce by unpacking
`npm pack braces@3.0.3`, applying that patch inside package/, then running
`npm pack ./package`. npm's lockfile verifies the archive integrity.
Run `node --test scripts/security/braces-patch.test.cjs` after `npm ci`.
