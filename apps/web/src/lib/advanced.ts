import {
  ApiContractError,
  isNonnegativeInteger,
  isRecord,
  isStringArray,
  isUuid,
  pathIdentifier,
  postJson,
  requestJson,
  type JsonRecord,
} from "@/lib/api-client";
import type { AdvancedRunKind } from "@/lib/jobs";

const maximumEditorCharacters = 64 * 1024;
const sensitiveParts = new Set([
  "authorization",
  "credential",
  "password",
  "secret",
]);

export interface HostedModelOption {
  readonly provider: string;
  readonly model: string;
  readonly connection_id: string;
  readonly max_output_tokens_per_request: number;
  readonly request_delay_seconds: number;
  readonly prompt_template_ids: readonly string[];
}

export interface LocalModelOption {
  readonly model: string;
  readonly revision: string;
  readonly allowed_devices: readonly string[];
  readonly allowed_quantizations: readonly string[];
  readonly allow_phon_rl_adapters: boolean;
}

export interface HuggingFaceRepositoryOption {
  readonly dataset: string;
  readonly config: string;
  readonly split: string;
  readonly text_column: string;
  readonly revision: string;
  readonly language: string;
  readonly max_samples: number;
}

export interface DatgRuntimeOption {
  readonly runtime_id: string;
  readonly allowed_quantizations: readonly string[];
}

export interface DatgIndexPublication {
  readonly schema_id: "corpuskit.datg-index-publication.v1";
  readonly build_run_id: string;
  readonly cache_key_sha256: string;
  readonly content_sha256: string;
  readonly runtime_id: string;
  readonly language: string;
  readonly unit: "phoneme" | "diphone" | "triphone";
  readonly vocabulary_size: number;
  readonly indexed_token_count: number;
  readonly size_bytes: number;
  readonly created_at: string;
}

export interface DatgLogitDeltaPreview {
  readonly schema_id: "corpuskit.datg-logit-delta-preview.v1";
  readonly cache_key_sha256: string;
  readonly original_logits: readonly (readonly number[])[];
  readonly delta_logits: readonly (readonly number[])[];
  readonly modified_logits: readonly (readonly number[])[];
  readonly attribute_token_ids: readonly number[];
  readonly anti_attribute_token_ids: readonly number[];
  readonly generation_executed: false;
  readonly model_loaded: false;
  readonly network_used: false;
}

export interface PhonRlRuntimeOption {
  readonly runtime_id: string;
  readonly allow_peft: boolean;
  readonly allow_static_prompts: boolean;
  readonly allowed_prompt_strategies: readonly string[];
}

export interface AdvancedCapabilities {
  readonly advanced_operation_routes_validation_only: true;
  readonly durable_run_submission_route: "/api/v1/runs";
  readonly hosted_models: readonly HostedModelOption[];
  readonly huggingface_repositories: readonly HuggingFaceRepositoryOption[];
  readonly local_models: readonly LocalModelOption[];
  readonly datg_runtimes: readonly DatgRuntimeOption[];
  readonly phon_rl_runtimes: readonly PhonRlRuntimeOption[];
  readonly datg_inspection: "configured_read_only" | "unavailable";
  readonly phon_rl_lab: "bounded_optional_dependency";
}

export interface CliPreview {
  readonly workflow: string;
  readonly argv: readonly string[];
  readonly posix_command: string;
  readonly powershell_command: string;
  readonly reproducibility: string;
  readonly warnings: readonly string[];
}

export type LabOperation =
  | "datg-targets"
  | "datg-covered"
  | "datg-frequency"
  | "datg-logit-preview"
  | "reward-peek"
  | "reward-commit"
  | "reward-tokens"
  | "reward-hierarchical"
  | "ppo-log-probabilities"
  | "ppo-kl-penalty"
  | "ppo-gae"
  | "ppo-clip-loss"
  | "ppo-value-head";

