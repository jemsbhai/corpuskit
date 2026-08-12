import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) =>
    fetch(input, init),
}));

import {
  ProjectApiError,
  ProjectContractError,
  corpusExportHref,
  createManualCorpus,
  createProject,
  getCurrentPrincipal,
  importCorpus,
  listAllSentences,
  listCorpora,
  listProjects,
  listSentences,
  listVersions,
  requestProjectDeletion,
  workspaceError,
} from "./projects";

const project = {
  id: "project-1",
  name: "Demo",
  description: "Workspace",
  created_at: "2026-08-11T00:00:00Z",
};
const corpus = {
  id: "corpus-1",
  project_id: "project-1",
  name: "Seed",
  created_at: "2026-08-11T00:00:00Z",
};
const version = {
  id: "version-1",
  corpus_id: "corpus-1",
  parent_version_id: null,
  version_number: 1,
  language: "en-us",
  sentence_count: 1,
  content_sha256: "a".repeat(64),
  corpusgen_version: "0.1.7",
  created_at: "2026-08-11T00:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("project API client", () => {
  it("loads the current tenant role with a strict contract", async () => {
    const principal = {
      subject: "owner-1",
      organization_id: "00000000-0000-4000-8000-000000000001",
      role: "owner",
      display_name: "Owner",
    } as const;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(principal))
      .mockResolvedValueOnce(Response.json({ ...principal, role: "root" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentPrincipal()).resolves.toEqual(principal);
    await expect(getCurrentPrincipal()).rejects.toBeInstanceOf(
      ProjectContractError,
    );
  });

  it("parses all read contracts and encodes identifiers", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json([project]))
      .mockResolvedValueOnce(Response.json([corpus]))
      .mockResolvedValueOnce(Response.json([version]))
      .mockResolvedValueOnce(
        Response.json([
          { ordinal: 0, original_text: "  Héllo ", normalized_text: "Héllo" },
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listProjects()).resolves.toEqual([project]);
    await expect(listCorpora("project / 1")).resolves.toEqual([corpus]);
    await expect(listVersions("project / 1", "corpus / 1")).resolves.toEqual([
      version,
    ]);
    await expect(
      listSentences("project / 1", "corpus / 1", "version / 1"),
    ).resolves.toEqual([
      { ordinal: 0, original_text: "  Héllo ", normalized_text: "Héllo" },
    ]);
    expect(fetchMock.mock.calls[1]?.[0]).toContain("project%20%2F%201");
    expect(fetchMock.mock.calls[3]?.[0]).toContain("limit=500");
  });

  it("hydrates every sentence page and verifies exact contiguous lineage", async () => {
    const firstPage = Array.from({ length: 500 }, (_, ordinal) => ({
      ordinal,
      original_text: `Original ${ordinal}`,
      normalized_text: `Normalized ${ordinal}`,
    }));
    const last = {
      ordinal: 500,
      original_text: "Original 500",
      normalized_text: "Normalized 500",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(firstPage))
      .mockResolvedValueOnce(Response.json([last]));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      listAllSentences("project-1", "corpus-1", "version-1", 501),
    ).resolves.toHaveLength(501);
    expect(fetchMock.mock.calls[0]?.[0]).toContain("offset=0&limit=500");
    expect(fetchMock.mock.calls[1]?.[0]).toContain("offset=500&limit=1");
  });

  it("rejects incomplete, reordered, and impossible full-version hydration", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json([
          { ordinal: 0, original_text: "One", normalized_text: "One" },
        ]),
      )
      .mockResolvedValueOnce(
        Response.json([
          { ordinal: 1, original_text: "One", normalized_text: "One" },
          { ordinal: 0, original_text: "Two", normalized_text: "Two" },
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      listAllSentences("project-1", "corpus-1", "version-1", 2),
    ).rejects.toBeInstanceOf(ProjectContractError);
    await expect(
      listAllSentences("project-1", "corpus-1", "version-1", 2),
    ).rejects.toBeInstanceOf(ProjectContractError);
    await expect(
      listAllSentences("project-1", "corpus-1", "version-1", 0),
    ).rejects.toBeInstanceOf(ProjectContractError);
  });

  it("creates projects and manual corpora with strict JSON requests", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(project, { status: 201 }))
      .mockResolvedValueOnce(
        Response.json({ corpus, version }, { status: 201 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createProject({ name: "Demo", description: "Workspace" }),
    ).resolves.toEqual(project);
    await expect(
      createManualCorpus("project-1", {
        name: "Seed",
        language: "en-us",
        sentences: ["Hello"],
      }),
    ).resolves.toEqual({ corpus, version });
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("content-type")).toBe(
      "application/json",
    );
    expect(init.body).toBe(
      JSON.stringify({ name: "Demo", description: "Workspace" }),
    );
  });

  it("requests project deletion with DELETE and validates the lifecycle", async () => {
    const deletion = {
      project_id: "project-1",
      state: "deletion_pending",
      requested_at: "2026-08-11T00:00:00Z",
      retention_until: "2026-09-10T00:00:00Z",
    } as const;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(deletion, { status: 202 }))
      .mockResolvedValueOnce(
        Response.json({ ...deletion, state: "active" }, { status: 202 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      requestProjectDeletion("project / 1", "DELETE Demo"),
    ).resolves.toEqual(deletion);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toContain("project%20%2F%201");
    expect(init.method).toBe("DELETE");
    expect(init.body).toBe(JSON.stringify({ confirmation: "DELETE Demo" }));
    await expect(
      requestProjectDeletion("project-1", "DELETE Demo"),
    ).rejects.toBeInstanceOf(ProjectContractError);
  });

  it("sends a browser-owned multipart body without setting content-type", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(Response.json({ corpus, version }, { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["text\nHello\n"], "seed.csv", { type: "text/csv" });
    await importCorpus("project-1", {
      name: "Seed",
      language: "en-us",
      format: "csv",
      textColumn: "text",
      file,
    });
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.headers).toBeUndefined();
    const form = init.body as FormData;
    expect(form.get("format")).toBe("csv");
    expect(form.get("text_column")).toBe("text");
    expect(form.get("file")).toBe(file);
  });

  it("does not add a text column for non-CSV files", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(Response.json({ corpus, version }));
    vi.stubGlobal("fetch", fetchMock);
    await importCorpus("project-1", {
      name: "Seed",
      language: "en-us",
      format: "txt",
      textColumn: null,
      file: new File(["Hello"], "seed.txt", { type: "text/plain" }),
    });
    const form = (fetchMock.mock.calls[0]?.[1] as RequestInit).body as FormData;
    expect(form.has("text_column")).toBe(false);
  });

  it("normalizes safe API errors without leaking malformed bodies", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json(
          {
            code: "resource_conflict",
            message: "Already exists.",
            request_id: "request-1",
          },
          { status: 409 },
        ),
      )
      .mockResolvedValueOnce(new Response("private stack", { status: 503 }))
      .mockResolvedValueOnce(new Response("too large", { status: 413 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listProjects()).rejects.toMatchObject({
      name: "ProjectApiError",
      code: "resource_conflict",
      requestId: "request-1",
    });
    await expect(listProjects()).rejects.toThrow(
      "project request could not be completed",
    );
    await expect(listProjects()).rejects.toThrow("10 MiB");
  });

  it("rejects malformed success contracts", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json([{ id: 3 }]))
      .mockResolvedValueOnce(
        Response.json({
          corpus,
          version: { ...version, content_sha256: "bad" },
        }),
      )
      .mockResolvedValueOnce(Response.json([{ ...version, sentence_count: 0 }]))
      .mockResolvedValueOnce(
        Response.json([
          { ordinal: -1, original_text: "x", normalized_text: "x" },
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);
    await expect(listProjects()).rejects.toBeInstanceOf(ProjectContractError);
    await expect(
      createManualCorpus("project-1", {
        name: "x",
        language: "en-us",
        sentences: ["x"],
      }),
    ).rejects.toBeInstanceOf(ProjectContractError);
    await expect(listVersions("project-1", "corpus-1")).rejects.toBeInstanceOf(
      ProjectContractError,
    );
    await expect(
      listSentences("project-1", "corpus-1", "version-1"),
    ).rejects.toBeInstanceOf(ProjectContractError);
  });

  it("builds same-origin export links and friendly failures", () => {
    expect(
      corpusExportHref("project / 1", "corpus / 1", "version / 1", "csv"),
    ).toBe(
      "/api/v1/projects/project%20%2F%201/corpora/corpus%20%2F%201/versions/version%20%2F%201/export?format=csv",
    );
    expect(
      workspaceError(new ProjectApiError("Denied.", 403, "forbidden", "req-7")),
    ).toBe("Denied. Request req-7.");
    expect(workspaceError(new ProjectContractError())).toContain(
      "incompatible response",
    );
    expect(workspaceError(new Error("secret"))).toBe(
      "The project workspace is temporarily unavailable.",
    );
  });
});
