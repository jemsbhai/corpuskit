import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const projectContextState = vi.hoisted(() => ({
  selectedCorpusVersion: null as null | {
    corpus: Record<string, unknown>;
    version: Record<string, unknown>;
  },
  corpusVersionLoading: false,
  corpusVersionError: false,
}));

const project = {
  id: "123e4567-e89b-42d3-a456-426614174001",
  name: "Replay project",
  description: "",
  created_at: "2026-08-11T12:00:00Z",
};
const corpus = {
  id: "123e4567-e89b-42d3-a456-426614174002",
  project_id: project.id,
  name: "Trusted corpus",
  created_at: "2026-08-11T12:00:00Z",
};
const version = {
  id: "123e4567-e89b-42d3-a456-426614174003",
  corpus_id: corpus.id,
  parent_version_id: null,
  version_number: 3,
  language: "fr-fr",
  sentence_count: 2,
  content_sha256: "c".repeat(64),
  corpusgen_version: "0.1.7",
  created_at: "2026-08-11T12:00:00Z",
};

vi.mock("@/components/project-context", () => ({
  useProjectContext: () => ({
    projects: [project],
    selectedProject: project,
    loading: false,
    error: false,
    selectProject: vi.fn(),
    refreshProjects: vi.fn(),
    selectedCorpusVersion: projectContextState.selectedCorpusVersion,
    corpusVersionLoading: projectContextState.corpusVersionLoading,
    corpusVersionError: projectContextState.corpusVersionError,
    selectCorpusVersion: vi.fn(),
  }),
}));
vi.mock("@/lib/jobs", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/jobs")>("@/lib/jobs");
  return {
    ...actual,
    jobsApi: {
      ...actual.jobsApi,
      list: vi.fn(),
      get: vi.fn(),
      events: vi.fn(),
      cancel: vi.fn(),
      retry: vi.fn(),
      submit: vi.fn(),
    },
  };
});
vi.mock("@/lib/projects", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/projects")>("@/lib/projects");
  return {
    ...actual,
    getCurrentPrincipal: vi.fn(),
    listAllSentences: vi.fn(),
  };
});
vi.mock("@/lib/replays", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/replays")>("@/lib/replays");
  return {
    ...actual,
    replaysApi: { submit: vi.fn(), get: vi.fn() },
  };
});

import { JobCenter } from "@/components/job-center";
import { ApiRequestError } from "@/lib/api-client";
import { jobsApi, type RunRecord } from "@/lib/jobs";
import { getCurrentPrincipal, listAllSentences } from "@/lib/projects";
import { replaysApi, type ReplayStatus } from "@/lib/replays";

const sourceRun: RunRecord = {
  id: "123e4567-e89b-42d3-a456-426614174010",
  project_id: project.id,
  corpus_version_id: null,
  parent_run_id: null,
  kind: "evaluate",
  state: "succeeded",
  attempt: 1,
  spec_sha256: "a".repeat(64),
  outbox_state: "published",
  cancellation_requested_at: null,
  created_at: "2026-08-11T12:00:00Z",
  result_summary: null,
  failure_code: null,
};
const queuedReplay: ReplayStatus = {
  replay_run_id: "123e4567-e89b-42d3-a456-426614174020",
  source_run_id: sourceRun.id,
  source_manifest_artifact_id: "123e4567-e89b-42d3-a456-426614174030",
  expected_manifest_sha256: "b".repeat(64),
  observed_manifest_artifact_id: null,
  classification: "exact",
  lifecycle: "queued",
  comparison: null,
};
const comparedReplay: ReplayStatus = {
  ...queuedReplay,
  observed_manifest_artifact_id: "123e4567-e89b-42d3-a456-426614174031",
  lifecycle: "compared",
  comparison: {
    classification: "exact",
    verdict: "exact-match",
    replay_inputs_match: true,
    outputs_match: true,
    differences: [],
  },
};

const jobs = vi.mocked(jobsApi);
const principal = vi.mocked(getCurrentPrincipal);
const allSentences = vi.mocked(listAllSentences);
const replays = vi.mocked(replaysApi);

beforeEach(() => {
  vi.clearAllMocks();
  projectContextState.selectedCorpusVersion = null;
  projectContextState.corpusVersionLoading = false;
  projectContextState.corpusVersionError = false;
  jobs.list.mockResolvedValue([sourceRun]);
  jobs.get.mockResolvedValue(sourceRun);
  jobs.events.mockResolvedValue([]);
  principal.mockResolvedValue({
    subject: "user|editor",
    organization_id: "123e4567-e89b-42d3-a456-426614174000",
    role: "editor",
    display_name: "Replay editor",
  });
  replays.submit.mockResolvedValue({ replay: queuedReplay, created: true });
  replays.get.mockResolvedValue(comparedReplay);
  allSentences.mockResolvedValue([]);
});

