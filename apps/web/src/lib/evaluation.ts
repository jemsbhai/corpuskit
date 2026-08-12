import { authenticatedFetch } from "@/lib/browser-auth";

export type CoverageUnit = "phoneme" | "diphone" | "triphone";
export type TargetMode = "derived" | "explicit" | "phoible";

export interface EvaluationRequest {
  readonly sentences: string[];
  readonly language: string;
  readonly unit: CoverageUnit;
  readonly target: {
    readonly mode: TargetMode;
    readonly phonemes: string[];
  };
}

export interface UnitCount {
  readonly unit: string;
  readonly count: number;
}

export interface SentenceDetail {
  readonly index: number;
  readonly text: string;
  readonly phoneme_count: number;
  readonly new_units: string[];
  readonly all_phonemes: string[];
}

export interface UnitSource {
  readonly unit: string;
  readonly sentence_indices: number[];
}

export interface DistributionMetrics {
  readonly entropy: number;
  readonly normalized_entropy: number;
  readonly jsd_uniform: number;
  readonly coefficient_of_variation: number;
  readonly min_count: number;
  readonly max_count: number;
  readonly count_ratio: number;
  readonly zero_count: number;
  readonly pcd_uniform: number;
  readonly jsd_reference: number | null;
  readonly pearson_correlation: number | null;
}

export interface TextQualityMetrics {
  readonly sentence_length_words_mean: number;
  readonly sentence_length_words_median: number;
  readonly sentence_length_words_std: number;
  readonly sentence_length_words_min: number;
  readonly sentence_length_words_max: number;
  readonly sentence_length_phonemes_mean: number;
  readonly sentence_length_phonemes_median: number;
  readonly sentence_length_phonemes_std: number;
  readonly sentence_length_phonemes_min: number;
  readonly sentence_length_phonemes_max: number;
  readonly total_words: number;
  readonly unique_words: number;
  readonly type_token_ratio: number;
  readonly hapax_ratio: number;
  readonly flesch_reading_ease: number | null;
  readonly flesch_kincaid_grade: number | null;
}

export interface EvaluationResult {
  readonly language: string;
  readonly unit: CoverageUnit;
  readonly target_mode: TargetMode;
  readonly target_units: string[];
  readonly covered_units: string[];
  readonly missing_units: string[];
  readonly coverage: number;
  readonly total_sentences: number;
  readonly unit_counts: UnitCount[];
  readonly sentence_details: SentenceDetail[];
  readonly unit_sources: UnitSource[];
  readonly distribution: DistributionMetrics | null;
  readonly text_quality: TextQualityMetrics | null;
}

export class EvaluationContractError extends Error {
  constructor(
    message = "The evaluation API returned an incompatible response.",
  ) {
    super(message);
    this.name = "EvaluationContractError";
  }
}

export class EvaluationApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly operation: string | null;
  readonly requestId: string | null;

  constructor(options: {
    message: string;
    status: number;
    code: string;
    operation?: string | null;
    requestId?: string | null;
  }) {
    super(options.message);
    this.name = "EvaluationApiError";
    this.status = options.status;
    this.code = options.code;
    this.operation = options.operation ?? null;
    this.requestId = options.requestId ?? null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}

function isIntegerArray(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.every((item) => Number.isInteger(item) && item >= 0)
  );
}

function isUnitCount(value: unknown): value is UnitCount {
  return (
    isRecord(value) &&
    typeof value.unit === "string" &&
    Number.isInteger(value.count) &&
    (value.count as number) >= 0
  );
}

function isSentenceDetail(value: unknown): value is SentenceDetail {
  return (
    isRecord(value) &&
    Number.isInteger(value.index) &&
    (value.index as number) >= 0 &&
    typeof value.text === "string" &&
    Number.isInteger(value.phoneme_count) &&
    (value.phoneme_count as number) >= 0 &&
    isStringArray(value.new_units) &&
    isStringArray(value.all_phonemes)
  );
}

