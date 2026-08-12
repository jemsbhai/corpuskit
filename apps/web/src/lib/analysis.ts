import type {
  CoverageUnit,
  DistributionMetrics,
  TextQualityMetrics,
} from "@/lib/evaluation";
import { authenticatedFetch } from "@/lib/browser-auth";

export interface PageResult<T> {
  readonly items: T[];
  readonly total: number;
  readonly offset: number;
  readonly limit: number;
}

export interface PhonologyStatus {
  readonly cache_available: boolean;
  readonly loaded: boolean;
  readonly revision: string;
  readonly sha256: string;
  readonly stats: null | {
    readonly inventory_count: number;
    readonly language_count: number;
    readonly segment_count: number;
  };
}

export interface LanguageSummary {
  readonly iso639_3: string;
  readonly glottocode: string;
  readonly language_name: string;
  readonly inventory_count: number;
  readonly sources: string[];
}

export interface EspeakMapping {
  readonly espeak_code: string;
  readonly iso639_3: string;
}

export interface InventorySources {
  readonly identifier: string;
  readonly sources: string[];
}

export interface FeatureCatalog {
  readonly names: string[];
}

export interface PhoneticFeature {
  readonly name: string;
  readonly value: string;
}

export interface Segment {
  readonly phoneme: string;
  readonly segment_class: "consonant" | "vowel" | "tone";
  readonly marginal: boolean;
  readonly allophones: string[];
  readonly features: PhoneticFeature[];
  readonly glyph_id: string;
}

export interface Inventory {
  readonly inventory_id: number;
  readonly language_name: string;
  readonly iso639_3: string;
  readonly glottocode: string;
  readonly specific_dialect: string | null;
  readonly source: string;
  readonly segments: Segment[];
  readonly phonemes: string[];
  readonly consonants: string[];
  readonly vowels: string[];
  readonly tones: string[];
  readonly marginal_phonemes: string[];
  readonly size: number;
  readonly consonant_count: number;
  readonly vowel_count: number;
  readonly tone_count: number;
}

export interface UnitCountInput {
  readonly unit: string;
  readonly count: number;
}

export interface UnitWeightInput {
  readonly unit: string;
  readonly weight: number;
}

export type RateValue =
  | { readonly status: "finite"; readonly value: number }
  | {
      readonly status: "positive_infinity" | "not_computed";
      readonly value: null;
    };

export interface ErrorRateDetail {
  readonly index: number;
  readonly reference: string;
  readonly hypothesis: string;
  readonly wer: RateValue;
  readonly cer: RateValue;
  readonly per: RateValue;
}

export interface ErrorRateResult {
  readonly wer: RateValue;
  readonly cer: RateValue;
  readonly per: RateValue;
  readonly ser: RateValue;
  readonly details: ErrorRateDetail[];
}

export interface CoverageSnapshot {
  readonly sentence_index: number;
  readonly coverage: number;
  readonly covered_count: number;
  readonly new_units_count: number;
  readonly new_units: string[];
}

export interface CoverageTrajectory {
  readonly unit: CoverageUnit;
  readonly target_size: number;
  readonly coverages: number[];
  readonly gains: number[];
  readonly snapshots: CoverageSnapshot[];
}

export class AnalysisContractError extends Error {
  constructor() {
    super("The analysis API returned an incompatible response.");
    this.name = "AnalysisContractError";
  }
}

export class AnalysisApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "AnalysisApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function nonnegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function strings(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}

function nullableFinite(value: unknown): value is number | null {
  return value === null || finite(value);
}

export function parsePage<T>(
  value: unknown,
  itemParser: (item: unknown) => T,
): PageResult<T> {
  if (!record(value) || !Array.isArray(value.items))
    throw new AnalysisContractError();
  const items = value.items.map(itemParser);
  if (
    !nonnegativeInteger(value.total) ||
    !nonnegativeInteger(value.offset) ||
    !nonnegativeInteger(value.limit)
  ) {
    throw new AnalysisContractError();
  }
  return {
    items,
    total: value.total,
    offset: value.offset,
    limit: value.limit,
  };
}

