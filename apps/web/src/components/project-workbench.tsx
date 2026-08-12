"use client";

import type { FormEvent, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { useProjectContext } from "@/components/project-context";
import {
  MAX_CORPUS_FILE_BYTES,
  MAX_CORPUS_SENTENCES,
  MAX_SENTENCE_CHARACTERS,
  corpusExportHref,
  createManualCorpus,
  createProject,
  getCurrentPrincipal,
  importCorpus,
  listCorpora,
  listSentences,
  listVersions,
  requestProjectDeletion,
  workspaceError,
  type Corpus,
  type CorpusFileFormat,
  type CorpusSentence,
  type CorpusVersion,
  type Project,
  type ProjectDeletion,
} from "@/lib/projects";

export function ProjectWorkbench() {
  const projectContext = useProjectContext();
  const projects = projectContext?.projects ?? [];
  const selectedProject = projectContext?.selectedProject ?? null;
  const sharedCorpusVersion = projectContext?.selectedCorpusVersion ?? null;
  const [corpora, setCorpora] = useState<Corpus[]>([]);
  const [selectedCorpus, setSelectedCorpus] = useState<Corpus | null>(null);
  const [versions, setVersions] = useState<CorpusVersion[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<CorpusVersion | null>(
    null,
  );
  const [sentences, setSentences] = useState<CorpusSentence[]>([]);
  const [canDeleteProjects, setCanDeleteProjects] = useState(false);
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [projectActionNotice, setProjectActionNotice] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    void getCurrentPrincipal(controller.signal)
      .then((principal) => {
        if (!active) return;
        setCanDeleteProjects(
          principal.role === "owner" || principal.role === "admin",
        );
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setError(workspaceError(caught));
        setNotice("");
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    let active = true;
    const timer = setTimeout(() => {
      setSelectedCorpus(null);
      setVersions([]);
      setSelectedVersion(null);
      setSentences([]);
      if (!selectedProject) {
        setCorpora([]);
        if (!projectContext?.loading) {
          setNotice(projects.length ? "Select a project." : "No projects yet.");
        }
        return;
      }
      setPending(true);
      setError(null);
      void listCorpora(selectedProject.id)
        .then((items) => {
          if (!active) return;
          setCorpora(items);
          setNotice(
            `${selectedProject.name} opened. ${items.length} corpus${items.length === 1 ? "" : "es"} available.`,
          );
        })
        .catch((caught: unknown) => {
          if (!active) return;
          setCorpora([]);
          setError(workspaceError(caught));
          setNotice("");
        })
        .finally(() => {
          if (active) setPending(false);
        });
    }, 0);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [projectContext?.loading, projects.length, selectedProject]);

  useEffect(() => {
    if (
      !selectedProject ||
      !sharedCorpusVersion ||
      sharedCorpusVersion.corpus.project_id !== selectedProject.id
    ) {
      return;
    }
    if (
      selectedCorpus?.id === sharedCorpusVersion.corpus.id &&
      selectedVersion?.id === sharedCorpusVersion.version.id
    ) {
      return;
    }
    let active = true;
    const timer = setTimeout(() => {
      setPending(true);
      setError(null);
      void Promise.all([
        listVersions(selectedProject.id, sharedCorpusVersion.corpus.id),
        listSentences(
          selectedProject.id,
          sharedCorpusVersion.corpus.id,
          sharedCorpusVersion.version.id,
        ),
      ])
        .then(([items, rows]) => {
          if (!active) return;
          const version = items.find(
            (item) => item.id === sharedCorpusVersion.version.id,
          );
          if (!version) throw new Error("missing_shared_version");
          setSelectedCorpus(sharedCorpusVersion.corpus);
          setVersions(items);
          setSelectedVersion(version);
          setSentences(rows);
          setNotice(
            `${sharedCorpusVersion.corpus.name} version ${version.version_number} restored for this project.`,
          );
        })
        .catch((caught: unknown) => {
          if (!active) return;
          setError(workspaceError(caught));
          setNotice("");
        })
        .finally(() => {
          if (active) setPending(false);
        });
    }, 0);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [
    selectedCorpus?.id,
    selectedProject,
    selectedVersion?.id,
    sharedCorpusVersion,
  ]);

  function chooseProject(project: Project) {
    setProjectActionNotice("");
    projectContext?.selectProject(project.id);
  }

  async function chooseCorpus(corpus: Corpus) {
    if (!selectedProject) return;
    setPending(true);
    setError(null);
    projectContext?.selectCorpusVersion(null);
    setSelectedCorpus(corpus);
    setSentences([]);
    try {
      const items = await listVersions(selectedProject.id, corpus.id);
      setVersions(items);
      const latest = items.at(-1) ?? null;
      setSelectedVersion(latest);
      if (latest) {
        setSentences(
          await listSentences(selectedProject.id, corpus.id, latest.id),
        );
        projectContext?.selectCorpusVersion({ corpus, version: latest });
      }
      setNotice(
        `${corpus.name} opened. ${items.length} immutable version${items.length === 1 ? "" : "s"} available.`,
      );
    } catch (caught) {
      setVersions([]);
      setSelectedVersion(null);
      setError(workspaceError(caught));
      setNotice("");
    } finally {
      setPending(false);
    }
  }

  async function chooseVersion(version: CorpusVersion) {
    if (!selectedProject || !selectedCorpus) return;
    setPending(true);
    setError(null);
    try {
      setSentences(
        await listSentences(selectedProject.id, selectedCorpus.id, version.id),
      );
      setSelectedVersion(version);
      projectContext?.selectCorpusVersion({
        corpus: selectedCorpus,
        version,
      });
      setNotice(`Version ${version.version_number} loaded.`);
    } catch (caught) {
      setError(workspaceError(caught));
      setNotice("");
    } finally {
      setPending(false);
    }
  }

  async function refreshCorpora(createdName: string) {
    if (!selectedProject) return;
    const items = await listCorpora(selectedProject.id);
    setCorpora(items);
    const created =
      items.find((corpus) => corpus.name === createdName) ?? items.at(-1);
    if (created) await chooseCorpus(created);
  }

  return (
    <div className="projects-workbench">
      <div
        className="workspace-announcements"
        aria-live="polite"
        aria-atomic="true"
      >
        {pending ||
        projectContext?.loading ||
        projectContext?.corpusVersionLoading ? (
          <p role="status">Working…</p>
        ) : projectContext?.error ? (
          <p role="alert">Projects are temporarily unavailable. Try again.</p>
        ) : projectContext?.corpusVersionError ? (
          <p role="alert">
            The selected corpus version could not be restored. Choose it again.
          </p>
        ) : error ? (
          <p role="alert">{error}</p>
        ) : projectActionNotice ? (
          <p role="status">{projectActionNotice}</p>
        ) : notice ? (
          <p role="status">{notice}</p>
        ) : null}
      </div>

      <section className="workspace-panel" aria-labelledby="projects-heading">
        <div className="workspace-heading">
          <div>
            <p className="eyebrow">
              <span>01</span> Tenant workspace
            </p>
            <h2 id="projects-heading">Projects</h2>
          </div>
          <p>
            Projects group immutable corpora for the authenticated organization.
          </p>
        </div>
        <div className="project-layout">
          <ProjectList
            projects={projects}
            selected={selectedProject}
            onSelect={chooseProject}
          />
          <ProjectForm
            pending={pending}
            onCreated={async (project) => {
              await projectContext?.refreshProjects(project.id);
            }}
            onError={setError}
          />
        </div>
        {selectedProject && canDeleteProjects ? (
          <ProjectDeletionForm
            project={selectedProject}
            pending={pending}
            onDeleted={(deletion) => {
              projectContext?.selectProject(null);
              setCorpora([]);
              setSelectedCorpus(null);
              setVersions([]);
              setSelectedVersion(null);
              setSentences([]);
              setError(null);
              setProjectActionNotice(
                `${selectedProject.name} is no longer available. Its data is retained until at least ${new Date(deletion.retention_until).toLocaleDateString()}.`,
              );
            }}
            onError={setError}
          />
        ) : null}
      </section>

      <section className="workspace-panel" aria-labelledby="corpora-heading">
        <div className="workspace-heading">
          <div>
            <p className="eyebrow">
              <span>02</span> Source material
            </p>
            <h2 id="corpora-heading">Corpora</h2>
          </div>
          <p>
            Manual entry and strict TXT, CSV, or JSON imports create version 1.
            Corpus update and deletion are not available.
          </p>
        </div>
        {!selectedProject ? (
          <WorkspaceEmpty>
            Select a project before creating or browsing a corpus.
          </WorkspaceEmpty>
        ) : (
          <div className="corpus-layout">
            <CorpusList
              corpora={corpora}
              selected={selectedCorpus}
              onSelect={chooseCorpus}
            />
            <CorpusForm
              projectId={selectedProject.id}
              pending={pending}
              onCreated={async (name) => {
                await refreshCorpora(name);
              }}
              onError={setError}
            />
          </div>
        )}
      </section>

      <section className="workspace-panel" aria-labelledby="versions-heading">
        <div className="workspace-heading">
          <div>
            <p className="eyebrow">
              <span>03</span> Reproducible snapshot
            </p>
            <h2 id="versions-heading">Versions &amp; sentences</h2>
          </div>
          <p>
            Every digest identifies normalized text in deterministic sentence
            order.
          </p>
        </div>
        {!selectedCorpus || !selectedProject ? (
          <WorkspaceEmpty>
            Select a corpus to inspect its immutable initial version.
          </WorkspaceEmpty>
        ) : (
          <VersionBrowser
            project={selectedProject}
            corpus={selectedCorpus}
            versions={versions}
            selected={selectedVersion}
            sentences={sentences}
            pending={pending}
            onSelect={chooseVersion}
          />
        )}
      </section>
    </div>
  );
}

function ProjectDeletionForm({
  project,
  pending,
  onDeleted,
  onError,
}: {
  project: Project;
  pending: boolean;
  onDeleted: (deletion: ProjectDeletion) => void;
  onError: (message: string | null) => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submissionLock = useRef(false);
  const expected = `DELETE ${project.name}`;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submissionLock.current || pending || confirmation !== expected) {
      return;
    }
    submissionLock.current = true;
    setSubmitting(true);
    onError(null);
    try {
      const deletion = await requestProjectDeletion(project.id, confirmation);
      onDeleted(deletion);
      window.dispatchEvent(new Event("corpuskit:projects-changed"));
    } catch (caught) {
      onError(workspaceError(caught));
    } finally {
      submissionLock.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form
      aria-busy={submitting}
      className="workspace-form project-deletion-form"
      onSubmit={(event) => void submit(event)}
    >
      <h3>Delete this project</h3>
      <p id="project-deletion-help">
        Access ends immediately. CorpusKit retains the project and its artifacts
        for at least 30 days before permanent removal. Active runs must finish
        first.
      </p>
      <p>
        To confirm, type <code dir="auto">{expected}</code> exactly.
      </p>
      <label htmlFor="project-deletion-confirmation">
        Deletion confirmation
      </label>
      <input
        id="project-deletion-confirmation"
        aria-describedby="project-deletion-help"
        autoComplete="off"
        maxLength={167}
        required
        spellCheck={false}
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
      />
      <button
        className="button project-deletion-button"
        disabled={pending || submitting || confirmation !== expected}
        type="submit"
      >
        {submitting ? "Requesting deletion…" : "Delete project"}
      </button>
    </form>
  );
}

function ProjectList({
  projects,
  selected,
  onSelect,
}: {
  projects: readonly Project[];
  selected: Project | null;
  onSelect: (project: Project) => void;
}) {
  if (!projects.length)
    return (
      <WorkspaceEmpty>
        No project has been created for this tenant.
      </WorkspaceEmpty>
    );
  return (
    <ul className="workspace-choice-list" aria-label="Available projects">
      {projects.map((project) => (
        <li key={project.id}>
          <button
            type="button"
            aria-pressed={selected?.id === project.id}
            onClick={() => onSelect(project)}
          >
            <strong dir="auto">{project.name}</strong>
            <span dir="auto">{project.description || "No description"}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function ProjectForm({
  pending,
  onCreated,
  onError,
}: {
  pending: boolean;
  onCreated: (project: Project) => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submissionLock = useRef(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submissionLock.current) return;
    submissionLock.current = true;
    setSubmitting(true);
    onError(null);
    try {
      const project = await createProject({ name, description });
      setName("");
      setDescription("");
      await onCreated(project);
      window.dispatchEvent(new Event("corpuskit:projects-changed"));
    } catch (caught) {
      onError(workspaceError(caught));
    } finally {
      submissionLock.current = false;
      setSubmitting(false);
    }
  }
  return (
    <form
      aria-busy={submitting}
      className="workspace-form"
      onSubmit={(event) => void submit(event)}
    >
      <h3>Create a project</h3>
      <label htmlFor="project-name">Project name</label>
      <input
        id="project-name"
        required
        maxLength={160}
        value={name}
        onChange={(event) => setName(event.target.value)}
      />
      <label htmlFor="project-description">
        Description <span>optional</span>
      </label>
      <textarea
        id="project-description"
        maxLength={4000}
        value={description}
        onChange={(event) => setDescription(event.target.value)}
      />
      <button
        className="button button-primary"
        disabled={pending || submitting}
        type="submit"
      >
        {submitting ? "Creating project…" : "Create project"}
      </button>
    </form>
  );
}

function CorpusList({
  corpora,
  selected,
  onSelect,
}: {
  corpora: Corpus[];
  selected: Corpus | null;
  onSelect: (corpus: Corpus) => Promise<void>;
}) {
  if (!corpora.length)
    return <WorkspaceEmpty>This project has no corpora yet.</WorkspaceEmpty>;
  return (
    <ul
      className="workspace-choice-list"
      aria-label="Corpora in selected project"
    >
      {corpora.map((corpus) => (
        <li key={corpus.id}>
          <button
            type="button"
            aria-pressed={selected?.id === corpus.id}
            onClick={() => void onSelect(corpus)}
          >
            <strong dir="auto">{corpus.name}</strong>
            <span>Immutable corpus</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function CorpusForm({
  projectId,
  pending,
  onCreated,
  onError,
}: {
  projectId: string;
  pending: boolean;
  onCreated: (name: string) => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [mode, setMode] = useState<"manual" | "file">("manual");
  const [name, setName] = useState("");
  const [language, setLanguage] = useState("en-us");
  const [sentenceText, setSentenceText] = useState("");
  const [format, setFormat] = useState<CorpusFileFormat>("txt");
  const [textColumn, setTextColumn] = useState("text");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submissionLock = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submissionLock.current) return;
    submissionLock.current = true;
    setSubmitting(true);
    onError(null);
    try {
      if (mode === "manual") {
        const sentences = sentenceText.split(/\r?\n/u);
        if (
          sentences.length > MAX_CORPUS_SENTENCES ||
          sentences.some((item) => item.length > MAX_SENTENCE_CHARACTERS)
        ) {
          throw new Error("client_limit");
        }
        await createManualCorpus(projectId, { name, language, sentences });
      } else {
        if (!file || file.size > MAX_CORPUS_FILE_BYTES)
          throw new Error("client_file_limit");
        await importCorpus(projectId, {
          name,
          language,
          format,
          textColumn: format === "csv" ? textColumn : null,
          file,
        });
      }
      setName("");
      setSentenceText("");
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await onCreated(name);
    } catch (caught) {
      if (caught instanceof Error && caught.message === "client_limit") {
        onError(
          "Manual input is limited to 10,000 lines and 2,000 characters per sentence.",
        );
      } else if (
        caught instanceof Error &&
        caught.message === "client_file_limit"
      ) {
        onError("Choose one UTF-8 file no larger than 10 MiB.");
      } else {
        onError(workspaceError(caught));
      }
    } finally {
      submissionLock.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form
      aria-busy={submitting}
      className="workspace-form corpus-form"
      onSubmit={(event) => void submit(event)}
    >
      <h3>Create an immutable corpus</h3>
      <fieldset className="mode-picker">
        <legend>Input method</legend>
        <label>
          <input
            type="radio"
            name="corpus-mode"
            checked={mode === "manual"}
            onChange={() => setMode("manual")}
          />{" "}
          Manual sentences
        </label>
        <label>
          <input
            type="radio"
            name="corpus-mode"
            checked={mode === "file"}
            onChange={() => setMode("file")}
          />{" "}
          File import
        </label>
      </fieldset>
      <div className="workspace-fields-two">
        <div>
          <label htmlFor="corpus-name">Corpus name</label>
          <input
            id="corpus-name"
            required
            maxLength={160}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div>
          <label htmlFor="corpus-language">eSpeak language</label>
          <input
            id="corpus-language"
            required
            maxLength={64}
            value={language}
            onChange={(event) => setLanguage(event.target.value)}
          />
        </div>
      </div>
      {mode === "manual" ? (
        <div>
          <label htmlFor="manual-sentences">
            Sentences <span>one per line</span>
          </label>
          <textarea
            id="manual-sentences"
            required
            value={sentenceText}
            onChange={(event) => setSentenceText(event.target.value)}
          />
          <p className="field-help">
            Blank lines and normalized duplicates are removed; the first
            occurrence wins.
          </p>
        </div>
      ) : (
        <div className="file-fields">
          <label htmlFor="file-format">File format</label>
          <select
            id="file-format"
            value={format}
            onChange={(event) => {
              setFormat(event.target.value as CorpusFileFormat);
              setFile(null);
              if (fileInput.current) fileInput.current.value = "";
            }}
          >
            <option value="txt">TXT — one sentence per line</option>
            <option value="csv">CSV — header row required</option>
            <option value="json">JSON — {`{"sentences":[...]}`}</option>
          </select>
          {format === "csv" ? (
            <>
              <label htmlFor="csv-column">CSV text column</label>
              <input
                id="csv-column"
                required
                maxLength={160}
                value={textColumn}
                onChange={(event) => setTextColumn(event.target.value)}
              />
            </>
          ) : null}
          <label htmlFor="corpus-file">UTF-8 {format.toUpperCase()} file</label>
          <input
            id="corpus-file"
            ref={fileInput}
            required
            type="file"
            accept={
              format === "txt"
                ? ".txt,text/plain"
                : format === "csv"
                  ? ".csv,text/csv"
                  : ".json,application/json"
            }
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <p className="field-help">
            10 MiB maximum. Archives and mismatched extensions, MIME types, or
            schemas are rejected.
          </p>
        </div>
      )}
      <button
        className="button button-primary"
        disabled={pending || submitting}
        type="submit"
      >
        {submitting ? "Creating corpus…" : "Create corpus"}
      </button>
    </form>
  );
}

function VersionBrowser({
  project,
  corpus,
  versions,
  selected,
  sentences,
  pending,
  onSelect,
}: {
  project: Project;
  corpus: Corpus;
  versions: CorpusVersion[];
  selected: CorpusVersion | null;
  sentences: CorpusSentence[];
  pending: boolean;
  onSelect: (version: CorpusVersion) => Promise<void>;
}) {
  return (
    <div className="version-browser">
      <div className="version-strip" aria-label="Corpus versions">
        {versions.map((version) => (
          <button
            type="button"
            key={version.id}
            aria-pressed={selected?.id === version.id}
            disabled={pending}
            onClick={() => void onSelect(version)}
          >
            <strong>Version {version.version_number}</strong>
            <span>
              {version.sentence_count} sentences · {version.language}
            </span>
          </button>
        ))}
      </div>
      {selected ? (
        <>
          <div className="version-metadata">
            <div>
              <span>Content SHA-256</span>
              <code>{selected.content_sha256}</code>
            </div>
            <div>
              <span>CorpusGen contract</span>
              <strong>{selected.corpusgen_version}</strong>
            </div>
            <nav aria-label="Download this corpus version">
              {(["txt", "json", "csv"] as const).map((format) => (
                <a
                  key={format}
                  download={`corpus-v${selected.version_number}.${format}`}
                  href={corpusExportHref(
                    project.id,
                    corpus.id,
                    selected.id,
                    format,
                  )}
                >
                  {format.toUpperCase()}
                </a>
              ))}
            </nav>
          </div>
          <div
            className="sentence-table-wrap"
            role="region"
            aria-label="Scrollable corpus sentences"
            tabIndex={0}
          >
            <table className="workspace-table">
              <caption>Normalized sentences in deterministic order</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Normalized text</th>
                  <th scope="col">Original text</th>
                </tr>
              </thead>
              <tbody>
                {sentences.map((sentence) => (
                  <tr key={sentence.ordinal}>
                    <th scope="row">{sentence.ordinal + 1}</th>
                    <td dir="auto">{sentence.normalized_text}</td>
                    <td dir="auto">{sentence.original_text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="field-help">
            Sentence browsing is a preview capped at the first 500 rows.
            Downloads always contain the complete immutable version.
          </p>
          {selected.sentence_count > sentences.length ? (
            <p className="field-help">
              Showing the first {sentences.length} of {selected.sentence_count}{" "}
              sentences.
            </p>
          ) : null}
        </>
      ) : (
        <WorkspaceEmpty>
          No version is available for this corpus.
        </WorkspaceEmpty>
      )}
    </div>
  );
}

function WorkspaceEmpty({ children }: { children: ReactNode }) {
  return <p className="workspace-empty">{children}</p>;
}
