"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  listCorpora,
  listProjects,
  listVersions,
  type Corpus,
  type CorpusVersion,
  type Project,
} from "@/lib/projects";

const storageKey = "corpuskit.selected-project.v1";
const corpusVersionStorageKey = "corpuskit.selected-corpus-version.v1";

export interface SelectedCorpusVersion {
  readonly corpus: Corpus;
  readonly version: CorpusVersion;
}

interface ProjectContextValue {
  readonly projects: readonly Project[];
  readonly selectedProject: Project | null;
  readonly loading: boolean;
  readonly error: boolean;
  readonly selectProject: (id: string | null) => void;
  readonly refreshProjects: (preferredId?: string | null) => Promise<void>;
  readonly selectedCorpusVersion: SelectedCorpusVersion | null;
  readonly corpusVersionLoading: boolean;
  readonly corpusVersionError: boolean;
  readonly selectCorpusVersion: (
    selection: SelectedCorpusVersion | null,
  ) => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({
  children,
}: {
  readonly children: ReactNode;
}) {
  const [projects, setProjects] = useState<readonly Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [selectedCorpusVersion, setSelectedCorpusVersion] =
    useState<SelectedCorpusVersion | null>(null);
  const [corpusVersionLoading, setCorpusVersionLoading] = useState(false);
  const [corpusVersionError, setCorpusVersionError] = useState(false);
  const selectedIdRef = useRef<string | null>(null);
  const corpusSelectionRevision = useRef(0);

  const refreshProjects = useCallback(async (preferredId?: string | null) => {
    setLoading(true);
    setError(false);
    try {
      const next = await listProjects();
      setProjects(next);
      const stored = sessionStorage.getItem(storageKey);
      const candidate =
        preferredId === undefined
          ? (selectedIdRef.current ?? stored)
          : preferredId;
      const accepted = next.find(({ id }) => id === candidate);
      const nextId = accepted?.id ?? next[0]?.id ?? null;
      if (
        nextId === null ||
        (selectedIdRef.current !== null && nextId !== selectedIdRef.current)
      ) {
        clearCorpusVersionSelection();
      }
      selectedIdRef.current = nextId;
      setSelectedId(nextId);
      if (nextId) sessionStorage.setItem(storageKey, nextId);
      else sessionStorage.removeItem(storageKey);
    } catch {
      setProjects([]);
      clearCorpusVersionSelection();
      selectedIdRef.current = null;
      setSelectedId(null);
      sessionStorage.removeItem(storageKey);
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void refreshProjects(), 0);
    const refresh = () => void refreshProjects();
    window.addEventListener("corpuskit:projects-changed", refresh);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("corpuskit:projects-changed", refresh);
    };
  }, [refreshProjects]);

  const selectProject = useCallback(
    (id: string | null) => {
      const accepted = projects.find((project) => project.id === id) ?? null;
      if (accepted?.id !== selectedIdRef.current) clearCorpusVersionSelection();
      selectedIdRef.current = accepted?.id ?? null;
      setSelectedId(accepted?.id ?? null);
      if (accepted) sessionStorage.setItem(storageKey, accepted.id);
      else sessionStorage.removeItem(storageKey);
    },
    [projects],
  );

  const selectedProject =
    projects.find((project) => project.id === selectedId) ?? null;
  const selectedProjectId = selectedProject?.id ?? null;

  function clearCorpusVersionSelection() {
    corpusSelectionRevision.current += 1;
    setSelectedCorpusVersion(null);
    setCorpusVersionError(false);
    if (typeof sessionStorage !== "undefined")
      sessionStorage.removeItem(corpusVersionStorageKey);
  }

  const selectCorpusVersion = useCallback(
    (selection: SelectedCorpusVersion | null) => {
      if (
        !selection ||
        !selectedProjectId ||
        selection.corpus.project_id !== selectedProjectId ||
        selection.version.corpus_id !== selection.corpus.id
      ) {
        clearCorpusVersionSelection();
        return;
      }
      corpusSelectionRevision.current += 1;
      setSelectedCorpusVersion(selection);
      setCorpusVersionError(false);
      sessionStorage.setItem(
        corpusVersionStorageKey,
        JSON.stringify({
          project_id: selectedProjectId,
          corpus_id: selection.corpus.id,
          version_id: selection.version.id,
        }),
      );
    },
    [selectedProjectId],
  );

  useEffect(() => {
    let active = true;
    const revision = corpusSelectionRevision.current;
    const timer = setTimeout(() => {
      if (!selectedProjectId) {
        setCorpusVersionLoading(false);
        return;
      }
      setCorpusVersionLoading(true);
      setCorpusVersionError(false);
      void hydrateCorpusVersion(selectedProjectId)
        .then((selection) => {
          if (!active || corpusSelectionRevision.current !== revision) return;
          setSelectedCorpusVersion(selection);
          if (selection) {
            sessionStorage.setItem(
              corpusVersionStorageKey,
              JSON.stringify({
                project_id: selectedProjectId,
                corpus_id: selection.corpus.id,
                version_id: selection.version.id,
              }),
            );
          } else sessionStorage.removeItem(corpusVersionStorageKey);
        })
        .catch(() => {
          if (!active || corpusSelectionRevision.current !== revision) return;
          setSelectedCorpusVersion(null);
          sessionStorage.removeItem(corpusVersionStorageKey);
          setCorpusVersionError(true);
        })
        .finally(() => {
          if (active && corpusSelectionRevision.current === revision)
            setCorpusVersionLoading(false);
        });
    }, 0);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [selectedProjectId]);

  const value = useMemo<ProjectContextValue>(
    () => ({
      projects,
      selectedProject,
      loading,
      error,
      selectProject,
      refreshProjects,
      selectedCorpusVersion,
      corpusVersionLoading,
      corpusVersionError,
      selectCorpusVersion,
    }),
    [
      corpusVersionError,
      corpusVersionLoading,
      error,
      loading,
      projects,
      refreshProjects,
      selectCorpusVersion,
      selectProject,
      selectedCorpusVersion,
      selectedProject,
    ],
  );

  return (
    <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
  );
}

export function useProjectContext(): ProjectContextValue | null {
  return useContext(ProjectContext);
}

export function clearSelectedProject(): void {
  if (typeof sessionStorage !== "undefined") {
    sessionStorage.removeItem(storageKey);
    sessionStorage.removeItem(corpusVersionStorageKey);
  }
}

async function hydrateCorpusVersion(
  projectId: string,
): Promise<SelectedCorpusVersion | null> {
  const stored = readStoredCorpusVersion(projectId);
  const corpora = await listCorpora(projectId);
  const corpus =
    corpora.find((item) => item.id === stored?.corpusId) ?? corpora[0] ?? null;
  if (!corpus) return null;
  const versions = await listVersions(projectId, corpus.id);
  const version =
    versions.find((item) => item.id === stored?.versionId) ??
    versions.at(-1) ??
    null;
  return version ? { corpus, version } : null;
}

function readStoredCorpusVersion(
  projectId: string,
): { readonly corpusId: string; readonly versionId: string } | null {
  try {
    const raw = sessionStorage.getItem(corpusVersionStorageKey);
    if (!raw) return null;
    const value = JSON.parse(raw) as unknown;
    if (
      typeof value !== "object" ||
      value === null ||
      !("project_id" in value) ||
      !("corpus_id" in value) ||
      !("version_id" in value) ||
      value.project_id !== projectId ||
      typeof value.corpus_id !== "string" ||
      typeof value.version_id !== "string"
    )
      return null;
    return { corpusId: value.corpus_id, versionId: value.version_id };
  } catch {
    return null;
  }
}
