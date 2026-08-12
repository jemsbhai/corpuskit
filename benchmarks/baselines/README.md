# Performance baseline governance

This directory holds approved, exact-profile performance baselines. It is intentionally
empty until the repository has a clean source commit from which the first baseline can
be generated. A dirty or `local-uncommitted` result is diagnostic evidence and MUST NOT
be added here or attached to a release record.

Baseline creation is a two-commit procedure:

1. Create the clean root source commit containing the benchmark fixture and runner. The
   scheduled policy permits a missing baseline only before `HEAD` exists or while this
   root commit is checked out; release validation never permits the exception.
2. From that clean commit, run the documented 20-sample/3-warmup command to an ignored
   temporary path. Review the environment profile, fixture digest, timings, and memory
   series, then add the JSON baseline in a separate reviewed commit.

Once the repository has any commit after the root commit, a missing approved baseline
fails scheduled verification. Release validation always requires the exact-profile
baseline, validates its schema and clean provenance, and requires its measured source
revision to be an ancestor of the candidate.

Verification reads a baseline and writes a separate comparison report. Neither the
comparator nor CI has an update-baseline option, and verification MUST NOT overwrite an
approved baseline.
