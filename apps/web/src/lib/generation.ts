import {
  ApiContractError,
  isFiniteNumber,
  isNonnegativeInteger,
  isRecord,
  isStringArray,
  postJson,
  requestJson,
} from "@/lib/api-client";

export type CoverageUnit = "phoneme" | "diphone" | "triphone";
export type SourceKind = "raw_text" | "prephonemized" | "hugging_face";

export interface RepositoryGenerationResult {
  readonly schema_id: "corpuskit.repository-generation-result.v1";
  readonly execution_mode: "synchronous_preview" | "worker_activity";
  readonly source_kind: SourceKind;
  readonly unit: CoverageUnit;
  readonly accepted: readonly {
    readonly source_id: string;
    readonly text: string;
    readonly phonemes: readonly string[];
    readonly iteration: number;
    readonly coverage_gain: number;
  }[];
  readonly coverage: number;
  readonly covered_units: readonly string[];
  readonly missing_units: readonly string[];
  readonly iterations: number;
  readonly elapsed_seconds: number;
  readonly stop_reason: string;
}

export interface CandidateScore {
  readonly source_id: string;
  readonly text: string;
  readonly phonemes: readonly string[];
  readonly coverage_gain: number;
  readonly weighted_coverage_gain: number;
  readonly phonotactic_score: number;
  readonly fluency_score: number;
  readonly readability_status: "available" | "unavailable";
  readonly readability_score: number | null;
  readonly composite_score: number;
  readonly new_units: readonly string[];
}

export interface CompositeScoringResult {
  readonly schema_id: "corpuskit.composite-scoring-result.v1";
  readonly ranked: readonly CandidateScore[];
  readonly committed: CandidateScore | null;
  readonly covered_units_before: readonly string[];
  readonly covered_units_after: readonly string[];
}

export interface PhonotacticArtifact {
  readonly artifact_type:
    | "corpusgen.ngram-phonotactic-scorer"
    | "corpusgen.ngram-phonotactic-constraint";
  readonly schema_version: 1;
  readonly payload: Readonly<Record<string, unknown>>;
  readonly content_sha256: string;
}

export interface PhonotacticScoreResult {
  readonly artifact_type: PhonotacticArtifact["artifact_type"];
  readonly scores: readonly number[];
}

export interface ReadabilityResult {
  readonly text: string;
  readonly status: "available" | "unavailable";
  readonly flesch_reading_ease: number | null;
  readonly score: number | null;
  readonly accepted_by_filter: boolean | null;
}

function coverageUnit(value: unknown): value is CoverageUnit {
  return value === "phoneme" || value === "diphone" || value === "triphone";
}

function sourceKind(value: unknown): value is SourceKind {
  return (
    value === "raw_text" ||
    value === "prephonemized" ||
    value === "hugging_face"
  );
}

