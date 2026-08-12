# Curated multilingual demonstration

`POST /api/v1/labs/demos/multilingual` runs a fixed, bounded demonstration of the complete
inventory → G2P → evaluation → selection path. It accepts only catalogue identifiers, never
arbitrary text, paths, model identifiers, or URLs. An empty `cases` array selects the full suite.

| Case | Voice | Writing system | What it demonstrates |
|---|---|---|---|
| `latin-english` | `en-us` | Latin | eSpeak voice mapping, PHOIBLE inventory, derived coverage, greedy selection |
| `arabic` | `ar` | Arabic | native right-to-left script G2P and Arabic PHOIBLE mapping |
| `indic-devanagari` | `hi` | Devanagari | native Indic-script G2P and Hindi inventory mapping |
| `cjk-mandarin` | `cmn` | Han | Mandarin G2P and a PHOIBLE inventory with contour tones |
| `tonal-vietnamese` | `vi` | tonal Latin | diacritics in raw text and a tonal PHOIBLE inventory |

Every case uses two fixed, human-readable sentences. A pass requires:

1. a non-empty PHOIBLE inventory resolved through CorpusKit's eSpeak-to-ISO mapping;
2. non-empty IPA and phoneme sequences for every sentence;
3. 100% derived-target evaluation coverage; and
4. 100% greedy selection coverage with at least one selected sentence.

The response preserves transcription, inventory, evaluation, and selected-sentence evidence.
PHOIBLE counts and observed eSpeak phonemes are deliberately presented as independent sources;
the demo does not imply that the two inventories are identical.

Failures are isolated per case. A missing voice, unusable native-script implementation, or
inventory problem returns a stable application error code and no coverage claim for that case;
the other cases continue. Raw dependency exceptions and filesystem details are not returned.

## Acceptance runtime

The deployed Linux API image is the normative native-script runtime. Automated Linux acceptance
uses the real pinned CorpusGen 0.1.7 package, eSpeak NG, and checksum-verified PHOIBLE snapshot.
It requires all five cases to pass and specifically verifies non-zero tone inventories for
Mandarin and Vietnamese.

The currently supported Windows eSpeak binary uses a narrow-character input boundary that can
return empty output for Arabic, Devanagari, and Han input. CorpusKit reports those cases as failed
instead of treating an empty target as 100% coverage. The Windows test is therefore skipped, while
the same full suite is mandatory in the Linux container and CI release gate.
