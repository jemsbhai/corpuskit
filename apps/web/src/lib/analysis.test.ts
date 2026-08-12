import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) =>
    fetch(input, init),
}));

import {
  analysisApi,
  AnalysisContractError,
  parseDistribution,
  parseErrorRates,
  parseInventory,
  parsePhonologyStatus,
  parseTextQuality,
  parseTrajectory,
} from "@/lib/analysis";

const distribution = {
  entropy: 1.2,
  normalized_entropy: 0.8,
  jsd_uniform: 0.1,
  coefficient_of_variation: 0.4,
  min_count: 0,
  max_count: 5,
  count_ratio: 0,
  zero_count: 1,
  pcd_uniform: 0.72,
  jsd_reference: null,
  pearson_correlation: 0.9,
};

const textQuality = {
  sentence_length_words_mean: 2,
  sentence_length_words_median: 2,
  sentence_length_words_std: 0,
  sentence_length_words_min: 2,
  sentence_length_words_max: 2,
  sentence_length_phonemes_mean: 4,
  sentence_length_phonemes_median: 4,
  sentence_length_phonemes_std: 0,
  sentence_length_phonemes_min: 4,
  sentence_length_phonemes_max: 4,
  total_words: 2,
  unique_words: 2,
  type_token_ratio: 1,
  hapax_ratio: 1,
  flesch_reading_ease: null,
  flesch_kincaid_grade: 1.5,
};

const segment = {
  phoneme: "p",
  segment_class: "consonant",
  marginal: false,
  allophones: ["pʰ"],
  features: [{ name: "voice", value: "-" }],
  glyph_id: "1",
};

