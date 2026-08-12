import { authenticatedFetch } from "@/lib/browser-auth";

export const MAX_CORPUS_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_CORPUS_SENTENCES = 10_000;
export const MAX_SENTENCE_CHARACTERS = 2_000;

export type CorpusFileFormat = "txt" | "csv" | "json";
export type CorpusExportFormat = CorpusFileFormat;
export type ProjectRole = "owner" | "admin" | "editor" | "viewer";

export interface ProjectPrincipal {
  readonly subject: string;
  readonly organization_id: string;
  readonly role: ProjectRole;
  readonly display_name: string | null;
}

export interface Project {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly created_at: string;
}

export interface ProjectDeletion {
  readonly project_id: string;
  readonly state: "deletion_pending";
  readonly requested_at: string;
  readonly retention_until: string;
}

export interface Corpus {
  readonly id: string;
  readonly project_id: string;
  readonly name: string;
  readonly created_at: string;
}

export interface CorpusVersion {
  readonly id: string;
  readonly corpus_id: string;
  readonly parent_version_id: string | null;
  readonly version_number: number;
  readonly language: string;
  readonly sentence_count: number;
  readonly content_sha256: string;
  readonly corpusgen_version: string;
  readonly created_at: string;
}

export interface CorpusSentence {
  readonly ordinal: number;
  readonly original_text: string;
  readonly normalized_text: string;
}

export interface CorpusCreation {
  readonly corpus: Corpus;
  readonly version: CorpusVersion;
}

export class ProjectApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(
    message: string,
    status: number,
    code: string,
    requestId: string | null,
  ) {
    super(message);
    this.name = "ProjectApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export class ProjectContractError extends Error {
  constructor() {
    super("The project API returned an incompatible response.");
    this.name = "ProjectContractError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isProjectRole(value: unknown): value is ProjectRole {
  return (
    value === "owner" ||
    value === "admin" ||
    value === "editor" ||
    value === "viewer"
  );
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/u.test(value);
}

function isProject(value: unknown): value is Project {
  return (
    isRecord(value) &&
    isString(value.id) &&
    isString(value.name) &&
    isString(value.description) &&
    isString(value.created_at)
  );
}

function isProjectPrincipal(value: unknown): value is ProjectPrincipal {
  return (
    isRecord(value) &&
    isString(value.subject) &&
    isString(value.organization_id) &&
    isProjectRole(value.role) &&
    (value.display_name === null || isString(value.display_name))
  );
}

function isProjectDeletion(value: unknown): value is ProjectDeletion {
  return (
    isRecord(value) &&
    isString(value.project_id) &&
    value.state === "deletion_pending" &&
    isString(value.requested_at) &&
    isString(value.retention_until)
  );
}

function isCorpus(value: unknown): value is Corpus {
  return (
    isRecord(value) &&
    isString(value.id) &&
    isString(value.project_id) &&
    isString(value.name) &&
    isString(value.created_at)
  );
}

function isVersion(value: unknown): value is CorpusVersion {
  return (
    isRecord(value) &&
    isString(value.id) &&
    isString(value.corpus_id) &&
    (value.parent_version_id === null || isString(value.parent_version_id)) &&
    Number.isInteger(value.version_number) &&
    (value.version_number as number) > 0 &&
    isString(value.language) &&
    Number.isInteger(value.sentence_count) &&
    (value.sentence_count as number) > 0 &&
    isSha256(value.content_sha256) &&
    isString(value.corpusgen_version) &&
    isString(value.created_at)
  );
}

function isSentence(value: unknown): value is CorpusSentence {
  return (
    isRecord(value) &&
    Number.isInteger(value.ordinal) &&
    (value.ordinal as number) >= 0 &&
    isString(value.original_text) &&
    isString(value.normalized_text)
  );
}

function parseArray<T>(
  value: unknown,
  guard: (item: unknown) => item is T,
): T[] {
  if (!Array.isArray(value) || !value.every(guard)) {
    throw new ProjectContractError();
  }
  return value;
}

async function apiError(response: Response): Promise<ProjectApiError> {
  let value: unknown = null;
  try {
    value = await response.json();
  } catch {
    // The stable fallback below intentionally hides upstream response details.
  }
  if (isRecord(value) && isString(value.message)) {
    return new ProjectApiError(
      value.message,
      response.status,
      isString(value.code) ? value.code : "request_failed",
      isString(value.request_id) ? value.request_id : null,
    );
  }
  return new ProjectApiError(
    response.status === 413
      ? "The import is larger than the 10 MiB limit."
      : "The project request could not be completed.",
    response.status,
    "request_failed",
    response.headers.get("x-request-id"),
  );
}

async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = await authenticatedFetch(path, {
    cache: "no-store",
    ...init,
  });
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as unknown;
}

