import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import { ApiContractError } from "@/lib/api-client";
import { authenticatedFetch } from "@/lib/browser-auth";
import {
  generationApi,
  parseCompositeScoring,
  parsePhonotacticArtifact,
  parsePhonotacticScores,
  parseReadability,
  parseRepositoryGeneration,
} from "@/lib/generation";

const fetchMock = vi.mocked(authenticatedFetch);
const hash = "a".repeat(64);
const artifact = {
  artifact_type: "corpusgen.ngram-phonotactic-scorer",
  schema_version: 1,
  payload: { n: 2 },
  content_sha256: hash,
};
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
const repository = {
  schema_id: "corpuskit.repository-generation-result.v1",
  execution_mode: "worker_activity",
  source_kind: "prephonemized",
  unit: "triphone",
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
  elapsed_seconds: 0.1,
  stop_reason: "target_coverage",
};
const composite = {
  schema_id: "corpuskit.composite-scoring-result.v1",
  ranked: [score],
  committed: score,
  covered_units_before: [],
  covered_units_after: ["a"],
};
const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });

afterEach(() => fetchMock.mockReset());

describe("generation and scoring transport", () => {
  it("calls every mounted preview and scoring endpoint", async () => {
    fetchMock
      .mockResolvedValueOnce(response(repository))
      .mockResolvedValueOnce(response(composite))
      .mockResolvedValueOnce(response(artifact))
      .mockResolvedValueOnce(
        response({
          ...artifact,
          artifact_type: "corpusgen.ngram-phonotactic-constraint",
        }),
      )
      .mockResolvedValueOnce(
        response({ artifact_type: artifact.artifact_type, scores: [0.5] }),
      )
      .mockResolvedValueOnce(
        response({
          results: [
            {
              text: "A",
              status: "available",
              flesch_reading_ease: 80,
              score: 1,
              accepted_by_filter: true,
            },
          ],
        }),
      );
    await expect(generationApi.preview({})).resolves.toMatchObject({
      execution_mode: "worker_activity",
    });
    await expect(generationApi.composite({})).resolves.toMatchObject({
      committed: score,
    });
    await expect(generationApi.trainScorer({})).resolves.toMatchObject({
      artifact_type: artifact.artifact_type,
    });
    await expect(generationApi.trainConstraint({})).resolves.toMatchObject({
      artifact_type: "corpusgen.ngram-phonotactic-constraint",
    });
    await expect(generationApi.scorePhonotactics({})).resolves.toMatchObject({
      scores: [0.5],
    });
    await expect(generationApi.readability({})).resolves.toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it("treats a rolling-upgrade composite without fluency as zero", () => {
    const legacyScore = { ...score } as Record<string, unknown>;
    delete legacyScore.fluency_score;

    expect(
      parseCompositeScoring({
        ...composite,
        ranked: [legacyScore],
        committed: legacyScore,
      }),
    ).toMatchObject({
      ranked: [{ fluency_score: 0 }],
      committed: { fluency_score: 0 },
    });
  });
});

describe("generation result fail-closed branches", () => {
  it.each([
    { ...repository, execution_mode: "other" },
    { ...repository, source_kind: "other" },
    { ...repository, unit: "other" },
    { ...repository, accepted: {} },
    { ...repository, accepted: [null] },
    { ...repository, accepted: [{ ...repository.accepted[0], source_id: 1 }] },
    { ...repository, accepted: [{ ...repository.accepted[0], text: 1 }] },
    { ...repository, accepted: [{ ...repository.accepted[0], phonemes: [1] }] },
    { ...repository, accepted: [{ ...repository.accepted[0], iteration: -1 }] },
    {
      ...repository,
      accepted: [{ ...repository.accepted[0], coverage_gain: -1 }],
    },
    { ...repository, coverage: -1 },
    { ...repository, covered_units: [1] },
    { ...repository, missing_units: [1] },
    { ...repository, iterations: -1 },
    { ...repository, elapsed_seconds: "x" },
    { ...repository, stop_reason: 1 },
  ])("rejects malformed repository fields", (value) =>
    expect(() => parseRepositoryGeneration(value)).toThrow(ApiContractError),
  );

  it.each([
    { ...composite, ranked: {} },
    { ...composite, covered_units_before: [1] },
    { ...composite, covered_units_after: [1] },
    { ...composite, ranked: [{ ...score, source_id: 1 }] },
    { ...composite, ranked: [{ ...score, text: 1 }] },
    { ...composite, ranked: [{ ...score, phonemes: [1] }] },
    { ...composite, ranked: [{ ...score, coverage_gain: -1 }] },
    { ...composite, ranked: [{ ...score, weighted_coverage_gain: "x" }] },
    { ...composite, ranked: [{ ...score, phonotactic_score: "x" }] },
    { ...composite, ranked: [{ ...score, fluency_score: 1.1 }] },
    { ...composite, ranked: [{ ...score, readability_status: "other" }] },
    { ...composite, ranked: [{ ...score, readability_score: "x" }] },
    { ...composite, ranked: [{ ...score, composite_score: "x" }] },
    { ...composite, ranked: [{ ...score, new_units: [1] }] },
  ])("rejects malformed composite fields", (value) =>
    expect(() => parseCompositeScoring(value)).toThrow(ApiContractError),
  );

  it("covers nullable and alternative normalized values", () => {
    expect(
      parseCompositeScoring({
        ...composite,
        committed: null,
        ranked: [
          {
            ...score,
            readability_status: "unavailable",
            readability_score: null,
          },
        ],
      }).committed,
    ).toBeNull();
    expect(
      parseRepositoryGeneration({
        ...repository,
        execution_mode: "synchronous_preview",
        source_kind: "raw_text",
        unit: "phoneme",
      }).unit,
    ).toBe("phoneme");
    expect(
      parseRepositoryGeneration({
        ...repository,
        source_kind: "hugging_face",
        unit: "diphone",
      }).source_kind,
    ).toBe("hugging_face");
    expect(
      parsePhonotacticArtifact({
        ...artifact,
        artifact_type: "corpusgen.ngram-phonotactic-constraint",
      }).artifact_type,
    ).toContain("constraint");
  });

  it("rejects malformed n-gram and readability values", () => {
    for (const value of [
      null,
      { ...artifact, schema_version: 2 },
      { ...artifact, payload: [] },
      { ...artifact, content_sha256: "bad" },
    ])
      expect(() => parsePhonotacticArtifact(value)).toThrow(ApiContractError);
    for (const value of [
      null,
      { artifact_type: artifact.artifact_type, scores: {} },
      { artifact_type: artifact.artifact_type, scores: [Number.NaN] },
    ])
      expect(() => parsePhonotacticScores(value)).toThrow(ApiContractError);
    for (const value of [
      null,
      { results: {} },
      { results: [null] },
      {
        results: [
          {
            text: 1,
            status: "available",
            flesch_reading_ease: 1,
            score: 1,
            accepted_by_filter: true,
          },
        ],
      },
      {
        results: [
          {
            text: "A",
            status: "other",
            flesch_reading_ease: 1,
            score: 1,
            accepted_by_filter: true,
          },
        ],
      },
      {
        results: [
          {
            text: "A",
            status: "available",
            flesch_reading_ease: "x",
            score: 1,
            accepted_by_filter: true,
          },
        ],
      },
      {
        results: [
          {
            text: "A",
            status: "available",
            flesch_reading_ease: 1,
            score: "x",
            accepted_by_filter: true,
          },
        ],
      },
      {
        results: [
          {
            text: "A",
            status: "available",
            flesch_reading_ease: 1,
            score: 1,
            accepted_by_filter: "yes",
          },
        ],
      },
    ])
      expect(() => parseReadability(value)).toThrow(ApiContractError);
  });
});
