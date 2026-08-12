"use client";

import Link from "next/link";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import { useProjectContext } from "@/components/project-context";
import { describeRequestError } from "@/lib/api-client";
import {
  artifactsApi,
  maximumArtifactBytes,
  maximumPromptArtifactBytes,
  verifiedArtifactDownload,
  type ArtifactRecord,
  type PublicUploadKind,
  type SignedDownload,
} from "@/lib/artifacts";
import { saveBytes, sha256Hex } from "@/lib/workbench-input";

const pageSize = 50;

export function ArtifactManager() {
  const projects = useProjectContext();
  const project = projects?.selectedProject ?? null;
  const projectId = project?.id ?? null;
  const [artifacts, setArtifacts] = useState<readonly ArtifactRecord[]>([]);
  const [pageLength, setPageLength] = useState(0);
  const [offset, setOffset] = useState(0);
  const [kind, setKind] = useState("");
  const [highlight, setHighlight] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    const value = new URLSearchParams(window.location.search).get("artifact");
    return value && /^[0-9a-f-]{36}$/iu.test(value) ? value : null;
  });
  const [state, setState] = useState(() => (highlight ? "" : "active"));
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    if (!projectId) {
      setArtifacts([]);
      setPageLength(0);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const page = await artifactsApi.list(projectId, offset, pageSize);
      setPageLength(page.length);
      if (!highlight || page.some((artifact) => artifact.id === highlight)) {
        setArtifacts(page);
        return;
      }
      const linked = await artifactsApi.get(projectId, highlight);
      if (linked.project_id !== projectId)
        throw new TypeError("The linked artifact is outside this project.");
      setArtifacts([linked, ...page]);
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setLoading(false);
    }
  }, [highlight, offset, projectId]);
  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);
  useEffect(() => {
    if (!highlight || loading) return;
    const timer = setTimeout(() => {
      document.getElementById(`artifact-${highlight}`)?.focus();
    }, 0);
    return () => clearTimeout(timer);
  }, [artifacts, highlight, loading]);
  const filtered = useMemo(
    () =>
      artifacts.filter(
        (artifact) =>
          (!kind || artifact.kind === kind) &&
          (!state || artifact.state === state) &&
          (!search.trim() ||
            `${artifact.filename} ${artifact.kind} ${artifact.sha256}`
              .toLocaleLowerCase()
              .includes(search.trim().toLocaleLowerCase())),
      ),
    [artifacts, kind, search, state],
  );
  if (!project)
    return (
      <section className="workbench-shell">
        <div className="availability-card">
          <h2>Select a project first</h2>
          <p>
            Artifacts are isolated to the project chosen in the global picker.
          </p>
          <Link
            className="button button-primary"
            href="/projects"
            prefetch={false}
          >
            Open Project Workspace
          </Link>
        </div>
      </section>
    );
  return (
    <section className="workbench-shell">
      <ArtifactUpload
        projectId={project.id}
        onUploaded={(artifact) => {
          setArtifacts((current) => [
            artifact,
            ...current.filter((item) => item.id !== artifact.id),
          ]);
          setHighlight(artifact.id);
        }}
      />
      <section className="workbench-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Project: {project.name}</p>
            <h2>Immutable artifacts</h2>
          </div>
          <button
            disabled={loading}
            onClick={() => void refresh()}
            type="button"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <div className="form-grid three-columns">
          <div>
            <label className="field-label" htmlFor="artifact-search">
              Search filename, kind, or digest
            </label>
            <input
              id="artifact-search"
              onChange={(event) => setSearch(event.target.value)}
              type="search"
              value={search}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="artifact-kind">
              Kind
            </label>
            <select
              id="artifact-kind"
              onChange={(event) => setKind(event.target.value)}
              value={kind}
            >
              <option value="">All kinds</option>
              {Array.from(new Set(artifacts.map((item) => item.kind)))
                .sort()
                .map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="artifact-state">
              State
            </label>
            <select
              id="artifact-state"
              onChange={(event) => setState(event.target.value)}
              value={state}
            >
              <option value="">All states</option>
              <option value="active">Active</option>
              <option value="tombstoned">Tombstoned</option>
              <option value="deleted">Deleted</option>
            </select>
          </div>
        </div>
        {error ? (
          <ErrorNotice message={error} title="Artifacts unavailable" />
        ) : null}
        {!loading && !filtered.length ? (
          <p className="workbench-empty">
            No matching artifacts exist in this project.
          </p>
        ) : (
          <ul className="artifact-list" aria-label="Project artifacts">
            {filtered.map((artifact) => (
              <ArtifactCard
                artifact={artifact}
                highlighted={artifact.id === highlight}
                key={artifact.id}
                onRemoved={() => {
                  setArtifacts((current) =>
                    current.filter((item) => item.id !== artifact.id),
                  );
                  setHighlight(null);
                }}
                projectId={project.id}
              />
            ))}
          </ul>
        )}
        <nav aria-label="Artifact pages" className="pagination">
          <button
            disabled={loading || offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - pageSize))}
            type="button"
          >
            Previous
          </button>
          <span>
            {pageLength
              ? `Rows ${offset + 1}–${offset + pageLength}`
              : "No page rows"}
            {artifacts.length > pageLength ? " · linked artifact included" : ""}
          </span>
          <button
            disabled={loading || pageLength < pageSize}
            onClick={() => setOffset((value) => value + pageSize)}
            type="button"
          >
            Next
          </button>
        </nav>
      </section>
    </section>
  );
}