export function parsePhonologyStatus(value: unknown): PhonologyStatus {
  if (
    !record(value) ||
    typeof value.cache_available !== "boolean" ||
    typeof value.loaded !== "boolean" ||
    typeof value.revision !== "string" ||
    !/^[0-9a-f]{40}$/u.test(value.revision) ||
    typeof value.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.sha256)
  ) {
    throw new AnalysisContractError();
  }
  if (value.stats !== null) {
    if (
      !record(value.stats) ||
      !nonnegativeInteger(value.stats.inventory_count) ||
      !nonnegativeInteger(value.stats.language_count) ||
      !nonnegativeInteger(value.stats.segment_count)
    ) {
      throw new AnalysisContractError();
    }
  }
  return value as unknown as PhonologyStatus;
}

export function parseLanguage(value: unknown): LanguageSummary {
  if (
    !record(value) ||
    typeof value.iso639_3 !== "string" ||
    typeof value.glottocode !== "string" ||
    typeof value.language_name !== "string" ||
    !nonnegativeInteger(value.inventory_count) ||
    !strings(value.sources)
  ) {
    throw new AnalysisContractError();
  }
  return value as unknown as LanguageSummary;
}

export function parseMapping(value: unknown): EspeakMapping {
  if (
    !record(value) ||
    typeof value.espeak_code !== "string" ||
    typeof value.iso639_3 !== "string"
  ) {
    throw new AnalysisContractError();
  }
  return value as unknown as EspeakMapping;
}

export function parseSegment(value: unknown): Segment {
  if (
    !record(value) ||
    typeof value.phoneme !== "string" ||
    (value.segment_class !== "consonant" &&
      value.segment_class !== "vowel" &&
      value.segment_class !== "tone") ||
    typeof value.marginal !== "boolean" ||
    !strings(value.allophones) ||
    !Array.isArray(value.features) ||
    typeof value.glyph_id !== "string"
  ) {
    throw new AnalysisContractError();
  }
  const features = value.features.map((feature) => {
    if (
      !record(feature) ||
      typeof feature.name !== "string" ||
      typeof feature.value !== "string" ||
      !/^[+\-0](?:,[+\-0])*$/u.test(feature.value) ||
      feature.value.length > 15
    ) {
      throw new AnalysisContractError();
    }
    return feature as unknown as PhoneticFeature;
  });
  return { ...(value as unknown as Omit<Segment, "features">), features };
}

export function parseInventory(value: unknown): Inventory {
  if (
    !record(value) ||
    !nonnegativeInteger(value.inventory_id) ||
    typeof value.language_name !== "string" ||
    typeof value.iso639_3 !== "string" ||
    typeof value.glottocode !== "string" ||
    (value.specific_dialect !== null &&
      typeof value.specific_dialect !== "string") ||
    typeof value.source !== "string" ||
    !Array.isArray(value.segments) ||
    !strings(value.phonemes) ||
    !strings(value.consonants) ||
    !strings(value.vowels) ||
    !strings(value.tones) ||
    !strings(value.marginal_phonemes) ||
    !nonnegativeInteger(value.size) ||
    !nonnegativeInteger(value.consonant_count) ||
    !nonnegativeInteger(value.vowel_count) ||
    !nonnegativeInteger(value.tone_count)
  ) {
    throw new AnalysisContractError();
  }
  const segments = value.segments.map(parseSegment);
  return { ...(value as unknown as Omit<Inventory, "segments">), segments };
}

const distributionFloatFields = [
  "entropy",
  "normalized_entropy",
  "jsd_uniform",
  "coefficient_of_variation",
  "count_ratio",
  "pcd_uniform",
] as const;
const distributionIntegerFields = [
  "min_count",
  "max_count",
  "zero_count",
] as const;

export function parseDistribution(value: unknown): DistributionMetrics {
  if (
    !record(value) ||
    !distributionFloatFields.every((field) => finite(value[field])) ||
    !distributionIntegerFields.every((field) =>
      nonnegativeInteger(value[field]),
    ) ||
    !nullableFinite(value.jsd_reference) ||
    !nullableFinite(value.pearson_correlation)
  ) {
    throw new AnalysisContractError();
  }
  return value as unknown as DistributionMetrics;
}