const validationPaths: Record<AdvancedRunKind, string> = {
  "generate-repository": "/api/v1/generation/repository/validate",
  "generate-llm": "/api/v1/model-runtime/hosted/validate",
  "generate-local": "/api/v1/model-runtime/local/validate",
  perplexity: "/api/v1/model-runtime/analysis/validate",
  "build-datg-index": "/api/v1/datg/index/validate",
  "generate-datg": "/api/v1/datg/generation/validate",
  "train-phon-rl": "/api/v1/phon-rl/training/validate",
};

const estimatePaths: Partial<Record<AdvancedRunKind, string>> = {
  "generate-llm": "/api/v1/model-runtime/hosted/estimate",
  perplexity: "/api/v1/model-runtime/analysis/estimate",
  "train-phon-rl": "/api/v1/phon-rl/training/estimate",
};

const validationContracts: Record<
  AdvancedRunKind,
  { readonly schema: string; readonly operation?: string }
> = {
  "generate-repository": {
    schema: "corpuskit.repository-generation-validation.v1",
    operation: "repository_generation",
  },
  "generate-llm": {
    schema: "corpuskit.model-runtime-validation.v1",
    operation: "hosted_generation",
  },
  "generate-local": {
    schema: "corpuskit.model-runtime-validation.v1",
    operation: "local_generation",
  },
  perplexity: {
    schema: "corpuskit.model-runtime-validation.v1",
    operation: "language_model_analysis",
  },
  "build-datg-index": {
    schema: "corpuskit.datg-runtime-validation.v1",
    operation: "build_index",
  },
  "generate-datg": {
    schema: "corpuskit.datg-runtime-validation.v1",
    operation: "guided_generation",
  },
  "train-phon-rl": {
    schema: "corpuskit.phon-rl-training-validation.v1",
  },
};

const estimateContracts: Partial<Record<AdvancedRunKind, string>> = {
  "generate-llm": "corpuskit.hosted-cost-estimate.v1",
  perplexity: "corpuskit.language-model-analysis-estimate.v1",
  "train-phon-rl": "corpuskit.phon-rl-resource-estimate.v1",
};

const labPaths: Record<Exclude<LabOperation, `datg-${string}`>, string> = {
  "reward-peek": "/api/v1/phon-rl/reward/peek",
  "reward-commit": "/api/v1/phon-rl/reward/commit",
  "reward-tokens": "/api/v1/phon-rl/reward/tokens",
  "reward-hierarchical": "/api/v1/phon-rl/reward/hierarchical",
  "ppo-log-probabilities": "/api/v1/phon-rl/ppo/log-probabilities",
  "ppo-kl-penalty": "/api/v1/phon-rl/ppo/kl-penalty",
  "ppo-gae": "/api/v1/phon-rl/ppo/gae",
  "ppo-clip-loss": "/api/v1/phon-rl/ppo/clip-loss",
  "ppo-value-head": "/api/v1/phon-rl/ppo/value-head",
};

export const advancedApi = {
  capabilities(signal?: AbortSignal) {
    return requestJson(
      "/api/v1/advanced/capabilities",
      parseAdvancedCapabilities,
      { signal },
    );
  },
  validate(kind: AdvancedRunKind, spec: JsonRecord, signal?: AbortSignal) {
    return requestJson(
      validationPaths[kind],
      (value) => parseValidation(value, kind),
      postJson(spec, signal),
    );
  },
  estimate(kind: AdvancedRunKind, spec: JsonRecord, signal?: AbortSignal) {
    const path = estimatePaths[kind];
    return path
      ? requestJson(
          path,
          (value) => parseEstimate(value, kind),
          postJson(spec, signal),
        )
      : Promise.resolve<JsonRecord>({ estimate: "not_applicable" });
  },
  datgIndexes(projectId: string, signal?: AbortSignal) {
    return requestJson(
      `/api/v1/projects/${pathIdentifier(projectId)}/datg/indexes`,
      parseDatgIndexes,
      { signal },
    );
  },
  lab(
    operation: LabOperation,
    request: JsonRecord,
    projectId?: string,
    signal?: AbortSignal,
  ) {
    const path = operation.startsWith("datg-")
      ? datgLabPath(operation, projectId)
      : labPaths[operation as Exclude<LabOperation, `datg-${string}`>];
    if (operation === "datg-logit-preview")
      return requestJson(
        path,
        (value) => parseDatgLogitPreview(value, request),
        postJson(request, signal),
      );
    return requestJson(path, parseRecord, postJson(request, signal));
  },
  cli(request: JsonRecord, signal?: AbortSignal) {
    return requestJson(
      "/api/v1/labs/cli/preview",
      parseCliPreview,
      postJson(request, signal),
    );
  },
};

