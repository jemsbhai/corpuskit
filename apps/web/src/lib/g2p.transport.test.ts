import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import { ApiContractError } from "@/lib/api-client";
import { authenticatedFetch } from "@/lib/browser-auth";
import { g2pApi, parseTranscription } from "@/lib/g2p";

const fetchMock = vi.mocked(authenticatedFetch);
const valid = {
  text: "hi",
  language: "en-us",
  ipa: "haɪ",
  phonemes: ["h", "aɪ"],
  diphones: ["h aɪ"],
  triphones: [],
  phoneme_count: 2,
  unique_phonemes: ["h", "aɪ"],
};
const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });

afterEach(() => fetchMock.mockReset());

describe("G2P transport", () => {
  it("calls all supported endpoints and preserves batch order", async () => {
    fetchMock
      .mockResolvedValueOnce(
        response({ backend: "espeak", languages: ["en-us", "ar"] }),
      )
      .mockResolvedValueOnce(response(valid))
      .mockResolvedValueOnce(response([valid, { ...valid, text: "مرحبا" }]))
      .mockResolvedValueOnce(
        response({
          backend: "espeak",
          requested_language: "en",
          variants: [valid],
        }),
      );
    await expect(g2pApi.languages()).resolves.toMatchObject({
      languages: ["en-us", "ar"],
    });
    await expect(g2pApi.single("hi", "en-us")).resolves.toMatchObject({
      ipa: "haɪ",
    });
    await expect(g2pApi.batch(["hi", "مرحبا"], "en-us")).resolves.toHaveLength(
      2,
    );
    await expect(g2pApi.variants("hi", "en")).resolves.toMatchObject({
      requested_language: "en",
      variants: [valid],
    });
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/labs/g2p/languages",
      "/api/v1/g2p",
      "/api/v1/g2p/batch",
      "/api/v1/labs/g2p/variants",
    ]);
  });

  it.each([
    null,
    [],
    { ...valid, text: 1 },
    { ...valid, language: 1 },
    { ...valid, ipa: 1 },
    { ...valid, phonemes: [1] },
    { ...valid, diphones: [1] },
    { ...valid, triphones: [1] },
    { ...valid, phoneme_count: -1 },
    { ...valid, unique_phonemes: [1] },
    { ...valid, phoneme_count: 1 },
  ])("rejects each malformed transcription field", (value) =>
    expect(() => parseTranscription(value)).toThrow(ApiContractError),
  );

  it("rejects malformed language and variant catalogs", async () => {
    for (const value of [
      {},
      { backend: 1, languages: [] },
      { backend: "x", languages: [1] },
    ]) {
      fetchMock.mockResolvedValueOnce(response(value));
      await expect(g2pApi.languages()).rejects.toBeInstanceOf(ApiContractError);
    }
    for (const value of [
      {},
      { backend: 1, requested_language: "en", variants: [] },
      { backend: "x", requested_language: 1, variants: [] },
      { backend: "x", requested_language: "en", variants: {} },
      {
        backend: "x",
        requested_language: "en",
        variants: [{ ...valid, ipa: 1 }],
      },
    ]) {
      fetchMock.mockResolvedValueOnce(response(value));
      await expect(g2pApi.variants("hi", "en")).rejects.toBeInstanceOf(
        ApiContractError,
      );
    }
  });
});
