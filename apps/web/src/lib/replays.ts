import {
  ApiContractError,
  isRecord,
  isStringArray,
  isUuid,
  pathIdentifier,
  requestJson,
} from "@/lib/api-client";

export type ReplayClassification = "exact" | "best-effort" | "nonreproducible";
export type ReplayLifecycle = "queued" | "running" | "compared" | "unavailable";
export type ReplayVerdict =
  | "exact-match"
  | "best-effort-match"
  | "best-effort-divergence"
  | "mismatch"
  | "nonreproducible";

export interface ReplayComparison {
  readonly classification: ReplayClassification;
  readonly verdict: ReplayVerdict;
  readonly replay_inputs_match: boolean;
  readonly outputs_match: boolean;
  readonly differences: readonly string[];
}

export interface ReplayStatus {
  readonly replay_run_id: string;
  readonly source_run_id: string;
  readonly source_manifest_artifact_id: string;
  readonly expected_manifest_sha256: string;
  readonly observed_manifest_artifact_id: string | null;
  readonly classification: ReplayClassification;
  readonly lifecycle: ReplayLifecycle;
  readonly comparison: ReplayComparison | null;
}

export interface ReplayCreation {
  readonly replay: ReplayStatus;
  readonly created: boolean;
}

const classifications = new Set<ReplayClassification>([
  "exact",
  "best-effort",
  "nonreproducible",
]);
const lifecycles = new Set<ReplayLifecycle>([
  "queued",
  "running",
  "compared",
  "unavailable",
]);
const verdicts = new Set<ReplayVerdict>([
  "exact-match",
  "best-effort-match",
  "best-effort-divergence",
  "mismatch",
  "nonreproducible",
]);
const sha256Pattern = /^[0-9a-f]{64}$/u;
const differencePattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/u;
const idempotencyKeyPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;

function parseComparison(value: unknown): ReplayComparison | null {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    typeof value.classification !== "string" ||
    !classifications.has(value.classification as ReplayClassification) ||
    typeof value.verdict !== "string" ||
    !verdicts.has(value.verdict as ReplayVerdict) ||
    typeof value.replay_inputs_match !== "boolean" ||
    typeof value.outputs_match !== "boolean" ||
    !isStringArray(value.differences) ||
    value.differences.length > 64 ||
    value.differences.some((item) => !differencePattern.test(item))
  ) {
    throw new ApiContractError();
  }
  return {
    classification: value.classification as ReplayClassification,
    verdict: value.verdict as ReplayVerdict,
    replay_inputs_match: value.replay_inputs_match,
    outputs_match: value.outputs_match,
    differences: value.differences,
  };
}

export function parseReplayStatus(value: unknown): ReplayStatus {
  if (
    !isRecord(value) ||
    !isUuid(value.replay_run_id) ||
    !isUuid(value.source_run_id) ||
    !isUuid(value.source_manifest_artifact_id) ||
    typeof value.expected_manifest_sha256 !== "string" ||
    !sha256Pattern.test(value.expected_manifest_sha256) ||
    (value.observed_manifest_artifact_id !== null &&
      !isUuid(value.observed_manifest_artifact_id)) ||
    typeof value.classification !== "string" ||
    !classifications.has(value.classification as ReplayClassification) ||
    typeof value.lifecycle !== "string" ||
    !lifecycles.has(value.lifecycle as ReplayLifecycle)
  ) {
    throw new ApiContractError();
  }
  const comparison = parseComparison(value.comparison);
  if (
    (value.lifecycle === "compared") !== (comparison !== null) ||
    (comparison === null && value.observed_manifest_artifact_id !== null) ||
    (comparison !== null &&
      (value.observed_manifest_artifact_id === null ||
        comparison.classification !== value.classification))
  ) {
    throw new ApiContractError();
  }
  return {
    replay_run_id: value.replay_run_id,
    source_run_id: value.source_run_id,
    source_manifest_artifact_id: value.source_manifest_artifact_id,
    expected_manifest_sha256: value.expected_manifest_sha256,
    observed_manifest_artifact_id: value.observed_manifest_artifact_id,
    classification: value.classification as ReplayClassification,
    lifecycle: value.lifecycle as ReplayLifecycle,
    comparison,
  };
}

function parseReplayCreation(value: unknown): ReplayCreation {
  if (!isRecord(value) || typeof value.created !== "boolean") {
    throw new ApiContractError();
  }
  return { replay: parseReplayStatus(value.replay), created: value.created };
}

function idempotencyHeaders(key: string): HeadersInit {
  if (!idempotencyKeyPattern.test(key)) {
    throw new TypeError("Invalid idempotency key.");
  }
  return { "Idempotency-Key": key };
}

export const replaysApi = {
  submit(
    projectId: string,
    sourceRunId: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) {
    return requestJson(
      `/api/v1/projects/${pathIdentifier(projectId)}/runs/${pathIdentifier(sourceRunId)}/replays`,
      parseReplayCreation,
      {
        method: "POST",
        headers: idempotencyHeaders(idempotencyKey),
        ...(signal ? { signal } : {}),
      },
    );
  },
  get(replayRunId: string, signal?: AbortSignal) {
    return requestJson(
      `/api/v1/replays/${pathIdentifier(replayRunId)}`,
      parseReplayStatus,
      { ...(signal ? { signal } : {}) },
    );
  },
};