function jsonRequest(
  body: unknown,
  method: "POST" | "DELETE" = "POST",
): RequestInit {
  return {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export async function listProjects(signal?: AbortSignal): Promise<Project[]> {
  return parseArray(
    await requestJson("/api/v1/projects", { signal }),
    isProject,
  );
}

export async function getCurrentPrincipal(
  signal?: AbortSignal,
): Promise<ProjectPrincipal> {
  const value = await requestJson("/api/v1/auth/me", { signal });
  if (!isProjectPrincipal(value)) throw new ProjectContractError();
  return value;
}

export async function createProject(input: {
  readonly name: string;
  readonly description: string;
}): Promise<Project> {
  const value = await requestJson("/api/v1/projects", jsonRequest(input));
  if (!isProject(value)) throw new ProjectContractError();
  return value;
}

export async function requestProjectDeletion(
  projectId: string,
  confirmation: string,
): Promise<ProjectDeletion> {
  const value = await requestJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}`,
    jsonRequest({ confirmation }, "DELETE"),
  );
  if (!isProjectDeletion(value)) throw new ProjectContractError();
  return value;
}

export async function listCorpora(projectId: string): Promise<Corpus[]> {
  return parseArray(
    await requestJson(
      `/api/v1/projects/${encodeURIComponent(projectId)}/corpora`,
    ),
    isCorpus,
  );
}

export async function createManualCorpus(
  projectId: string,
  input: {
    readonly name: string;
    readonly language: string;
    readonly sentences: string[];
  },
): Promise<CorpusCreation> {
  const value = await requestJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/corpora`,
    jsonRequest(input),
  );
  return parseCreation(value);
}

export async function importCorpus(
  projectId: string,
  input: {
    readonly name: string;
    readonly language: string;
    readonly format: CorpusFileFormat;
    readonly textColumn: string | null;
    readonly file: File;
  },
): Promise<CorpusCreation> {
  const form = new FormData();
  form.set("name", input.name);
  form.set("language", input.language);
  form.set("format", input.format);
  if (input.textColumn) form.set("text_column", input.textColumn);
  form.set("file", input.file);
  return parseCreation(
    await requestJson(
      `/api/v1/projects/${encodeURIComponent(projectId)}/corpora/imports`,
      { method: "POST", body: form },
    ),
  );
}

export async function listVersions(
  projectId: string,
  corpusId: string,
): Promise<CorpusVersion[]> {
  return parseArray(
    await requestJson(
      `/api/v1/projects/${encodeURIComponent(projectId)}/corpora/${encodeURIComponent(corpusId)}/versions`,
    ),
    isVersion,
  );
}

export async function listSentences(
  projectId: string,
  corpusId: string,
  versionId: string,
): Promise<CorpusSentence[]> {
  return parseArray(
    await requestJson(
      `/api/v1/projects/${encodeURIComponent(projectId)}/corpora/${encodeURIComponent(corpusId)}/versions/${encodeURIComponent(versionId)}/sentences?offset=0&limit=500`,
    ),
    isSentence,
  );
}

export async function listAllSentences(
  projectId: string,
  corpusId: string,
  versionId: string,
  expectedCount: number,
  signal?: AbortSignal,
): Promise<CorpusSentence[]> {
  if (
    !Number.isSafeInteger(expectedCount) ||
    expectedCount < 1 ||
    expectedCount > MAX_CORPUS_SENTENCES
  ) {
    throw new ProjectContractError();
  }
  const sentences: CorpusSentence[] = [];
  while (sentences.length < expectedCount) {
    const offset = sentences.length;
    const limit = Math.min(500, expectedCount - offset);
    const page = parseArray(
      await requestJson(
        `/api/v1/projects/${encodeURIComponent(projectId)}/corpora/${encodeURIComponent(corpusId)}/versions/${encodeURIComponent(versionId)}/sentences?offset=${offset}&limit=${limit}`,
        { signal },
      ),
      isSentence,
    );
    if (
      page.length !== limit ||
      page.some((sentence, index) => sentence.ordinal !== offset + index)
    ) {
      throw new ProjectContractError();
    }
    sentences.push(...page);
  }
  return sentences;
}

export function corpusExportHref(
  projectId: string,
  corpusId: string,
  versionId: string,
  format: CorpusExportFormat,
): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/corpora/${encodeURIComponent(corpusId)}/versions/${encodeURIComponent(versionId)}/export?format=${format}`;
}

function parseCreation(value: unknown): CorpusCreation {
  if (
    !isRecord(value) ||
    !isCorpus(value.corpus) ||
    !isVersion(value.version)
  ) {
    throw new ProjectContractError();
  }
  return value as unknown as CorpusCreation;
}

export function workspaceError(error: unknown): string {
  if (
    error instanceof ProjectApiError ||
    error instanceof ProjectContractError
  ) {
    const request =
      error instanceof ProjectApiError && error.requestId
        ? ` Request ${error.requestId}.`
        : "";
    return `${error.message}${request}`;
  }
  return "The project workspace is temporarily unavailable.";
}
