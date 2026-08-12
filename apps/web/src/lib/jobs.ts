import {
  ApiContractError,
  isNonnegativeInteger,
  isRecord,
  isUuid,
  pathIdentifier,
  postJson,
  queryString,
  requestJson,
  type JsonRecord,
} from "@/lib/api-client";

export const coreRunKinds = [
  "phonemize",
  "evaluate",
  "distribution",
  "trajectory",
  "error-rates",
  "select",
] as const;
export const advancedRunKinds = [
  "generate-repository",
  "generate-llm",
  "generate-local",
  "perplexity",
  "build-datg-index",
  "generate-datg",
  "train-phon-rl",
] as const;
export const supportedRunKinds = [
  ...coreRunKinds,
  ...advancedRunKinds,
] as const;
export type CoreRunKind = (typeof coreRunKinds)[number];
export type AdvancedRunKind = (typeof advancedRunKinds)[number];
export type SupportedRunKind = (typeof supportedRunKinds)[number];
export type RunState =
  | "draft"
  | "queued"
  | "provisioning"
  | "running"
  | "cancelling"
  | "cancelled"
  | "succeeded"
  | "failed";

export interface RunRecord {
  readonly id: string;
  readonly project_id: string;
  readonly corpus_version_id: string | null;
  readonly parent_run_id: string | null;
  readonly kind: string;
  readonly state: RunState;
  readonly attempt: number;
  readonly spec_sha256: string;
  readonly outbox_state: string;
  readonly cancellation_requested_at: string | null;
  readonly created_at: string;
  readonly result_summary: JsonRecord | null;
  readonly failure_code: string | null;
}

export interface RunEvent {
  readonly sequence: number;
  readonly event_type: string;
  readonly payload: JsonRecord;
  readonly occurred_at: string;
}

const progressPhaseValues = [
  "validating",
  "preparing_repository",
  "generating",
  "candidate_accepted",
  "staging_result",
  "preparing_training",
  "training",
  "checkpointing",
  "finished",
  "failed",
] as const;
type RunProgressPhase = (typeof progressPhaseValues)[number];
const progressPhases = new Set<string>(progressPhaseValues);

export interface RunProgress {
  readonly schema_version: 1;
  readonly activity_attempt: number;
  readonly sequence: number;
  readonly phase: RunProgressPhase;
  readonly completed: number | null;
  readonly total: number | null;
  readonly coverage: number | null;
  readonly accepted_count: number | null;
}

const progressKeys = new Set([
  "schema_version",
  "activity_attempt",
  "sequence",
  "phase",
  "completed",
  "total",
  "coverage",
  "accepted_count",
]);

const states = new Set<RunState>([
  "draft",
  "queued",
  "provisioning",
  "running",
  "cancelling",
  "cancelled",
  "succeeded",
  "failed",
]);

export function parseRun(value: unknown): RunRecord {
  if (
    !isRecord(value) ||
    !isUuid(value.id) ||
    !isUuid(value.project_id) ||
    (value.corpus_version_id !== null && !isUuid(value.corpus_version_id)) ||
    (value.parent_run_id !== null && !isUuid(value.parent_run_id)) ||
    typeof value.kind !== "string" ||
    typeof value.state !== "string" ||
    !states.has(value.state as RunState) ||
    !isNonnegativeInteger(value.attempt) ||
    typeof value.spec_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.spec_sha256) ||
    typeof value.outbox_state !== "string" ||
    (value.cancellation_requested_at !== null &&
      typeof value.cancellation_requested_at !== "string") ||
    typeof value.created_at !== "string" ||
    (value.result_summary !== null && !isRecord(value.result_summary)) ||
    (value.failure_code !== null && typeof value.failure_code !== "string")
  )
    throw new ApiContractError();
  return {
    id: value.id,
    project_id: value.project_id,
    corpus_version_id: value.corpus_version_id,
    parent_run_id: value.parent_run_id,
    kind: value.kind,
    state: value.state as RunState,
    attempt: value.attempt,
    spec_sha256: value.spec_sha256,
    outbox_state: value.outbox_state,
    cancellation_requested_at: value.cancellation_requested_at,
    created_at: value.created_at,
    result_summary: value.result_summary,
    failure_code: value.failure_code,
  };
}

