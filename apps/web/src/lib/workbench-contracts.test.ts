import { describe, expect, it } from "vitest";

import { ApiContractError } from "@/lib/api-client";
import {
  parseArtifact,
  parseArtifactCreation,
  parseArtifacts,
  parseSignedDownload,
} from "@/lib/artifacts";
import { parseCoverage, parseEstimate } from "@/lib/coverage-lab";
import { parseTranscription, parseTranscriptions } from "@/lib/g2p";
import {
  parseCompositeScoring,
  parsePhonotacticArtifact,
  parsePhonotacticScores,
  parseReadability,
  parseRepositoryGeneration,
} from "@/lib/generation";
import { parseEvents, parseRun, parseRuns, resultArtifactId } from "@/lib/jobs";
import { parseSelection } from "@/lib/selection";

const uuid = "123e4567-e89b-42d3-a456-426614174000";
const uuid2 = "123e4567-e89b-42d3-a456-426614174001";
const hash = "a".repeat(64);

describe("G2P response contracts", () => {
  const transcription = {
    text: "hi",
    language: "en-us",
    ipa: "haɪ",
    phonemes: ["h", "aɪ"],
    diphones: ["h aɪ"],
    triphones: [],
    phoneme_count: 2,
    unique_phonemes: ["h", "aɪ"],
  };
  it("preserves ordered Unicode transcription fields", () => {
    expect(parseTranscription(transcription).phonemes).toEqual(["h", "aɪ"]);
    expect(parseTranscriptions([transcription])).toHaveLength(1);
  });
  it("rejects arrays and inconsistent counts", () => {
    expect(() =>
      parseTranscription({ ...transcription, phoneme_count: 1 }),
    ).toThrow(ApiContractError);
    expect(() => parseTranscription({ ...transcription, ipa: 2 })).toThrow(
      ApiContractError,
    );
    expect(() => parseTranscriptions({})).toThrow(ApiContractError);
  });
});

describe("coverage response contracts", () => {
  const snapshot = {
    coverage: 0.5,
    target_size: 2,
    covered_count: 1,
    target_units: ["p", "b"],
    covered_units: ["p"],
    missing_units: ["b"],
    unit_counts: [{ unit: "p", count: 1 }],
    unit_sources: [{ unit: "p", sentence_indices: [0] }],
  };
  it("parses estimates, ordered steps, counts, and provenance", () => {
    expect(
      parseEstimate({
        phoneme_count: 2,
        unit: "diphone",
        exponent: 2,
        estimated_target_size: 4,
        max_target_size: 20_000,
        within_limit: true,
      }).estimated_target_size,
    ).toBe(4);
    expect(
      parseCoverage({
        unit: "phoneme",
        steps: [{ sentence_index: 0, coverage: 0.5, new_units: ["p"] }],
        final: snapshot,
        next_targets: ["b"],
        after_reset: {
          ...snapshot,
          coverage: 0,
          covered_count: 0,
          covered_units: [],
          missing_units: ["p", "b"],
          unit_counts: [],
          unit_sources: [],
        },
      }).final.unit_sources[0]?.sentence_indices,
    ).toEqual([0]);
  });
  it.each([
    {},
    {
      phoneme_count: -1,
      unit: "phoneme",
      exponent: 1,
      estimated_target_size: 1,
      max_target_size: 1,
      within_limit: true,
    },
    {
      phoneme_count: 1,
      unit: "quadphone",
      exponent: 1,
      estimated_target_size: 1,
      max_target_size: 1,
      within_limit: true,
    },
  ])("rejects malformed estimates", (value) =>
    expect(() => parseEstimate(value)).toThrow(ApiContractError),
  );
  it("rejects malformed steps and nested provenance", () => {
    expect(() =>
      parseCoverage({
        unit: "phoneme",
        steps: [{ sentence_index: -1, coverage: 2, new_units: [] }],
        final: snapshot,
        next_targets: [],
        after_reset: snapshot,
      }),
    ).toThrow(ApiContractError);
    expect(() =>
      parseCoverage({
        unit: "phoneme",
        steps: [],
        final: { ...snapshot, unit_counts: [{ unit: "p", count: -1 }] },
        next_targets: [],
        after_reset: snapshot,
      }),
    ).toThrow(ApiContractError);
    expect(() =>
      parseCoverage({
        unit: "phoneme",
        steps: [],
        final: {
          ...snapshot,
          unit_sources: [{ unit: "p", sentence_indices: [-1] }],
        },
        next_targets: [],
        after_reset: snapshot,
      }),
    ).toThrow(ApiContractError);
  });
});

