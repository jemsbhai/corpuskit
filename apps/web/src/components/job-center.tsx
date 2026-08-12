"use client";

import Link from "next/link";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type SelectedCorpusVersion,
  useProjectContext,
} from "@/components/project-context";
import { describeRequestError } from "@/lib/api-client";
import {
  buildSupportedRunSpec,
  coreRunKinds,
  isTerminalRun,
  jobsApi,
  mergeMonotonicEvents,
  nextEventCursor,
  parseRunProgress,
  resultArtifactId,
  supportedRunKinds,
  type RunEvent,
  type RunRecord,
  type RunState,
  type CoreRunKind,
} from "@/lib/jobs";
import {
  getCurrentPrincipal,
  listAllSentences,
  type CorpusSentence,
  type ProjectRole,
} from "@/lib/projects";
import { replaysApi, type ReplayStatus } from "@/lib/replays";
import { nonEmptyLines, uniqueTokens } from "@/lib/workbench-input";

const runStates: readonly RunState[] = [
  "draft",
  "queued",
  "provisioning",
  "running",
  "cancelling",
  "cancelled",
  "succeeded",
  "failed",
];

const versionBackedRowLimits = {
  phonemize: 500,
  evaluate: 500,
  select: 2_000,
} as const satisfies Partial<Record<CoreRunKind, number>>;

