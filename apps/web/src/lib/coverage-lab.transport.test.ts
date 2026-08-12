import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import { ApiContractError } from "@/lib/api-client";
import { authenticatedFetch } from "@/lib/browser-auth";
import {
  coverageLabApi,
  parseCoverage,
  parseEstimate,
} from "@/lib/coverage-lab";

const fetchMock = vi.mocked(authenticatedFetch);
const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });
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
const coverage = {
  unit: "phoneme",
  steps: [{ sentence_index: 0, coverage: 0.5, new_units: ["p"] }],
  final: snapshot,
  next_targets: ["b"],
  after_reset: { ...snapshot, coverage: 0, covered_count: 0 },
};

afterEach(() => fetchMock.mockReset());

describe("coverage contracts", () => {
  it.each([
    null,
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
      unit: "bad",
      exponent: 1,
      estimated_target_size: 1,
      max_target_size: 1,
      within_limit: true,
    },
    {
      phoneme_count: 1,
      unit: "diphone",
      exponent: -1,
      estimated_target_size: 1,
      max_target_size: 1,
      within_limit: true,
    },
    {
      phoneme_count: 1,
      unit: "triphone",
      exponent: 3,
      estimated_target_size: -1,
      max_target_size: 1,
      within_limit: true,
    },
    {
      phoneme_count: 1,
      unit: "phoneme",
      exponent: 1,
      estimated_target_size: 1,
      max_target_size: -1,
      within_limit: true,
    },
    {
      phoneme_count: 1,
      unit: "phoneme",
      exponent: 1,
      estimated_target_size: 1,
      max_target_size: 1,
      within_limit: "yes",
    },
  ])("rejects malformed target estimates", (value) =>
    expect(() => parseEstimate(value)).toThrow(ApiContractError),
  );

  it("accepts all normalized unit values", () => {
    for (const [unit, exponent] of [
      ["phoneme", 1],
      ["diphone", 2],
      ["triphone", 3],
    ] as const) {
      expect(
        parseEstimate({
          phoneme_count: 2,
          unit,
          exponent,
          estimated_target_size: 2 ** exponent,
          max_target_size: 20_000,
          within_limit: true,
        }).unit,
      ).toBe(unit);
    }
  });

  it.each([
    null,
    { ...coverage, unit: "bad" },
    { ...coverage, steps: {} },
    { ...coverage, next_targets: [1] },
    {
      ...coverage,
      steps: [{ sentence_index: -1, coverage: 0.5, new_units: [] }],
    },
    {
      ...coverage,
      steps: [{ sentence_index: 0, coverage: "x", new_units: [] }],
    },
    {
      ...coverage,
      steps: [{ sentence_index: 0, coverage: -1, new_units: [] }],
    },
    { ...coverage, steps: [{ sentence_index: 0, coverage: 2, new_units: [] }] },
    {
      ...coverage,
      steps: [{ sentence_index: 0, coverage: 0, new_units: [1] }],
    },
    { ...coverage, final: { ...snapshot, coverage: "x" } },
    { ...coverage, final: { ...snapshot, coverage: -1 } },
    { ...coverage, final: { ...snapshot, coverage: 2 } },
    { ...coverage, final: { ...snapshot, target_size: -1 } },
    { ...coverage, final: { ...snapshot, covered_count: -1 } },
    { ...coverage, final: { ...snapshot, target_units: [1] } },
    { ...coverage, final: { ...snapshot, covered_units: [1] } },
    { ...coverage, final: { ...snapshot, missing_units: [1] } },
    { ...coverage, final: { ...snapshot, unit_counts: {} } },
    { ...coverage, final: { ...snapshot, unit_sources: {} } },
    { ...coverage, final: { ...snapshot, unit_counts: [null] } },
    {
      ...coverage,
      final: { ...snapshot, unit_counts: [{ unit: 1, count: 1 }] },
    },
    {
      ...coverage,
      final: { ...snapshot, unit_counts: [{ unit: "p", count: -1 }] },
    },
    { ...coverage, final: { ...snapshot, unit_sources: [null] } },
    {
      ...coverage,
      final: { ...snapshot, unit_sources: [{ unit: 1, sentence_indices: [] }] },
    },
    {
      ...coverage,
      final: {
        ...snapshot,
        unit_sources: [{ unit: "p", sentence_indices: {} }],
      },
    },
    {
      ...coverage,
      final: {
        ...snapshot,
        unit_sources: [{ unit: "p", sentence_indices: [-1] }],
      },
    },
  ])("rejects malformed coverage snapshots and steps", (value) =>
    expect(() => parseCoverage(value)).toThrow(ApiContractError),
  );

  it("parses a complete tracked result", () =>
    expect(parseCoverage(coverage).final.unit_counts[0]?.count).toBe(1));
});

