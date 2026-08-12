import {
  ApiContractError,
  isFiniteNumber,
  isNonnegativeInteger,
  isRecord,
  isStringArray,
  postJson,
  requestJson,
} from "@/lib/api-client";
import type { CoverageUnit, WeightValue } from "@/lib/coverage-lab";

export type SelectionAlgorithm =
  "greedy" | "celf" | "stochastic" | "distribution" | "ilp" | "nsga2";

export interface ParetoSolution {
  readonly coverage: number;
  readonly sentence_count: number;
  readonly selected_indices: number[];
  readonly kl_divergence: number | null;
}

export interface SelectionMetadata {
  readonly evaluations: number | null;
  readonly epsilon: number | null;
  readonly seed: number | null;
  readonly sample_size: number | null;
  readonly kl_divergence: number | null;
  readonly solver_status: string | null;
  readonly pareto_front: ParetoSolution[];
}

export interface CorpusSelection {
  readonly selected_indices: number[];
  readonly selected_sentences: string[];
  readonly coverage: number;
  readonly covered_units: string[];
  readonly missing_units: string[];
  readonly unit: CoverageUnit;
  readonly target_mode: "derived" | "explicit" | "phoible";
  readonly algorithm: SelectionAlgorithm;
  readonly elapsed_seconds: number;
  readonly iterations: number;
  readonly metadata: SelectionMetadata;
}

const algorithms = new Set<SelectionAlgorithm>([
  "greedy",
  "celf",
  "stochastic",
  "distribution",
  "ilp",
  "nsga2",
]);

function nullableNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function nullableInteger(value: unknown): value is number | null {
  return value === null || isNonnegativeInteger(value);
}

export function parseSelection(value: unknown): CorpusSelection {
  if (
    !isRecord(value) ||
    !Array.isArray(value.selected_indices) ||
    !value.selected_indices.every(isNonnegativeInteger) ||
    !isStringArray(value.selected_sentences) ||
    !isFiniteNumber(value.coverage) ||
    value.coverage < 0 ||
    value.coverage > 1 ||
    !isStringArray(value.covered_units) ||
    !isStringArray(value.missing_units) ||
    (value.unit !== "phoneme" &&
      value.unit !== "diphone" &&
      value.unit !== "triphone") ||
    (value.target_mode !== "derived" &&
      value.target_mode !== "explicit" &&
      value.target_mode !== "phoible") ||
    typeof value.algorithm !== "string" ||
    !algorithms.has(value.algorithm as SelectionAlgorithm) ||
    !isFiniteNumber(value.elapsed_seconds) ||
    value.elapsed_seconds < 0 ||
    !isNonnegativeInteger(value.iterations) ||
    !isRecord(value.metadata)
  )
    throw new ApiContractError();
  const metadata = value.metadata;
  if (
    !nullableInteger(metadata.evaluations) ||
    !nullableNumber(metadata.epsilon) ||
    !nullableInteger(metadata.seed) ||
    !nullableInteger(metadata.sample_size) ||
    !nullableNumber(metadata.kl_divergence) ||
    (metadata.solver_status !== null &&
      typeof metadata.solver_status !== "string") ||
    !Array.isArray(metadata.pareto_front)
  )
    throw new ApiContractError();
  const pareto = metadata.pareto_front.map((item) => {
    if (
      !isRecord(item) ||
      !isFiniteNumber(item.coverage) ||
      item.coverage < 0 ||
      item.coverage > 1 ||
      !isNonnegativeInteger(item.sentence_count) ||
      !Array.isArray(item.selected_indices) ||
      !item.selected_indices.every(isNonnegativeInteger) ||
      !nullableNumber(item.kl_divergence)
    )
      throw new ApiContractError();
    return item as unknown as ParetoSolution;
  });
  return {
    ...(value as unknown as Omit<CorpusSelection, "metadata">),
    algorithm: value.algorithm as SelectionAlgorithm,
    metadata: {
      ...(metadata as unknown as Omit<SelectionMetadata, "pareto_front">),
      pareto_front: pareto,
    },
  };
}

export const selectionApi = {
  select: (
    body: {
      readonly candidates: readonly string[];
      readonly language: string;
      readonly unit: CoverageUnit;
      readonly target: {
        readonly mode: "derived" | "explicit" | "phoible";
        readonly phonemes: readonly string[];
      };
      readonly options: {
        readonly algorithm: SelectionAlgorithm;
        readonly max_sentences: number | null;
        readonly target_coverage: number;
        readonly weights: readonly WeightValue[];
        readonly epsilon: number;
        readonly seed: number | null;
        readonly target_distribution: readonly WeightValue[];
        readonly ilp_time_limit_seconds: number;
        readonly population_size: number;
        readonly generations: number;
      };
    },
    signal?: AbortSignal,
  ) =>
    requestJson("/api/v1/selections", parseSelection, postJson(body, signal)),
};