export function parseRuns(value: unknown): readonly RunRecord[] {
  if (!Array.isArray(value)) throw new ApiContractError();
  return value.map(parseRun);
}

export function parseEvents(value: unknown): readonly RunEvent[] {
  if (!Array.isArray(value)) throw new ApiContractError();
  return value.map((item) => {
    if (
      !isRecord(item) ||
      !isNonnegativeInteger(item.sequence) ||
      typeof item.event_type !== "string" ||
      !isRecord(item.payload) ||
      typeof item.occurred_at !== "string"
    )
      throw new ApiContractError();
    const event = {
      sequence: item.sequence,
      event_type: item.event_type,
      payload: item.payload,
      occurred_at: item.occurred_at,
    };
    if (event.event_type === "run.progress") parseRunProgress(event);
    return event;
  });
}

export function parseRunProgress(event: RunEvent): RunProgress | null {
  if (event.event_type !== "run.progress") return null;
  const value = event.payload;
  if (
    Object.keys(value).some((key) => !progressKeys.has(key)) ||
    value.schema_version !== 1 ||
    !isNonnegativeInteger(value.activity_attempt) ||
    value.activity_attempt < 1 ||
    value.activity_attempt > 100 ||
    !isNonnegativeInteger(value.sequence) ||
    value.sequence >= 128 ||
    typeof value.phase !== "string" ||
    !progressPhases.has(value.phase) ||
    (value.completed !== null &&
      (!isNonnegativeInteger(value.completed) || value.completed > 10_000)) ||
    (value.total !== null &&
      (!isNonnegativeInteger(value.total) ||
        value.total < 1 ||
        value.total > 10_000)) ||
    (value.completed === null) !== (value.total === null) ||
    (typeof value.completed === "number" &&
      typeof value.total === "number" &&
      value.completed > value.total) ||
    (value.coverage !== null &&
      (typeof value.coverage !== "number" ||
        !Number.isFinite(value.coverage) ||
        value.coverage < 0 ||
        value.coverage > 1)) ||
    (value.accepted_count !== null &&
      (!isNonnegativeInteger(value.accepted_count) ||
        value.accepted_count > 10_000))
  )
    throw new ApiContractError();
  return {
    schema_version: 1,
    activity_attempt: value.activity_attempt,
    sequence: value.sequence,
    phase: value.phase as RunProgressPhase,
    completed: value.completed,
    total: value.total,
    coverage: value.coverage,
    accepted_count: value.accepted_count,
  };
}

export function mergeMonotonicEvents(
  current: readonly RunEvent[],
  incoming: readonly RunEvent[],
): readonly RunEvent[] {
  const bySequence = new Map(current.map((event) => [event.sequence, event]));
  for (const event of incoming)
    if (!bySequence.has(event.sequence)) bySequence.set(event.sequence, event);
  return Array.from(bySequence.values()).sort(
    (left, right) => left.sequence - right.sequence,
  );
}

export function nextEventCursor(events: readonly RunEvent[]): number {
  return events.reduce(
    (greatest, event) => Math.max(greatest, event.sequence),
    0,
  );
}

export function isTerminalRun(state: RunState): boolean {
  return state === "cancelled" || state === "succeeded" || state === "failed";
}

export function resultArtifactId(run: RunRecord): string | null {
  const value = run.result_summary?.artifact_id;
  return isUuid(value) ? value : null;
}

export interface JobBuilderInput {
  readonly language: string;
  readonly unit: "phoneme" | "diphone" | "triphone";
  readonly primaryRows: readonly string[];
  readonly secondaryRows: readonly string[];
  readonly targetUnits: readonly string[];
  readonly counts: readonly { readonly unit: string; readonly count: number }[];
  readonly caseSensitive: boolean;
  readonly seed: number;
}