function ArtifactUpload({
  projectId,
  onUploaded,
}: {
  readonly projectId: string;
  readonly onUploaded: (artifact: ArtifactRecord) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [uploadKind, setUploadKind] = useState<PublicUploadKind>("corpus-text");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (file.size > maximumArtifactBytes)
        throw new TypeError("The file exceeds the 10 MiB upload limit.");
      const bytes = await file.arrayBuffer();
      if (uploadKind === "prompt-set") validatePromptArtifact(bytes);
      const digest = await sha256Hex(bytes);
      const result = await artifactsApi.upload(
        projectId,
        file,
        digest,
        uploadKind,
      );
      onUploaded(result.artifact);
      setNotice(
        result.created
          ? uploadKind === "prompt-set"
            ? "Canonical Phon-RL prompt set uploaded and verified."
            : "Corpus text uploaded and verified."
          : "Identical immutable content already existed; the existing artifact was returned.",
      );
      setFile(null);
    } catch (caught) {
      setError(
        caught instanceof TypeError
          ? caught.message
          : describeRequestError(caught),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="workbench-form" onSubmit={(event) => void submit(event)}>
      <div className="workbench-form-heading">
        <div>
          <p className="eyebrow">Public upload allowlist</p>
          <h2>Upload immutable input artifact</h2>
        </div>
        <span className="status-pill status-available">
          corpus-text + prompt-set
        </span>
      </div>
      <p className="form-status">
        The browser computes SHA-256 before upload. Run results can only be
        adopted by a trusted worker and cannot be uploaded here.
      </p>
      <label className="field-label" htmlFor="artifact-upload-kind">
        Artifact kind
      </label>
      <select
        id="artifact-upload-kind"
        onChange={(event) => {
          setUploadKind(event.target.value as PublicUploadKind);
          setFile(null);
          setError(null);
          setNotice(null);
        }}
        value={uploadKind}
      >
        <option value="corpus-text">Corpus text</option>
        <option value="prompt-set">Phon-RL prompt set</option>
      </select>
      <label className="field-label" htmlFor="artifact-file">
        {uploadKind === "prompt-set"
          ? "Canonical prompt artifact JSON · maximum 8 MiB"
          : "UTF-8 corpus text file · maximum 10 MiB"}
      </label>
      <input
        accept={
          uploadKind === "prompt-set"
            ? "application/json,.json"
            : "text/plain,.txt"
        }
        id="artifact-file"
        onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        required
        type="file"
      />
      <button
        className="button button-primary"
        disabled={busy || !file}
        type="submit"
      >
        {busy
          ? "Hashing and uploading…"
          : uploadKind === "prompt-set"
            ? "Upload verified prompt set"
            : "Upload verified corpus text"}
      </button>
      {uploadKind === "prompt-set" ? (
        <p className="field-help">
          Use exact canonical compact JSON with schema ID{" "}
          <code>corpuskit.phon-rl-prompt-artifact.v1</code>. Prompt text stays
          in the project artifact and is never copied into run specifications,
          events, or result summaries.
        </p>
      ) : null}
      {notice ? (
        <p aria-live="polite" className="success-notice" role="status">
          {notice}
        </p>
      ) : null}
      {error ? (
        <ErrorNotice message={error} title="Upload not completed" />
      ) : null}
    </form>
  );
}

function validatePromptArtifact(bytes: ArrayBuffer): void {
  if (bytes.byteLength > maximumPromptArtifactBytes)
    throw new TypeError("The prompt artifact exceeds the 8 MiB upload limit.");
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new TypeError("The prompt artifact must be valid UTF-8.");
  }
  let value: unknown;
  try {
    value = JSON.parse(text) as unknown;
  } catch {
    throw new TypeError("The prompt artifact must be valid JSON.");
  }
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    Object.keys(value).sort().join(",") !== "prompts,schema_id"
  )
    throw new TypeError("The prompt artifact must use the exact v1 schema.");
  const record = value as Record<string, unknown>;
  const prompts = record.prompts;
  if (
    record.schema_id !== "corpuskit.phon-rl-prompt-artifact.v1" ||
    !Array.isArray(prompts) ||
    prompts.length < 1 ||
    prompts.length > 10_000 ||
    prompts.some(
      (prompt) =>
        typeof prompt !== "string" ||
        !prompt.trim() ||
        Array.from(prompt).length > 4_000,
    )
  )
    throw new TypeError(
      "The prompt artifact contains invalid or unbounded prompts.",
    );
  const canonical = JSON.stringify({
    prompts,
    schema_id: "corpuskit.phon-rl-prompt-artifact.v1",
  });
  if (text !== canonical)
    throw new TypeError("The prompt artifact must use canonical compact JSON.");
}