export function JobCenter() {
  const projects = useProjectContext();
  const project = projects?.selectedProject ?? null;
  const [runs, setRuns] = useState<readonly RunRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [stateFilter, setStateFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [role, setRole] = useState<ProjectRole | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);
  const selected = runs.find((run) => run.id === selectedId) ?? null;

  useEffect(() => {
    const controller = new AbortController();
    void getCurrentPrincipal(controller.signal)
      .then((principal) => setRole(principal.role))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setRoleError(describeRequestError(caught));
        }
      });
    return () => controller.abort();
  }, []);

  const refresh = useCallback(async () => {
    if (!project) {
      setRuns([]);
      setSelectedId(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await jobsApi.list({
        state: stateFilter,
        kind: kindFilter,
        limit: 100,
      });
      const scoped = response.filter((run) => run.project_id === project.id);
      setRuns(scoped);
      setSelectedId((current) =>
        scoped.some((run) => run.id === current)
          ? current
          : (scoped[0]?.id ?? null),
      );
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setLoading(false);
    }
  }, [kindFilter, project, stateFilter]);

  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);

  if (!project) return <ProjectRequired />;
  return (
    <section className="workbench-shell">
      <RunBuilder
        corpusVersionError={projects?.corpusVersionError ?? false}
        corpusVersionLoading={projects?.corpusVersionLoading ?? false}
        projectId={project.id}
        projectName={project.name}
        selectedCorpusVersion={projects?.selectedCorpusVersion ?? null}
        onSubmitted={(run) => {
          setRuns((current) => [
            run,
            ...current.filter((item) => item.id !== run.id),
          ]);
          setSelectedId(run.id);
        }}
      />
      <div className="job-layout">
        <section className="workbench-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Selected project only</p>
              <h2>Durable runs</h2>
            </div>
            <button
              disabled={loading}
              onClick={() => void refresh()}
              type="button"
            >
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          <div className="form-grid two-columns">
            <div>
              <label className="field-label" htmlFor="job-state-filter">
                State
              </label>
              <select
                id="job-state-filter"
                onChange={(event) => setStateFilter(event.target.value)}
                value={stateFilter}
              >
                <option value="">All states</option>
                {runStates.map((state) => (
                  <option key={state} value={state}>
                    {state}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="field-label" htmlFor="job-kind-filter">
                Kind
              </label>
              <select
                id="job-kind-filter"
                onChange={(event) => setKindFilter(event.target.value)}
                value={kindFilter}
              >
                <option value="">All kinds</option>
                {supportedRunKinds.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {error ? (
            <ErrorNotice message={error} title="Runs unavailable" />
          ) : null}
          {!loading && !runs.length ? (
            <p className="workbench-empty">
              No matching runs exist in {project.name}.
            </p>
          ) : (
            <ul className="run-list" aria-label="Runs in selected project">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    aria-current={selectedId === run.id ? "true" : undefined}
                    className={selectedId === run.id ? "is-active" : ""}
                    onClick={() => setSelectedId(run.id)}
                    type="button"
                  >
                    <span>
                      <strong>{labelKind(run.kind)}</strong>
                      <small>
                        Attempt {run.attempt} ·{" "}
                        {new Date(run.created_at).toLocaleString()}
                      </small>
                    </span>
                    <StateBadge state={run.state} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <RunDetail
          canReplay={role !== null && role !== "viewer"}
          key={selected?.id ?? "empty"}
          onRunChanged={(run) => {
            setRuns((current) => [
              run,
              ...current.filter((item) => item.id !== run.id),
            ]);
            setSelectedId(run.id);
          }}
          replayPermissionError={roleError}
          run={selected}
        />
      </div>
    </section>
  );
}

function RunBuilder({
  projectId,
  projectName,
  selectedCorpusVersion,
  corpusVersionLoading,
  corpusVersionError,
  onSubmitted,
}: {
  readonly projectId: string;
  readonly projectName: string;
  readonly selectedCorpusVersion: SelectedCorpusVersion | null;
  readonly corpusVersionLoading: boolean;
  readonly corpusVersionError: boolean;
  readonly onSubmitted: (run: RunRecord) => void;
}) {
  const [kind, setKind] = useState<CoreRunKind>("phonemize");
  const [language, setLanguage] = useState("en-us");
  const [unit, setUnit] = useState<"phoneme" | "diphone" | "triphone">(
    "phoneme",
  );
  const [primary, setPrimary] = useState(
    "Pack my box with five dozen liquor jugs.\nSphinx of black quartz, judge my vow.",
  );
  const [secondary, setSecondary] = useState("Pack my box.\nJudge my vow.");
  const [targets, setTargets] = useState("p b t d k ɡ");
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [seed, setSeed] = useState("42");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useInlineInput, setUseInlineInput] = useState(false);
  const [versionRows, setVersionRows] = useState<readonly CorpusSentence[]>([]);
  const [versionRowsLoading, setVersionRowsLoading] = useState(false);
  const [versionRowsError, setVersionRowsError] = useState(false);
  const summaryRef = useRef<HTMLDivElement>(null);
  const versionRowLimit = versionBackedRowLimit(kind);
  const supportsVersionInput = versionRowLimit !== null;
  const usesVersionInput =
    supportsVersionInput && selectedCorpusVersion !== null && !useInlineInput;
  const versionExceedsLimit =
    usesVersionInput &&
    versionRowLimit !== null &&
    selectedCorpusVersion.version.sentence_count > versionRowLimit;
  const versionReady =
    usesVersionInput &&
    !versionExceedsLimit &&
    !corpusVersionLoading &&
    !corpusVersionError &&
    !versionRowsLoading &&
    !versionRowsError &&
    versionRows.length === selectedCorpusVersion.version.sentence_count;

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setUseInlineInput(false);
      setVersionRows([]);
      setVersionRowsError(false);
      if (!selectedCorpusVersion || versionRowLimit === null) {
        setVersionRowsLoading(false);
        return;
      }
      if (selectedCorpusVersion.version.sentence_count > versionRowLimit) {
        setVersionRowsLoading(false);
        return;
      }
      setVersionRowsLoading(true);
      void listAllSentences(
        projectId,
        selectedCorpusVersion.corpus.id,
        selectedCorpusVersion.version.id,
        selectedCorpusVersion.version.sentence_count,
        controller.signal,
      )
        .then((rows) => {
          if (active) setVersionRows(rows);
        })
        .catch((caught: unknown) => {
          if (
            active &&
            !(caught instanceof DOMException && caught.name === "AbortError")
          ) {
            setVersionRowsError(true);
          }
        })
        .finally(() => {
          if (active) setVersionRowsLoading(false);
        });
    }, 0);
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [projectId, selectedCorpusVersion, versionRowLimit]);
  function changeKind(next: CoreRunKind) {
    setKind(next);
    setUseInlineInput(false);
    if (next === "trajectory") setPrimary("p a t\nb a d\nk a t");
    else if (next === "distribution") setPrimary("p,4\nb,2\nt,0");
    else if (next === "error-rates") {
      setPrimary("the cat sat\nhello world");
      setSecondary("the cat sits\nhello word");
    }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (usesVersionInput && !versionReady) {
        if (versionExceedsLimit) {
          throw new RangeError(
            versionLimitMessage(
              kind,
              selectedCorpusVersion.version.sentence_count,
            ),
          );
        }
        throw new TypeError(
          "The selected corpus version has not been fully verified. Refresh it or use explicit inline input.",
        );
      }
      const primaryRows = usesVersionInput
        ? versionRows.map((sentence) => sentence.normalized_text)
        : nonEmptyLines(
            primary,
            kind === "phonemize" || kind === "evaluate" ? 500 : 2_000,
          );
      const secondaryRows = secondary.trim()
        ? nonEmptyLines(secondary, 500)
        : [];
      const counts = kind === "distribution" ? parseCounts(primaryRows) : [];
      const spec = buildSupportedRunSpec(kind, {
        language: usesVersionInput
          ? selectedCorpusVersion.version.language
          : language,
        unit,
        primaryRows,
        secondaryRows,
        targetUnits: uniqueTokens(targets, 256),
        counts,
        caseSensitive,
        seed: Number(seed),
      });
      const run = await jobsApi.submit(
        {
          project_id: projectId,
          corpus_version_id: usesVersionInput
            ? selectedCorpusVersion.version.id
            : null,
          kind,
          spec,
        },
        `ui-submit-${crypto.randomUUID()}`,
      );
      onSubmitted(run);
    } catch (caught) {
      setError(
        caught instanceof TypeError || caught instanceof RangeError
          ? caught.message
          : describeRequestError(caught),
      );
      queueMicrotask(() => summaryRef.current?.focus());
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="workbench-form" onSubmit={(event) => void submit(event)}>
      <div className="workbench-form-heading">
        <div>
          <p className="eyebrow">Typed builder · No arbitrary JSON</p>
          <h2>Submit a durable run</h2>
        </div>
        <span className="worker-profile">Project: {projectName}</span>
      </div>
      {error ? (
        <div
          className="error-notice"
          ref={summaryRef}
          role="alert"
          tabIndex={-1}
        >
          <strong>Run not submitted</strong>
          <p>{error}</p>
        </div>
      ) : null}
      <div className="form-grid two-columns">
        <div>
          <label className="field-label" htmlFor="job-kind">
            Supported run kind
          </label>
          <select
            id="job-kind"
            onChange={(event) => changeKind(event.target.value as CoreRunKind)}
            value={kind}
          >
            {coreRunKinds.map((item) => (
              <option key={item} value={item}>
                {labelKind(item)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="field-label" htmlFor="job-language">
            Language
          </label>
          <input
            disabled={
              usesVersionInput ||
              kind === "distribution" ||
              kind === "trajectory" ||
              kind === "error-rates"
            }
            id="job-language"
            maxLength={32}
            onChange={(event) => setLanguage(event.target.value)}
            value={
              usesVersionInput
                ? selectedCorpusVersion.version.language
                : language
            }
          />
        </div>
      </div>
      {supportsVersionInput ? (
        <fieldset className="mode-picker">
          <legend>Authoritative corpus input</legend>
          <label>
            <input
              checked={selectedCorpusVersion !== null && !useInlineInput}
              disabled={selectedCorpusVersion === null}
              name="job-corpus-source"
              onChange={() => setUseInlineInput(false)}
              type="radio"
            />{" "}
            Selected immutable version
          </label>
          <label>
            <input
              checked={
                useInlineInput ||
                (selectedCorpusVersion === null &&
                  !corpusVersionLoading &&
                  !corpusVersionError)
              }
              name="job-corpus-source"
              onChange={() => setUseInlineInput(true)}
              type="radio"
            />{" "}
            Explicit inline input (no lineage)
          </label>
          {selectedCorpusVersion ? (
            <p className="field-help">
              <strong dir="auto">{selectedCorpusVersion.corpus.name}</strong>{" "}
              version {selectedCorpusVersion.version.version_number} ·{" "}
              {selectedCorpusVersion.version.sentence_count} sentences ·{" "}
              {selectedCorpusVersion.version.language} · SHA-256{" "}
              <code>{selectedCorpusVersion.version.content_sha256}</code>
            </p>
          ) : corpusVersionLoading ? (
            <p className="field-help">Restoring the selected corpus version…</p>
          ) : (
            <p className="field-help">
              No immutable version is selected. Choose one in the{" "}
              <Link href="/projects">Project workspace</Link>, or submit
              explicit inline input without lineage.
            </p>
          )}
          {usesVersionInput && versionRowsLoading ? (
            <p className="field-help" role="status">
              Verifying every ordered sentence before enabling submission…
            </p>
          ) : null}
          {versionExceedsLimit ? (
            <p className="field-help" role="alert">
              {versionLimitMessage(
                kind,
                selectedCorpusVersion.version.sentence_count,
              )}
            </p>
          ) : null}
          {usesVersionInput && (corpusVersionError || versionRowsError) ? (
            <p className="field-help" role="alert">
              CorpusKit could not verify this exact version. Re-select it in the{" "}
              <Link href="/projects">Project workspace</Link> or choose explicit
              inline input.
            </p>
          ) : null}
          {versionReady ? (
            <p className="field-help" role="status">
              Verified {versionRows.length.toLocaleString()} ordered sentences.
              This run will carry the selected version ID and exact normalized
              content.
            </p>
          ) : null}
        </fieldset>
      ) : (
        <p className="field-help">
          This run kind uses explicit inline input and does not attach corpus
          lineage.
        </p>
      )}
      {kind !== "phonemize" &&
      kind !== "error-rates" &&
      kind !== "distribution" ? (
        <div className="form-grid two-columns">
          <div>
            <label className="field-label" htmlFor="job-unit">
              Coverage unit
            </label>
            <select
              id="job-unit"
              onChange={(event) => setUnit(event.target.value as typeof unit)}
              value={unit}
            >
              <option value="phoneme">Phoneme</option>
              <option value="diphone">Diphone</option>
              <option value="triphone">Triphone</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="job-targets">
              Explicit target units
            </label>
            <input
              className="ipa-input"
              id="job-targets"
              lang="und-fonipa"
              onChange={(event) => setTargets(event.target.value)}
              value={targets}
            />
          </div>
        </div>
      ) : kind === "distribution" ? (
        <div>
          <label className="field-label" htmlFor="job-targets">
            Distribution target units
          </label>
          <input
            className="ipa-input"
            id="job-targets"
            lang="und-fonipa"
            onChange={(event) => setTargets(event.target.value)}
            value={targets}
          />
        </div>
      ) : null}
      <label className="field-label" htmlFor="job-primary">
        {usesVersionInput
          ? "Inline input · inactive while immutable version is selected"
          : primaryLabel(kind)}
      </label>
      <textarea
        disabled={usesVersionInput}
        dir="auto"
        id="job-primary"
        onChange={(event) => setPrimary(event.target.value)}
        rows={7}
        value={primary}
      />
      {kind === "error-rates" ? (
        <>
          <label className="field-label" htmlFor="job-secondary">
            Hypotheses · matching rows
          </label>
          <textarea
            dir="auto"
            id="job-secondary"
            onChange={(event) => setSecondary(event.target.value)}
            rows={5}
            value={secondary}
          />
          <label className="check-row">
            <input
              checked={caseSensitive}
              onChange={(event) => setCaseSensitive(event.target.checked)}
              type="checkbox"
            />{" "}
            Case-sensitive comparison
          </label>
        </>
      ) : null}
      {kind === "select" ? (
        <div>
          <label className="field-label" htmlFor="job-seed">
            Replayable seed
          </label>
          <input
            id="job-seed"
            min="0"
            onChange={(event) => setSeed(event.target.value)}
            type="number"
            value={seed}
          />
        </div>
      ) : null}
      <div className="form-actions">
        <button
          className="button button-primary"
          disabled={
            busy ||
            (supportsVersionInput &&
              !useInlineInput &&
              (corpusVersionLoading ||
                corpusVersionError ||
                (selectedCorpusVersion !== null && !versionReady)))
          }
          type="submit"
        >
          {busy ? "Submitting…" : "Submit durable run"}
        </button>
        <span className="form-status">
          Submission and retry use unique idempotency keys. Advanced worker
          kinds are intentionally absent.
        </span>
      </div>
    </form>
  );
}

function RunDetail({
  run,
  onRunChanged,
  canReplay,
  replayPermissionError,
}: {
  readonly run: RunRecord | null;
  readonly onRunChanged: (run: RunRecord) => void;
  readonly canReplay: boolean;
  readonly replayPermissionError: string | null;
}) {
  const [detail, setDetail] = useState(run);
  const [events, setEvents] = useState<readonly RunEvent[]>([]);
  const [connection, setConnection] = useState<
    "connected" | "reconnecting" | "stopped"
  >("connected");
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cursorRef = useRef(0);
  const cursor = useMemo(() => nextEventCursor(events), [events]);
  useEffect(() => {
    if (!detail) return;
    cursorRef.current = 0;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function poll() {
      if (!detail || !active) return;
      try {
        const [latest, incoming] = await Promise.all([
          jobsApi.get(detail.id, controller.signal),
          jobsApi.events(detail.id, cursorRef.current, controller.signal),
        ]);
        if (!active) return;
        setDetail(latest);
        onRunChanged(latest);
        setEvents((current) => {
          const merged = mergeMonotonicEvents(current, incoming);
          cursorRef.current = nextEventCursor(merged);
          return merged;
        });
        if (isTerminalRun(latest.state)) {
          setConnection("stopped");
          return;
        }
        setConnection("connected");
        timer = setTimeout(() => void poll(), 2_000);
      } catch (caught) {
        if (
          !active ||
          (caught instanceof DOMException && caught.name === "AbortError")
        )
          return;
        setConnection("reconnecting");
        timer = setTimeout(() => void poll(), 4_000);
      }
    }
    void poll();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // A fresh effect is intentionally created only for a different run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id]);
  if (!detail)
    return (
      <section className="workbench-panel">
        <p className="workbench-empty">
          Select a run to inspect its state and ordered event stream.
        </p>
      </section>
    );
  const activeRun = detail;
  async function cancel() {
    setActionBusy(true);
    setError(null);
    try {
      const next = await jobsApi.cancel(activeRun.id);
      setDetail(next);
      onRunChanged(next);
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setActionBusy(false);
    }
  }
  async function retry() {
    setActionBusy(true);
    setError(null);
    try {
      const next = await jobsApi.retry(
        activeRun.id,
        `ui-retry-${crypto.randomUUID()}`,
      );
      setDetail(next);
      onRunChanged(next);
      setEvents([]);
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setActionBusy(false);
    }
  }
  const artifactId = resultArtifactId(detail);
  const cancellable = [
    "draft",
    "queued",
    "provisioning",
    "running",
    "cancelling",
  ].includes(detail.state);
  return (
    <section className="workbench-panel run-detail">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Attempt {detail.attempt}</p>
          <h2>{labelKind(detail.kind)}</h2>
        </div>
        <StateBadge state={detail.state} />
      </div>
      <div aria-live="polite" className="poll-status" role="status">
        <span
          className={`connection-dot is-${connection}`}
          aria-hidden="true"
        />
        {connection === "connected"
          ? `Polling after event ${cursor}`
          : connection === "reconnecting"
            ? "Connection interrupted; retrying from the last event cursor"
            : "Terminal state; polling stopped"}
      </div>
      <dl className="inline-metadata">
        <div>
          <dt>Created</dt>
          <dd>{new Date(detail.created_at).toLocaleString()}</dd>
        </div>
        <div>
          <dt>Outbox</dt>
          <dd>{detail.outbox_state}</dd>
        </div>
        <div>
          <dt>Spec digest</dt>
          <dd className="digest">
            <abbr title={detail.spec_sha256}>
              {detail.spec_sha256.slice(0, 12)}…
            </abbr>
          </dd>
        </div>
        <div>
          <dt>Failure</dt>
          <dd>{detail.failure_code ?? "None"}</dd>
        </div>
      </dl>
      <div className="compact-actions">
        <button
          disabled={actionBusy || !cancellable}
          onClick={() => void cancel()}
          type="button"
        >
          Request cancellation
        </button>
        <button
          disabled={actionBusy || !isTerminalRun(detail.state)}
          onClick={() => void retry()}
          type="button"
        >
          Retry as a new attempt
        </button>
        {artifactId ? (
          <Link
            className="button button-secondary"
            href={`/artifacts?artifact=${encodeURIComponent(artifactId)}`}
          >
            Inspect final artifact
          </Link>
        ) : null}
      </div>
      {error ? (
        <ErrorNotice message={error} title="Run action not completed" />
      ) : null}
      <ReplayPanel
        canSubmit={canReplay}
        permissionError={replayPermissionError}
        sourceRun={detail}
      />
      <h3>Monotonic event stream</h3>
      {events.length ? (
        <ol className="event-list" aria-label="Run events">
          {events.map((event) => (
            <RunEventItem event={event} key={event.sequence} />
          ))}
        </ol>
      ) : (
        <p className="workbench-empty">
          No events are available after cursor 0.
        </p>
      )}
    </section>
  );
}

function RunEventItem({ event }: { readonly event: RunEvent }) {
  const progress = parseRunProgress(event);
  const label = progress
    ? progress.phase.replaceAll("_", " ")
    : event.event_type.replaceAll("_", " ");
  const percent =
    progress !== null && progress.completed !== null && progress.total !== null
      ? Math.round((progress.completed / progress.total) * 100)
      : null;
  return (
    <li>
      <span>{event.sequence}</span>
      <div>
        <strong>{label}</strong>
        {progress ? (
          <span className="event-progress">
            Attempt {progress.activity_attempt}
            {progress.completed !== null && progress.total !== null
              ? ` · ${progress.completed} / ${progress.total} (${percent}%)`
              : ""}
            {progress.coverage !== null
              ? ` · ${Math.round(progress.coverage * 100)}% coverage`
              : ""}
            {progress.accepted_count !== null
              ? ` · ${progress.accepted_count} accepted`
              : ""}
          </span>
        ) : null}
        <time dateTime={event.occurred_at}>
          {new Date(event.occurred_at).toLocaleString()}
        </time>
      </div>
    </li>
  );
}

function ReplayPanel({
  sourceRun,
  canSubmit,
  permissionError,
}: {
  readonly sourceRun: RunRecord;
  readonly canSubmit: boolean;
  readonly permissionError: string | null;
}) {
  const [replay, setReplay] = useState<ReplayStatus | null>(null);
  const [created, setCreated] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<
    "idle" | "polling" | "reconnecting" | "stopped"
  >("idle");
  const idempotencyKey = useRef<string | null>(null);

  useEffect(() => {
    if (!replay) return;
    if (replay.lifecycle === "compared" || replay.lifecycle === "unavailable") {
      return;
    }
    const replayRunId = replay.replay_run_id;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function poll() {
      try {
        const next = await replaysApi.get(replayRunId, controller.signal);
        if (!active) return;
        setReplay(next);
        setError(null);
        if (next.lifecycle === "compared" || next.lifecycle === "unavailable") {
          setConnection("stopped");
          return;
        }
        setConnection("polling");
        timer = setTimeout(() => void poll(), 2_000);
      } catch (caught) {
        if (
          !active ||
          (caught instanceof DOMException && caught.name === "AbortError")
        ) {
          return;
        }
        setError(describeRequestError(caught));
        setConnection("reconnecting");
        timer = setTimeout(() => void poll(), 4_000);
      }
    }
    void poll();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // Polling owns the full lifecycle for one immutable replay ID.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replay?.replay_run_id]);

  if (!isTerminalRun(sourceRun.state)) return null;
  const eligible = sourceRun.state === "succeeded";
  async function submitReplay() {
    setBusy(true);
    setError(null);
    idempotencyKey.current ??= `ui-replay-${sourceRun.id}-${crypto.randomUUID()}`;
    try {
      const result = await replaysApi.submit(
        sourceRun.project_id,
        sourceRun.id,
        idempotencyKey.current,
      );
      setReplay(result.replay);
      setCreated(result.created);
      setConnection(
        result.replay.lifecycle === "compared" ||
          result.replay.lifecycle === "unavailable"
          ? "stopped"
          : "polling",
      );
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="replay-panel" aria-labelledby="replay-panel-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Server-built manifest only</p>
          <h3 id="replay-panel-heading">Replay and compare</h3>
        </div>
        {replay ? (
          <span className={`replay-lifecycle replay-${replay.lifecycle}`}>
            {replay.lifecycle}
          </span>
        ) : null}
      </div>
      <p className="boundary-note">
        Replay clones the stored recipe and provenance boundary. No parameters
        or manifest fields are accepted from this page.
      </p>
      <div className="compact-actions">
        <button
          disabled={busy || !eligible || !canSubmit || replay !== null}
          onClick={() => void submitReplay()}
          type="button"
        >
          {busy ? "Submitting replay…" : "Replay this terminal run"}
        </button>
      </div>
      {!eligible ? (
        <p className="form-status">
          Only a succeeded terminal run has a finalized source manifest to
          replay. Failed and cancelled runs remain inspectable above.
        </p>
      ) : !canSubmit ? (
        <p className="form-status">
          {permissionError ??
            "Replay submission requires an owner, admin, or editor role."}
        </p>
      ) : null}
      {error ? (
        <ErrorNotice message={error} title="Replay action not completed" />
      ) : null}
      {replay ? (
        <ReplayProjection
          connection={connection}
          created={created}
          replay={replay}
        />
      ) : null}
    </section>
  );
}

function ReplayProjection({
  replay,
  created,
  connection,
}: {
  readonly replay: ReplayStatus;
  readonly created: boolean | null;
  readonly connection: "idle" | "polling" | "reconnecting" | "stopped";
}) {
  const comparison = replay.comparison;
  return (
    <div className="replay-projection" aria-live="polite">
      <p className="success-notice" role="status">
        {created === false
          ? "The existing idempotent replay was restored."
          : "Replay accepted with one idempotency key."}{" "}
        {connection === "polling"
          ? "Comparison status is updating."
          : connection === "reconnecting"
            ? "Status polling was interrupted and will retry."
            : replay.lifecycle === "compared"
              ? "Manifest comparison is complete."
              : replay.lifecycle === "unavailable"
                ? "Comparison is unavailable for this replay."
                : "Replay status is ready."}
      </p>
      <p className="form-status">
        Source run <span className="digest">{replay.source_run_id}</span>
      </p>
      <dl className="inline-metadata replay-metadata">
        <div>
          <dt>Classification</dt>
          <dd>{replay.classification}</dd>
        </div>
        <div>
          <dt>Lifecycle</dt>
          <dd>{replay.lifecycle}</dd>
        </div>
        <div>
          <dt>Replay run</dt>
          <dd className="digest">{replay.replay_run_id}</dd>
        </div>
        <div>
          <dt>Expected manifest</dt>
          <dd className="digest">
            <abbr title={replay.expected_manifest_sha256}>
              {replay.expected_manifest_sha256.slice(0, 12)}…
            </abbr>
          </dd>
        </div>
      </dl>
      <div className="compact-actions">
        <Link
          className="button button-secondary"
          href={`/artifacts?artifact=${encodeURIComponent(replay.source_manifest_artifact_id)}`}
        >
          Inspect source manifest
        </Link>
        {replay.observed_manifest_artifact_id ? (
          <Link
            className="button button-secondary"
            href={`/artifacts?artifact=${encodeURIComponent(replay.observed_manifest_artifact_id)}`}
          >
            Inspect observed manifest
          </Link>
        ) : null}
      </div>
      {comparison ? (
        <section className="replay-comparison" aria-label="Replay comparison">
          <h4>{labelKind(comparison.verdict)}</h4>
          <dl className="comparison-summary">
            <div>
              <dt>Replay-critical inputs</dt>
              <dd>{comparison.replay_inputs_match ? "Match" : "Differ"}</dd>
            </div>
            <div>
              <dt>Ordered outputs</dt>
              <dd>{comparison.outputs_match ? "Match" : "Differ"}</dd>
            </div>
          </dl>
          {comparison.differences.length ? (
            <div>
              <strong>Different manifest fields</strong>
              <ul className="token-list">
                {comparison.differences.map((difference) => (
                  <li key={difference}>{difference}</li>
                ))}
              </ul>
            </div>
          ) : (
            <p>No replay-critical input fields differ.</p>
          )}
        </section>
      ) : (
        <p className="workbench-empty">
          The replay has not published an observed manifest comparison yet.
        </p>
      )}
    </div>
  );
}

function parseCounts(
  rows: readonly string[],
): readonly { readonly unit: string; readonly count: number }[] {
  const seen = new Set<string>();
  return rows.map((row) => {
    const [unit = "", raw = "", ...extra] = row
      .split(",")
      .map((item) => item.trim());
    const count = Number(raw);
    if (
      extra.length ||
      !unit ||
      seen.has(unit) ||
      !Number.isSafeInteger(count) ||
      count < 0
    )
      throw new TypeError(
        "Counts must use unique unit,non-negative-integer rows.",
      );
    seen.add(unit);
    return { unit, count };
  });
}
function primaryLabel(kind: CoreRunKind): string {
  if (kind === "distribution") return "Counts · unit,count per row";
  if (kind === "trajectory") return "Ordered phoneme sequences · one per row";
  if (kind === "error-rates") return "References · one per row";
  if (kind === "select") return "Candidate sentences · one per row";
  return kind === "evaluate"
    ? "Corpus sentences · one per row"
    : "Text rows to phonemize";
}

function versionBackedRowLimit(kind: CoreRunKind): number | null {
  if (kind === "phonemize" || kind === "evaluate" || kind === "select")
    return versionBackedRowLimits[kind];
  return null;
}

function versionLimitMessage(kind: CoreRunKind, sentenceCount: number): string {
  const limit = versionBackedRowLimit(kind);
  if (limit === null) return "This run kind does not accept corpus lineage.";
  return `This immutable version has ${sentenceCount.toLocaleString()} sentences, but one ${labelKind(kind)} run accepts at most ${limit.toLocaleString()}. CorpusKit will not truncate it. Import an explicit bounded derived version for one atomic run; full-version execution requires a future chunked job.`;
}

function labelKind(kind: string): string {
  return kind
    .split("-")
    .map((item) => item.charAt(0).toUpperCase() + item.slice(1))
    .join(" ");
}
function StateBadge({ state }: { readonly state: RunState }) {
  return (
    <span className={`state-badge state-${state}`}>
      <i aria-hidden="true" />
      {state}
    </span>
  );
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
function ProjectRequired() {
  return (
    <section className="workbench-shell">
      <div className="availability-card">
        <h2>Select a project first</h2>
        <p>
          Runs are always scoped to the global project picker. Create a project
          in Project Workspace if this organization has none.
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
}