export function buildSupportedRunSpec(
  kind: CoreRunKind,
  input: JobBuilderInput,
): JsonRecord {
  if (!input.language.trim() || input.language.length > 32)
    throw new TypeError("Language must contain 1 to 32 characters.");
  if (!input.primaryRows.length)
    throw new TypeError("At least one input row is required.");
  const target = {
    mode: input.targetUnits.length ? "explicit" : "derived",
    phonemes: input.targetUnits,
  };
  if (kind === "phonemize")
    return { texts: input.primaryRows, language: input.language };
  if (kind === "evaluate")
    return {
      sentences: input.primaryRows,
      language: input.language,
      unit: input.unit,
      target,
    };
  if (kind === "distribution")
    return {
      counts: input.counts,
      target_units: input.targetUnits,
      reference_distribution: null,
    };
  if (kind === "trajectory")
    return {
      phoneme_sequences: input.primaryRows.map(splitUnits),
      target_units: input.targetUnits,
      unit: input.unit,
    };
  if (kind === "error-rates") {
    if (input.primaryRows.length !== input.secondaryRows.length)
      throw new TypeError(
        "Reference and hypothesis rows must have equal length.",
      );
    return {
      references: input.primaryRows,
      hypotheses: input.secondaryRows,
      reference_phonemes: null,
      hypothesis_phonemes: null,
      case_sensitive: input.caseSensitive,
    };
  }
  if (!Number.isSafeInteger(input.seed) || input.seed < 0)
    throw new TypeError("Selection seed must be a non-negative integer.");
  return {
    candidates: input.primaryRows,
    language: input.language,
    unit: input.unit,
    target,
    options: {
      algorithm: "greedy",
      max_sentences: Math.min(input.primaryRows.length, 100),
      target_coverage: 1,
      weights: [],
      epsilon: 0.1,
      seed: input.seed,
      target_distribution: [],
      ilp_time_limit_seconds: 10,
      population_size: 50,
      generations: 100,
    },
  };
}

function splitUnits(value: string): string[] {
  const units = value.trim().split(/\s+/u).filter(Boolean);
  if (!units.length) throw new TypeError("Phoneme rows cannot be empty.");
  return units;
}

function idempotencyHeaders(key: string): HeadersInit {
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(key))
    throw new TypeError("Invalid idempotency key.");
  return { "Idempotency-Key": key };
}

export const jobsApi = {
  list(
    filters: {
      readonly state?: string;
      readonly kind?: string;
      readonly offset?: number;
      readonly limit?: number;
    },
    signal?: AbortSignal,
  ) {
    return requestJson(`/api/v1/runs${queryString(filters)}`, parseRuns, {
      signal,
    });
  },
  get(id: string, signal?: AbortSignal) {
    return requestJson(`/api/v1/runs/${pathIdentifier(id)}`, parseRun, {
      signal,
    });
  },
  events(id: string, after: number, signal?: AbortSignal) {
    return requestJson(
      `/api/v1/runs/${pathIdentifier(id)}/events${queryString({ after, limit: 100 })}`,
      parseEvents,
      { signal },
    );
  },
  submit(
    body: {
      readonly project_id: string;
      readonly corpus_version_id: string | null;
      readonly kind: SupportedRunKind;
      readonly spec: JsonRecord;
    },
    idempotencyKey: string,
    signal?: AbortSignal,
  ) {
    return requestJson("/api/v1/runs", parseRun, {
      ...postJson(body, signal),
      headers: idempotencyHeaders(idempotencyKey),
    });
  },
  cancel(id: string, signal?: AbortSignal) {
    return requestJson(
      `/api/v1/runs/${pathIdentifier(id)}/cancellation`,
      parseRun,
      postJson({}, signal),
    );
  },
  retry(id: string, idempotencyKey: string, signal?: AbortSignal) {
    return requestJson(`/api/v1/runs/${pathIdentifier(id)}/retries`, parseRun, {
      ...postJson({}, signal),
      headers: idempotencyHeaders(idempotencyKey),
    });
  },
};
