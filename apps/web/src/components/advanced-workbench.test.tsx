import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  capabilities: vi.fn(),
  datgIndexes: vi.fn(),
  validate: vi.fn(),
  estimate: vi.fn(),
  lab: vi.fn(),
  cli: vi.fn(),
  submit: vi.fn(),
  principal: vi.fn(),
  selected: true,
  projectId: "123e4567-e89b-42d3-a456-426614174001",
  contextMissing: false,
}));

vi.mock("@/components/project-context", () => ({
  useProjectContext: () =>
    mocks.contextMissing
      ? null
      : {
          projects: [],
          selectedProject: mocks.selected
            ? {
                id: mocks.projectId,
                name: "Demo project",
                description: "",
                created_at: "2026-08-11T12:00:00Z",
              }
            : null,
          loading: false,
          error: false,
          selectProject: vi.fn(),
          refreshProjects: vi.fn(),
        },
}));

vi.mock("@/lib/advanced", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/advanced")>("@/lib/advanced");
  return {
    ...actual,
    advancedApi: {
      capabilities: mocks.capabilities,
      datgIndexes: mocks.datgIndexes,
      validate: mocks.validate,
      estimate: mocks.estimate,
      lab: mocks.lab,
      cli: mocks.cli,
    },
  };
});

vi.mock("@/lib/jobs", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
  return { ...actual, jobsApi: { ...actual.jobsApi, submit: mocks.submit } };
});

vi.mock("@/lib/projects", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/projects")>("@/lib/projects");
  return { ...actual, getCurrentPrincipal: mocks.principal };
});

import { AdvancedWorkbench } from "@/components/advanced-workbench";