describe("selection response contract", () => {
  const valid = {
    selected_indices: [0],
    selected_sentences: ["A"],
    coverage: 1,
    covered_units: ["p"],
    missing_units: [],
    unit: "phoneme",
    target_mode: "explicit",
    algorithm: "nsga2",
    elapsed_seconds: 0.1,
    iterations: 2,
    metadata: {
      evaluations: 3,
      epsilon: 0.1,
      seed: 42,
      sample_size: 1,
      kl_divergence: null,
      solver_status: null,
      pareto_front: [
        {
          coverage: 1,
          sentence_count: 1,
          selected_indices: [0],
          kl_divergence: null,
        },
      ],
    },
  };
  it("parses metadata and Pareto solutions", () =>
    expect(parseSelection(valid).metadata.pareto_front[0]?.coverage).toBe(1));
  it.each([
    { ...valid, coverage: 2 },
    { ...valid, algorithm: "invented" },
    { ...valid, selected_indices: [-1] },
    { ...valid, metadata: { ...valid.metadata, evaluations: -1 } },
    {
      ...valid,
      metadata: {
        ...valid.metadata,
        pareto_front: [
          {
            coverage: -1,
            sentence_count: 1,
            selected_indices: [],
            kl_divergence: null,
          },
        ],
      },
    },
  ])("rejects invented or non-finite normalized results", (value) =>
    expect(() => parseSelection(value)).toThrow(ApiContractError),
  );
});

describe("generation and scoring response contracts", () => {
  const score = {
    source_id: "row-1",
    text: "A",
    phonemes: ["a"],
    coverage_gain: 1,
    weighted_coverage_gain: 1,
    phonotactic_score: 0.5,
    fluency_score: 0,
    readability_status: "available",
    readability_score: 0.8,
    composite_score: 1.2,
    new_units: ["a"],
  };
  it("parses repository, composite, n-gram, and readability results", () => {
    expect(
      parseRepositoryGeneration({
        schema_id: "corpuskit.repository-generation-result.v1",
        execution_mode: "synchronous_preview",
        source_kind: "raw_text",
        unit: "phoneme",
        accepted: [
          {
            source_id: "row-1",
            text: "A",
            phonemes: ["a"],
            iteration: 1,
            coverage_gain: 1,
          },
        ],
        coverage: 1,
        covered_units: ["a"],
        missing_units: [],
        iterations: 1,
        elapsed_seconds: 0.01,
        stop_reason: "target_coverage",
      }).accepted,
    ).toHaveLength(1);
    expect(
      parseCompositeScoring({
        schema_id: "corpuskit.composite-scoring-result.v1",
        ranked: [score],
        committed: null,
        covered_units_before: [],
        covered_units_after: ["a"],
      }).ranked[0]?.text,
    ).toBe("A");
    const artifact = parsePhonotacticArtifact({
      artifact_type: "corpusgen.ngram-phonotactic-scorer",
      schema_version: 1,
      payload: { n: 2 },
      content_sha256: hash,
    });
    expect(artifact.payload).toEqual({ n: 2 });
    expect(
      parsePhonotacticScores({
        artifact_type: artifact.artifact_type,
        scores: [0, 1],
      }).scores,
    ).toEqual([0, 1]);
    expect(
      parseReadability({
        results: [
          {
            text: "A",
            status: "unavailable",
            flesch_reading_ease: null,
            score: null,
            accepted_by_filter: null,
          },
        ],
      })[0]?.status,
    ).toBe("unavailable");
  });
  it.each([
    {},
    { schema_id: "wrong" },
    {
      schema_id: "corpuskit.repository-generation-result.v1",
      execution_mode: "synchronous_preview",
      source_kind: "raw_text",
      unit: "phoneme",
      accepted: [],
      coverage: 2,
      covered_units: [],
      missing_units: [],
      iterations: 0,
      elapsed_seconds: 0,
      stop_reason: "done",
    },
  ])("rejects malformed repository responses", (value) =>
    expect(() => parseRepositoryGeneration(value)).toThrow(ApiContractError),
  );
  it("rejects malformed scorer outputs", () => {
    expect(() =>
      parseCompositeScoring({
        schema_id: "corpuskit.composite-scoring-result.v1",
        ranked: [{ ...score, readability_status: "maybe" }],
        committed: null,
        covered_units_before: [],
        covered_units_after: [],
      }),
    ).toThrow(ApiContractError);
    expect(() =>
      parsePhonotacticArtifact({
        artifact_type: "other",
        schema_version: 1,
        payload: {},
        content_sha256: hash,
      }),
    ).toThrow(ApiContractError);
    expect(() =>
      parsePhonotacticScores({ artifact_type: "other", scores: [1] }),
    ).toThrow(ApiContractError);
    expect(() =>
      parseReadability({
        results: [
          {
            text: "A",
            status: "available",
            score: "secret",
            flesch_reading_ease: null,
            accepted_by_filter: true,
          },
        ],
      }),
    ).toThrow(ApiContractError);
  });
});