export function parseEditorObject(value: string): JsonRecord {
  if (!value.trim() || value.length > maximumEditorCharacters)
    throw new TypeError("Configuration must be a JSON object under 64 KiB.");
  let parsed: unknown;
  try {
    parsed = JSON.parse(value) as unknown;
  } catch {
    throw new TypeError("Configuration must be valid JSON.");
  }
  if (!isRecord(parsed))
    throw new TypeError("Configuration must be a JSON object.");
  inspectSecrets(parsed);
  return parsed;
}

export function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

export function runTemplate(
  kind: AdvancedRunKind,
  catalog: AdvancedCapabilities | null,
  datgIndex: DatgIndexPublication | null = null,
): JsonRecord {
  if (kind === "generate-repository") {
    const option = catalog?.huggingface_repositories[0];
    return {
      source: {
        kind: "hugging_face",
        spec: {
          dataset: option?.dataset ?? "unconfigured/dataset",
          config: option?.config ?? "default",
          split: option?.split ?? "train",
          text_column: option?.text_column ?? "text",
          revision: option?.revision ?? "0".repeat(40),
          language: option?.language ?? "en-us",
          max_samples: Math.min(100, option?.max_samples ?? 100),
          trust_remote_code: false,
        },
      },
      target: { phonemes: ["p", "b"], unit: "phoneme" },
      stopping: {
        target_coverage: 1,
        max_sentences: 10,
        max_iterations: 10,
        timeout_seconds: 30,
      },
      candidates_per_iteration: 5,
      activity_timeout_seconds: 120,
    };
  }
  if (kind === "generate-llm") {
    const option = catalog?.hosted_models[0];
    return {
      selection: {
        provider: option?.provider ?? "unconfigured",
        model: option?.model ?? "unconfigured/model",
        connection_id: option?.connection_id ?? "unconfigured-connection",
      },
      target: { phonemes: ["p", "b"], unit: "phoneme" },
      stopping: {
        max_sentences: 2,
        max_iterations: 2,
        timeout_seconds: 30,
      },
      candidates_per_iteration: 2,
      max_tokens_per_request: Math.min(
        64,
        option?.max_output_tokens_per_request ?? 64,
      ),
      ...(option?.prompt_template_ids[0]
        ? { prompt_template_id: option.prompt_template_ids[0] }
        : {}),
      activity_timeout_seconds: 60,
    };
  }
  if (kind === "generate-local" || kind === "perplexity") {
    const option = catalog?.local_models[0];
    const device = preferredDevice(option);
    const selection = {
      pin: {
        model: option?.model ?? "unconfigured/model",
        revision: option?.revision ?? "0".repeat(40),
      },
      device,
      quantization: preferredQuantization(option, device),
    };
    if (kind === "perplexity")
      return {
        selection,
        texts: [
          { source_id: "sentence-1", text: "A complete sample sentence." },
          { source_id: "sentence-2", text: "Another short sample." },
        ],
        batch_size: 2,
        max_length: 128,
        composite_scoring: {
          target: { phonemes: ["p", "b"], unit: "phoneme" },
          candidates: [
            {
              source_id: "sentence-1",
              text: "A complete sample sentence.",
              phonemes: ["p"],
            },
            {
              source_id: "sentence-2",
              text: "Another short sample.",
              phonemes: ["b"],
            },
          ],
          options: {
            weights: {
              coverage: 0,
              phonotactic: 0,
              readability: 0,
              fluency: 1,
            },
          },
          top_k: 2,
        },
        activity_timeout_seconds: 120,
      };
    return {
      selection,
      target: { phonemes: ["p", "b"], unit: "phoneme" },
      stopping: {
        max_sentences: 2,
        max_iterations: 2,
        timeout_seconds: 30,
      },
      candidates_per_iteration: 2,
      max_new_tokens: 64,
      seed: 7,
      activity_timeout_seconds: 120,
    };
  }
  if (kind === "build-datg-index")
    return {
      runtime_id: catalog?.datg_runtimes[0]?.runtime_id ?? "unconfigured-datg",
      language: "en-us",
      unit: "phoneme",
      batch_size: 256,
      max_vocabulary_size: 50000,
      activity_timeout_seconds: 300,
    };
  if (kind === "generate-datg") {
    const option = catalog?.datg_runtimes[0];
    return {
      runtime_id: option?.runtime_id ?? "unconfigured-datg",
      ...(datgIndex
        ? { index_cache_key_sha256: datgIndex.cache_key_sha256 }
        : {}),
      language: "en-us",
      unit: "phoneme",
      target_phonemes: ["p", "b"],
      target_units: ["p", "b"],
      coverage_sequences: [],
      quantization: option?.allowed_quantizations[0] ?? "none",
      candidates: 2,
      max_new_tokens: 64,
      seed: 7,
      activity_timeout_seconds: 300,
    };
  }
  const option = catalog?.phon_rl_runtimes[0];
  return {
    runtime_id: option?.runtime_id ?? "unconfigured-rl",
    language: "en-us",
    unit: "phoneme",
    target_phonemes: ["p", "b"],
    prompt_source: {
      kind: "strategy",
      strategy_id: option?.allowed_prompt_strategies[0] ?? "missing-units-v1",
      requested_prompts: 2,
    },
    parameters: {
      num_steps: 2,
      batch_size: 2,
      max_new_tokens: 32,
      seed: 7,
      activity_timeout_seconds: 3600,
    },
  };
}