const qualityFloatFields = [
  "sentence_length_words_mean",
  "sentence_length_words_median",
  "sentence_length_words_std",
  "sentence_length_phonemes_mean",
  "sentence_length_phonemes_median",
  "sentence_length_phonemes_std",
  "type_token_ratio",
  "hapax_ratio",
] as const;
const qualityIntegerFields = [
  "sentence_length_words_min",
  "sentence_length_words_max",
  "sentence_length_phonemes_min",
  "sentence_length_phonemes_max",
  "total_words",
  "unique_words",
] as const;

export function parseTextQuality(value: unknown): TextQualityMetrics {
  if (
    !record(value) ||
    !qualityFloatFields.every((field) => finite(value[field])) ||
    !qualityIntegerFields.every((field) => nonnegativeInteger(value[field])) ||
    !nullableFinite(value.flesch_reading_ease) ||
    !nullableFinite(value.flesch_kincaid_grade)
  ) {
    throw new AnalysisContractError();
  }
  return value as unknown as TextQualityMetrics;
}

function parseRate(value: unknown): RateValue {
  if (!record(value) || typeof value.status !== "string")
    throw new AnalysisContractError();
  if (value.status === "finite" && finite(value.value))
    return { status: "finite", value: value.value };
  if (
    (value.status === "positive_infinity" || value.status === "not_computed") &&
    value.value === null
  ) {
    return { status: value.status, value: null };
  }
  throw new AnalysisContractError();
}

export function parseErrorRates(value: unknown): ErrorRateResult {
  if (!record(value) || !Array.isArray(value.details))
    throw new AnalysisContractError();
  const details = value.details.map((detail) => {
    if (
      !record(detail) ||
      !nonnegativeInteger(detail.index) ||
      typeof detail.reference !== "string" ||
      typeof detail.hypothesis !== "string"
    ) {
      throw new AnalysisContractError();
    }
    return {
      index: detail.index,
      reference: detail.reference,
      hypothesis: detail.hypothesis,
      wer: parseRate(detail.wer),
      cer: parseRate(detail.cer),
      per: parseRate(detail.per),
    };
  });
  return {
    wer: parseRate(value.wer),
    cer: parseRate(value.cer),
    per: parseRate(value.per),
    ser: parseRate(value.ser),
    details,
  };
}

export function parseTrajectory(value: unknown): CoverageTrajectory {
  if (
    !record(value) ||
    (value.unit !== "phoneme" &&
      value.unit !== "diphone" &&
      value.unit !== "triphone") ||
    !nonnegativeInteger(value.target_size) ||
    !Array.isArray(value.coverages) ||
    !value.coverages.every((item) => finite(item) && item >= 0 && item <= 1) ||
    !Array.isArray(value.gains) ||
    !value.gains.every(nonnegativeInteger) ||
    !Array.isArray(value.snapshots)
  ) {
    throw new AnalysisContractError();
  }
  const snapshots = value.snapshots.map((snapshot) => {
    if (
      !record(snapshot) ||
      !nonnegativeInteger(snapshot.sentence_index) ||
      !finite(snapshot.coverage) ||
      snapshot.coverage < 0 ||
      snapshot.coverage > 1 ||
      !nonnegativeInteger(snapshot.covered_count) ||
      !nonnegativeInteger(snapshot.new_units_count) ||
      !strings(snapshot.new_units)
    ) {
      throw new AnalysisContractError();
    }
    return snapshot as unknown as CoverageSnapshot;
  });
  return {
    unit: value.unit,
    target_size: value.target_size,
    coverages: value.coverages as number[],
    gains: value.gains as number[],
    snapshots,
  };
}

async function api<T>(
  path: string,
  parser: (value: unknown) => T,
  init?: RequestInit,
): Promise<T> {
  const response = await authenticatedFetch(path, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
    },
    ...init,
  });
  if (!response.ok) {
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (record(body) && typeof body.message === "string") {
      throw new AnalysisApiError(
        response.status,
        typeof body.code === "string" ? body.code : "request_failed",
        body.message,
        typeof body.request_id === "string" ? body.request_id : null,
      );
    }
    throw new AnalysisApiError(
      response.status,
      "request_failed",
      `Request failed with HTTP ${response.status}.`,
    );
  }
  return parser((await response.json()) as unknown);
}

function query(
  parameters: Record<string, string | number | boolean | undefined>,
): string {
  const result = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== undefined && value !== "") result.set(key, String(value));
  }
  const encoded = result.toString();
  return encoded ? `?${encoded}` : "";
}

