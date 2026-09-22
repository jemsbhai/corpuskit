# Accelerate checkpoint safety patch

This directory contains the complete Apache-2.0 Accelerate 1.15.0 wheel with a narrow
CorpusKit security patch, identified as **1.15.0+corpuskit.1**. It retains every upstream
archive member, dependency declaration, and license. It is not an official Hugging Face
release. The upstream source package is recorded in `provenance.json` with its immutable
PyPI URL and SHA-256, along with every original file digest and all modified file digests.

The patch repairs the checkpoint-path traversal and special-file denial of service in
[CVE-2026-69112](https://github.com/advisories/GHSA-4j2p-28q2-5m79), also identified as
PYSEC-2026-3804. Upstream 1.15.0 still contains the vulnerable path join. The related
[unmerged upstream proposal](https://github.com/huggingface/accelerate/pull/4138) addresses
lexical traversal only; this patch additionally rejects nonregular index/shard files and
symlinks outside the model boundary.

`checkpoint-safety.patch` changes only `accelerate/utils/modeling.py`:

- Validate every index reference before loading any shard: reject absolute, drive, UNC,
  parent-traversing, NUL-containing, or alternate-stream paths, and missing/nonregular files.
- Resolve symlinks within the checkpoint directory; also permit normal immutable Hub
  snapshots (`<repository>/snapshots/<40-character revision>`) linking to that repository's
  real `blobs/` directory. A blob-directory symlink cannot expand the boundary.
- Validate index files before opening them, use a nonblocking index descriptor where supported,
  bound index JSON to 16 MiB, and reject malformed/empty weight maps.
- Preserve raw and wrapped weight maps, nested shards, `.bin` and safetensors formats,
  direct index inputs, model dispatch, and legitimate Hub file symlinks. The logical filename
  is preserved so extensionless Hub blobs continue through the correct safetensors reader.

The normal model cache must remain read-only throughout validation and loading. The patch
protects against malicious checkpoint content; it does not make mutable local caches safe
against an attacker who can race filesystem changes while the weight reader opens files.

The builder changes the package version in `__init__.py` and wheel `METADATA`, renames the
dist-info directory, and regenerates `RECORD`. It preserves all other upstream bytes. ZIP
members are sorted with fixed timestamps and permissions; compression is deflate level 9.

```powershell
python scripts/security/accelerate_patch.py verify
python scripts/security/accelerate_patch.py rebuild-check
python scripts/security/accelerate_patch.py verify-installed
python -m pytest -o addopts= -q tests/integration/test_accelerate_patch_runtime.py
```

`rebuild-check` downloads only the pinned official wheel, verifies its hash and original
RECORD, reapplies the exact-context patch, and requires byte-for-byte reproduction of the
checked-in artifact. An already downloaded wheel can be passed with `--upstream-wheel`.
`build --output PATH` writes a rebuilt wheel only after the same verification. The artifact
verifier checks every wheel member and its RECORD digest, all original dependency declarations,
and the original license. `verify-installed` compares every installed wheel-owned file and
its RECORD row against the verified wheel and rejects extra package files.

CI must verify this artifact before mapping the local build to upstream `accelerate==1.15.0`
for vulnerability queries. Plain pip-audit may silently skip unpublished local versions;
that is not acceptable evidence. The dependency audit must cover all upstream and transitive
dependencies and fail on unexpected omissions, skipped packages, and new advisories. This
patch is an actual code remediation, not an advisory exemption or version-only workaround.

The wheel is installed through this repository's locked uv source mapping. Ordinary PyPI
resolution cannot fetch this unpublished local build. It has not been uploaded to PyPI;
deployments claiming the patched dependency must use the verified checked-in wheel and lock.