export const labOperations: readonly {
  readonly value: LabOperation;
  readonly label: string;
}[] = [
  { value: "datg-targets", label: "DATG target tokens" },
  { value: "datg-covered", label: "DATG covered anti-tokens" },
  { value: "datg-frequency", label: "DATG frequency anti-tokens" },
  { value: "datg-logit-preview", label: "DATG logit delta preview" },
  { value: "reward-peek", label: "Phon-RL reward peek" },
  { value: "reward-commit", label: "Phon-RL reward state transition" },
  { value: "reward-tokens", label: "Phon-RL token rewards" },
  { value: "reward-hierarchical", label: "Phon-RL hierarchical reward" },
  { value: "ppo-log-probabilities", label: "PPO log probabilities" },
  { value: "ppo-kl-penalty", label: "PPO KL penalty" },
  { value: "ppo-gae", label: "PPO generalized advantage estimate" },
  { value: "ppo-clip-loss", label: "PPO clipped loss" },
  { value: "ppo-value-head", label: "PPO value head" },
];

const rewardState = {
  target_phonemes: ["p", "b"],
  unit: "phoneme",
  committed: [],
  revision: 0,
};
const sentenceReward = {
  state: rewardState,
  source_id: "sentence:1",
  phonemes: ["p", "a", "b"],
  text: "A safe sample sentence.",
};
const tokenPieces = [
  { token_id: 1, decoded_text: "sample", raw_token: "sample" },
];