export const analysisApi = {
  phonologyStatus: (signal?: AbortSignal) =>
    api("/api/v1/phonology/status", parsePhonologyStatus, { signal }),
  loadPhonology: (signal?: AbortSignal) =>
    api("/api/v1/phonology/load", parsePhonologyStatus, {
      method: "POST",
      signal,
    }),
  languages: (search: string, signal?: AbortSignal, offset = 0, limit = 50) =>
    api(
      `/api/v1/phonology/languages${query({ query: search, offset, limit })}`,
      (value) => parsePage(value, parseLanguage),
      { signal },
    ),
  espeakMappings: (
    search: string,
    signal?: AbortSignal,
    offset = 0,
    limit = 50,
  ) =>
    api(
      `/api/v1/phonology/espeak-mappings${query({ query: search, offset, limit })}`,
      (value) => parsePage(value, parseMapping),
      { signal },
    ),
  inventory: (
    identifier: string,
    source: string | undefined,
    union: boolean,
    signal?: AbortSignal,
  ) =>
    api(
      `/api/v1/phonology/inventories/${encodeURIComponent(identifier)}${query({ source: union ? undefined : source, union })}`,
      parseInventory,
      { signal },
    ),
  featureCatalog: (signal?: AbortSignal) =>
    api(
      "/api/v1/phonology/features",
      (value): FeatureCatalog => {
        if (!record(value) || !strings(value.names)) {
          throw new AnalysisContractError();
        }
        return { names: value.names };
      },
      { signal },
    ),
  inventorySources: (identifier: string, signal?: AbortSignal) =>
    api(
      `/api/v1/phonology/inventories/${encodeURIComponent(identifier)}/sources`,
      (value): InventorySources => {
        if (
          !record(value) ||
          typeof value.identifier !== "string" ||
          !strings(value.sources)
        ) {
          throw new AnalysisContractError();
        }
        return { identifier: value.identifier, sources: value.sources };
      },
      { signal },
    ),
  allInventories: (
    identifier: string,
    offset = 0,
    limit = 5,
    signal?: AbortSignal,
  ) =>
    api(
      `/api/v1/phonology/inventories/${encodeURIComponent(identifier)}/all${query({ offset, limit })}`,
      (value) => parsePage(value, parseInventory),
      { signal },
    ),
  segments: (
    identifier: string,
    filters: {
      readonly source?: string;
      readonly union?: boolean;
      readonly segmentClass?: "consonant" | "vowel" | "tone";
      readonly marginal?: boolean;
      readonly featureName?: string;
      readonly featureValue?: string;
      readonly offset?: number;
      readonly limit?: number;
    },
    signal?: AbortSignal,
  ) =>
    api(
      `/api/v1/phonology/inventories/${encodeURIComponent(identifier)}/segments${query(
        {
          source: filters.union ? undefined : filters.source,
          union: filters.union,
          segment_class: filters.segmentClass,
          marginal: filters.marginal,
          feature_name: filters.featureName,
          feature_value: filters.featureValue,
          offset: filters.offset ?? 0,
          limit: filters.limit ?? 50,
        },
      )}`,
      (value) => parsePage(value, parseSegment),
      { signal },
    ),
  distribution: (
    body: {
      counts: UnitCountInput[];
      target_units: string[];
      reference_distribution: UnitWeightInput[] | null;
    },
    signal?: AbortSignal,
  ) =>
    api("/api/v1/analyses/distribution", parseDistribution, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  textQuality: (
    body: { sentences: string[]; phoneme_sequences: string[][] },
    signal?: AbortSignal,
  ) =>
    api("/api/v1/analyses/text-quality", parseTextQuality, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  errorRates: (
    body: {
      references: string[];
      hypotheses: string[];
      reference_phonemes: string[][] | null;
      hypothesis_phonemes: string[][] | null;
      case_sensitive: boolean;
    },
    signal?: AbortSignal,
  ) =>
    api("/api/v1/analyses/error-rates", parseErrorRates, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  trajectory: (
    body: {
      phoneme_sequences: string[][];
      target_units: string[];
      unit: CoverageUnit;
    },
    signal?: AbortSignal,
  ) =>
    api("/api/v1/analyses/coverage-trajectory", parseTrajectory, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
};
