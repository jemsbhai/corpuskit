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
export type WeightStrategy =
  "uniform" | "inverse_frequency" | "linguistic_class";

export interface WeightValue {
  readonly unit: string;
  readonly weight: number;
}

export interface TargetSpaceEstimate {
  readonly phoneme_count: number;
  readonly unit: CoverageUnit;
  readonly exponent: number;
  readonly estimated_target_size: number;
  readonly max_target_size: number;
  readonly within_limit: boolean;
}

interface UnitCount {
  readonly unit: string;
  readonly count: number;
}

interface UnitSources {
  readonly unit: string;
  readonly sentence_indices: number[];
}

export interface CoverageSnapshot {
  readonly coverage: number;
  readonly target_size: number;
  readonly covered_count: number;
  readonly target_units: string[];
  readonly covered_units: string[];
  readonly missing_units: string[];
  readonly unit_counts: UnitCount[];
  readonly unit_sources: UnitSources[];
}

export interface CoverageStep {
  readonly sentence_index: number;
  readonly coverage: number;
  readonly new_units: string[];
}

export interface CoverageLabResult {
  readonly unit: CoverageUnit;
  readonly steps: CoverageStep[];
  readonly final: CoverageSnapshot;
  readonly next_targets: string[];
  readonly after_reset: CoverageSnapshot;
}

export interface WeightSet {
  readonly weights: WeightValue[];
  readonly count: number;
  readonly total: number;
  readonly mean: number;
}

export interface RenderedReport {
  readonly verbosity: "minimal" | "normal" | "verbose";
  readonly media_type: string;
  readonly content: string;
}

export interface ExportedReport {
  readonly format: "json" | "jsonld";
  readonly media_type: string;
  readonly canonical_json: string;
}

function coverageUnit(value: unknown): value is CoverageUnit {
  return value === "phoneme" || value === "diphone" || value === "triphone";
}

function parseWeight(value: unknown): WeightValue {
  if (
    !isRecord(value) ||
    typeof value.unit !== "string" ||
    !isFiniteNumber(value.weight) ||
    value.weight < 0
  )
    throw new ApiContractError();
  return value as unknown as WeightValue;
}

function parseSnapshot(value: unknown): CoverageSnapshot {
  if (
    !isRecord(value) ||
    !isFiniteNumber(value.coverage) ||
    value.coverage < 0 ||
    value.coverage > 1 ||
    !isNonnegativeInteger(value.target_size) ||
    !isNonnegativeInteger(value.covered_count) ||
    !isStringArray(value.target_units) ||
    !isStringArray(value.covered_units) ||
    !isStringArray(value.missing_units) ||
    !Array.isArray(value.unit_counts) ||
    !Array.isArray(value.unit_sources)
  )
    throw new ApiContractError();
  const unitCounts = value.unit_counts.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.unit !== "string" ||
      !isNonnegativeInteger(item.count)
    ) {
      throw new ApiContractError();
    }
    return item as unknown as UnitCount;
  });
  const unitSources = value.unit_sources.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.unit !== "string" ||
      !Array.isArray(item.sentence_indices) ||
      !item.sentence_indices.every(isNonnegativeInteger)
    )
      throw new ApiContractError();
    return item as unknown as UnitSources;
  });
  return {
    ...(value as unknown as Omit<
      CoverageSnapshot,
      "unit_counts" | "unit_sources"
    >),
    unit_counts: unitCounts,
    unit_sources: unitSources,
  };
}

export function parseEstimate(value: unknown): TargetSpaceEstimate {
  if (
    !isRecord(value) ||
    !isNonnegativeInteger(value.phoneme_count) ||
    !coverageUnit(value.unit) ||
    !isNonnegativeInteger(value.exponent) ||
    !isNonnegativeInteger(value.estimated_target_size) ||
    !isNonnegativeInteger(value.max_target_size) ||
    typeof value.within_limit !== "boolean"
  )
    throw new ApiContractError();
  return value as unknown as TargetSpaceEstimate;
}