export function labTemplate(
  operation: LabOperation,
  datgIndex: DatgIndexPublication | null = null,
): JsonRecord {
  if (operation === "datg-targets")
    return {
      ...(datgIndex ? { cache_key_sha256: datgIndex.cache_key_sha256 } : {}),
      target_units: ["p"],
      max_results: 100,
    };
  if (operation === "datg-covered")
    return {
      ...(datgIndex ? { cache_key_sha256: datgIndex.cache_key_sha256 } : {}),
      covered_units: ["p"],
      max_results: 100,
    };
  if (operation === "datg-frequency")
    return {
      ...(datgIndex ? { cache_key_sha256: datgIndex.cache_key_sha256 } : {}),
      unit_counts: [{ unit: "p", count: 2 }],
      threshold: 1,
      max_results: 100,
    };
  if (operation === "datg-logit-preview") {
    const defaults = datgPreviewTargets(datgIndex?.unit ?? "phoneme");
    const width = Math.max(1, Math.min(datgIndex?.vocabulary_size ?? 4, 16));
    return {
      ...(datgIndex ? { cache_key_sha256: datgIndex.cache_key_sha256 } : {}),
      target_phonemes: defaults.targetPhonemes,
      target_units: [defaults.targetUnit],
      coverage_sequences: [{ phonemes: defaults.coveredPhonemes }],
      guidance: {
        boost_strength: 2.5,
        penalty_strength: -1.25,
        anti_attribute_mode: "covered",
        frequency_threshold: 0,
      },
      logits: [Array.from({ length: width }, (_, tokenId) => tokenId)],
    };
  }
  if (operation === "reward-peek" || operation === "reward-commit")
    return sentenceReward;
  if (operation === "reward-tokens")
    return { state: rewardState, pieces: tokenPieces };
  if (operation === "reward-hierarchical")
    return { sentence: sentenceReward, pieces: tokenPieces };
  if (operation === "ppo-log-probabilities")
    return { logits: [[[1, 0]]], actions: { values: [[0]] } };
  if (operation === "ppo-kl-penalty")
    return {
      policy_log_probs: { values: [[0, -1]] },
      reference_log_probs: { values: [[0, -2]] },
    };
  if (operation === "ppo-gae")
    return {
      rewards: { values: [[1, 0]] },
      values: { values: [[0.5, 0.25]] },
      mask: { values: [[true, true]] },
    };
  if (operation === "ppo-clip-loss")
    return {
      advantages: { values: [[1]] },
      old_log_probs: { values: [[-0.2]] },
      new_log_probs: { values: [[-0.1]] },
      mask: { values: [[true]] },
    };
  return { hidden_states_2d: { values: [[1, 0]] }, seed: 7, dropout: 0 };
}

export const cliWorkflows = [
  "inventory",
  "evaluate",
  "select",
  "generate",
] as const;
export type CliWorkflow = (typeof cliWorkflows)[number];

export function cliTemplate(workflow: CliWorkflow): JsonRecord {
  if (workflow === "inventory")
    return { workflow, language: "eng", output_format: "json" };
  if (workflow === "evaluate")
    return {
      workflow,
      language: "en-us",
      sentences: ["Pack my box with five dozen liquor jugs."],
      target: "derived",
      unit: "phoneme",
      output_format: "json",
      verbosity: "normal",
    };
  if (workflow === "select")
    return {
      workflow,
      language: "en-us",
      file_path: "corpus.txt",
      target: "derived",
      unit: "phoneme",
      algorithm: "greedy",
      target_coverage: 1,
      output_format: "json",
    };
  return {
    workflow,
    backend: "repository",
    language: "en-us",
    file_path: "corpus.txt",
    target_source: "phoible",
    max_sentences: 25,
    max_iterations: 100,
    timeout_seconds: 300,
    output_format: "json",
  };
}

function parseRecord(value: unknown): JsonRecord {
  if (!isRecord(value)) throw new ApiContractError();
  return value;
}

function datgLabPath(operation: LabOperation, projectId?: string): string {
  if (!projectId)
    throw new TypeError("Select a project before inspecting DATG indexes.");
  const base = `/api/v1/projects/${pathIdentifier(projectId)}/datg/index/inspect`;
  if (operation === "datg-targets") return `${base}/targets`;
  if (operation === "datg-covered") return `${base}/anti/covered`;
  if (operation === "datg-frequency") return `${base}/anti/frequency`;
  if (operation === "datg-logit-preview")
    return `/api/v1/projects/${pathIdentifier(projectId)}/datg/index/preview/logits`;
  throw new TypeError("The DATG inspection operation is not supported.");
}

