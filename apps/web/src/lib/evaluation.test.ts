import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) =>
    fetch(input, init),
}));

import {
  evaluateCorpus,
  EvaluationApiError,
  EvaluationContractError,
  parseEvaluationResult,
} from "@/lib/evaluation";

export const validEvaluationResult = {
  language: "en-us",
  unit: "phoneme",
  target_mode: "explicit",
  target_units: ["h", "ə", "l", "oʊ", "z"],
  covered_units: ["h", "ə", "l", "oʊ"],
  missing_units: ["z"],
  coverage: 0.8,
  total_sentences: 1,
  unit_counts: [
    { unit: "h", count: 1 },
    { unit: "ə", count: 1 },
  ],
  sentence_details: [
    {
      index: 0,
      text: "Hello.",
      phoneme_count: 4,
      new_units: ["h", "ə", "l", "oʊ"],
      all_phonemes: ["h", "ə", "l", "oʊ"],
    },
  ],
  unit_sources: [{ unit: "h", sentence_indices: [0] }],
  distribution: {
    entropy: 2,
    normalized_entropy: 0.86,
    jsd_uniform: 0.08,
    coefficient_of_variation: 0.3,
    min_count: 0,
    max_count: 1,
    count_ratio: 0,
    zero_count: 1,
    pcd_uniform: 0.736,
    jsd_reference: null,
    pearson_correlation: null,
  },
  text_quality: {
    sentence_length_words_mean: 1,
    sentence_length_words_median: 1,
    sentence_length_words_std: 0,
    sentence_length_words_min: 1,
    sentence_length_words_max: 1,
    sentence_length_phonemes_mean: 4,
    sentence_length_phonemes_median: 4,
    sentence_length_phonemes_std: 0,
    sentence_length_phonemes_min: 4,
    sentence_length_phonemes_max: 4,
    total_words: 1,
    unique_words: 1,
    type_token_ratio: 1,
    hapax_ratio: 1,
    flesch_reading_ease: 121.2,
    flesch_kincaid_grade: -3.4,
  },
} as const;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("evaluation API contract", () => {
  it("accepts the exact evaluation DTO", () => {
    const result = parseEvaluationResult(validEvaluationResult);

    expect(result.coverage).toBe(0.8);
    expect(result.sentence_details[0]?.new_units).toEqual([
      "h",
      "ə",
      "l",
      "oʊ",
    ]);
  });

  it("rejects incomplete and unsafe result shapes", () => {
    expect(() =>
      parseEvaluationResult({ ...validEvaluationResult, coverage: 1.2 }),
    ).toThrow(EvaluationContractError);
    expect(() =>
      parseEvaluationResult({
        ...validEvaluationResult,
        sentence_details: [{ text: "bad" }],
      }),
    ).toThrow(EvaluationContractError);
  });

  it("posts the normalized request to the real endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(validEvaluationResult), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await evaluateCorpus({
      sentences: ["Hello."],
      language: "en-us",
      unit: "phoneme",
      target: { mode: "explicit", phonemes: ["h"] },
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/evaluations",
      expect.objectContaining({ method: "POST", cache: "no-store" }),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(request.body as string)).toEqual({
      sentences: ["Hello."],
      language: "en-us",
      unit: "phoneme",
      target: { mode: "explicit", phonemes: ["h"] },
    });
  });

  it("preserves the safe application error envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "engine_unavailable",
            message: "The evaluation engine is unavailable.",
            operation: "evaluation",
            request_id: "req-123",
          }),
          { status: 503 },
        ),
      ),
    );

    const promise = evaluateCorpus({
      sentences: ["Hello."],
      language: "en-us",
      unit: "phoneme",
      target: { mode: "derived", phonemes: [] },
    });

    await expect(promise).rejects.toMatchObject({
      status: 503,
      code: "engine_unavailable",
      requestId: "req-123",
    } satisfies Partial<EvaluationApiError>);
  });
});
