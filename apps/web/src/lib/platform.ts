import {
  ApiContractError,
  isFiniteNumber,
  isNonnegativeInteger,
  isRecord,
  isUuid,
  queryString,
  requestJson,
  type JsonRecord,
} from "@/lib/api-client";

export interface QuotaPolicy {
  readonly max_concurrent_cpu_jobs: number;
  readonly max_concurrent_expensive_jobs: number;
  readonly max_artifact_bytes: number;
  readonly max_artifact_count: number;
  readonly max_corpus_sentences: number;
  readonly max_generation_accepted_sentences: number;
  readonly max_generation_iterations: number;
  readonly max_activity_deadline_seconds: number;
  readonly max_provider_input_tokens: number;
  readonly max_provider_output_tokens: number;
  readonly max_provider_cost_microusd: number;
  readonly max_rl_steps: number;
  readonly max_rl_tokens: number;
  readonly max_checkpoint_bytes: number;
}

export interface QuotaUsage {
  readonly active_cpu_jobs: number;
  readonly active_expensive_jobs: number;
  readonly artifact_bytes: number;
  readonly artifact_count: number;
  readonly corpus_sentences: number;
}

export interface QuotaSnapshot {
  readonly policy: QuotaPolicy;
  readonly usage: QuotaUsage;
}

export interface AuditEvent {
  readonly sequence: number;
  readonly actor_kind: "user" | "service";
  readonly actor_id: string;
  readonly action: AuditAction;
  readonly resource_type: AuditResourceType;
  readonly resource_id: string;
  readonly request_id: string | null;
  readonly occurred_at: string;
  readonly metadata: JsonRecord;
  readonly previous_hash: string;
  readonly event_hash: string;
}

export interface AuditPage {
  readonly events: readonly AuditEvent[];
  readonly next_cursor: string | null;
}

export type AuditAction = (typeof auditActions)[number];
export type AuditResourceType = (typeof auditResourceTypes)[number];

const auditActions = [
  "project.created",
  "project.deletion_requested",
  "project.purged",
  "corpus.created",
  "corpus.version_created",
  "run.submitted",
  "run.cancellation_requested",
  "run.retry_submitted",
  "run.succeeded",
  "run.failed",
  "run.cancelled",
  "artifact.created",
  "artifact.tombstoned",
  "artifact.purged",
  "artifact.adopted",
  "run.manifest_created",
  "run.replay_submitted",
  "run.replay_compared",
  "quota.policy_changed",
  "quota.reservation_expired",
] as const;

const auditResourceTypes = [
  "project",
  "corpus",
  "run",
  "artifact",
  "quota-policy",
  "quota-reservation",
  "replay",
] as const;

const actionSet = new Set<string>(auditActions);
const resourceTypeSet = new Set<string>(auditResourceTypes);

export const platformApi = {
  quota(signal?: AbortSignal) {
    return requestJson("/api/v1/platform/quota", parseQuota, { signal });
  },
  auditEvents(
    options: { readonly cursor?: string | null; readonly limit?: number } = {},
    signal?: AbortSignal,
  ) {
    const limit = options.limit ?? 25;
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200)
      throw new TypeError("Audit page size must be between 1 and 200.");
    return requestJson(
      `/api/v1/platform/audit-events${queryString({ cursor: options.cursor, limit })}`,
      parseAuditPage,
      { signal },
    );
  },
};

function parseQuota(value: unknown): QuotaSnapshot {
  if (!isRecord(value) || !isRecord(value.policy) || !isRecord(value.usage))
    throw new ApiContractError();
  const policy = value.policy;
  const usage = value.usage;
  const positivePolicyFields = [
    "max_concurrent_cpu_jobs",
    "max_concurrent_expensive_jobs",
    "max_artifact_bytes",
    "max_artifact_count",
    "max_corpus_sentences",
    "max_generation_accepted_sentences",
    "max_generation_iterations",
    "max_provider_input_tokens",
    "max_provider_output_tokens",
    "max_provider_cost_microusd",
    "max_rl_steps",
    "max_rl_tokens",
    "max_checkpoint_bytes",
  ] as const;
  if (
    positivePolicyFields.some(
      (field) => !isNonnegativeInteger(policy[field]) || policy[field] < 1,
    ) ||
    !isFiniteNumber(policy.max_activity_deadline_seconds) ||
    policy.max_activity_deadline_seconds <= 0 ||
    !isNonnegativeInteger(usage.active_cpu_jobs) ||
    !isNonnegativeInteger(usage.active_expensive_jobs) ||
    !isNonnegativeInteger(usage.artifact_bytes) ||
    !isNonnegativeInteger(usage.artifact_count) ||
    !isNonnegativeInteger(usage.corpus_sentences)
  )
    throw new ApiContractError();
  return value as unknown as QuotaSnapshot;
}

function parseAuditPage(value: unknown): AuditPage {
  if (
    !isRecord(value) ||
    !Array.isArray(value.events) ||
    (value.next_cursor !== null && typeof value.next_cursor !== "string")
  )
    throw new ApiContractError();
  return {
    events: value.events.map(parseAuditEvent),
    next_cursor: value.next_cursor,
  };
}

function parseAuditEvent(value: unknown): AuditEvent {
  if (
    !isRecord(value) ||
    !Number.isSafeInteger(value.sequence) ||
    (value.sequence as number) < 1 ||
    (value.actor_kind !== "user" && value.actor_kind !== "service") ||
    typeof value.actor_id !== "string" ||
    !value.actor_id ||
    typeof value.action !== "string" ||
    !actionSet.has(value.action) ||
    typeof value.resource_type !== "string" ||
    !resourceTypeSet.has(value.resource_type) ||
    !isUuid(value.resource_id) ||
    (value.request_id !== null && typeof value.request_id !== "string") ||
    !isTimestamp(value.occurred_at) ||
    !isRecord(value.metadata) ||
    !isSha256(value.previous_hash) ||
    !isSha256(value.event_hash)
  )
    throw new ApiContractError();
  return value as unknown as AuditEvent;
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/u.test(value);
}

function isTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value) &&
    !Number.isNaN(Date.parse(value))
  );
}