export function parseCoverage(value: unknown): CoverageLabResult {
  if (
    !isRecord(value) ||
    !coverageUnit(value.unit) ||
    !Array.isArray(value.steps) ||
    !isStringArray(value.next_targets)
  ) {
    throw new ApiContractError();
  }
  const steps = value.steps.map((step) => {
    if (
      !isRecord(step) ||
      !isNonnegativeInteger(step.sentence_index) ||
      !isFiniteNumber(step.coverage) ||
      step.coverage < 0 ||
      step.coverage > 1 ||
      !isStringArray(step.new_units)
    )
      throw new ApiContractError();
    return step as unknown as CoverageStep;
  });
  return {
    unit: value.unit,
    steps,
    final: parseSnapshot(value.final),
    next_targets: value.next_targets,
    after_reset: parseSnapshot(value.after_reset),
  };
}

export const coverageLabApi = {
  estimate: (
    target_phonemes: readonly string[],
    unit: CoverageUnit,
    signal?: AbortSignal,
  ) =>
    requestJson(
      "/api/v1/labs/coverage/estimate",
      parseEstimate,
      postJson({ target_phonemes, unit, max_target_size: 20_000 }, signal),
    ),
  track: (
    body: {
      readonly target_phonemes: readonly string[];
      readonly unit: CoverageUnit;
      readonly phoneme_sequences: readonly (readonly string[])[];
      readonly weights: readonly WeightValue[];
      readonly next_targets_limit: number;
    },
    signal?: AbortSignal,
  ) =>
    requestJson(
      "/api/v1/labs/coverage/track",
      parseCoverage,
      postJson(body, signal),
    ),
  computeWeights: (
    body: {
      readonly strategy: WeightStrategy;
      readonly target_units: readonly string[];
      readonly unit: CoverageUnit;
      readonly corpus_phonemes: readonly (readonly string[])[];
      readonly class_weights: readonly WeightValue[];
    },
    signal?: AbortSignal,
  ) =>
    requestJson(
      "/api/v1/labs/weights/compute",
      (value): WeightSet => {
        if (
          !isRecord(value) ||
          !Array.isArray(value.weights) ||
          !isNonnegativeInteger(value.count) ||
          !isFiniteNumber(value.total) ||
          !isFiniteNumber(value.mean)
        )
          throw new ApiContractError();
        return {
          weights: value.weights.map(parseWeight),
          count: value.count,
          total: value.total,
          mean: value.mean,
        };
      },
      postJson(body, signal),
    ),
  validateWeights: (
    kind: "unit" | "component",
    weights: readonly WeightValue[],
    signal?: AbortSignal,
  ) =>
    requestJson(
      "/api/v1/labs/weights/validate",
      (value): { readonly valid: true; readonly count: number } => {
        if (
          !isRecord(value) ||
          value.valid !== true ||
          !isNonnegativeInteger(value.count)
        ) {
          throw new ApiContractError();
        }
        return { valid: true, count: value.count };
      },
      postJson({ kind, weights }, signal),
    ),
  render: (body: unknown, signal?: AbortSignal) =>
    requestJson(
      "/api/v1/labs/reports/render",
      (value): RenderedReport => {
        if (
          !isRecord(value) ||
          (value.verbosity !== "minimal" &&
            value.verbosity !== "normal" &&
            value.verbosity !== "verbose") ||
          typeof value.media_type !== "string" ||
          typeof value.content !== "string"
        )
          throw new ApiContractError();
        return value as unknown as RenderedReport;
      },
      postJson(body, signal),
    ),
  export: (body: unknown, signal?: AbortSignal) =>
    requestJson(
      "/api/v1/labs/reports/export",
      (value): ExportedReport => {
        if (
          !isRecord(value) ||
          (value.format !== "json" && value.format !== "jsonld") ||
          typeof value.media_type !== "string" ||
          typeof value.canonical_json !== "string"
        )
          throw new ApiContractError();
        return value as unknown as ExportedReport;
      },
      postJson(body, signal),
    ),
};