function datgPreviewTargets(unit: DatgIndexPublication["unit"]): {
  readonly targetPhonemes: readonly string[];
  readonly targetUnit: string;
  readonly coveredPhonemes: readonly string[];
} {
  if (unit === "diphone")
    return {
      targetPhonemes: ["p", "b", "t"],
      targetUnit: "p-b",
      coveredPhonemes: ["b", "t"],
    };
  if (unit === "triphone")
    return {
      targetPhonemes: ["p", "b", "t", "k"],
      targetUnit: "p-b-t",
      coveredPhonemes: ["b", "t", "k"],
    };
  return {
    targetPhonemes: ["p", "b"],
    targetUnit: "p",
    coveredPhonemes: ["b"],
  };
}

function parseDatgLogitPreview(
  value: unknown,
  request: JsonRecord,
): DatgLogitDeltaPreview {
  if (
    !isRecord(value) ||
    value.schema_id !== "corpuskit.datg-logit-delta-preview.v1" ||
    typeof value.cache_key_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.cache_key_sha256) ||
    value.cache_key_sha256 !== request.cache_key_sha256 ||
    value.generation_executed !== false ||
    value.model_loaded !== false ||
    value.network_used !== false
  )
    throw new ApiContractError();
  const original = parseLogitMatrix(value.original_logits);
  const deltas = parseLogitMatrix(value.delta_logits);
  const modified = parseLogitMatrix(value.modified_logits);
  const requested = parseLogitMatrix(request.logits);
  if (
    !sameMatrixShape(original, deltas) ||
    !sameMatrixShape(original, modified) ||
    !sameMatrixShape(original, requested)
  )
    throw new ApiContractError();
  for (let row = 0; row < original.length; row += 1)
    for (let tokenId = 0; tokenId < original[row]!.length; tokenId += 1) {
      if (
        original[row]![tokenId] !== requested[row]![tokenId] ||
        modified[row]![tokenId]! - original[row]![tokenId]! !==
          deltas[row]![tokenId]
      )
        throw new ApiContractError();
    }
  const width = original[0]!.length;
  const attribute = parsePreviewTokenIds(value.attribute_token_ids, width);
  const antiAttribute = parsePreviewTokenIds(
    value.anti_attribute_token_ids,
    width,
  );
  return {
    schema_id: value.schema_id,
    cache_key_sha256: value.cache_key_sha256,
    original_logits: original,
    delta_logits: deltas,
    modified_logits: modified,
    attribute_token_ids: attribute,
    anti_attribute_token_ids: antiAttribute,
    generation_executed: false,
    model_loaded: false,
    network_used: false,
  };
}

function parseLogitMatrix(value: unknown): readonly (readonly number[])[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 8)
    throw new ApiContractError();
  const rows = value.map((row) => {
    if (
      !Array.isArray(row) ||
      row.length < 1 ||
      row.length > 2048 ||
      row.some((item) => typeof item !== "number" || !Number.isFinite(item))
    )
      throw new ApiContractError();
    return row as readonly number[];
  });
  if (rows.some((row) => row.length !== rows[0]!.length))
    throw new ApiContractError();
  return rows;
}

function sameMatrixShape(
  left: readonly (readonly number[])[],
  right: readonly (readonly number[])[],
): boolean {
  return left.length === right.length && left[0]!.length === right[0]!.length;
}

function parsePreviewTokenIds(
  value: unknown,
  width: number,
): readonly number[] {
  if (
    !Array.isArray(value) ||
    value.length > width ||
    value.some(
      (item) =>
        !Number.isSafeInteger(item) ||
        (item as number) < 0 ||
        (item as number) >= width,
    )
  )
    throw new ApiContractError();
  const tokenIds = value as number[];
  if (
    tokenIds.some(
      (tokenId, index) => index > 0 && tokenIds[index - 1]! >= tokenId,
    )
  )
    throw new ApiContractError();
  return tokenIds;
}

function parseDatgIndexes(value: unknown): readonly DatgIndexPublication[] {
  if (!Array.isArray(value)) throw new ApiContractError();
  return value.map(parseDatgIndex);
}

