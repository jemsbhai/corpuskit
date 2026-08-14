import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectWorkbench } from "./project-workbench";
import { ProjectProvider } from "./project-context";
import {
  createManualCorpus,
  createManualVersion,
  createProject,
  getCurrentPrincipal,
  importCorpus,
  importCorpusVersion,
  listCorpora,
  listProjects,
  listSentences,
  listVersions,
  requestProjectDeletion,
} from "@/lib/projects";

vi.mock("@/lib/projects", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/projects")>();
  return {
    ...original,
    createManualCorpus: vi.fn(),
    createManualVersion: vi.fn(),
    createProject: vi.fn(),
    getCurrentPrincipal: vi.fn(),
    importCorpus: vi.fn(),
    importCorpusVersion: vi.fn(),
    listCorpora: vi.fn(),
    listProjects: vi.fn(),
    listSentences: vi.fn(),
    listVersions: vi.fn(),
    requestProjectDeletion: vi.fn(),
  };
});

const project = {
  id: "project-1",
  name: "Demo project",
  description: "Speech research",
  created_at: "2026-08-11T00:00:00Z",
};
const corpus = {
  id: "corpus-1",
  project_id: project.id,
  name: "Unicode seed",
  created_at: "2026-08-11T00:00:00Z",
};
const version = {
  id: "version-1",
  corpus_id: corpus.id,
  parent_version_id: null,
  version_number: 1,
  language: "en-us",
  sentence_count: 1,
  content_sha256: "a".repeat(64),
  corpusgen_version: "0.1.7",
  created_at: "2026-08-11T00:00:00Z",
};
const versionTwo = {
  ...version,
  id: "version-2",
  parent_version_id: version.id,
  version_number: 2,
  content_sha256: "b".repeat(64),
};

function renderWorkbench() {
  return render(
    <ProjectProvider>
      <ProjectWorkbench />
    </ProjectProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listProjects).mockResolvedValue([]);
  vi.mocked(listCorpora).mockResolvedValue([]);
  vi.mocked(listVersions).mockResolvedValue([]);
  vi.mocked(listSentences).mockResolvedValue([]);
  vi.mocked(getCurrentPrincipal).mockResolvedValue({
    subject: "owner-1",
    organization_id: "00000000-0000-4000-8000-000000000001",
    role: "owner",
    display_name: "Owner",
  });
});