const inventory = {
  inventory_id: 1,
  language_name: "English",
  iso639_3: "eng",
  glottocode: "stan1293",
  specific_dialect: null,
  source: "phoible",
  segments: [segment],
  phonemes: ["p"],
  consonants: ["p"],
  vowels: [],
  tones: [],
  marginal_phonemes: [],
  size: 1,
  consonant_count: 1,
  vowel_count: 0,
  tone_count: 0,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("analysis runtime contracts", () => {
  it("accepts exact status, inventory, and metric DTOs", () => {
    expect(
      parsePhonologyStatus({
        cache_available: true,
        loaded: true,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: { inventory_count: 1, language_count: 1, segment_count: 1 },
      }).stats?.segment_count,
    ).toBe(1);
    expect(
      parsePhonologyStatus({
        cache_available: false,
        loaded: false,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: null,
      }).stats,
    ).toBeNull();
    expect(parseInventory(inventory).segments[0]?.features[0]?.name).toBe(
      "voice",
    );
    expect(
      parseInventory({
        ...inventory,
        segments: [
          { ...segment, features: [{ name: "contour", value: "0,0,-,+" }] },
        ],
      }).segments[0]?.features[0]?.value,
    ).toBe("0,0,-,+");
    expect(parseDistribution(distribution).pearson_correlation).toBe(0.9);
    expect(parseTextQuality(textQuality).flesch_reading_ease).toBeNull();
  });

  it("explicitly loads the provisioned PHOIBLE snapshot with an empty POST", async () => {
    const loaded = {
      cache_available: true,
      loaded: true,
      revision: "b".repeat(40),
      sha256: "c".repeat(64),
      stats: {
        inventory_count: 3020,
        language_count: 2095,
        segment_count: 105484,
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(loaded)));
    vi.stubGlobal("fetch", fetchMock);

    expect((await analysisApi.loadPhonology()).stats?.inventory_count).toBe(
      3020,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/phonology/load",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it.each([
    ["status", () => parsePhonologyStatus({ loaded: true })],
    [
      "status stats",
      () =>
        parsePhonologyStatus({
          cache_available: true,
          loaded: true,
          revision: "b".repeat(40),
          sha256: "c".repeat(64),
          stats: {},
        }),
    ],
    ["inventory", () => parseInventory({ ...inventory, size: -1 })],
    [
      "segment",
      () =>
        parseInventory({
          ...inventory,
          segments: [{ ...segment, segment_class: "other" }],
        }),
    ],
    [
      "feature",
      () =>
        parseInventory({
          ...inventory,
          segments: [{ ...segment, features: [{ name: "voice", value: "?" }] }],
        }),
    ],
    [
      "distribution",
      () => parseDistribution({ ...distribution, entropy: Number.NaN }),
    ],
    [
      "text quality",
      () => parseTextQuality({ ...textQuality, total_words: -1 }),
    ],
  ])("rejects malformed %s data", (_, parse) => {
    expect(parse).toThrow(AnalysisContractError);
  });

  it("models finite, infinite, and unavailable error rates without conflating them", () => {
    const result = parseErrorRates({
      wer: { status: "finite", value: 0.25 },
      cer: { status: "positive_infinity", value: null },
      per: { status: "not_computed", value: null },
      ser: { status: "finite", value: 1 },
      details: [
        {
          index: 0,
          reference: "hello",
          hypothesis: "hullo",
          wer: { status: "finite", value: 1 },
          cer: { status: "finite", value: 0.2 },
          per: { status: "not_computed", value: null },
        },
      ],
    });
    expect(result.cer.status).toBe("positive_infinity");
    expect(result.per.status).toBe("not_computed");
  });

  it.each([
    {
      wer: { status: "finite", value: null },
      cer: { status: "finite", value: 0 },
      per: { status: "finite", value: 0 },
      ser: { status: "finite", value: 0 },
      details: [],
    },
    {
      wer: { status: "mystery", value: null },
      cer: { status: "finite", value: 0 },
      per: { status: "finite", value: 0 },
      ser: { status: "finite", value: 0 },
      details: [],
    },
    {
      wer: { status: "finite", value: 0 },
      cer: { status: "finite", value: 0 },
      per: { status: "finite", value: 0 },
      ser: { status: "finite", value: 0 },
      details: [{ index: -1 }],
    },
  ])("rejects invalid rate DTO %#", (value) => {
    expect(() => parseErrorRates(value)).toThrow(AnalysisContractError);
  });

  it("validates coverage trajectory bounds and snapshots", () => {
    const value = {
      unit: "phoneme",
      target_size: 2,
      coverages: [0.5, 1],
      gains: [1, 1],
      snapshots: [
        {
          sentence_index: 0,
          coverage: 0.5,
          covered_count: 1,
          new_units_count: 1,
          new_units: ["p"],
        },
        {
          sentence_index: 1,
          coverage: 1,
          covered_count: 2,
          new_units_count: 1,
          new_units: ["b"],
        },
      ],
    };
    expect(parseTrajectory(value).snapshots).toHaveLength(2);
    expect(() => parseTrajectory({ ...value, coverages: [1.2] })).toThrow(
      AnalysisContractError,
    );
    expect(() =>
      parseTrajectory({
        ...value,
        snapshots: [{ ...value.snapshots[0], coverage: -1 }],
      }),
    ).toThrow(AnalysisContractError);
    expect(() => parseTrajectory({ ...value, unit: "word" })).toThrow(
      AnalysisContractError,
    );
  });
});

describe("analysis API client", () => {
  it("calls inventory lookup endpoints with encoded, normalized queries", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              {
                iso639_3: "eng",
                glottocode: "stan1293",
                language_name: "English",
                inventory_count: 1,
                sources: ["phoible"],
              },
            ],
            total: 1,
            offset: 0,
            limit: 50,
          }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [{ espeak_code: "en-us", iso639_3: "eng" }],
            total: 1,
            offset: 0,
            limit: 50,
          }),
        ),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify(inventory)));
    vi.stubGlobal("fetch", fetchMock);

    expect((await analysisApi.languages("English")).items[0]?.iso639_3).toBe(
      "eng",
    );
    expect(
      (await analysisApi.espeakMappings("en-us")).items[0]?.espeak_code,
    ).toBe("en-us");
    expect(
      (await analysisApi.inventory("eng/name", "source one", true)).size,
    ).toBe(1);
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "/api/v1/phonology/inventories/eng%2Fname?union=true",
    );
  });

  it("posts each analysis DTO as JSON and parses results", async () => {
    const rateResult = {
      wer: { status: "finite", value: 0 },
      cer: { status: "finite", value: 0 },
      per: { status: "not_computed", value: null },
      ser: { status: "finite", value: 0 },
      details: [],
    };
    const trajectory = {
      unit: "phoneme",
      target_size: 1,
      coverages: [1],
      gains: [1],
      snapshots: [
        {
          sentence_index: 0,
          coverage: 1,
          covered_count: 1,
          new_units_count: 1,
          new_units: ["p"],
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(distribution)))
      .mockResolvedValueOnce(new Response(JSON.stringify(textQuality)))
      .mockResolvedValueOnce(new Response(JSON.stringify(rateResult)))
      .mockResolvedValueOnce(new Response(JSON.stringify(trajectory)));
    vi.stubGlobal("fetch", fetchMock);

    await analysisApi.distribution({
      counts: [{ unit: "p", count: 1 }],
      target_units: ["p"],
      reference_distribution: null,
    });
    await analysisApi.textQuality({
      sentences: ["pea"],
      phoneme_sequences: [["p", "i"]],
    });
    await analysisApi.errorRates({
      references: ["a"],
      hypotheses: ["a"],
      reference_phonemes: null,
      hypothesis_phonemes: null,
      case_sensitive: false,
    });
    await analysisApi.trajectory({
      phoneme_sequences: [["p"]],
      target_units: ["p"],
      unit: "phoneme",
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(
      fetchMock.mock.calls.every(
        (call) => (call[1] as RequestInit).method === "POST",
      ),
    ).toBe(true);
  });

  it("surfaces sanitized API errors and request identifiers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "data_unavailable",
            message: "PHOIBLE is not provisioned.",
            request_id: "req-safe",
          }),
          { status: 503 },
        ),
      ),
    );
    await expect(analysisApi.phonologyStatus()).rejects.toMatchObject({
      status: 503,
      code: "data_unavailable",
      requestId: "req-safe",
    });
  });

  it("uses a generic message for non-JSON errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("bad gateway", { status: 502 })),
    );
    await expect(analysisApi.phonologyStatus()).rejects.toThrow(
      "Request failed with HTTP 502",
    );
  });

  it("rejects malformed page envelopes", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ items: [], total: -1, offset: 0, limit: 50 }),
          ),
        ),
    );
    await expect(analysisApi.languages("")).rejects.toBeInstanceOf(
      AnalysisContractError,
    );
  });
});