function parseDatgIndex(value: unknown): DatgIndexPublication {
  if (
    !isRecord(value) ||
    value.schema_id !== "corpuskit.datg-index-publication.v1" ||
    !isUuid(value.build_run_id) ||
    typeof value.cache_key_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.cache_key_sha256) ||
    typeof value.content_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.content_sha256) ||
    typeof value.runtime_id !== "string" ||
    typeof value.language !== "string" ||
    (value.unit !== "phoneme" &&
      value.unit !== "diphone" &&
      value.unit !== "triphone") ||
    !isNonnegativeInteger(value.vocabulary_size) ||
    value.vocabulary_size < 1 ||
    !isNonnegativeInteger(value.indexed_token_count) ||
    value.indexed_token_count > value.vocabulary_size ||
    !isNonnegativeInteger(value.size_bytes) ||
    value.size_bytes < 1 ||
    typeof value.created_at !== "string" ||
    !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value.created_at) ||
    Number.isNaN(Date.parse(value.created_at))
  )
    throw new ApiContractError();
  return value as unknown as DatgIndexPublication;
}

function parseValidation(value: unknown, kind: AdvancedRunKind): JsonRecord {
  const contract = validationContracts[kind];
  if (
    !isRecord(value) ||
    value.schema_id !== contract.schema ||
    value.valid !== true ||
    value.worker_only !== true ||
    value.network_during_validation !== false ||
    (contract.operation !== undefined && value.operation !== contract.operation)
  )
    throw new ApiContractError();
  return value;
}

function parseEstimate(value: unknown, kind: AdvancedRunKind): JsonRecord {
  const schema = estimateContracts[kind];
  if (
    schema === undefined ||
    !isRecord(value) ||
    value.schema_id !== schema ||
    value.network_during_estimate !== false
  )
    throw new ApiContractError();
  return value;
}

function parseAdvancedCapabilities(value: unknown): AdvancedCapabilities {
  if (
    !isRecord(value) ||
    value.schema_id !== "corpuskit.advanced-capabilities.v2" ||
    value.advanced_operation_routes_validation_only !== true ||
    value.durable_run_submission_route !== "/api/v1/runs" ||
    (value.datg_inspection !== "configured_read_only" &&
      value.datg_inspection !== "unavailable") ||
    value.phon_rl_lab !== "bounded_optional_dependency" ||
    !Array.isArray(value.hosted_models) ||
    !Array.isArray(value.huggingface_repositories) ||
    !Array.isArray(value.local_models) ||
    !Array.isArray(value.datg_runtimes) ||
    !Array.isArray(value.phon_rl_runtimes)
  )
    throw new ApiContractError();
  return {
    advanced_operation_routes_validation_only: true,
    durable_run_submission_route: "/api/v1/runs",
    hosted_models: value.hosted_models.map(parseHosted),
    huggingface_repositories: value.huggingface_repositories.map(
      parseHuggingFaceRepository,
    ),
    local_models: value.local_models.map(parseLocal),
    datg_runtimes: value.datg_runtimes.map(parseDatg),
    phon_rl_runtimes: value.phon_rl_runtimes.map(parsePhonRl),
    datg_inspection: value.datg_inspection,
    phon_rl_lab: "bounded_optional_dependency",
  };
}

function parseHuggingFaceRepository(
  value: unknown,
): HuggingFaceRepositoryOption {
  if (
    !isRecord(value) ||
    typeof value.dataset !== "string" ||
    typeof value.config !== "string" ||
    typeof value.split !== "string" ||
    typeof value.text_column !== "string" ||
    typeof value.revision !== "string" ||
    !/^[0-9a-f]{40}$/u.test(value.revision) ||
    typeof value.language !== "string" ||
    !Number.isSafeInteger(value.max_samples) ||
    (value.max_samples as number) <= 0
  )
    throw new ApiContractError();
  return {
    dataset: value.dataset,
    config: value.config,
    split: value.split,
    text_column: value.text_column,
    revision: value.revision,
    language: value.language,
    max_samples: value.max_samples as number,
  };
}