describe("Job Center exact corpus lineage", () => {
  it.each([
    ["phonemize", "texts"],
    ["evaluate", "sentences"],
    ["select", "candidates"],
  ] as const)(
    "submits %s with the fully verified selected version",
    async (kind, sourceField) => {
      projectContextState.selectedCorpusVersion = { corpus, version };
      allSentences.mockResolvedValue([
        { ordinal: 0, original_text: "  Été ", normalized_text: "Été" },
        { ordinal: 1, original_text: "Déjà", normalized_text: "Déjà" },
      ]);
      jobs.submit.mockResolvedValue({ ...sourceRun, kind });
      const user = userEvent.setup();
      render(<JobCenter />);

      if (kind !== "phonemize") {
        await user.selectOptions(
          screen.getByLabelText("Supported run kind"),
          kind,
        );
      }
      expect(
        await screen.findByText(/Verified 2 ordered sentences/u),
      ).toBeInTheDocument();
      expect(screen.getByLabelText("Language")).toHaveValue("fr-fr");
      await user.click(
        screen.getByRole("button", { name: "Submit durable run" }),
      );

      await waitFor(() => expect(jobs.submit).toHaveBeenCalledTimes(1));
      const [request] = jobs.submit.mock.calls[0] ?? [];
      expect(request).toMatchObject({
        project_id: project.id,
        corpus_version_id: version.id,
        kind,
      });
      expect(request?.spec[sourceField]).toEqual(["Été", "Déjà"]);
      expect(request?.spec.language).toBe("fr-fr");
      expect(allSentences).toHaveBeenCalledWith(
        project.id,
        corpus.id,
        version.id,
        version.sentence_count,
        expect.any(AbortSignal),
      );
    },
  );

  it("keeps unlinked inline submission explicit when a version is available", async () => {
    projectContextState.selectedCorpusVersion = { corpus, version };
    allSentences.mockResolvedValue([
      { ordinal: 0, original_text: "Été", normalized_text: "Été" },
      { ordinal: 1, original_text: "Déjà", normalized_text: "Déjà" },
    ]);
    jobs.submit.mockResolvedValue(sourceRun);
    const user = userEvent.setup();
    render(<JobCenter />);

    expect(
      await screen.findByText(/Verified 2 ordered sentences/u),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("radio", {
        name: "Explicit inline input (no lineage)",
      }),
    );
    const input = screen.getByLabelText("Text rows to phonemize");
    await user.clear(input);
    await user.type(input, "Inline one\nInline two");
    await user.click(
      screen.getByRole("button", { name: "Submit durable run" }),
    );

    await waitFor(() => expect(jobs.submit).toHaveBeenCalledTimes(1));
    expect(jobs.submit.mock.calls[0]?.[0]).toMatchObject({
      corpus_version_id: null,
      spec: { texts: ["Inline one", "Inline two"], language: "en-us" },
    });
  });

  it.each([
    ["phonemize", 500],
    ["evaluate", 500],
    ["select", 2_000],
  ] as const)(
    "accepts an exact %s atomic version boundary of %i rows",
    async (kind, limit) => {
      projectContextState.selectedCorpusVersion = {
        corpus,
        version: { ...version, sentence_count: limit },
      };
      allSentences.mockResolvedValue(
        Array.from({ length: limit }, (_, ordinal) => ({
          ordinal,
          original_text: `Original ${ordinal}`,
          normalized_text: `Normalized ${ordinal}`,
        })),
      );
      const user = userEvent.setup();
      render(<JobCenter />);

      if (kind !== "phonemize") {
        await user.selectOptions(
          screen.getByLabelText("Supported run kind"),
          kind,
        );
      }

      expect(
        await screen.findByText(
          new RegExp(
            `Verified ${limit.toLocaleString()} ordered sentences`,
            "u",
          ),
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Submit durable run" }),
      ).toBeEnabled();
      expect(allSentences).toHaveBeenLastCalledWith(
        project.id,
        corpus.id,
        version.id,
        limit,
        expect.any(AbortSignal),
      );
    },
  );

  it.each([
    ["phonemize", 500],
    ["evaluate", 500],
    ["select", 2_000],
  ] as const)(
    "rejects a %s version at the %i-row atomic limit plus one before pagination",
    async (kind, limit) => {
      projectContextState.selectedCorpusVersion = {
        corpus,
        version: { ...version, sentence_count: limit + 1 },
      };
      const user = userEvent.setup();
      render(<JobCenter />);

      if (kind !== "phonemize") {
        await user.selectOptions(
          screen.getByLabelText("Supported run kind"),
          kind,
        );
      }

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        `one ${kind === "select" ? "Select" : kind === "evaluate" ? "Evaluate" : "Phonemize"} run accepts at most ${limit.toLocaleString()}`,
      );
      expect(alert).toHaveTextContent("will not truncate");
      expect(alert).toHaveTextContent("future chunked job");
      expect(allSentences).not.toHaveBeenCalled();
      expect(
        screen.getByRole("button", { name: "Submit durable run" }),
      ).toBeDisabled();
    },
  );
});

