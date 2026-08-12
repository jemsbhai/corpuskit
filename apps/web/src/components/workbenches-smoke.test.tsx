import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/project-context", () => ({
  useProjectContext: () => ({
    projects: [
      {
        id: "123e4567-e89b-42d3-a456-426614174001",
        name: "Demo",
        description: "",
        created_at: "2026-08-11T12:00:00Z",
      },
    ],
    selectedProject: {
      id: "123e4567-e89b-42d3-a456-426614174001",
      name: "Demo",
      description: "",
      created_at: "2026-08-11T12:00:00Z",
    },
    loading: false,
    error: false,
    selectProject: vi.fn(),
    refreshProjects: vi.fn(),
  }),
}));
vi.mock("@/lib/projects", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/projects")>("@/lib/projects");
  return {
    ...actual,
    getCurrentPrincipal: vi.fn().mockResolvedValue({
      subject: "user|editor",
      organization_id: "123e4567-e89b-42d3-a456-426614174000",
      role: "editor",
      display_name: "Acceptance editor",
    }),
  };
});
vi.mock("@/lib/g2p", () => ({
  g2pApi: {
    languages: vi
      .fn()
      .mockResolvedValue({ backend: "espeak", languages: ["en-us"] }),
    variants: vi.fn(),
    single: vi.fn(),
    batch: vi.fn(),
  },
}));
vi.mock("@/lib/analysis", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/analysis")>("@/lib/analysis");
  return {
    ...actual,
    analysisApi: {
      phonologyStatus: vi.fn().mockResolvedValue({
        cache_available: true,
        loaded: true,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: { language_count: 1, inventory_count: 1, segment_count: 1 },
      }),
      loadPhonology: vi.fn(),
      featureCatalog: vi.fn().mockResolvedValue({
        names: Array.from({ length: 38 }, (_, index) => `feature-${index}`),
      }),
      languages: vi.fn(),
      espeakMappings: vi.fn(),
      inventorySources: vi.fn(),
      inventory: vi.fn(),
      allInventories: vi.fn(),
      segments: vi.fn(),
    },
  };
});
vi.mock("@/lib/capabilities", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/capabilities")>(
      "@/lib/capabilities",
    );
  return {
    ...actual,
    fetchCapabilityCatalog: vi.fn().mockResolvedValue({
      source: "api",
      environment: "test",
      engineVersion: "0.1.7",
      checkedAt: "2026-08-11T12:00:00Z",
      capabilities: [
        {
          id: "optimization",
          name: "Optimization",
          description: "Ready",
          status: "available",
          profile: "CPU",
          required: false,
        },
        {
          id: "phoible",
          name: "PHOIBLE",
          description: "Ready",
          status: "available",
          profile: "CPU",
          required: false,
        },
      ],
    }),
  };
});
vi.mock("@/lib/jobs", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
  return {
    ...actual,
    jobsApi: { ...actual.jobsApi, list: vi.fn().mockResolvedValue([]) },
  };
});
vi.mock("@/lib/artifacts", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/artifacts")>("@/lib/artifacts");
  return {
    ...actual,
    artifactsApi: {
      ...actual.artifactsApi,
      list: vi.fn().mockResolvedValue([]),
      get: vi.fn(),
    },
  };
});
vi.mock("@/lib/selection", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/selection")>("@/lib/selection");
  return {
    ...actual,
    selectionApi: {
      select: vi
        .fn()
        .mockImplementation(
          async (request: {
            readonly candidates: readonly string[];
            readonly options: { readonly algorithm: string };
          }) => ({
            selected_indices: [0],
            selected_sentences: [request.candidates[0]],
            coverage: 0.5,
            covered_units: ["p"],
            missing_units: ["b"],
            unit: "phoneme",
            target_mode: "explicit",
            algorithm: request.options.algorithm,
            elapsed_seconds: 0.01,
            iterations: 1,
            metadata: {
              evaluations: 1,
              epsilon: null,
              seed: null,
              sample_size: null,
              kl_divergence: null,
              solver_status: null,
              pareto_front: [],
            },
          }),
        ),
    },
  };
});

import { ArtifactManager } from "@/components/artifact-manager";
import { CoverageWeightingLab } from "@/components/coverage-weighting-lab";
import { GenerationScoringStudio } from "@/components/generation-scoring-studio";
import { G2PStudio } from "@/components/g2p-studio";
import { InventoryExplorer } from "@/components/inventory-explorer";
import { JobCenter } from "@/components/job-center";
import { SelectionStudio } from "@/components/selection-studio";
import { analysisApi } from "@/lib/analysis";
import { artifactsApi } from "@/lib/artifacts";

afterEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("production workbench surfaces", () => {
  it("renders G2P and Inventory with Unicode/provenance semantics", async () => {
    const { unmount } = render(<G2PStudio />);
    expect(
      screen.getByRole("heading", { name: /transcription controls/i }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/1 supported eSpeak language code/i),
    ).toBeInTheDocument();
    unmount();
    render(<InventoryExplorer />);
    expect(screen.getByRole("search")).toBeInTheDocument();
    expect(await screen.findByText("38 / 38 features")).toBeInTheDocument();
    expect(screen.getByText(/PHOIBLE-backed/i)).toBeInTheDocument();
  });

  it("explicitly loads PHOIBLE and displays the returned statistics", async () => {
    const user = userEvent.setup();
    vi.mocked(analysisApi.phonologyStatus).mockResolvedValueOnce({
      cache_available: true,
      loaded: false,
      revision: "b".repeat(40),
      sha256: "c".repeat(64),
      stats: null,
    });
    vi.mocked(analysisApi.loadPhonology).mockResolvedValueOnce({
      cache_available: true,
      loaded: true,
      revision: "b".repeat(40),
      sha256: "c".repeat(64),
      stats: {
        language_count: 2095,
        inventory_count: 3020,
        segment_count: 105484,
      },
    });

    render(<InventoryExplorer />);
    await user.click(
      await screen.findByRole("button", { name: "Load PHOIBLE statistics" }),
    );

    expect(analysisApi.loadPhonology).toHaveBeenCalledOnce();
    expect(await screen.findByText(/2,095 languages/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Load PHOIBLE/ })).toBeNull();
  });

  it("exposes every coverage and generation section as keyboard-operable tabs", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<CoverageWeightingLab />);
    await user.click(screen.getByRole("tab", { name: /weight strategies/i }));
    expect(
      screen.getByRole("heading", { name: /weight strategy builder/i }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /reports/i }));
    expect(
      screen.getByRole("heading", { name: /evaluation report renderer/i }),
    ).toBeInTheDocument();
    unmount();
    render(<GenerationScoringStudio />);
    await user.click(screen.getByRole("tab", { name: /model validation/i }));
    expect(
      screen.getByText(/No provider key, model credential/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Open hosted configuration/i }),
    ).toHaveAttribute("href", "/advanced");
    expect(
      screen.getByRole("link", { name: /Open local configuration/i }),
    ).toHaveAttribute("href", "/advanced");
  });

  it("renders six selection options and honest empty comparison state", async () => {
    render(<SelectionStudio />);
    expect(
      await screen.findByRole("option", { name: "NSGA-II" }),
    ).toBeEnabled();
    expect(
      screen.getByLabelText("Algorithm").querySelectorAll("option"),
    ).toHaveLength(6);
    expect(screen.getByText(/Run a supported selector/i)).toBeInTheDocument();
  });

  it("runs every selector on identical inputs and compares all six", async () => {
    const user = userEvent.setup();
    const { selectionApi } = await import("@/lib/selection");
    render(<SelectionStudio />);
    const selector = screen.getByLabelText("Algorithm");
    const algorithms = [
      "greedy",
      "celf",
      "stochastic",
      "distribution",
      "ilp",
      "nsga2",
    ] as const;

    for (const algorithm of algorithms) {
      await user.selectOptions(selector, algorithm);
      const run = await screen.findByRole("button", { name: "Run selection" });
      await user.click(run);
      await screen.findByRole("heading", {
        level: 3,
        name: new RegExp(`^${algorithm} · 50\\.0%$`, "i"),
      });
    }

    expect(selectionApi.select).toHaveBeenCalledTimes(6);
    const requests = vi
      .mocked(selectionApi.select)
      .mock.calls.map(([request]) => request);
    expect(requests.map((request) => request.options.algorithm)).toEqual(
      algorithms,
    );
    expect(
      requests.every(
        (request) =>
          request.options.max_sentences === null &&
          request.language === requests[0]?.language &&
          request.unit === requests[0]?.unit &&
          JSON.stringify(request.candidates) ===
            JSON.stringify(requests[0]?.candidates) &&
          JSON.stringify(request.target) ===
            JSON.stringify(requests[0]?.target),
      ),
    ).toBe(true);
    expect(screen.getAllByRole("columnheader")).toHaveLength(7);
    for (const algorithm of algorithms)
      expect(
        screen.getByRole("heading", {
          level: 3,
          name: new RegExp(`^${algorithm}`, "i"),
        }),
      ).toBeInTheDocument();
  });

  it("scopes Job Center and Artifact Manager to the selected project", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<JobCenter />);
    expect(screen.getByText("Project: Demo")).toBeInTheDocument();
    expect(screen.getByText(/Typed builder/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/No matching runs exist in Demo/i),
    ).toBeInTheDocument();
    unmount();
    render(<ArtifactManager />);
    expect(screen.getByText(/corpus-text \+ prompt-set/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/No matching artifacts exist/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/UTF-8 corpus text file/i)).toHaveAttribute(
      "accept",
      "text/plain,.txt",
    );
    await user.selectOptions(
      screen.getByLabelText(/Artifact kind/i),
      "prompt-set",
    );
    expect(
      screen.getByLabelText(/Canonical prompt artifact JSON/i),
    ).toHaveAttribute("accept", "application/json,.json");
  });

  it("resolves a directly linked artifact outside the current page", async () => {
    const linked = {
      id: "123e4567-e89b-42d3-a456-426614174099",
      project_id: "123e4567-e89b-42d3-a456-426614174001",
      run_id: "123e4567-e89b-42d3-a456-426614174098",
      kind: "run-result",
      sha256: "d".repeat(64),
      size_bytes: 512,
      media_type: "application/json",
      filename: "linked-result.json",
      state: "active" as const,
      retention_until: "2026-09-11T12:00:00Z",
      created_at: "2026-08-11T12:00:00Z",
    };
    window.history.replaceState(null, "", `/?artifact=${linked.id}`);
    vi.mocked(artifactsApi.get).mockResolvedValueOnce(linked);

    render(<ArtifactManager />);

    expect(await screen.findByText(linked.filename)).toBeInTheDocument();
    expect(artifactsApi.get).toHaveBeenCalledWith(linked.project_id, linked.id);
    expect(
      screen.getByText("No page rows · linked artifact included"),
    ).toBeVisible();
    await waitFor(() =>
      expect(document.activeElement).toHaveAttribute(
        "id",
        `artifact-${linked.id}`,
      ),
    );
  });
});