function nullableFinite(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function parseCandidateScore(value: unknown): CandidateScore {
  if (!isRecord(value)) throw new ApiContractError();
  const fluencyScore = value.fluency_score ?? 0;
  if (
    typeof value.source_id !== "string" ||
    typeof value.text !== "string" ||
    !isStringArray(value.phonemes) ||
    !isNonnegativeInteger(value.coverage_gain) ||
    !isFiniteNumber(value.weighted_coverage_gain) ||
    !isFiniteNumber(value.phonotactic_score) ||
    !isFiniteNumber(fluencyScore) ||
    fluencyScore < 0 ||
    fluencyScore > 1 ||
    (value.readability_status !== "available" &&
      value.readability_status !== "unavailable") ||
    !nullableFinite(value.readability_score) ||
    !isFiniteNumber(value.composite_score) ||
    !isStringArray(value.new_units)
  ) {
    throw new ApiContractError();
  }
  return { ...value, fluency_score: fluencyScore } as unknown as CandidateScore;
}

export function parseRepositoryGeneration(
  value: unknown,
): RepositoryGenerationResult {
  if (
    !isRecord(value) ||
    value.schema_id !== "corpuskit.repository-generation-result.v1" ||
    (value.execution_mode !== "synchronous_preview" &&
      value.execution_mode !== "worker_activity") ||
    !sourceKind(value.source_kind) ||
    !coverageUnit(value.unit) ||
    !Array.isArray(value.accepted) ||
    !value.accepted.every(
      (item) =>
        isRecord(item) &&
        typeof item.source_id === "string" &&
        typeof item.text === "string" &&
        isStringArray(item.phonemes) &&
        isNonnegativeInteger(item.iteration) &&
        isNonnegativeInteger(item.coverage_gain),
    ) ||
    !isFiniteNumber(value.coverage) ||
    value.coverage < 0 ||
    value.coverage > 1 ||
    !isStringArray(value.covered_units) ||
    !isStringArray(value.missing_units) ||
    !isNonnegativeInteger(value.iterations) ||
    !isFiniteNumber(value.elapsed_seconds) ||
    typeof value.stop_reason !== "string"
  ) {
    throw new ApiContractError();
  }
  return value as unknown as RepositoryGenerationResult;
}

export function parseCompositeScoring(value: unknown): CompositeScoringResult {
  if (
    !isRecord(value) ||
    value.schema_id !== "corpuskit.composite-scoring-result.v1" ||
    !Array.isArray(value.ranked) ||
    !isStringArray(value.covered_units_before) ||
    !isStringArray(value.covered_units_after)
  ) {
    throw new ApiContractError();
  }
  const ranked = value.ranked.map(parseCandidateScore);
  const committed =
    value.committed === null ? null : parseCandidateScore(value.committed);
  return {
    schema_id: value.schema_id,
    ranked,
    committed,
    covered_units_before: value.covered_units_before,
    covered_units_after: value.covered_units_after,
  };
}

export function parsePhonotacticArtifact(value: unknown): PhonotacticArtifact {
  if (
    !isRecord(value) ||
    (value.artifact_type !== "corpusgen.ngram-phonotactic-scorer" &&
      value.artifact_type !== "corpusgen.ngram-phonotactic-constraint") ||
    value.schema_version !== 1 ||
    !isRecord(value.payload) ||
    typeof value.content_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.content_sha256)
  ) {
    throw new ApiContractError();
  }
  return value as unknown as PhonotacticArtifact;
}

export function parsePhonotacticScores(value: unknown): PhonotacticScoreResult {
  if (
    !isRecord(value) ||
    !Array.isArray(value.scores) ||
    !value.scores.every(isFiniteNumber)
  ) {
    throw new ApiContractError();
  }
  const artifact = parsePhonotacticArtifact({
    artifact_type: value.artifact_type,
    schema_version: 1,
    payload: {},
    content_sha256: "0".repeat(64),
  });
  return { artifact_type: artifact.artifact_type, scores: value.scores };
}

export function parseReadability(value: unknown): readonly ReadabilityResult[] {
  if (!isRecord(value) || !Array.isArray(value.results))
    throw new ApiContractError();
  return value.results.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.text !== "string" ||
      (item.status !== "available" && item.status !== "unavailable") ||
      !nullableFinite(item.flesch_reading_ease) ||
      !nullableFinite(item.score) ||
      (item.accepted_by_filter !== null &&
        typeof item.accepted_by_filter !== "boolean")
    ) {
      throw new ApiContractError();
    }
    return item as unknown as ReadabilityResult;
  });
}

export const generationApi = {
  preview(body: unknown, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/generation/preview",
      parseRepositoryGeneration,
      postJson(body, signal),
    );
  },
  composite(body: unknown, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/scoring/composite",
      parseCompositeScoring,
      postJson(body, signal),
    );
  },
  trainScorer(body: unknown, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/scoring/ngram/scorers",
      parsePhonotacticArtifact,
      postJson(body, signal),
    );
  },
  trainConstraint(body: unknown, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/scoring/ngram/constraints",
      parsePhonotacticArtifact,
      postJson(body, signal),
    );
  },
  scorePhonotactics(body: unknown, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/scoring/phonotactics",
      parsePhonotacticScores,
      postJson(body, signal),
    );
  },
  readability(body: unknown, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/scoring/readability",
      parseReadability,
      postJson(body, signal),
    );
  },
};