describe("Job Center durable replay", () => {
  it("renders sanitized durable progress while a run is active", async () => {
    const running = {
      ...sourceRun,
      kind: "train-phon-rl",
      state: "running" as const,
    };
    jobs.list.mockResolvedValueOnce([running]);
    jobs.get.mockResolvedValue(running);
    jobs.events.mockResolvedValue([
      {
        sequence: 4,
        event_type: "run.progress",
        payload: {
          schema_version: 1,
          activity_attempt: 1,
          sequence: 0,
          phase: "training",
          completed: 25,
          total: 100,
          coverage: null,
          accepted_count: null,
        },
        occurred_at: "2026-08-11T12:00:04Z",
      },
    ]);

    render(<JobCenter />);

    expect(await screen.findByText("training")).toBeInTheDocument();
    expect(
      screen.getByText(/Attempt 1.*25 \/ 100 \(25%\)/u),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/prompt|secret|source id/iu),
    ).not.toBeInTheDocument();
  });

  it("submits a succeeded run once and displays the completed source comparison", async () => {
    const user = userEvent.setup();
    render(<JobCenter />);

    const replayButton = await screen.findByRole("button", {
      name: "Replay this terminal run",
    });
    await waitFor(() => expect(replayButton).toBeEnabled());
    await user.click(replayButton);

    await waitFor(() => expect(replays.submit).toHaveBeenCalledTimes(1));
    const [projectId, sourceId, key] = replays.submit.mock.calls[0] ?? [];
    expect(projectId).toBe(project.id);
    expect(sourceId).toBe(sourceRun.id);
    expect(key).toMatch(
      new RegExp(`^ui-replay-${sourceRun.id}-[0-9a-f-]{36}$`, "u"),
    );
    await expect(
      screen.findByRole("heading", { name: "Exact Match" }),
    ).resolves.toBeInTheDocument();
    expect(screen.getByText("Replay-critical inputs")).toBeInTheDocument();
    expect(screen.getAllByText("Match")).toHaveLength(2);
    expect(
      screen.getByText("No replay-critical input fields differ."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Inspect source manifest" }),
    ).toHaveAttribute(
      "href",
      `/artifacts?artifact=${queuedReplay.source_manifest_artifact_id}`,
    );
    expect(
      screen.getByRole("link", { name: "Inspect observed manifest" }),
    ).toHaveAttribute(
      "href",
      `/artifacts?artifact=${comparedReplay.observed_manifest_artifact_id}`,
    );
    expect(replays.get).toHaveBeenCalledWith(
      queuedReplay.replay_run_id,
      expect.any(AbortSignal),
    );
  });

  it("reuses the same idempotency key after an uncertain failed response", async () => {
    const user = userEvent.setup();
    replays.submit
      .mockRejectedValueOnce(
        new ApiRequestError(503, "request_failed", "request-1"),
      )
      .mockResolvedValueOnce({ replay: comparedReplay, created: false });
    render(<JobCenter />);

    const replayButton = await screen.findByRole("button", {
      name: "Replay this terminal run",
    });
    await waitFor(() => expect(replayButton).toBeEnabled());
    await user.click(replayButton);
    expect(
      await screen.findByText(/service is temporarily unavailable/i),
    ).toBeInTheDocument();
    await user.click(replayButton);
    await screen.findByText(/existing idempotent replay was restored/i);

    expect(replays.submit).toHaveBeenCalledTimes(2);
    expect(replays.submit.mock.calls[0]?.[2]).toBe(
      replays.submit.mock.calls[1]?.[2],
    );
  });

  it("fails closed for viewers and terminal runs without a finalized manifest", async () => {
    principal.mockResolvedValueOnce({
      subject: "user|viewer",
      organization_id: "123e4567-e89b-42d3-a456-426614174000",
      role: "viewer",
      display_name: "Replay viewer",
    });
    const { unmount } = render(<JobCenter />);
    const viewerButton = await screen.findByRole("button", {
      name: "Replay this terminal run",
    });
    await waitFor(() => expect(principal).toHaveBeenCalled());
    expect(viewerButton).toBeDisabled();
    expect(
      screen.getByText(/requires an owner, admin, or editor role/i),
    ).toBeInTheDocument();
    unmount();

    jobs.list.mockResolvedValueOnce([{ ...sourceRun, state: "failed" }]);
    jobs.get.mockResolvedValueOnce({ ...sourceRun, state: "failed" });
    render(<JobCenter />);
    const failedButton = await screen.findByRole("button", {
      name: "Replay this terminal run",
    });
    expect(failedButton).toBeDisabled();
    expect(
      screen.getByText(
        /Only a succeeded terminal run has a finalized source manifest/i,
      ),
    ).toBeInTheDocument();
    expect(replays.submit).not.toHaveBeenCalled();
  });
});
