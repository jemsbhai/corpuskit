"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { useProjectContext } from "@/components/project-context";
import {
  advancedApi,
  cliTemplate,
  cliWorkflows,
  formatJson,
  labOperations,
  labTemplate,
  parseEditorObject,
  runTemplate,
  type AdvancedCapabilities,
  type CliPreview,
  type CliWorkflow,
  type DatgIndexPublication,
  type DatgLogitDeltaPreview,
  type LabOperation,
} from "@/lib/advanced";
import { describeRequestError, type JsonRecord } from "@/lib/api-client";
import {
  advancedRunKinds,
  jobsApi,
  type AdvancedRunKind,
  type RunRecord,
} from "@/lib/jobs";
import { getCurrentPrincipal, type ProjectRole } from "@/lib/projects";

export function AdvancedWorkbench() {
  const projects = useProjectContext();
  const project = projects?.selectedProject ?? null;
  const projectId = project?.id ?? null;
  const [catalog, setCatalog] = useState<AdvancedCapabilities | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [role, setRole] = useState<ProjectRole | null>(null);
  const [loading, setLoading] = useState(true);
  const [datgCatalog, setDatgCatalog] = useState<{
    readonly projectId: string;
    readonly indexes: readonly DatgIndexPublication[];
    readonly error: string | null;
  } | null>(null);
  const [datgLoadingProjectId, setDatgLoadingProjectId] = useState<
    string | null
  >(null);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      advancedApi.capabilities(controller.signal),
      getCurrentPrincipal(controller.signal),
    ])
      .then(([nextCatalog, principal]) => {
        setCatalog(nextCatalog);
        setRole(principal.role);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setCatalogError(describeRequestError(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  function refreshDatgIndexes() {
    if (!project) return;
    const projectId = project.id;
    setDatgLoadingProjectId(projectId);
    void advancedApi
      .datgIndexes(projectId)
      .then((indexes) => {
        setDatgCatalog({ projectId, indexes, error: null });
      })
      .catch((error: unknown) => {
        setDatgCatalog({
          projectId,
          indexes: [],
          error: describeRequestError(error),
        });
      })
      .finally(() => setDatgLoadingProjectId(null));
  }

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    void advancedApi
      .datgIndexes(projectId, controller.signal)
      .then((indexes) => {
        if (!controller.signal.aborted)
          setDatgCatalog({ projectId, indexes, error: null });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setDatgCatalog({
            projectId,
            indexes: [],
            error: describeRequestError(error),
          });
      });
    return () => controller.abort();
  }, [projectId]);

  const projectIndexes =
    project && datgCatalog?.projectId === project.id ? datgCatalog.indexes : [];
  const datgIndex = projectIndexes[0] ?? null;
  const datgLoading =
    project !== null &&
    (datgLoadingProjectId === project.id ||
      datgCatalog?.projectId !== project.id);
  const surfaceKey = `${project?.id ?? "no-project"}:${
    catalog ? "catalog" : "no-catalog"
  }:${datgIndex?.cache_key_sha256 ?? "no-index"}`;

  return (
    <section className="advanced-shell">
      <CapabilitySummary
        catalog={catalog}
        error={catalogError}
        loading={loading}
        role={role}
      />
      <AdvancedRunBuilder
        key={`run:${surfaceKey}`}
        catalog={catalog}
        canManage={role !== "viewer" && role !== null}
        datgIndex={datgIndex}
      />
      <BoundedLab
        key={`lab:${surfaceKey}`}
        catalog={catalog}
        datgError={
          project && datgCatalog?.projectId === project.id
            ? datgCatalog.error
            : null
        }
        datgIndex={datgIndex}
        datgLoading={datgLoading}
        onRefreshDatg={refreshDatgIndexes}
        projectId={project?.id ?? null}
      />
      <CliPreviewPanel />
    </section>
  );
}

function CapabilitySummary({
  catalog,
  error,
  loading,
  role,
}: {
  readonly catalog: AdvancedCapabilities | null;
  readonly error: string | null;
  readonly loading: boolean;
  readonly role: ProjectRole | null;
}) {
  return (
    <section className="workbench-panel advanced-summary" aria-busy={loading}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Server policy</p>
          <h2>Advanced capability gates</h2>
        </div>
        <span className="boundary-badge">Validate, then queue</span>
      </div>
      <p>
        Advanced operation routes validate and estimate without executing.{" "}
        Approved requests queue through{" "}
        <code>
          POST {catalog?.durable_run_submission_route ?? "/api/v1/runs"}
        </code>
        . Provider calls, model loading, DATG builds and generation, and Phon-RL
        training run only as durable worker jobs.
      </p>
      {error ? (
        <Notice title="Capability catalog unavailable" message={error} />
      ) : null}
      {catalog ? (
        <dl className="advanced-gates">
          <Gate
            label="Hosted models"
            count={catalog.hosted_models.length}
            detail={hostedPolicyDetail(catalog.hosted_models)}
          />
          <Gate
            label="Hugging Face repositories"
            count={catalog.huggingface_repositories.length}
            detail="external-provider worker"
          />
          <Gate
            label="Local models"
            count={catalog.local_models.length}
            detail={
              catalog.local_models.some((item) => item.allow_phon_rl_adapters)
                ? "GPU inference worker · Phon-RL adapters enabled"
                : "GPU inference worker"
            }
          />
          <Gate
            label="DATG runtimes"
            count={catalog.datg_runtimes.length}
            detail={
              catalog.datg_inspection === "configured_read_only"
                ? "read-only index inspection enabled"
                : "index inspection unavailable"
            }
          />
          <Gate
            label="Phon-RL runtimes"
            count={catalog.phon_rl_runtimes.length}
            detail="GPU training worker"
          />
        </dl>
      ) : null}
      {catalog &&
      catalog.hosted_models.length +
        catalog.huggingface_repositories.length +
        catalog.local_models.length +
        catalog.datg_runtimes.length +
        catalog.phon_rl_runtimes.length ===
        0 ? (
        <p className="boundary-note" role="status">
          No advanced worker allowlists are configured. Validation and job
          submission will fail closed until an operator enables a runtime.
        </p>
      ) : null}
      {role === "viewer" ? (
        <p className="boundary-note" role="status">
          Viewer access is read-only. You can inspect presets and use the
          bounded lab and CLI preview; an editor, admin, or owner must validate
          policy and queue a durable run.
        </p>
      ) : null}
    </section>
  );
}

function hostedPolicyDetail(
  models: AdvancedCapabilities["hosted_models"],
): string {
  const maximumDelay = Math.max(
    0,
    ...models.map((item) => item.request_delay_seconds),
  );
  return maximumDelay > 0
    ? `external-provider worker; up to ${maximumDelay} s server pacing`
    : "external-provider worker; zero server request delay";
}

function Gate({
  label,
  count,
  detail,
}: {
  readonly label: string;
  readonly count: number;
  readonly detail: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <strong>{count ? `${count} configured` : "Unavailable"}</strong>
        <span>{detail}</span>
      </dd>
    </div>
  );
}

function AdvancedRunBuilder({
  catalog,
  canManage,
  datgIndex,
}: {
  readonly catalog: AdvancedCapabilities | null;
  readonly canManage: boolean;
  readonly datgIndex: DatgIndexPublication | null;
}) {
  const projects = useProjectContext();
  const project = projects?.selectedProject ?? null;
  const [kind, setKind] = useState<AdvancedRunKind>("generate-llm");
  const [editor, setEditor] = useState(() =>
    formatJson(runTemplate("generate-llm", catalog, datgIndex)),
  );
  const [validatedEditor, setValidatedEditor] = useState<string | null>(null);
  const [validatedSpec, setValidatedSpec] = useState<JsonRecord | null>(null);
  const [externalProcessingConfirmed, setExternalProcessingConfirmed] =
    useState(false);
  const [validation, setValidation] = useState<JsonRecord | null>(null);
  const [estimate, setEstimate] = useState<JsonRecord | null>(null);
  const [run, setRun] = useState<RunRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const submissionLock = useRef(false);

  function changeKind(next: AdvancedRunKind) {
    setKind(next);
    setEditor(formatJson(runTemplate(next, catalog, datgIndex)));
    setValidatedEditor(null);
    setValidatedSpec(null);
    setExternalProcessingConfirmed(false);
    setValidation(null);
    setEstimate(null);
    setRun(null);
    setError(null);
  }

  async function validate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canManage || (kind === "generate-datg" && !datgIndex)) return;
    setBusy(true);
    setError(null);
    setValidatedEditor(null);
    setValidatedSpec(null);
    setValidation(null);
    setEstimate(null);
    try {
      const spec = effectiveRunSpec(kind, editor, externalProcessingConfirmed);
      const result = await advancedApi.validate(kind, spec);
      const cost = await advancedApi.estimate(kind, spec);
      setValidation(result);
      setEstimate(cost.estimate === "not_applicable" ? null : cost);
      setValidatedEditor(editor);
      setValidatedSpec(spec);
      queueMicrotask(() => resultRef.current?.focus());
    } catch (caught) {
      setError(editorError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (
      submissionLock.current ||
      !canManage ||
      !project ||
      validatedEditor !== editor ||
      !validation ||
      !validatedSpec
    )
      return;
    submissionLock.current = true;
    setBusy(true);
    setError(null);
    try {
      const idempotencyKey = `advanced-${kind}-${crypto.randomUUID()}`;
      const created = await jobsApi.submit(
        {
          project_id: project.id,
          corpus_version_id: null,
          kind,
          spec: validatedSpec,
        },
        idempotencyKey,
      );
      setRun(created);
      queueMicrotask(() => resultRef.current?.focus());
    } catch (caught) {
      setError(editorError(caught));
    } finally {
      submissionLock.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="workbench-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Validate, then queue</p>
          <h2>Advanced durable run</h2>
        </div>
        <span className="boundary-badge">No inline execution</span>
      </div>
      <form aria-busy={busy} onSubmit={(event) => void validate(event)}>
        <label className="field-label" htmlFor="advanced-run-kind">
          Worker operation
        </label>
        <select
          id="advanced-run-kind"
          value={kind}
          onChange={(event) =>
            changeKind(event.target.value as AdvancedRunKind)
          }
        >
          {advancedRunKinds.map((item) => (
            <option key={item} value={item}>
              {runKindLabel(item)}
            </option>
          ))}
        </select>
        <label className="field-label" htmlFor="advanced-run-spec">
          Non-secret run configuration
        </label>
        <textarea
          aria-describedby="advanced-run-help"
          className="json-editor"
          id="advanced-run-spec"
          maxLength={64 * 1024}
          required
          spellCheck={false}
          value={editor}
          onChange={(event) => {
            setEditor(event.target.value);
            setExternalProcessingConfirmed(false);
            setValidatedEditor(null);
            setValidatedSpec(null);
            setValidation(null);
            setEstimate(null);
            setRun(null);
          }}
        />
        <p className="field-help" id="advanced-run-help">
          Credentials and secret-shaped keys are rejected. Choose only
          server-published connection, model, runtime, and immutable revision
          identifiers. Hosted generation confirms external data processing.
          Fluency and perplexity analysis may include a composite ranking; it
          reuses one offline, allowlisted local-model bundle in the worker.
        </p>
        {kind === "generate-llm" ? (
          <label className="check-row">
            <input
              checked={externalProcessingConfirmed}
              onChange={(event) => {
                setExternalProcessingConfirmed(event.target.checked);
                setValidatedEditor(null);
                setValidatedSpec(null);
                setValidation(null);
                setEstimate(null);
                setRun(null);
              }}
              type="checkbox"
            />{" "}
            I understand this run sends generation prompts and target details to
            the configured external provider.
          </label>
        ) : null}
        <div className="advanced-actions">
          <button
            className="button button-primary"
            disabled={
              busy ||
              !canManage ||
              (kind === "generate-datg" && !datgIndex) ||
              (kind === "generate-llm" && !externalProcessingConfirmed)
            }
            type="submit"
          >
            {busy ? "Checking…" : "Validate policy"}
          </button>
          <button
            className="button"
            disabled={
              busy ||
              !canManage ||
              !project ||
              (kind === "generate-datg" && !datgIndex) ||
              validatedEditor !== editor ||
              !validation ||
              !validatedSpec
            }
            onClick={() => void submit()}
            type="button"
          >
            Queue durable run
          </button>
        </div>
      </form>
      {!project ? (
        <p className="boundary-note">
          Select a project before queuing. Validation remains available without
          one.
        </p>
      ) : null}
      {project && kind === "generate-datg" && !datgIndex ? (
        <p className="boundary-note" role="status">
          Build a DATG index first, then refresh the index catalog below. No
          placeholder cache key will be submitted.
        </p>
      ) : null}
      {error ? (
        <Notice title="Advanced request rejected" message={error} />
      ) : null}
      {validation ? (
        <div className="advanced-result" ref={resultRef} tabIndex={-1}>
          <h3>Policy validation passed</h3>
          <pre>{formatJson(validation)}</pre>
          {estimate ? (
            <>
              <h3>Conservative estimate</h3>
              <pre>{formatJson(estimate)}</pre>
            </>
          ) : null}
        </div>
      ) : null}
      {run ? (
        <div className="success-notice" role="status">
          <strong>{runKindLabel(kind)} queued.</strong>
          <span>
            Run {run.id} is {run.state}.{" "}
            <Link href="/jobs">Open Job Center</Link>
          </span>
        </div>
      ) : null}
    </section>
  );
}

function BoundedLab({
  catalog,
  datgError,
  datgIndex,
  datgLoading,
  onRefreshDatg,
  projectId,
}: {
  readonly catalog: AdvancedCapabilities | null;
  readonly datgError: string | null;
  readonly datgIndex: DatgIndexPublication | null;
  readonly datgLoading: boolean;
  readonly onRefreshDatg: () => void;
  readonly projectId: string | null;
}) {
  const [operation, setOperation] = useState<LabOperation>("datg-targets");
  const [editor, setEditor] = useState(() =>
    formatJson(labTemplate("datg-targets", datgIndex)),
  );
  const [result, setResult] = useState<
    JsonRecord | DatgLogitDeltaPreview | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isDatgOperation = operation.startsWith("datg-");
  const datgReady =
    !isDatgOperation ||
    (projectId !== null &&
      datgIndex !== null &&
      catalog?.datg_inspection === "configured_read_only");

  async function run(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!datgReady || datgLoading) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const request = parseEditorObject(editor);
      setResult(
        await (isDatgOperation
          ? advancedApi.lab(operation, request, projectId ?? undefined)
          : advancedApi.lab(operation, request)),
      );
    } catch (caught) {
      setError(editorError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workbench-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Bounded, stateless calculations</p>
          <h2>DATG & Phon-RL lab</h2>
        </div>
        <span className="boundary-badge">CPU only</span>
      </div>
      <p>
        Inspect a pre-provisioned DATG index or run reviewed reward and PPO
        calculations. The lab cannot build an index, generate text, load a
        model, or start training.
      </p>
      {catalog?.datg_inspection === "unavailable" && isDatgOperation ? (
        <p className="boundary-note" role="status">
          DATG inspection is unavailable because no read-only index cache is
          configured.
        </p>
      ) : null}
      {isDatgOperation && catalog?.datg_inspection !== "unavailable" ? (
        <div className="advanced-index-state" aria-live="polite">
          {datgLoading ? <p>Loading DATG index catalog…</p> : null}
          {!datgLoading && !projectId ? (
            <p>Select a project to inspect its DATG indexes.</p>
          ) : null}
          {!datgLoading && projectId && !datgIndex ? (
            <p>Build a DATG index first.</p>
          ) : null}
          {datgIndex ? (
            <p>
              Using index <code>{datgIndex.cache_key_sha256}</code> built by run{" "}
              <code>{datgIndex.build_run_id}</code>.
            </p>
          ) : null}
          <button
            className="button"
            disabled={!projectId || datgLoading}
            onClick={onRefreshDatg}
            type="button"
          >
            Refresh DATG indexes
          </button>
        </div>
      ) : null}
      {isDatgOperation && datgError ? (
        <Notice title="DATG index catalog unavailable" message={datgError} />
      ) : null}
      <form aria-busy={busy} onSubmit={(event) => void run(event)}>
        <label className="field-label" htmlFor="advanced-lab-operation">
          Lab operation
        </label>
        <select
          id="advanced-lab-operation"
          value={operation}
          onChange={(event) => {
            const next = event.target.value as LabOperation;
            setOperation(next);
            setEditor(formatJson(labTemplate(next, datgIndex)));
            setResult(null);
            setError(null);
          }}
        >
          {labOperations.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <label className="field-label" htmlFor="advanced-lab-request">
          Bounded request
        </label>
        <textarea
          className="json-editor compact"
          id="advanced-lab-request"
          maxLength={64 * 1024}
          required
          spellCheck={false}
          value={editor}
          onChange={(event) => setEditor(event.target.value)}
        />
        <button
          className="button button-primary"
          disabled={busy || datgLoading || !datgReady}
          type="submit"
        >
          {busy ? "Running…" : "Run bounded calculation"}
        </button>
      </form>
      {error ? <Notice title="Lab request rejected" message={error} /> : null}
      {result && isDatgLogitDeltaPreview(result) ? (
        <DatgLogitPreviewVisualization preview={result} />
      ) : result ? (
        <div className="advanced-result" aria-live="polite">
          <h3>Lab result</h3>
          <pre>{formatJson(result)}</pre>
        </div>
      ) : null}
    </section>
  );
}

function DatgLogitPreviewVisualization({
  preview,
}: {
  readonly preview: DatgLogitDeltaPreview;
}) {
  const attribute = new Set(preview.attribute_token_ids);
  const antiAttribute = new Set(preview.anti_attribute_token_ids);
  const cells = preview.original_logits.flatMap((row, rowIndex) =>
    row.map((before, tokenId) => {
      const isAttribute = attribute.has(tokenId);
      const isAntiAttribute = antiAttribute.has(tokenId);
      return {
        rowIndex,
        tokenId,
        before,
        delta: preview.delta_logits[rowIndex]![tokenId]!,
        after: preview.modified_logits[rowIndex]![tokenId]!,
        classification:
          isAttribute && isAntiAttribute
            ? "Attribute and anti-attribute"
            : isAttribute
              ? "Attribute"
              : isAntiAttribute
                ? "Anti-attribute"
                : "Unclassified",
        classificationClass:
          isAttribute && isAntiAttribute
            ? "both"
            : isAttribute
              ? "attribute"
              : isAntiAttribute
                ? "anti-attribute"
                : "unclassified",
      };
    }),
  );
  return (
    <section
      aria-labelledby="datg-logit-preview-heading"
      aria-live="polite"
      className="advanced-result"
    >
      <h3 id="datg-logit-preview-heading">Logit delta preview</h3>
      <p>
        This read-only calculation used the authorized cached index. It did not
        load a model, use a network, generate text, or queue a run.
      </p>
      <dl className="logit-preview-summary">
        <div>
          <dt>Attribute tokens</dt>
          <dd>{preview.attribute_token_ids.length}</dd>
        </div>
        <div>
          <dt>Anti-attribute tokens</dt>
          <dd>{preview.anti_attribute_token_ids.length}</dd>
        </div>
        <div>
          <dt>Matrix</dt>
          <dd>
            {preview.original_logits.length} by{" "}
            {preview.original_logits[0]!.length}
          </dd>
        </div>
      </dl>
      <div
        aria-label="Scrollable DATG logit delta table"
        className="logit-preview-table"
        role="region"
        tabIndex={0}
      >
        <table>
          <caption>
            Before, additive delta, and after values for each batch row and
            token ID
          </caption>
          <thead>
            <tr>
              <th scope="col">Batch row</th>
              <th scope="col">Token ID</th>
              <th scope="col">Classification</th>
              <th scope="col">Before</th>
              <th scope="col">Delta</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          <tbody>
            {cells.map((cell) => (
              <tr key={`${cell.rowIndex}:${cell.tokenId}`}>
                <th scope="row">{cell.rowIndex + 1}</th>
                <td>{cell.tokenId}</td>
                <td>
                  <span
                    className={`logit-classification logit-${cell.classificationClass}`}
                  >
                    {cell.classification}
                  </span>
                </td>
                <td>{String(cell.before)}</td>
                <td>{String(cell.delta)}</td>
                <td>{String(cell.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function isDatgLogitDeltaPreview(
  value: JsonRecord | DatgLogitDeltaPreview,
): value is DatgLogitDeltaPreview {
  return value.schema_id === "corpuskit.datg-logit-delta-preview.v1";
}

function CliPreviewPanel() {
  const [workflow, setWorkflow] = useState<CliWorkflow>("inventory");
  const [editor, setEditor] = useState(() =>
    formatJson(cliTemplate("inventory")),
  );
  const [shell, setShell] = useState<"posix" | "powershell">("posix");
  const [preview, setPreview] = useState<CliPreview | null>(null);
  const [copyState, setCopyState] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function generate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setCopyState(null);
    try {
      setPreview(await advancedApi.cli(parseEditorObject(editor)));
    } catch (caught) {
      setPreview(null);
      setError(editorError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!preview) return;
    const command =
      shell === "posix" ? preview.posix_command : preview.powershell_command;
    try {
      await navigator.clipboard.writeText(command);
      setCopyState("Command copied.");
    } catch {
      setCopyState("Copy is unavailable. Select the command text manually.");
    }
  }

  return (
    <section className="workbench-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Preview, never execute</p>
          <h2>CorpusGen CLI parity</h2>
        </div>
        <span className="boundary-badge">Shell-safe argv</span>
      </div>
      <p>
        Generate a copyable command for inventory, evaluation, selection, or
        generation. CorpusKit quotes the preview but never invokes a shell.
      </p>
      <form aria-busy={busy} onSubmit={(event) => void generate(event)}>
        <label className="field-label" htmlFor="cli-preview-workflow">
          Workflow
        </label>
        <select
          id="cli-preview-workflow"
          value={workflow}
          onChange={(event) => {
            const next = event.target.value as CliWorkflow;
            setWorkflow(next);
            setEditor(formatJson(cliTemplate(next)));
            setPreview(null);
            setError(null);
          }}
        >
          {cliWorkflows.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        <label className="field-label" htmlFor="cli-preview-request">
          Preview options
        </label>
        <textarea
          className="json-editor compact"
          id="cli-preview-request"
          maxLength={64 * 1024}
          required
          spellCheck={false}
          value={editor}
          onChange={(event) => setEditor(event.target.value)}
        />
        <button className="button button-primary" disabled={busy} type="submit">
          {busy ? "Generating…" : "Generate CLI preview"}
        </button>
      </form>
      {error ? <Notice title="Preview rejected" message={error} /> : null}
      {preview ? (
        <div className="cli-preview-result" aria-live="polite">
          <div className="form-grid two-columns">
            <div>
              <label className="field-label" htmlFor="cli-preview-shell">
                Shell display
              </label>
              <select
                id="cli-preview-shell"
                value={shell}
                onChange={(event) =>
                  setShell(event.target.value as "posix" | "powershell")
                }
              >
                <option value="posix">POSIX</option>
                <option value="powershell">PowerShell</option>
              </select>
            </div>
            <div className="copy-action">
              <button
                className="button"
                onClick={() => void copy()}
                type="button"
              >
                Copy command
              </button>
            </div>
          </div>
          <pre tabIndex={0}>
            {shell === "posix"
              ? preview.posix_command
              : preview.powershell_command}
          </pre>
          <p className="field-help">
            Reproducibility: {preview.reproducibility.replaceAll("_", " ")}.
          </p>
          {preview.warnings.length ? (
            <ul>
              {preview.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
          {copyState ? <p role="status">{copyState}</p> : null}
        </div>
      ) : null}
    </section>
  );
}

function Notice({
  title,
  message,
}: {
  readonly title: string;
  readonly message: string;
}) {
  return (
    <div className="error-notice" role="alert">
      <strong>{title}</strong>
      <span>{message}</span>
    </div>
  );
}

function editorError(error: unknown): string {
  if (error instanceof TypeError) return error.message;
  return describeRequestError(error);
}

function effectiveRunSpec(
  kind: AdvancedRunKind,
  editor: string,
  externalProcessingConfirmed: boolean,
): JsonRecord {
  const spec = parseEditorObject(editor);
  if (kind !== "generate-llm") return spec;
  if (Object.hasOwn(spec, "external_processing_confirmed")) {
    throw new TypeError(
      "External-processing consent must use the dedicated confirmation control.",
    );
  }
  if (!externalProcessingConfirmed) {
    throw new TypeError(
      "Confirm external processing before validating hosted generation.",
    );
  }
  return { ...spec, external_processing_confirmed: true };
}

function runKindLabel(kind: AdvancedRunKind): string {
  const labels: Record<AdvancedRunKind, string> = {
    "generate-repository": "Hugging Face repository generation",
    "generate-llm": "Hosted LLM generation",
    "generate-local": "Local model generation",
    perplexity: "Fluency & perplexity analysis",
    "build-datg-index": "Build DATG index",
    "generate-datg": "DATG-guided generation",
    "train-phon-rl": "Phon-RL training",
  };
  return labels[kind];
}