function parseHosted(value: unknown): HostedModelOption {
  if (!isRecord(value)) throw new ApiContractError();
  const requestDelaySeconds =
    value.request_delay_seconds === undefined ? 0 : value.request_delay_seconds;
  if (
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    typeof value.connection_id !== "string" ||
    !isStringArray(value.prompt_template_ids) ||
    !Number.isSafeInteger(value.max_output_tokens_per_request) ||
    (value.max_output_tokens_per_request as number) <= 0 ||
    typeof requestDelaySeconds !== "number" ||
    !Number.isFinite(requestDelaySeconds) ||
    requestDelaySeconds < 0 ||
    requestDelaySeconds > 30
  )
    throw new ApiContractError();
  return {
    provider: value.provider,
    model: value.model,
    connection_id: value.connection_id,
    max_output_tokens_per_request:
      value.max_output_tokens_per_request as number,
    request_delay_seconds: requestDelaySeconds,
    prompt_template_ids: value.prompt_template_ids,
  };
}

function parseLocal(value: unknown): LocalModelOption {
  if (
    !isRecord(value) ||
    typeof value.model !== "string" ||
    typeof value.revision !== "string" ||
    !isStringArray(value.allowed_devices) ||
    !isStringArray(value.allowed_quantizations) ||
    typeof value.allow_phon_rl_adapters !== "boolean"
  )
    throw new ApiContractError();
  return {
    model: value.model,
    revision: value.revision,
    allowed_devices: value.allowed_devices,
    allowed_quantizations: value.allowed_quantizations,
    allow_phon_rl_adapters: value.allow_phon_rl_adapters,
  };
}

function parseDatg(value: unknown): DatgRuntimeOption {
  if (
    !isRecord(value) ||
    typeof value.runtime_id !== "string" ||
    !isStringArray(value.allowed_quantizations)
  )
    throw new ApiContractError();
  return {
    runtime_id: value.runtime_id,
    allowed_quantizations: value.allowed_quantizations,
  };
}

function parsePhonRl(value: unknown): PhonRlRuntimeOption {
  if (
    !isRecord(value) ||
    typeof value.runtime_id !== "string" ||
    typeof value.allow_peft !== "boolean" ||
    typeof value.allow_static_prompts !== "boolean" ||
    !isStringArray(value.allowed_prompt_strategies)
  )
    throw new ApiContractError();
  return {
    runtime_id: value.runtime_id,
    allow_peft: value.allow_peft,
    allow_static_prompts: value.allow_static_prompts,
    allowed_prompt_strategies: value.allowed_prompt_strategies,
  };
}

function parseCliPreview(value: unknown): CliPreview {
  if (
    !isRecord(value) ||
    typeof value.workflow !== "string" ||
    !isStringArray(value.argv) ||
    typeof value.posix_command !== "string" ||
    typeof value.powershell_command !== "string" ||
    typeof value.reproducibility !== "string" ||
    !isStringArray(value.warnings)
  )
    throw new ApiContractError();
  return {
    workflow: value.workflow,
    argv: value.argv,
    posix_command: value.posix_command,
    powershell_command: value.powershell_command,
    reproducibility: value.reproducibility,
    warnings: value.warnings,
  };
}

function inspectSecrets(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(inspectSecrets);
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, child] of Object.entries(value)) {
    const parts = key
      .replaceAll(/([a-z0-9])([A-Z])/gu, "$1_$2")
      .toLowerCase()
      .split(/[^a-z0-9]+/u)
      .filter(Boolean);
    const last = parts.at(-1);
    const safeReference =
      last === "id" || last === "ref" || last === "reference";
    const compact = parts.join("");
    if (
      !safeReference &&
      (parts.some((part) => sensitiveParts.has(part)) ||
        last === "token" ||
        compact.includes("apikey") ||
        compact.includes("clientsecret"))
    )
      throw new TypeError(
        "Credentials are not accepted. Use server-managed connection identifiers.",
      );
    inspectSecrets(child);
  }
}

function preferredDevice(option: LocalModelOption | undefined): string {
  if (!option) return "cpu";
  return option.allowed_devices.includes("cpu")
    ? "cpu"
    : (option.allowed_devices[0] ?? "cpu");
}

function preferredQuantization(
  option: LocalModelOption | undefined,
  device: string,
): string {
  if (!option || option.allowed_quantizations.includes("none")) return "none";
  return device === "cuda"
    ? (option.allowed_quantizations[0] ?? "none")
    : "none";
}
