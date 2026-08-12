import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import {
  advancedApi,
  cliTemplate,
  cliWorkflows,
  formatJson,
  labOperations,
  labTemplate,
  parseEditorObject,
  runTemplate,
  type AdvancedCapabilities,
  type DatgIndexPublication,
} from "@/lib/advanced";
import { authenticatedFetch } from "@/lib/browser-auth";
import { advancedRunKinds } from "@/lib/jobs";

const fetchMock = vi.mocked(authenticatedFetch);
afterEach(() => fetchMock.mockReset());

const catalogPayload = {
  schema_id: "corpuskit.advanced-capabilities.v2",
  advanced_operation_routes_validation_only: true,
  durable_run_submission_route: "/api/v1/runs",
  hosted_models: [
    {
      provider: "openai",
      model: "openai/demo-model",
      connection_id: "demo-provider",
      max_output_tokens_per_request: 32,
      request_delay_seconds: 0.25,
      prompt_template_ids: ["coverage-v1"],
    },
  ],
  huggingface_repositories: [
    {
      dataset: "acme/demo-corpus",
      config: "default",
      split: "train",
      text_column: "text",
      revision: "c".repeat(40),
      language: "en-us",
      max_samples: 100,
    },
  ],
  local_models: [
    {
      model: "acme/tiny",
      revision: "a".repeat(40),
      allowed_devices: ["cuda"],
      allowed_quantizations: ["4bit"],
      allow_phon_rl_adapters: true,
    },
  ],
  datg_runtimes: [{ runtime_id: "tiny-datg", allowed_quantizations: ["none"] }],
  phon_rl_runtimes: [
    {
      runtime_id: "tiny-rl",
      allow_peft: false,
      allow_static_prompts: false,
      allowed_prompt_strategies: ["missing-units-v1"],
    },
  ],
  datg_inspection: "configured_read_only",
  phon_rl_lab: "bounded_optional_dependency",
};

const catalog: AdvancedCapabilities = {
  advanced_operation_routes_validation_only: true,
  durable_run_submission_route: "/api/v1/runs",
  hosted_models: catalogPayload.hosted_models,
  huggingface_repositories: catalogPayload.huggingface_repositories,
  local_models: catalogPayload.local_models,
  datg_runtimes: catalogPayload.datg_runtimes,
  phon_rl_runtimes: catalogPayload.phon_rl_runtimes,
  datg_inspection: "configured_read_only",
  phon_rl_lab: "bounded_optional_dependency",
};

const datgIndex: DatgIndexPublication = {
  schema_id: "corpuskit.datg-index-publication.v1",
  build_run_id: "123e4567-e89b-42d3-a456-426614174099",
  cache_key_sha256: "d".repeat(64),
  content_sha256: "e".repeat(64),
  runtime_id: "tiny-datg",
  language: "en-us",
  unit: "phoneme",
  vocabulary_size: 10,
  indexed_token_count: 8,
  size_bytes: 512,
  created_at: "2026-08-11T12:00:00Z",
};

const json = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });

