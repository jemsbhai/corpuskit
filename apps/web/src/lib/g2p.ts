import {
  ApiContractError,
  isNonnegativeInteger,
  isRecord,
  isStringArray,
  postJson,
  requestJson,
} from "@/lib/api-client";

export interface G2PTranscription {
  readonly text: string;
  readonly language: string;
  readonly ipa: string;
  readonly phonemes: string[];
  readonly diphones: string[];
  readonly triphones: string[];
  readonly phoneme_count: number;
  readonly unique_phonemes: string[];
}

export interface G2PLanguageCatalog {
  readonly backend: string;
  readonly languages: string[];
}

export interface G2PVariantSet {
  readonly backend: string;
  readonly requested_language: string;
  readonly variants: G2PTranscription[];
}

export function parseTranscription(value: unknown): G2PTranscription {
  if (
    !isRecord(value) ||
    typeof value.text !== "string" ||
    typeof value.language !== "string" ||
    typeof value.ipa !== "string" ||
    !isStringArray(value.phonemes) ||
    !isStringArray(value.diphones) ||
    !isStringArray(value.triphones) ||
    !isNonnegativeInteger(value.phoneme_count) ||
    !isStringArray(value.unique_phonemes) ||
    value.phoneme_count !== value.phonemes.length
  ) {
    throw new ApiContractError();
  }
  return value as unknown as G2PTranscription;
}

export function parseTranscriptions(value: unknown): G2PTranscription[] {
  if (!Array.isArray(value)) throw new ApiContractError();
  return value.map(parseTranscription);
}

export const g2pApi = {
  languages: (signal?: AbortSignal) =>
    requestJson(
      "/api/v1/labs/g2p/languages",
      (value): G2PLanguageCatalog => {
        if (
          !isRecord(value) ||
          typeof value.backend !== "string" ||
          !isStringArray(value.languages)
        ) {
          throw new ApiContractError();
        }
        return value as unknown as G2PLanguageCatalog;
      },
      { signal },
    ),
  single: (text: string, language: string, signal?: AbortSignal) =>
    requestJson(
      "/api/v1/g2p",
      parseTranscription,
      postJson({ text, language }, signal),
    ),
  batch: (texts: readonly string[], language: string, signal?: AbortSignal) =>
    requestJson(
      "/api/v1/g2p/batch",
      parseTranscriptions,
      postJson({ texts, language }, signal),
    ),
  variants: (text: string, language: string, signal?: AbortSignal) =>
    requestJson(
      "/api/v1/labs/g2p/variants",
      (value): G2PVariantSet => {
        if (
          !isRecord(value) ||
          typeof value.backend !== "string" ||
          typeof value.requested_language !== "string" ||
          !Array.isArray(value.variants)
        ) {
          throw new ApiContractError();
        }
        return {
          backend: value.backend,
          requested_language: value.requested_language,
          variants: value.variants.map(parseTranscription),
        };
      },
      postJson({ text, language }, signal),
    ),
};