describe("coverage, weight, and report transport", () => {
  it("calls every endpoint and accepts both canonical export formats", async () => {
    fetchMock
      .mockResolvedValueOnce(
        response({
          phoneme_count: 1,
          unit: "phoneme",
          exponent: 1,
          estimated_target_size: 1,
          max_target_size: 20_000,
          within_limit: true,
        }),
      )
      .mockResolvedValueOnce(response(coverage))
      .mockResolvedValueOnce(
        response({
          weights: [{ unit: "p", weight: 1 }],
          count: 1,
          total: 1,
          mean: 1,
        }),
      )
      .mockResolvedValueOnce(response({ valid: true, count: 1 }))
      .mockResolvedValueOnce(
        response({
          verbosity: "verbose",
          media_type: "text/plain",
          content: "report",
        }),
      )
      .mockResolvedValueOnce(
        response({
          format: "jsonld",
          media_type: "application/ld+json",
          canonical_json: "{}",
        }),
      );
    await coverageLabApi.estimate(["p"], "phoneme");
    await coverageLabApi.track({
      target_phonemes: ["p"],
      unit: "phoneme",
      phoneme_sequences: [["p"]],
      weights: [],
      next_targets_limit: 1,
    });
    await expect(
      coverageLabApi.computeWeights({
        strategy: "uniform",
        target_units: ["p"],
        unit: "phoneme",
        corpus_phonemes: [],
        class_weights: [],
      }),
    ).resolves.toMatchObject({ count: 1 });
    await expect(
      coverageLabApi.validateWeights("unit", [{ unit: "p", weight: 1 }]),
    ).resolves.toEqual({ valid: true, count: 1 });
    await expect(coverageLabApi.render({})).resolves.toMatchObject({
      verbosity: "verbose",
    });
    await expect(coverageLabApi.export({})).resolves.toMatchObject({
      format: "jsonld",
    });
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it("rejects every malformed weight summary branch", async () => {
    for (const value of [
      {},
      { weights: {}, count: 0, total: 0, mean: 0 },
      { weights: [], count: -1, total: 0, mean: 0 },
      { weights: [], count: 0, total: "x", mean: 0 },
      { weights: [], count: 0, total: 0, mean: "x" },
      { weights: [null], count: 1, total: 1, mean: 1 },
      { weights: [{ unit: 1, weight: 1 }], count: 1, total: 1, mean: 1 },
      { weights: [{ unit: "p", weight: "x" }], count: 1, total: 1, mean: 1 },
      { weights: [{ unit: "p", weight: -1 }], count: 1, total: 1, mean: 1 },
    ]) {
      fetchMock.mockResolvedValueOnce(response(value));
      await expect(
        coverageLabApi.computeWeights({
          strategy: "uniform",
          target_units: [],
          unit: "phoneme",
          corpus_phonemes: [],
          class_weights: [],
        }),
      ).rejects.toBeInstanceOf(ApiContractError);
    }
  });

  it("rejects malformed validation, render, and export envelopes", async () => {
    for (const value of [
      {},
      { valid: false, count: 0 },
      { valid: true, count: -1 },
    ]) {
      fetchMock.mockResolvedValueOnce(response(value));
      await expect(
        coverageLabApi.validateWeights("component", []),
      ).rejects.toBeInstanceOf(ApiContractError);
    }
    for (const value of [
      {},
      { verbosity: "other", media_type: "text/plain", content: "x" },
      { verbosity: "minimal", media_type: 1, content: "x" },
      { verbosity: "normal", media_type: "text/plain", content: 1 },
    ]) {
      fetchMock.mockResolvedValueOnce(response(value));
      await expect(coverageLabApi.render({})).rejects.toBeInstanceOf(
        ApiContractError,
      );
    }
    for (const value of [
      {},
      { format: "yaml", media_type: "text/plain", canonical_json: "x" },
      { format: "json", media_type: 1, canonical_json: "x" },
      { format: "jsonld", media_type: "application/json", canonical_json: 1 },
    ]) {
      fetchMock.mockResolvedValueOnce(response(value));
      await expect(coverageLabApi.export({})).rejects.toBeInstanceOf(
        ApiContractError,
      );
    }
  });
});