describe("advanced API transport", () => {
  it("parses a strictly shaped redacted capability catalog", async () => {
    fetchMock.mockResolvedValueOnce(json(catalogPayload));
    await expect(advancedApi.capabilities()).resolves.toEqual(catalog);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/advanced/capabilities",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("treats a rolling-upgrade catalog without server pacing as zero delay", async () => {
    const legacyHosted: Record<string, unknown> = {
      ...catalogPayload.hosted_models[0],
    };
    delete legacyHosted.request_delay_seconds;
    fetchMock.mockResolvedValueOnce(
      json({ ...catalogPayload, hosted_models: [legacyHosted] }),
    );

    const parsed = await advancedApi.capabilities();

    expect(parsed.hosted_models[0]?.request_delay_seconds).toBe(0);
  });

  it.each([
    {},
    { ...catalogPayload, schema_id: "wrong" },
    { ...catalogPayload, advanced_operation_routes_validation_only: false },
    { ...catalogPayload, durable_run_submission_route: "/api/v1/jobs" },
    { ...catalogPayload, datg_inspection: "writable" },
    { ...catalogPayload, phon_rl_lab: "unbounded" },
    { ...catalogPayload, hosted_models: {} },
    { ...catalogPayload, huggingface_repositories: {} },
    { ...catalogPayload, local_models: {} },
    { ...catalogPayload, datg_runtimes: {} },
    { ...catalogPayload, phon_rl_runtimes: {} },
    {
      ...catalogPayload,
      hosted_models: [{ ...catalogPayload.hosted_models[0], provider: 1 }],
    },
    {
      ...catalogPayload,
      hosted_models: [
        {
          ...catalogPayload.hosted_models[0],
          max_output_tokens_per_request: 0,
        },
      ],
    },
    {
      ...catalogPayload,
      hosted_models: [
        {
          ...catalogPayload.hosted_models[0],
          request_delay_seconds: 30.01,
        },
      ],
    },
    {
      ...catalogPayload,
      local_models: [{ ...catalogPayload.local_models[0], revision: 1 }],
    },
    {
      ...catalogPayload,
      local_models: [
        { ...catalogPayload.local_models[0], allow_phon_rl_adapters: "yes" },
      ],
    },
    {
      ...catalogPayload,
      huggingface_repositories: [
        {
          ...catalogPayload.huggingface_repositories[0],
          revision: "main",
        },
      ],
    },
    {
      ...catalogPayload,
      datg_runtimes: [{ runtime_id: 1, allowed_quantizations: [] }],
    },
    {
      ...catalogPayload,
      phon_rl_runtimes: [
        { ...catalogPayload.phon_rl_runtimes[0], allow_peft: "no" },
      ],
    },
  ])("rejects malformed catalog variant %#", async (payload) => {
    fetchMock.mockResolvedValueOnce(json(payload));
    await expect(advancedApi.capabilities()).rejects.toThrow(
      "incompatible response",
    );
  });

  it("routes every advanced validation and only supported estimates", async () => {
    const responses = new Map<string, Record<string, unknown>>([
      [
        "/api/v1/generation/repository/validate",
        {
          schema_id: "corpuskit.repository-generation-validation.v1",
          valid: true,
          operation: "repository_generation",
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/model-runtime/hosted/validate",
        {
          schema_id: "corpuskit.model-runtime-validation.v1",
          valid: true,
          operation: "hosted_generation",
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/model-runtime/local/validate",
        {
          schema_id: "corpuskit.model-runtime-validation.v1",
          valid: true,
          operation: "local_generation",
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/model-runtime/analysis/validate",
        {
          schema_id: "corpuskit.model-runtime-validation.v1",
          valid: true,
          operation: "language_model_analysis",
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/datg/index/validate",
        {
          schema_id: "corpuskit.datg-runtime-validation.v1",
          valid: true,
          operation: "build_index",
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/datg/generation/validate",
        {
          schema_id: "corpuskit.datg-runtime-validation.v1",
          valid: true,
          operation: "guided_generation",
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/phon-rl/training/validate",
        {
          schema_id: "corpuskit.phon-rl-training-validation.v1",
          valid: true,
          worker_only: true,
          network_during_validation: false,
        },
      ],
      [
        "/api/v1/model-runtime/hosted/estimate",
        {
          schema_id: "corpuskit.hosted-cost-estimate.v1",
          network_during_estimate: false,
        },
      ],
      [
        "/api/v1/model-runtime/analysis/estimate",
        {
          schema_id: "corpuskit.language-model-analysis-estimate.v1",
          network_during_estimate: false,
        },
      ],
      [
        "/api/v1/phon-rl/training/estimate",
        {
          schema_id: "corpuskit.phon-rl-resource-estimate.v1",
          network_during_estimate: false,
        },
      ],
    ]);
    fetchMock.mockImplementation(async (path) =>
      json(responses.get(typeof path === "string" ? path : path.toString())),
    );
    for (const kind of advancedRunKinds) {
      await expect(advancedApi.validate(kind, {})).resolves.toMatchObject({
        valid: true,
        worker_only: true,
        network_during_validation: false,
      });
      await advancedApi.estimate(kind, {});
    }
    const paths = fetchMock.mock.calls.map(([path]) => path);
    expect(paths).toEqual([
      "/api/v1/generation/repository/validate",
      "/api/v1/model-runtime/hosted/validate",
      "/api/v1/model-runtime/hosted/estimate",
      "/api/v1/model-runtime/local/validate",
      "/api/v1/model-runtime/analysis/validate",
      "/api/v1/model-runtime/analysis/estimate",
      "/api/v1/datg/index/validate",
      "/api/v1/datg/generation/validate",
      "/api/v1/phon-rl/training/validate",
      "/api/v1/phon-rl/training/estimate",
    ]);
  });

  it("routes every bounded lab operation and the CLI preview", async () => {
    fetchMock.mockImplementation(async (path, init) => {
      if (String(path).endsWith("/datg/index/preview/logits")) {
        const request = JSON.parse(String(init?.body)) as {
          cache_key_sha256: string;
          logits: number[][];
        };
        const deltas = request.logits.map((row) =>
          row.map((_, tokenId) =>
            tokenId === 1 ? 2.5 : tokenId === 2 ? -1.25 : 0,
          ),
        );
        return json({
          schema_id: "corpuskit.datg-logit-delta-preview.v1",
          cache_key_sha256: request.cache_key_sha256,
          original_logits: request.logits,
          delta_logits: deltas,
          modified_logits: request.logits.map((row, rowIndex) =>
            row.map((value, tokenId) => value + deltas[rowIndex]![tokenId]!),
          ),
          attribute_token_ids: [1],
          anti_attribute_token_ids: [2],
          generation_executed: false,
          model_loaded: false,
          network_used: false,
        });
      }
      return json({ ok: true });
    });
    for (const operation of labOperations)
      await advancedApi.lab(
        operation.value,
        labTemplate(operation.value, datgIndex),
        operation.value.startsWith("datg-")
          ? "123e4567-e89b-42d3-a456-426614174001"
          : undefined,
      );
    fetchMock.mockResolvedValueOnce(
      json({
        workflow: "inventory",
        argv: ["corpusgen", "inventory"],
        posix_command: "corpusgen inventory",
        powershell_command: "corpusgen inventory",
        reproducibility: "external_dependency",
        warnings: [],
      }),
    );
    await expect(
      advancedApi.cli(cliTemplate("inventory")),
    ).resolves.toMatchObject({
      argv: ["corpusgen", "inventory"],
    });
    expect(fetchMock.mock.calls.map(([path]) => path)).toContain(
      "/api/v1/phon-rl/ppo/value-head",
    );
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual(
      expect.arrayContaining([
        "/api/v1/projects/123e4567-e89b-42d3-a456-426614174001/datg/index/inspect/targets",
        "/api/v1/projects/123e4567-e89b-42d3-a456-426614174001/datg/index/inspect/anti/covered",
        "/api/v1/projects/123e4567-e89b-42d3-a456-426614174001/datg/index/inspect/anti/frequency",
        "/api/v1/projects/123e4567-e89b-42d3-a456-426614174001/datg/index/preview/logits",
      ]),
    );
  });

  it("binds the strict logit preview response to its request", async () => {
    const request = labTemplate("datg-logit-preview", datgIndex);
    const valid = {
      schema_id: "corpuskit.datg-logit-delta-preview.v1",
      cache_key_sha256: datgIndex.cache_key_sha256,
      original_logits: request.logits,
      delta_logits: [[0, 2.5, -1.25, 0, 0, 0, 0, 0, 0, 0]],
      modified_logits: [[0, 3.5, 0.75, 3, 4, 5, 6, 7, 8, 9]],
      attribute_token_ids: [1],
      anti_attribute_token_ids: [2],
      generation_executed: false,
      model_loaded: false,
      network_used: false,
    };
    fetchMock.mockResolvedValueOnce(json(valid));
    await expect(
      advancedApi.lab(
        "datg-logit-preview",
        request,
        "123e4567-e89b-42d3-a456-426614174001",
      ),
    ).resolves.toEqual(valid);

    for (const invalid of [
      { ...valid, cache_key_sha256: "e".repeat(64) },
      { ...valid, original_logits: [[9, 1, 2, 3, 4, 5, 6, 7, 8, 9]] },
      { ...valid, delta_logits: [[0, 99, -1.25, 0, 0, 0, 0, 0, 0, 0]] },
      { ...valid, anti_attribute_token_ids: [2, 1] },
      { ...valid, model_loaded: true },
    ]) {
      fetchMock.mockResolvedValueOnce(json(invalid));
      await expect(
        advancedApi.lab(
          "datg-logit-preview",
          request,
          "123e4567-e89b-42d3-a456-426614174001",
        ),
      ).rejects.toThrow("incompatible response");
    }
  });

  it("loads a strict project-scoped DATG publication catalog", async () => {
    fetchMock.mockResolvedValueOnce(json([datgIndex]));
    await expect(
      advancedApi.datgIndexes("123e4567-e89b-42d3-a456-426614174001"),
    ).resolves.toEqual([datgIndex]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/projects/123e4567-e89b-42d3-a456-426614174001/datg/indexes",
      expect.objectContaining({ cache: "no-store" }),
    );

    for (const invalid of [
      { ...datgIndex, schema_id: "wrong" },
      { ...datgIndex, cache_key_sha256: "0" },
      { ...datgIndex, unit: "word" },
      { ...datgIndex, indexed_token_count: 11 },
      { ...datgIndex, created_at: "not-a-time" },
    ]) {
      fetchMock.mockResolvedValueOnce(json([invalid]));
      await expect(
        advancedApi.datgIndexes("123e4567-e89b-42d3-a456-426614174001"),
      ).rejects.toThrow("incompatible response");
    }
  });

  it("rejects malformed generic and CLI responses", async () => {
    fetchMock.mockResolvedValueOnce(
      json({
        schema_id: "corpuskit.model-runtime-validation.v1",
        valid: true,
        operation: "hosted_generation",
        worker_only: true,
        network_during_validation: false,
      }),
    );
    await expect(advancedApi.validate("generate-local", {})).rejects.toThrow(
      "incompatible response",
    );
    fetchMock.mockResolvedValueOnce(
      json({
        schema_id: "corpuskit.hosted-cost-estimate.v1",
        network_during_estimate: true,
      }),
    );
    await expect(advancedApi.estimate("generate-llm", {})).rejects.toThrow(
      "incompatible response",
    );
    fetchMock.mockResolvedValueOnce(json([]));
    await expect(advancedApi.lab("reward-peek", {})).rejects.toThrow(
      "incompatible response",
    );
    fetchMock.mockResolvedValueOnce(
      json({
        workflow: "inventory",
        argv: "not-an-array",
        posix_command: "x",
        powershell_command: "x",
        reproducibility: "x",
        warnings: [],
      }),
    );
    await expect(advancedApi.cli({ workflow: "inventory" })).rejects.toThrow(
      "incompatible response",
    );
  });
});

describe("safe editor and templates", () => {
  it("parses JSON objects while rejecting malformed, oversized, and credential content", () => {
    expect(parseEditorObject('{"connection_id":"safe"}')).toEqual({
      connection_id: "safe",
    });
    expect(
      parseEditorObject(
        '{"credential_ref":"opaque","token_id":1,"nested":[{"max_tokens":5}]}',
      ),
    ).toMatchObject({ credential_ref: "opaque" });
    for (const value of ["", "[1]", "{", "x".repeat(64 * 1024 + 1)])
      expect(() => parseEditorObject(value)).toThrow(TypeError);
    for (const key of [
      "api_key",
      "clientSecret",
      "password",
      "authorization_header",
      "access_token",
      "credential_value",
    ])
      expect(() =>
        parseEditorObject(JSON.stringify({ [key]: "leak" })),
      ).toThrow("Credentials are not accepted");
  });

  it("builds all run templates from configured selectors and secure placeholders", () => {
    for (const kind of advancedRunKinds) {
      const configured = runTemplate(kind, catalog);
      const unavailable = runTemplate(kind, null);
      expect(configured).toBeTypeOf("object");
      expect(unavailable).toBeTypeOf("object");
      expect(formatJson(configured)).not.toContain("secret");
    }
    expect(runTemplate("generate-llm", catalog)).toMatchObject({
      selection: { connection_id: "demo-provider" },
      max_tokens_per_request: 32,
      prompt_template_id: "coverage-v1",
    });
    expect(runTemplate("generate-llm", catalog)).not.toHaveProperty(
      "external_processing_confirmed",
    );
    expect(runTemplate("generate-repository", catalog)).toMatchObject({
      source: {
        kind: "hugging_face",
        spec: {
          dataset: "acme/demo-corpus",
          revision: "c".repeat(40),
          max_samples: 100,
          trust_remote_code: false,
        },
      },
    });
    expect(runTemplate("generate-local", catalog)).toMatchObject({
      selection: { device: "cuda", quantization: "4bit" },
    });
    expect(runTemplate("perplexity", catalog)).toMatchObject({
      composite_scoring: {
        options: { weights: { fluency: 1 } },
        candidates: [{ source_id: "sentence-1" }, { source_id: "sentence-2" }],
      },
    });
    expect(runTemplate("build-datg-index", catalog)).toHaveProperty(
      "runtime_id",
      "tiny-datg",
    );
    expect(runTemplate("generate-datg", catalog, datgIndex)).toHaveProperty(
      "index_cache_key_sha256",
      datgIndex.cache_key_sha256,
    );
    expect(runTemplate("generate-datg", catalog)).not.toHaveProperty(
      "index_cache_key_sha256",
    );
    expect(runTemplate("train-phon-rl", catalog)).toHaveProperty(
      "prompt_source.strategy_id",
      "missing-units-v1",
    );
  });

  it("prefers CPU and no quantization when those choices are allowed", () => {
    const cpuCatalog: AdvancedCapabilities = {
      ...catalog,
      local_models: [
        {
          ...catalog.local_models[0]!,
          allowed_devices: ["cuda", "cpu"],
          allowed_quantizations: ["4bit", "none"],
        },
      ],
    };
    expect(runTemplate("generate-local", cpuCatalog)).toMatchObject({
      selection: { device: "cpu", quantization: "none" },
    });
  });

  it("builds every bounded lab and CLI workflow template", () => {
    for (const operation of labOperations)
      expect(labTemplate(operation.value)).toBeTypeOf("object");
    expect(labTemplate("reward-peek")).toBe(labTemplate("reward-commit"));
    expect(labTemplate("ppo-value-head")).toHaveProperty("hidden_states_2d");
    expect(labTemplate("datg-targets", datgIndex)).toHaveProperty(
      "cache_key_sha256",
      datgIndex.cache_key_sha256,
    );
    expect(labTemplate("datg-targets")).not.toHaveProperty("cache_key_sha256");
    expect(labTemplate("datg-logit-preview", datgIndex)).toMatchObject({
      cache_key_sha256: datgIndex.cache_key_sha256,
      target_units: ["p"],
      logits: [expect.arrayContaining([0, 1, 2])],
    });
    for (const workflow of cliWorkflows)
      expect(cliTemplate(workflow)).toMatchObject({ workflow });
    expect(cliTemplate("generate")).toMatchObject({
      backend: "repository",
      timeout_seconds: 300,
    });
  });
});