function isUnitSource(value: unknown): value is UnitSource {
  return (
    isRecord(value) &&
    typeof value.unit === "string" &&
    isIntegerArray(value.sentence_indices)
  );
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

function isDistribution(value: unknown): value is DistributionMetrics {
  return (
    isRecord(value) &&
    distributionFloatFields.every((field) => isFiniteNumber(value[field])) &&
    distributionIntegerFields.every(
      (field) =>
        Number.isInteger(value[field]) && (value[field] as number) >= 0,
    ) &&
    isNullableNumber(value.jsd_reference) &&
    isNullableNumber(value.pearson_correlation)
  );
}

const textQualityFloatFields = [
  "sentence_length_words_mean",
  "sentence_length_words_median",
  "sentence_length_words_std",
  "sentence_length_phonemes_mean",
  "sentence_length_phonemes_median",
  "sentence_length_phonemes_std",
  "type_token_ratio",
  "hapax_ratio",
] as const;

const textQualityIntegerFields = [
  "sentence_length_words_min",
  "sentence_length_words_max",
  "sentence_length_phonemes_min",
  "sentence_length_phonemes_max",
  "total_words",
  "unique_words",
] as const;

function isTextQuality(value: unknown): value is TextQualityMetrics {
  return (
    isRecord(value) &&
    textQualityFloatFields.every((field) => isFiniteNumber(value[field])) &&
    textQualityIntegerFields.every(
      (field) =>
        Number.isInteger(value[field]) && (value[field] as number) >= 0,
    ) &&
    isNullableNumber(value.flesch_reading_ease) &&
    isNullableNumber(value.flesch_kincaid_grade)
  );
}

export function parseEvaluationResult(value: unknown): EvaluationResult {
  if (!isRecord(value)) throw new EvaluationContractError();

  const validUnit =
    value.unit === "phoneme" ||
    value.unit === "diphone" ||
    value.unit === "triphone";
  const validTargetMode =
    value.target_mode === "derived" ||
    value.target_mode === "explicit" ||
    value.target_mode === "phoible";
  const validDistribution =
    value.distribution === null || isDistribution(value.distribution);
  const validTextQuality =
    value.text_quality === null || isTextQuality(value.text_quality);

  if (
    typeof value.language !== "string" ||
    !validUnit ||
    !validTargetMode ||
    !isStringArray(value.target_units) ||
    !isStringArray(value.covered_units) ||
    !isStringArray(value.missing_units) ||
    !isFiniteNumber(value.coverage) ||
    value.coverage < 0 ||
    value.coverage > 1 ||
    !Number.isInteger(value.total_sentences) ||
    (value.total_sentences as number) < 0 ||
    !Array.isArray(value.unit_counts) ||
    !value.unit_counts.every(isUnitCount) ||
    !Array.isArray(value.sentence_details) ||
    !value.sentence_details.every(isSentenceDetail) ||
    !Array.isArray(value.unit_sources) ||
    !value.unit_sources.every(isUnitSource) ||
    !validDistribution ||
    !validTextQuality
  ) {
    throw new EvaluationContractError();
  }

  return value as unknown as EvaluationResult;
}

async function parseErrorResponse(
  response: Response,
): Promise<EvaluationApiError> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    value = null;
  }

  if (isRecord(value) && typeof value.message === "string") {
    return new EvaluationApiError({
      status: response.status,
      code: typeof value.code === "string" ? value.code : "request_failed",
      message: value.message,
      operation: typeof value.operation === "string" ? value.operation : null,
      requestId: typeof value.request_id === "string" ? value.request_id : null,
    });
  }

  const validationMessage =
    response.status === 422
      ? "The API rejected the request. Check the sentence and configuration limits."
      : response.status === 413
        ? "The request is larger than this deployment's upload limit."
        : `Evaluation failed with HTTP ${response.status}.`;
  return new EvaluationApiError({
    status: response.status,
    code: response.status === 422 ? "invalid_request" : "request_failed",
    message: validationMessage,
  });
}

export async function evaluateCorpus(
  request: EvaluationRequest,
  signal?: AbortSignal,
): Promise<EvaluationResult> {
  const response = await authenticatedFetch("/api/v1/evaluations", {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) throw await parseErrorResponse(response);
  return parseEvaluationResult((await response.json()) as unknown);
}