function ArtifactCard({
  artifact,
  projectId,
  highlighted,
  onRemoved,
}: {
  readonly artifact: ArtifactRecord;
  readonly projectId: string;
  readonly highlighted: boolean;
  readonly onRemoved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signed, setSigned] = useState<SignedDownload | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  async function download() {
    setBusy(true);
    setError(null);
    try {
      const bytes = await verifiedArtifactDownload(projectId, artifact);
      saveBytes(
        safeFilename(artifact.filename),
        Uint8Array.from(bytes).buffer,
        artifact.media_type || "application/octet-stream",
      );
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }
  async function presign() {
    setBusy(true);
    setError(null);
    setSigned(null);
    try {
      setSigned(await artifactsApi.sign(projectId, artifact.id, 300));
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (confirmation !== "DELETE") return;
    setBusy(true);
    setError(null);
    try {
      await artifactsApi.remove(projectId, artifact.id);
      onRemoved();
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <li
      className={highlighted ? "artifact-card is-highlighted" : "artifact-card"}
      id={`artifact-${artifact.id}`}
      tabIndex={highlighted ? -1 : undefined}
    >
      <div className="artifact-heading">
        <div>
          <span
            className={`status-pill status-${artifact.state === "active" ? "available" : "unavailable"}`}
          >
            {artifact.state}
          </span>
          <h3>{artifact.filename}</h3>
          <p>
            {artifact.kind} · {artifact.media_type || "Unknown media type"}
          </p>
        </div>
        <strong>{formatBytes(artifact.size_bytes)}</strong>
      </div>
      <dl className="inline-metadata">
        <div>
          <dt>SHA-256</dt>
          <dd className="digest">
            <abbr title={artifact.sha256}>{artifact.sha256.slice(0, 16)}…</abbr>
          </dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{new Date(artifact.created_at).toLocaleString()}</dd>
        </div>
        <div>
          <dt>Retention</dt>
          <dd>{new Date(artifact.retention_until).toLocaleString()}</dd>
        </div>
        <div>
          <dt>Run output</dt>
          <dd>{artifact.run_id ? "Yes" : "No"}</dd>
        </div>
      </dl>
      <div className="compact-actions">
        <button
          disabled={
            busy ||
            artifact.state !== "active" ||
            artifact.size_bytes > maximumArtifactBytes
          }
          onClick={() => void download()}
          type="button"
        >
          Verified download
        </button>
        <button
          disabled={busy || artifact.state !== "active"}
          onClick={() => void presign()}
          type="button"
        >
          Create 5-minute link
        </button>
        <button
          className="danger-button"
          disabled={busy || artifact.state !== "active"}
          onClick={() => setConfirming(true)}
          type="button"
        >
          Delete
        </button>
      </div>
      {artifact.size_bytes > maximumArtifactBytes ? (
        <p className="form-status">
          This artifact exceeds the browser verification cap. Use a reviewed
          streaming client; the UI will not offer an unverified download.
        </p>
      ) : null}
      {signed ? (
        <p className="signed-link">
          <a href={signed.url} rel="noreferrer" target="_blank">
            Open signed download
          </a>
          <small>Expires {new Date(signed.expires_at).toLocaleString()}</small>
        </p>
      ) : null}
      {confirming ? (
        <div
          className="delete-confirmation"
          role="group"
          aria-label={`Confirm deletion of ${artifact.filename}`}
        >
          <p>
            Deletion tombstones this immutable record. Type{" "}
            <strong>DELETE</strong> to confirm.
          </p>
          <label className="field-label" htmlFor={`delete-${artifact.id}`}>
            Confirmation
          </label>
          <input
            autoComplete="off"
            id={`delete-${artifact.id}`}
            onChange={(event) => setConfirmation(event.target.value)}
            value={confirmation}
          />
          <div className="compact-actions">
            <button
              className="danger-button"
              disabled={confirmation !== "DELETE" || busy}
              onClick={() => void remove()}
              type="button"
            >
              Confirm delete
            </button>
            <button
              onClick={() => {
                setConfirming(false);
                setConfirmation("");
              }}
              type="button"
            >
              Keep artifact
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <ErrorNotice message={error} title="Artifact action not completed" />
      ) : null}
    </li>
  );
}

function safeFilename(value: string): string {
  const normalized = value
    .normalize("NFKC")
    .replace(/[^A-Za-z0-9._-]+/gu, "-")
    .replace(/^[.-]+/u, "")
    .slice(0, 128);
  return normalized || "artifact.bin";
}
function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KiB`;
  return `${(value / 1_048_576).toFixed(1)} MiB`;
}
function ErrorNotice({
  title,
  message,
}: {
  readonly title: string;
  readonly message: string;
}) {
  return (
    <div className="error-notice" role="alert">
      <strong>{title}</strong>
      <p>{message}</p>
    </div>
  );
}