describe("durable run and artifact response contracts", () => {
  const run = {
    id: uuid,
    organization_id: uuid2,
    project_id: uuid2,
    corpus_version_id: null,
    parent_run_id: null,
    kind: "evaluate",
    state: "succeeded",
    attempt: 1,
    spec: { secret: "ignored by client" },
    spec_sha256: hash,
    outbox_state: "sent",
    cancellation_requested_at: null,
    created_at: "2026-08-11T12:00:00Z",
    result_summary: { artifact_id: uuid },
    failure_code: null,
  };
  const artifact = {
    id: uuid,
    project_id: uuid2,
    run_id: uuid,
    kind: "run-result",
    sha256: hash,
    size_bytes: 20,
    media_type: "application/json",
    filename: "result.json",
    state: "active",
    retention_until: "2026-09-11T12:00:00Z",
    created_at: "2026-08-11T12:00:00Z",
  };
  it("drops tenant and raw spec fields while retaining safe run projection", () => {
    const parsed = parseRun(run) as unknown as Record<string, unknown>;
    expect(parsed.organization_id).toBeUndefined();
    expect(parsed.spec).toBeUndefined();
    expect(resultArtifactId(parseRun(run))).toBe(uuid);
    expect(
      resultArtifactId({
        ...parseRun(run),
        result_summary: { artifact_id: "bad" },
      }),
    ).toBeNull();
    expect(parseRuns([run])).toHaveLength(1);
    expect(
      parseEvents([
        {
          sequence: 1,
          event_type: "completed",
          payload: {},
          occurred_at: "2026-08-11T12:00:01Z",
        },
      ])[0]?.sequence,
    ).toBe(1);
  });
  it("parses project-scoped artifacts and safe signed URLs", () => {
    expect(parseArtifact(artifact).filename).toBe("result.json");
    expect(parseArtifacts([artifact])).toHaveLength(1);
    expect(parseArtifactCreation({ artifact, created: true }).created).toBe(
      true,
    );
    expect(
      parseSignedDownload({
        url: "https://objects.example.test/file?signature=opaque",
        expires_at: "2026-08-11T12:05:00Z",
      }).url,
    ).toContain("https://");
    expect(
      parseSignedDownload({
        url: "http://localhost:9000/file",
        expires_at: "2026-08-11T12:05:00Z",
      }).url,
    ).toContain("localhost");
  });
  it("rejects malformed, cross-contract, and unsafe signed URL values", () => {
    expect(() => parseRun({ ...run, state: "invented" })).toThrow(
      ApiContractError,
    );
    expect(() => parseRuns({})).toThrow(ApiContractError);
    expect(() =>
      parseEvents([
        { sequence: -1, event_type: "x", payload: {}, occurred_at: "now" },
      ]),
    ).toThrow(ApiContractError);
    expect(() => parseArtifact({ ...artifact, sha256: "bad" })).toThrow(
      ApiContractError,
    );
    expect(() => parseArtifacts({})).toThrow(ApiContractError);
    expect(() => parseArtifactCreation({ artifact, created: "yes" })).toThrow(
      ApiContractError,
    );
    expect(() =>
      parseSignedDownload({
        url: "http://evil.example/file",
        expires_at: "soon",
      }),
    ).toThrow(ApiContractError);
    expect(() =>
      parseSignedDownload({
        url: "https://user:pass@example.test/file",
        expires_at: "soon",
      }),
    ).toThrow(ApiContractError);
  });
});
