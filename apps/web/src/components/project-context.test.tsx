import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/projects", () => ({
  listCorpora: vi.fn(),
  listProjects: vi.fn(),
  listVersions: vi.fn(),
}));

import {
  clearSelectedProject,
  ProjectProvider,
  useProjectContext,
} from "@/components/project-context";
import { listCorpora, listProjects, listVersions } from "@/lib/projects";

const projects = [
  {
    id: "123e4567-e89b-42d3-a456-426614174001",
    name: "One",
    description: "",
    created_at: "2026-08-11T12:00:00Z",
  },
  {
    id: "123e4567-e89b-42d3-a456-426614174002",
    name: "Two",
    description: "",
    created_at: "2026-08-11T12:00:00Z",
  },
];
const corpus = {
  id: "123e4567-e89b-42d3-a456-426614174010",
  project_id: projects[0]!.id,
  name: "Seed",
  created_at: "2026-08-11T12:00:00Z",
};
const version = {
  id: "123e4567-e89b-42d3-a456-426614174011",
  corpus_id: corpus.id,
  parent_version_id: null,
  version_number: 1,
  language: "en-us",
  sentence_count: 2,
  content_sha256: "a".repeat(64),
  corpusgen_version: "0.1.7",
  created_at: "2026-08-11T12:00:00Z",
};

function Consumer() {
  const context = useProjectContext();
  if (!context) return <p>No provider</p>;
  return (
    <div>
      <p>
        {context.loading
          ? "Loading"
          : context.error
            ? "Error"
            : (context.selectedProject?.name ?? "None")}
      </p>
      <p>
        {context.corpusVersionLoading
          ? "Corpus loading"
          : context.corpusVersionError
            ? "Corpus error"
            : context.selectedCorpusVersion
              ? `${context.selectedCorpusVersion.corpus.name} v${context.selectedCorpusVersion.version.version_number}`
              : "No corpus version"}
      </p>
      <button
        onClick={() => context.selectProject(projects[1]!.id)}
        type="button"
      >
        Choose two
      </button>
      <button onClick={() => context.selectProject("unknown")} type="button">
        Clear invalid
      </button>
    </div>
  );
}

beforeEach(() => {
  vi.mocked(listCorpora).mockResolvedValue([]);
  vi.mocked(listVersions).mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  sessionStorage.clear();
});

describe("global selected-project context", () => {
  it("restores an allowed session-scoped choice and changes only to known projects", async () => {
    vi.mocked(listProjects).mockResolvedValue(projects);
    sessionStorage.setItem("corpuskit.selected-project.v1", projects[0]!.id);
    const user = userEvent.setup();
    render(
      <ProjectProvider>
        <Consumer />
      </ProjectProvider>,
    );
    expect(await screen.findByText("One")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Choose two" }));
    expect(screen.getByText("Two")).toBeInTheDocument();
    expect(sessionStorage.getItem("corpuskit.selected-project.v1")).toBe(
      projects[1]!.id,
    );
    await user.click(screen.getByRole("button", { name: "Clear invalid" }));
    expect(screen.getByText("None")).toBeInTheDocument();
    expect(sessionStorage.getItem("corpuskit.selected-project.v1")).toBeNull();
  });

  it("hydrates corpus lineage from server-returned objects and replaces stale stored IDs", async () => {
    vi.mocked(listProjects).mockResolvedValue([projects[0]!]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockResolvedValue([version]);
    sessionStorage.setItem("corpuskit.selected-project.v1", projects[0]!.id);
    sessionStorage.setItem(
      "corpuskit.selected-corpus-version.v1",
      JSON.stringify({
        project_id: projects[0]!.id,
        corpus_id: "untrusted-corpus-id",
        version_id: "untrusted-version-id",
      }),
    );

    render(
      <ProjectProvider>
        <Consumer />
      </ProjectProvider>,
    );

    expect(await screen.findByText("Seed v1")).toBeInTheDocument();
    expect(listCorpora).toHaveBeenCalledWith(projects[0]!.id);
    expect(listVersions).toHaveBeenCalledWith(projects[0]!.id, corpus.id);
    expect(
      JSON.parse(
        sessionStorage.getItem("corpuskit.selected-corpus-version.v1") ?? "{}",
      ),
    ).toEqual({
      project_id: projects[0]!.id,
      corpus_id: corpus.id,
      version_id: version.id,
    });
  });

  it("fails corpus-version hydration closed without exposing stored lineage", async () => {
    vi.mocked(listProjects).mockResolvedValue([projects[0]!]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockRejectedValue(new Error("private detail"));
    sessionStorage.setItem("corpuskit.selected-project.v1", projects[0]!.id);
    sessionStorage.setItem(
      "corpuskit.selected-corpus-version.v1",
      JSON.stringify({
        project_id: projects[0]!.id,
        corpus_id: corpus.id,
        version_id: version.id,
      }),
    );

    render(
      <ProjectProvider>
        <Consumer />
      </ProjectProvider>,
    );

    expect(await screen.findByText("Corpus error")).toBeInTheDocument();
    expect(
      sessionStorage.getItem("corpuskit.selected-corpus-version.v1"),
    ).toBeNull();
    expect(screen.queryByText("Seed v1")).not.toBeInTheDocument();
  });

  it("refreshes from the shared project event without exposing stale selections", async () => {
    vi.mocked(listProjects)
      .mockResolvedValueOnce([projects[0]!])
      .mockResolvedValueOnce(projects);
    render(
      <ProjectProvider>
        <Consumer />
      </ProjectProvider>,
    );
    expect(await screen.findByText("One")).toBeInTheDocument();
    act(() => window.dispatchEvent(new Event("corpuskit:projects-changed")));
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2));
    expect(screen.getByText("One")).toBeInTheDocument();
  });

  it("fails closed and clears storage when project discovery fails", async () => {
    vi.mocked(listProjects).mockRejectedValue(new Error("tenant secret"));
    sessionStorage.setItem("corpuskit.selected-project.v1", projects[0]!.id);
    render(
      <ProjectProvider>
        <Consumer />
      </ProjectProvider>,
    );
    expect(await screen.findByText("Error")).toBeInTheDocument();
    expect(sessionStorage.getItem("corpuskit.selected-project.v1")).toBeNull();
    clearSelectedProject();
  });

  it("represents a successful empty project list without manufacturing a selection", async () => {
    vi.mocked(listProjects).mockResolvedValue([]);
    sessionStorage.setItem(
      "corpuskit.selected-corpus-version.v1",
      JSON.stringify({
        project_id: projects[0]!.id,
        corpus_id: corpus.id,
        version_id: version.id,
      }),
    );
    render(
      <ProjectProvider>
        <Consumer />
      </ProjectProvider>,
    );
    expect(await screen.findByText("None")).toBeInTheDocument();
    expect(sessionStorage.getItem("corpuskit.selected-project.v1")).toBeNull();
    expect(
      sessionStorage.getItem("corpuskit.selected-corpus-version.v1"),
    ).toBeNull();
  });

  it("clears safely when browser session storage is unavailable", () => {
    vi.stubGlobal("sessionStorage", undefined);
    expect(() => clearSelectedProject()).not.toThrow();
    vi.unstubAllGlobals();
  });

  it("returns null without a provider", () => {
    render(<Consumer />);
    expect(screen.getByText("No provider")).toBeInTheDocument();
  });
});