describe("ProjectWorkbench", () => {
  it("requires exact confirmation and removes a deleted project", async () => {
    const user = userEvent.setup();
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.mocked(listProjects)
      .mockResolvedValueOnce([project])
      .mockResolvedValue([]);
    vi.mocked(requestProjectDeletion).mockResolvedValue({
      project_id: project.id,
      state: "deletion_pending",
      requested_at: "2026-08-11T00:00:00Z",
      retention_until: "2026-09-10T00:00:00Z",
    });
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    const confirmation = screen.getByLabelText("Deletion confirmation");
    const deleteButton = screen.getByRole("button", {
      name: "Delete project",
    });
    expect(deleteButton).toBeDisabled();
    await user.type(confirmation, "DELETE demo project");
    expect(deleteButton).toBeDisabled();
    await user.clear(confirmation);
    await user.type(confirmation, `DELETE ${project.name}`);
    expect(deleteButton).toBeEnabled();
    await user.click(deleteButton);

    await waitFor(() =>
      expect(requestProjectDeletion).toHaveBeenCalledWith(
        project.id,
        `DELETE ${project.name}`,
      ),
    );
    expect(
      await screen.findByText("No project has been created for this tenant."),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      `${project.name} is no longer available`,
    );
    expect(dispatch).toHaveBeenCalledWith(
      expect.objectContaining({ type: "corpuskit:projects-changed" }),
    );
  });

  it("does not present project deletion to editor or viewer roles", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(getCurrentPrincipal).mockResolvedValue({
      subject: "viewer-1",
      organization_id: "00000000-0000-4000-8000-000000000001",
      role: "viewer",
      display_name: null,
    });
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    expect(
      screen.queryByRole("button", { name: "Delete project" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Deletion confirmation")).toBeNull();
  });

  it("announces an empty tenant and creates a project", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects)
      .mockResolvedValueOnce([])
      .mockResolvedValue([project]);
    vi.mocked(createProject).mockResolvedValue(project);
    renderWorkbench();

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("No projects yet"),
    );
    expect(
      screen.getByText("No project has been created for this tenant."),
    ).toBeVisible();
    await user.type(screen.getByLabelText("Project name"), project.name);
    await user.type(screen.getByLabelText(/Description/), project.description);
    await user.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() =>
      expect(createProject).toHaveBeenCalledWith({
        name: project.name,
        description: project.description,
      }),
    );
    expect(
      await screen.findByRole("button", {
        name: /Demo project Speech research/,
      }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(listCorpora).toHaveBeenCalledWith(project.id);
  });

  it("creates a manual corpus then exposes digest, sentences, and all exports", async () => {
    const user = userEvent.setup();
    let corpusCreated = false;
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(createManualCorpus).mockImplementation(async () => {
      corpusCreated = true;
      return { corpus, version };
    });
    vi.mocked(listCorpora).mockImplementation(async () =>
      corpusCreated ? [corpus] : [],
    );
    vi.mocked(listVersions).mockResolvedValue([version]);
    vi.mocked(listSentences).mockResolvedValue([
      { ordinal: 0, original_text: "  Héllo  ", normalized_text: "Héllo" },
    ]);
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.type(screen.getByLabelText("Corpus name"), corpus.name);
    await user.clear(screen.getByLabelText("eSpeak language"));
    await user.type(screen.getByLabelText("eSpeak language"), "en-us");
    await user.type(screen.getByLabelText(/Sentences/), "  Héllo  \n\nHéllo");
    await user.click(screen.getByRole("button", { name: "Create corpus" }));

    await waitFor(() =>
      expect(createManualCorpus).toHaveBeenCalledWith(project.id, {
        name: corpus.name,
        language: "en-us",
        sentences: ["  Héllo  ", "", "Héllo"],
      }),
    );
    expect(await screen.findByText(version.content_sha256)).toBeVisible();
    const table = screen.getByRole("table", {
      name: "Normalized sentences in deterministic order",
    });
    expect(within(table).getAllByText(/Héllo/u)).toHaveLength(2);
    for (const format of ["TXT", "JSON", "CSV"]) {
      const link = screen.getByRole("link", { name: format });
      expect(link).toHaveAttribute(
        "download",
        `corpus-v1.${format.toLowerCase()}`,
      );
      expect(link).toHaveAttribute(
        "href",
        expect.stringContaining(`format=${format.toLowerCase()}`),
      );
    }
  });

  it("submits an explicit CSV column and selected UTF-8 file", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(importCorpus).mockResolvedValue({ corpus, version });
    vi.mocked(listCorpora)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([corpus]);
    vi.mocked(listVersions).mockResolvedValue([version]);
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(screen.getByRole("radio", { name: "File import" }));
    await user.type(screen.getByLabelText("Corpus name"), corpus.name);
    await user.selectOptions(screen.getByLabelText("File format"), "csv");
    await user.clear(screen.getByLabelText("CSV text column"));
    await user.type(screen.getByLabelText("CSV text column"), "utterance");
    const file = new File(["utterance\nHéllo\n"], "seed.csv", {
      type: "text/csv",
    });
    const csvInput = screen.getByLabelText(
      "UTF-8 CSV file",
    ) as HTMLInputElement;
    await user.upload(csvInput, file);
    expect(csvInput.files?.[0]).toBe(file);
    fireEvent.submit(
      screen.getByRole("button", { name: "Create corpus" }).closest("form")!,
    );

    await waitFor(() =>
      expect(importCorpus).toHaveBeenCalledWith(project.id, {
        name: corpus.name,
        language: "en-us",
        format: "csv",
        textColumn: "utterance",
        file,
      }),
    );
  });

  it("shows safe server and client validation errors", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockRejectedValue(
      new Error("private stack and token"),
    );
    const { unmount } = renderWorkbench();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "temporarily unavailable",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("private stack");
    unmount();

    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([]);
    renderWorkbench();
    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(screen.getByRole("radio", { name: "File import" }));
    await user.type(screen.getByLabelText("Corpus name"), "Too large");
    const large = new File(
      [new Uint8Array(10 * 1024 * 1024 + 1)],
      "large.txt",
      {
        type: "text/plain",
      },
    );
    const textInput = screen.getByLabelText(
      "UTF-8 TXT file",
    ) as HTMLInputElement;
    await user.upload(textInput, large);
    expect(textInput.files?.[0]).toBe(large);
    fireEvent.submit(
      screen.getByRole("button", { name: "Create corpus" }).closest("form")!,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no larger than 10 MiB",
    );
    expect(importCorpus).not.toHaveBeenCalled();
  });

  it("browses multiple immutable versions and sanitizes read failures", async () => {
    const user = userEvent.setup();
    let rejectVersionTwo = false;
    const latestVersion = { ...versionTwo, language: "fr-fr" };
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockResolvedValue([version, latestVersion]);
    vi.mocked(listSentences).mockImplementation(
      async (_projectId, _corpusId, versionId) => {
        if (versionId === versionTwo.id) {
          if (rejectVersionTwo) throw new Error("private database detail");
          return [
            {
              ordinal: 0,
              original_text: "Second",
              normalized_text: "Second",
            },
          ];
        }
        return [
          {
            ordinal: 0,
            original_text: "First",
            normalized_text: "First",
          },
        ];
      },
    );
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(screen.getByRole("button", { name: /Unicode seed/ }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "2 immutable versions",
    );
    expect(screen.getByText(versionTwo.content_sha256)).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Version 1/ }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Version 1 loaded",
    );
    expect(screen.getByLabelText("Version eSpeak language")).toHaveValue(
      "fr-fr",
    );
    expect(screen.getAllByText("First")).toHaveLength(2);
    rejectVersionTwo = true;
    await user.click(screen.getByRole("button", { name: /Version 2/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "temporarily unavailable",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("database");
  });

  it("creates a manual successor then refreshes and selects it", async () => {
    const user = userEvent.setup();
    let created = false;
    const createdVersion = {
      ...versionTwo,
      language: "fr-fr",
      sentence_count: 2,
    };
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockImplementation(async () =>
      created ? [version, createdVersion] : [version],
    );
    vi.mocked(listSentences).mockImplementation(
      async (_projectId, _corpusId, versionId) =>
        versionId === createdVersion.id
          ? [
              {
                ordinal: 0,
                original_text: " Première ",
                normalized_text: "Première",
              },
              {
                ordinal: 1,
                original_text: "Deuxième",
                normalized_text: "Deuxième",
              },
            ]
          : [
              {
                ordinal: 0,
                original_text: "First",
                normalized_text: "First",
              },
            ],
    );
    vi.mocked(createManualVersion).mockImplementation(async () => {
      created = true;
      return createdVersion;
    });
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: /Unicode seed/ }),
    );
    const language = await screen.findByLabelText("Version eSpeak language");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Create version" }),
      ).toBeEnabled(),
    );
    await user.clear(language);
    await user.type(language, "fr-fr");
    fireEvent.change(screen.getByLabelText(/Version sentences/), {
      target: { value: " Première \n\nDeuxième" },
    });
    await user.click(screen.getByRole("button", { name: "Create version" }));

    await waitFor(() =>
      expect(createManualVersion).toHaveBeenCalledWith(project.id, corpus.id, {
        language: "fr-fr",
        sentences: [" Première ", "", "Deuxième"],
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "version 2 created and selected",
    );
    const createdButton = screen.getByRole("button", { name: /Version 2/ });
    expect(createdButton).toHaveAttribute("aria-pressed", "true");
    expect(createdButton).toHaveFocus();
    expect(screen.getByText(createdVersion.content_sha256)).toBeVisible();
    expect(screen.getByLabelText(/Version sentences/)).toHaveValue("");
    expect(screen.getAllByText("Deuxième")).toHaveLength(2);
  });

  it("reports a committed version when its follow-up refresh fails", async () => {
    const user = userEvent.setup();
    let created = false;
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockImplementation(async () => {
      if (created) throw new Error("replica unavailable");
      return [version];
    });
    vi.mocked(listSentences).mockResolvedValue([
      { ordinal: 0, original_text: "First", normalized_text: "First" },
    ]);
    vi.mocked(createManualVersion).mockImplementation(async () => {
      created = true;
      return versionTwo;
    });
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: /Unicode seed/ }),
    );
    await user.type(
      await screen.findByLabelText(/Version sentences/),
      "Second",
    );
    await user.click(screen.getByRole("button", { name: "Create version" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "version 2 was created, but its sentences could not be refreshed",
    );
    const recovered = screen.getByRole("button", { name: /Version 2/ });
    expect(recovered).toHaveAttribute("aria-pressed", "true");
    expect(recovered).toHaveFocus();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Version sentences/)).toHaveValue("");
  });

  it("does not apply an append response after the user selects another corpus", async () => {
    const user = userEvent.setup();
    const otherCorpus = {
      ...corpus,
      id: "corpus-2",
      name: "Other corpus",
    };
    const otherVersion = {
      ...version,
      id: "other-version-1",
      corpus_id: otherCorpus.id,
      content_sha256: "c".repeat(64),
    };
    let appendResolved = false;
    let staleRefreshAttempted = false;
    let resolveAppend!: (value: typeof versionTwo) => void;
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([corpus, otherCorpus]);
    vi.mocked(listVersions).mockImplementation(async (_projectId, corpusId) => {
      if (appendResolved && corpusId === corpus.id)
        staleRefreshAttempted = true;
      return corpusId === otherCorpus.id ? [otherVersion] : [version];
    });
    vi.mocked(listSentences).mockImplementation(
      async (_projectId, corpusId) => [
        {
          ordinal: 0,
          original_text: corpusId === otherCorpus.id ? "Other" : "First",
          normalized_text: corpusId === otherCorpus.id ? "Other" : "First",
        },
      ],
    );
    vi.mocked(createManualVersion).mockReturnValue(
      new Promise((resolve) => {
        resolveAppend = resolve;
      }),
    );
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: /Unicode seed/ }),
    );
    await user.type(
      await screen.findByLabelText(/Version sentences/),
      "Second",
    );
    await user.click(screen.getByRole("button", { name: "Create version" }));
    await waitFor(() => expect(createManualVersion).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /Other corpus/ }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Other corpus/ }),
      ).toHaveAttribute("aria-pressed", "true"),
    );
    expect(await screen.findByText(otherVersion.content_sha256)).toBeVisible();

    appendResolved = true;
    resolveAppend(versionTwo);
    await Promise.resolve();
    await Promise.resolve();

    expect(staleRefreshAttempted).toBe(false);
    expect(screen.getByText(otherVersion.content_sha256)).toBeVisible();
    expect(
      screen.queryByText(versionTwo.content_sha256),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Other corpus/ }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("lets an editor import a version file and clears it after selection", async () => {
    const user = userEvent.setup();
    let created = false;
    vi.mocked(getCurrentPrincipal).mockResolvedValue({
      subject: "editor-1",
      organization_id: "00000000-0000-4000-8000-000000000001",
      role: "editor",
      display_name: "Editor",
    });
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockImplementation(async () =>
      created ? [version, versionTwo] : [version],
    );
    vi.mocked(listSentences).mockResolvedValue([]);
    vi.mocked(importCorpusVersion).mockImplementation(async () => {
      created = true;
      return versionTwo;
    });
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: /Unicode seed/ }),
    );
    expect(
      await screen.findByRole("form", {
        name: "Create the next immutable version",
      }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Delete project" }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Create version" }),
      ).toBeEnabled(),
    );
    await user.click(
      screen.getByRole("radio", { name: "Version file import" }),
    );
    await user.selectOptions(
      screen.getByLabelText("Version file format"),
      "csv",
    );
    await user.clear(screen.getByLabelText("Version CSV text column"));
    await user.type(
      screen.getByLabelText("Version CSV text column"),
      "utterance",
    );
    const file = new File(["utterance\nSecond\n"], "version.csv", {
      type: "text/csv",
    });
    const input = screen.getByLabelText(
      "UTF-8 CSV version file",
    ) as HTMLInputElement;
    await user.upload(input, file);
    expect(input.files?.[0]).toBe(file);
    fireEvent.submit(input.closest("form")!);

    await waitFor(() =>
      expect(importCorpusVersion).toHaveBeenCalledWith(project.id, corpus.id, {
        language: "en-us",
        format: "csv",
        textColumn: "utterance",
        file,
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "version 2 created and selected",
    );
    expect(input.value).toBe("");
    expect(input.files).toHaveLength(0);
  });

  it("keeps version creation read-only for viewers", async () => {
    const user = userEvent.setup();
    vi.mocked(getCurrentPrincipal).mockResolvedValue({
      subject: "viewer-1",
      organization_id: "00000000-0000-4000-8000-000000000001",
      role: "viewer",
      display_name: null,
    });
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockResolvedValue([version]);
    renderWorkbench();

    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: /Unicode seed/ }),
    );
    expect(
      await screen.findByText(
        /Viewers can inspect and export immutable versions/,
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create project" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Create corpus" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Create version" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Version eSpeak language")).toBeNull();
  });

  it("handles plural project loading, missing versions, and form failures", async () => {
    const user = userEvent.setup();
    const emptyProject = {
      ...project,
      id: "project-2",
      name: "Empty project",
      description: "",
    };
    vi.mocked(listProjects).mockResolvedValue([project, emptyProject]);
    vi.mocked(listCorpora).mockResolvedValue([corpus]);
    vi.mocked(listVersions).mockResolvedValue([]);
    vi.mocked(createProject).mockRejectedValue(
      new Error("secret conflict detail"),
    );
    renderWorkbench();

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Demo project opened",
      ),
    );
    expect(screen.getByText("No description")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Demo project/ }));
    await user.click(screen.getByRole("button", { name: /Unicode seed/ }));
    expect(
      await screen.findByText("No version is available for this corpus."),
    ).toBeVisible();

    await user.type(screen.getByLabelText("Project name"), "Duplicate");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "temporarily unavailable",
    );

    vi.mocked(createManualCorpus).mockRejectedValue(
      new Error("server internals"),
    );
    await user.type(screen.getByLabelText("Corpus name"), "Rejected corpus");
    await user.type(screen.getByLabelText(/Sentences/), "Hello");
    await user.click(screen.getByRole("button", { name: "Create corpus" }));
    expect(await screen.findByRole("alert")).not.toHaveTextContent("internals");
  });

  it("locks project creation while a request is in flight", async () => {
    const user = userEvent.setup();
    let resolveProject!: (value: typeof project) => void;
    vi.mocked(createProject).mockReturnValue(
      new Promise((resolve) => {
        resolveProject = resolve;
      }),
    );
    renderWorkbench();
    await screen.findByText("No project has been created for this tenant.");
    await user.type(screen.getByLabelText("Project name"), project.name);
    const form = screen.getByLabelText("Project name").closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);

    expect(createProject).toHaveBeenCalledTimes(1);
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getByRole("button", { name: "Creating project…" }),
    ).toBeDisabled();
    resolveProject(project);
    await waitFor(() => expect(form).toHaveAttribute("aria-busy", "false"));
  });

  it("locks corpus import and clears the native file control after success", async () => {
    const user = userEvent.setup();
    let resolveImport!: (value: {
      corpus: typeof corpus;
      version: typeof version;
    }) => void;
    vi.mocked(listProjects).mockResolvedValue([project]);
    vi.mocked(listCorpora)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([corpus]);
    vi.mocked(listVersions).mockResolvedValue([]);
    vi.mocked(importCorpus).mockReturnValue(
      new Promise((resolve) => {
        resolveImport = resolve;
      }),
    );
    renderWorkbench();
    await user.click(
      await screen.findByRole("button", { name: /Demo project/ }),
    );
    await user.click(screen.getByRole("radio", { name: "File import" }));
    await user.type(screen.getByLabelText("Corpus name"), corpus.name);
    const file = new File(["Hello"], "seed.txt", { type: "text/plain" });
    const input = screen.getByLabelText("UTF-8 TXT file") as HTMLInputElement;
    await user.upload(input, file);
    const form = input.closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);

    expect(importCorpus).toHaveBeenCalledTimes(1);
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getByRole("button", { name: "Creating corpus…" }),
    ).toBeDisabled();
    resolveImport({ corpus, version });
    await waitFor(() => expect(form).toHaveAttribute("aria-busy", "false"));
    expect(input.value).toBe("");
    expect(input.files).toHaveLength(0);
  });
});