const catalog = {
  advanced_operation_routes_validation_only: true as const,
  durable_run_submission_route: "/api/v1/runs" as const,
  hosted_models: [
    {
      provider: "openai",
      model: "openai/demo-model",
      connection_id: "demo-provider",
      max_output_tokens_per_request: 128,
      request_delay_seconds: 0.25,
      prompt_template_ids: [],
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
  local_models: [],
  datg_runtimes: [],
  phon_rl_runtimes: [],
  datg_inspection: "unavailable" as const,
  phon_rl_lab: "bounded_optional_dependency" as const,
};

beforeEach(() => {
  mocks.selected = true;
  mocks.projectId = "123e4567-e89b-42d3-a456-426614174001";
  mocks.contextMissing = false;
  mocks.capabilities.mockResolvedValue(catalog);
  mocks.datgIndexes.mockResolvedValue([]);
  mocks.validate.mockResolvedValue({ valid: true, worker_only: true });
  mocks.estimate.mockResolvedValue({ estimated_ceiling_usd: "0.01" });
  mocks.lab.mockResolvedValue({ values: [[0, 1]] });
  mocks.cli.mockResolvedValue({
    workflow: "inventory",
    argv: ["corpusgen", "inventory"],
    posix_command: "PYTHONUTF8=1 corpusgen inventory eng --format json",
    powershell_command:
      "$env:PYTHONUTF8='1'; corpusgen inventory eng --format json",
    reproducibility: "external_dependency",
    warnings: ["Requires the PHOIBLE snapshot."],
  });
  mocks.submit.mockResolvedValue({
    id: "123e4567-e89b-42d3-a456-426614174009",
    project_id: "123e4567-e89b-42d3-a456-426614174001",
    corpus_version_id: null,
    parent_run_id: null,
    kind: "generate-llm",
    state: "queued",
    attempt: 1,
    spec_sha256: "a".repeat(64),
    outbox_state: "pending",
    cancellation_requested_at: null,
    created_at: "2026-08-11T12:00:00Z",
    result_summary: null,
    failure_code: null,
  });
  mocks.principal.mockResolvedValue({
    subject: "user|editor",
    organization_id: "123e4567-e89b-42d3-a456-426614174000",
    role: "editor",
    display_name: "Editor",
  });
  vi.stubGlobal("crypto", { randomUUID: () => "fixed-idempotency-id" });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("advanced runtime workbench", () => {
  it("keeps viewer access read-only while retaining inspection tools", async () => {
    const user = userEvent.setup();
    mocks.principal.mockResolvedValueOnce({
      subject: "user|viewer",
      organization_id: "123e4567-e89b-42d3-a456-426614174000",
      role: "viewer",
      display_name: "Viewer",
    });
    render(<AdvancedWorkbench />);

    expect(
      await screen.findByText(/Viewer access is read-only/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Validate policy" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Queue durable run" }),
    ).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText("Lab operation"),
      "ppo-kl-penalty",
    );
    expect(
      screen.getByRole("button", { name: "Run bounded calculation" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Generate CLI preview" }),
    ).toBeEnabled();
  });

  it("discloses gates, validates policy, and queues only the unchanged spec", async () => {
    const user = userEvent.setup();
    render(<AdvancedWorkbench />);

    expect(await screen.findAllByText("1 configured")).toHaveLength(2);
    expect(screen.getAllByText("Unavailable")).toHaveLength(3);
    expect(
      screen.getByText(/up to 0\.25 s server pacing/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/provider calls, model loading/i),
    ).toBeInTheDocument();
    const consent = screen.getByRole("checkbox", {
      name: /sends generation prompts and target details/i,
    });
    expect(consent).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: "Validate policy" }),
    ).toBeDisabled();
    await user.click(consent);
    await user.click(screen.getByRole("button", { name: "Validate policy" }));
    expect(
      await screen.findByText("Policy validation passed"),
    ).toBeInTheDocument();
    expect(mocks.validate).toHaveBeenCalledWith(
      "generate-llm",
      expect.objectContaining({
        selection: expect.objectContaining({ connection_id: "demo-provider" }),
        external_processing_confirmed: true,
      }),
    );
    const queue = screen.getByRole("button", { name: "Queue durable run" });
    expect(queue).toBeEnabled();
    await user.click(queue);
    expect(
      await screen.findByText(/Hosted LLM generation queued/),
    ).toBeInTheDocument();
    expect(mocks.submit).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "generate-llm" }),
      "advanced-generate-llm-fixed-idempotency-id",
    );
    expect(
      screen.getByRole("link", { name: "Open Job Center" }),
    ).toHaveAttribute("href", "/jobs");

    await user.type(screen.getByLabelText("Non-secret run configuration"), " ");
    expect(queue).toBeDisabled();
  });

  it("rejects credential-shaped editor content before transport", async () => {
    const user = userEvent.setup();
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    const editor = screen.getByLabelText("Non-secret run configuration");
    fireEvent.change(editor, {
      target: { value: '{"api_key":"must-not-leak"}' },
    });
    await user.click(
      screen.getByRole("checkbox", {
        name: /sends generation prompts and target details/i,
      }),
    );
    await user.click(screen.getByRole("button", { name: "Validate policy" }));

    expect(
      await screen.findByText(/Credentials are not accepted/),
    ).toBeInTheDocument();
    expect(mocks.validate).not.toHaveBeenCalled();
    expect(screen.queryByText("must-not-leak")).not.toBeInTheDocument();
  });

  it("rejects JSON-authored consent and resets deliberate consent after edits", async () => {
    const user = userEvent.setup();
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    const consent = screen.getByRole("checkbox", {
      name: /sends generation prompts and target details/i,
    });
    const validate = screen.getByRole("button", { name: "Validate policy" });
    await user.click(consent);
    expect(validate).toBeEnabled();
    const editor = screen.getByLabelText("Non-secret run configuration");
    await user.type(editor, " ");
    expect(consent).not.toBeChecked();
    expect(validate).toBeDisabled();

    fireEvent.change(editor, {
      target: { value: '{"external_processing_confirmed":true}' },
    });
    await user.click(consent);
    await user.click(validate);
    expect(
      await screen.findByText(/dedicated confirmation control/i),
    ).toBeInTheDocument();
    expect(mocks.validate).not.toHaveBeenCalled();
  });

  it("switches advanced templates and renders a sanitized server rejection", async () => {
    const user = userEvent.setup();
    mocks.validate.mockRejectedValue(new Error("private provider path"));
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    await user.selectOptions(
      screen.getByLabelText("Worker operation"),
      "train-phon-rl",
    );
    expect(
      (
        screen.getByLabelText(
          "Non-secret run configuration",
        ) as HTMLTextAreaElement
      ).value,
    ).toContain("prompt_source");
    await user.click(screen.getByRole("button", { name: "Validate policy" }));
    expect(
      await screen.findByText(/temporarily unavailable/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/private provider path/i),
    ).not.toBeInTheDocument();
  });

  it("runs only allowlisted bounded lab operations and explains DATG unavailability", async () => {
    const user = userEvent.setup();
    render(<AdvancedWorkbench />);
    expect(
      await screen.findByText(/no read-only index cache is configured/i),
    ).toBeInTheDocument();
    await user.selectOptions(
      screen.getByLabelText("Lab operation"),
      "ppo-kl-penalty",
    );
    expect(
      screen.queryByText(/no read-only index cache is configured/i),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Run bounded calculation" }),
    );
    expect(await screen.findByText("Lab result")).toBeInTheDocument();
    expect(mocks.lab).toHaveBeenCalledWith(
      "ppo-kl-penalty",
      expect.objectContaining({ policy_log_probs: expect.any(Object) }),
    );
  });

  it("previews and copies POSIX and PowerShell commands without execution", async () => {
    const user = userEvent.setup();
    const writeText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue(undefined);
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    await user.click(
      screen.getByRole("button", { name: "Generate CLI preview" }),
    );
    const result = await screen.findByText(/PYTHONUTF8=1 corpusgen inventory/);
    expect(result).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy command" }));
    expect(await screen.findByText("Command copied.")).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith(
      "PYTHONUTF8=1 corpusgen inventory eng --format json",
    );
    await user.selectOptions(
      screen.getByLabelText("Shell display"),
      "powershell",
    );
    expect(screen.getByText(/\$env:PYTHONUTF8/)).toBeInTheDocument();
    expect(
      screen.getByText("Requires the PHOIBLE snapshot."),
    ).toBeInTheDocument();
  });

  it("shows unavailable catalog, missing project, and clipboard fallback states", async () => {
    const user = userEvent.setup();
    mocks.selected = false;
    mocks.capabilities.mockRejectedValue(new Error("private catalog failure"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    render(<AdvancedWorkbench />);
    expect(
      await screen.findByText("Capability catalog unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Select a project before queuing/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Queue durable run" }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Generate CLI preview" }),
    );
    await screen.findByText(/PYTHONUTF8=1 corpusgen inventory/);
    await user.click(screen.getByRole("button", { name: "Copy command" }));
    expect(await screen.findByText(/Copy is unavailable/)).toBeInTheDocument();
    expect(
      screen.queryByText(/private catalog failure/),
    ).not.toBeInTheDocument();
  });

  it("retains labelled controls and distinct alert/live regions", async () => {
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    for (const name of [
      "Worker operation",
      "Non-secret run configuration",
      "Lab operation",
      "Bounded request",
      "Workflow",
      "Preview options",
    ])
      expect(screen.getByLabelText(name)).toBeInTheDocument();
    const panels = screen.getAllByRole("heading", { level: 2 });
    expect(
      within(panels[0]!.closest("section")!).getByText("Validate, then queue"),
    ).toBeInTheDocument();
    expect(
      within(panels[0]!.closest("section")!).getByText("POST /api/v1/runs"),
    ).toBeInTheDocument();
  });

  it("shows the empty allowlist and configured read-only inspection states", async () => {
    mocks.contextMissing = true;
    mocks.capabilities.mockResolvedValue({
      ...catalog,
      hosted_models: [],
      huggingface_repositories: [],
      datg_inspection: "configured_read_only",
    });
    render(<AdvancedWorkbench />);
    expect(
      await screen.findByText(/No advanced worker allowlists are configured/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("read-only index inspection enabled"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Select a project before queuing/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/no read-only index cache is configured/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Select a project to inspect its DATG indexes."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Run bounded calculation" }),
    ).toBeDisabled();
    expect(
      (screen.getByLabelText("Bounded request") as HTMLTextAreaElement).value,
    ).not.toContain("0000000000000000");
  });

  it("uses the selected project's published DATG key for runs and inspection", async () => {
    const user = userEvent.setup();
    const index = {
      schema_id: "corpuskit.datg-index-publication.v1" as const,
      build_run_id: "123e4567-e89b-42d3-a456-426614174099",
      cache_key_sha256: "d".repeat(64),
      content_sha256: "e".repeat(64),
      runtime_id: "tiny-datg",
      language: "en-us",
      unit: "phoneme" as const,
      vocabulary_size: 10,
      indexed_token_count: 8,
      size_bytes: 512,
      created_at: "2026-08-11T12:00:00Z",
    };
    mocks.capabilities.mockResolvedValue({
      ...catalog,
      datg_runtimes: [
        { runtime_id: "tiny-datg", allowed_quantizations: ["none"] },
      ],
      datg_inspection: "configured_read_only",
    });
    mocks.datgIndexes.mockResolvedValue([index]);
    render(<AdvancedWorkbench />);

    expect(
      await screen.findAllByText(new RegExp(index.cache_key_sha256)),
    ).not.toHaveLength(0);
    expect(
      (screen.getByLabelText("Bounded request") as HTMLTextAreaElement).value,
    ).toContain(index.cache_key_sha256);
    await user.click(
      screen.getByRole("button", { name: "Run bounded calculation" }),
    );
    expect(mocks.lab).toHaveBeenCalledWith(
      "datg-targets",
      expect.objectContaining({ cache_key_sha256: index.cache_key_sha256 }),
      mocks.projectId,
    );

    await user.selectOptions(
      screen.getByLabelText("Worker operation"),
      "generate-datg",
    );
    expect(
      (
        screen.getByLabelText(
          "Non-secret run configuration",
        ) as HTMLTextAreaElement
      ).value,
    ).toContain(index.cache_key_sha256);
    await user.click(screen.getByRole("button", { name: "Validate policy" }));
    expect(mocks.validate).toHaveBeenCalledWith(
      "generate-datg",
      expect.objectContaining({
        index_cache_key_sha256: index.cache_key_sha256,
      }),
    );
  });

  it("visualizes exact DATG before, delta, after, and token classifications", async () => {
    const user = userEvent.setup();
    const index = {
      schema_id: "corpuskit.datg-index-publication.v1" as const,
      build_run_id: "123e4567-e89b-42d3-a456-426614174099",
      cache_key_sha256: "d".repeat(64),
      content_sha256: "e".repeat(64),
      runtime_id: "tiny-datg",
      language: "en-us",
      unit: "phoneme" as const,
      vocabulary_size: 3,
      indexed_token_count: 3,
      size_bytes: 512,
      created_at: "2026-08-11T12:00:00Z",
    };
    mocks.capabilities.mockResolvedValue({
      ...catalog,
      datg_inspection: "configured_read_only",
    });
    mocks.datgIndexes.mockResolvedValue([index]);
    mocks.lab.mockResolvedValue({
      schema_id: "corpuskit.datg-logit-delta-preview.v1",
      cache_key_sha256: index.cache_key_sha256,
      original_logits: [[0, 1, 2]],
      delta_logits: [[-1.25, 2.5, 0]],
      modified_logits: [[-1.25, 3.5, 2]],
      attribute_token_ids: [1],
      anti_attribute_token_ids: [0],
      generation_executed: false,
      model_loaded: false,
      network_used: false,
    });
    render(<AdvancedWorkbench />);
    await screen.findAllByText(new RegExp(index.cache_key_sha256));
    await user.selectOptions(
      screen.getByLabelText("Lab operation"),
      "datg-logit-preview",
    );
    await user.click(
      screen.getByRole("button", { name: "Run bounded calculation" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Logit delta preview" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/did not load a model, use a network, generate text/i),
    ).toBeInTheDocument();
    const table = screen.getByRole("table", {
      name: /Before, additive delta, and after values/i,
    });
    expect(within(table).getByText("Attribute")).toBeInTheDocument();
    expect(within(table).getByText("Anti-attribute")).toBeInTheDocument();
    expect(within(table).getByText("Unclassified")).toBeInTheDocument();
    expect(mocks.lab).toHaveBeenCalledWith(
      "datg-logit-preview",
      expect.objectContaining({
        cache_key_sha256: index.cache_key_sha256,
        logits: [[0, 1, 2]],
      }),
      mocks.projectId,
    );
  });

  it("refetches and clears DATG templates when the selected project changes", async () => {
    const firstIndex = {
      schema_id: "corpuskit.datg-index-publication.v1" as const,
      build_run_id: "123e4567-e89b-42d3-a456-426614174099",
      cache_key_sha256: "d".repeat(64),
      content_sha256: "e".repeat(64),
      runtime_id: "tiny-datg",
      language: "en-us",
      unit: "phoneme" as const,
      vocabulary_size: 10,
      indexed_token_count: 8,
      size_bytes: 512,
      created_at: "2026-08-11T12:00:00Z",
    };
    mocks.capabilities.mockResolvedValue({
      ...catalog,
      datg_inspection: "configured_read_only",
    });
    mocks.datgIndexes
      .mockResolvedValueOnce([firstIndex])
      .mockResolvedValueOnce([]);
    const { rerender } = render(<AdvancedWorkbench />);
    expect(
      await screen.findAllByText(new RegExp(firstIndex.cache_key_sha256)),
    ).not.toHaveLength(0);

    mocks.projectId = "123e4567-e89b-42d3-a456-426614174002";
    rerender(<AdvancedWorkbench />);
    expect(
      await screen.findByText("Build a DATG index first."),
    ).toBeInTheDocument();
    expect(
      (screen.getByLabelText("Bounded request") as HTMLTextAreaElement).value,
    ).not.toContain(firstIndex.cache_key_sha256);
    expect(mocks.datgIndexes).toHaveBeenLastCalledWith(
      mocks.projectId,
      expect.any(AbortSignal),
    );
  });

  it("does not update state after an in-flight catalog request is aborted", async () => {
    let rejectCatalog: ((error: Error) => void) | undefined;
    mocks.capabilities.mockReturnValue(
      new Promise((_, reject) => {
        rejectCatalog = reject;
      }),
    );
    const { unmount } = render(<AdvancedWorkbench />);
    unmount();
    await act(async () => rejectCatalog?.(new Error("late private failure")));
    expect(screen.queryByText(/late private failure/)).not.toBeInTheDocument();
  });

  it("omits non-applicable estimates and sanitizes durable submission failures", async () => {
    const user = userEvent.setup();
    mocks.estimate.mockResolvedValue({ estimate: "not_applicable" });
    mocks.submit.mockRejectedValue(new Error("private database path"));
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    await user.selectOptions(
      screen.getByLabelText("Worker operation"),
      "generate-local",
    );
    await user.click(screen.getByRole("button", { name: "Validate policy" }));
    expect(
      await screen.findByText("Policy validation passed"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Conservative estimate")).not.toBeInTheDocument();
    const queue = screen.getByRole("button", { name: "Queue durable run" });
    fireEvent.click(queue);
    fireEvent.click(queue);
    expect(
      await screen.findByText(/temporarily unavailable/i),
    ).toBeInTheDocument();
    expect(mocks.submit).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/private database path/)).not.toBeInTheDocument();
  });

  it("renders sanitized lab and CLI errors plus an empty-warning preview", async () => {
    const user = userEvent.setup();
    mocks.lab.mockRejectedValueOnce(new Error("private tensor detail"));
    mocks.cli.mockRejectedValueOnce(new Error("private shell detail"));
    render(<AdvancedWorkbench />);
    await screen.findAllByText("1 configured");
    await user.selectOptions(
      screen.getByLabelText("Lab operation"),
      "ppo-kl-penalty",
    );
    await user.click(
      screen.getByRole("button", { name: "Run bounded calculation" }),
    );
    expect(await screen.findByText("Lab request rejected")).toBeInTheDocument();
    expect(screen.queryByText(/private tensor detail/)).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Generate CLI preview" }),
    );
    expect(await screen.findByText("Preview rejected")).toBeInTheDocument();
    expect(screen.queryByText(/private shell detail/)).not.toBeInTheDocument();

    mocks.cli.mockResolvedValueOnce({
      workflow: "evaluate",
      argv: ["corpusgen", "evaluate"],
      posix_command: "corpusgen evaluate corpus.txt",
      powershell_command: "corpusgen evaluate corpus.txt",
      reproducibility: "exact_inputs_required",
      warnings: [],
    });
    await user.selectOptions(screen.getByLabelText("Workflow"), "evaluate");
    fireEvent.change(screen.getByLabelText("Preview options"), {
      target: {
        value: '{"workflow":"evaluate","language":"en-us","sentences":["Hi"]}',
      },
    });
    await user.click(
      screen.getByRole("button", { name: "Generate CLI preview" }),
    );
    expect(
      await screen.findByText("corpusgen evaluate corpus.txt"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });
});
