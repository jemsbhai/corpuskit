"use client";

import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  analysisApi,
  AnalysisApiError,
  AnalysisContractError,
  type CoverageTrajectory,
  type ErrorRateResult,
  type EspeakMapping,
  type Inventory,
  type LanguageSummary,
  type PhonologyStatus,
  type RateValue,
} from "@/lib/analysis";
import type {
  CoverageUnit,
  DistributionMetrics,
  TextQualityMetrics,
} from "@/lib/evaluation";

const tabs = [
  { id: "inventory", label: "Inventory explorer" },
  { id: "distribution", label: "Distribution" },
  { id: "quality", label: "Text quality" },
  { id: "errors", label: "Error rates" },
  { id: "trajectory", label: "Coverage trajectory" },
] as const;

type TabId = (typeof tabs)[number]["id"];

function lines(value: string): string[] {
  return value
    .split(/\r?\n/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

function tokens(value: string): string[] {
  return value
    .split(/[,\s]+/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

function uniqueTokens(value: string): string[] {
  return Array.from(new Set(tokens(value)));
}

function tokenLines(value: string): string[][] {
  return lines(value).map(tokens);
}

function readPairs(
  value: string,
  integerValues = false,
): { unit: string; value: number }[] | null {
  const parsed = lines(value).map((row) => {
    const [unit, rawValue, ...extra] = row
      .split(",")
      .map((part) => part.trim());
    const count = Number(rawValue);
    return unit &&
      rawValue &&
      extra.length === 0 &&
      Number.isFinite(count) &&
      count >= 0 &&
      (!integerValues || Number.isInteger(count))
      ? { unit, value: count }
      : null;
  });
  const units = parsed.flatMap((item) => (item ? [item.unit] : []));
  return parsed.every((item) => item !== null) &&
    new Set(units).size === units.length
    ? (parsed as { unit: string; value: number }[])
    : null;
}

function friendlyError(error: unknown): {
  message: string;
  reference: string | null;
} {
  if (error instanceof AnalysisApiError) {
    return { message: error.message, reference: error.requestId };
  }
  if (error instanceof AnalysisContractError) {
    return {
      message:
        "The API response did not match the supported contract. No result was shown.",
      reference: null,
    };
  }
  return {
    message:
      "The analysis API is unavailable. No result was computed or substituted.",
    reference: null,
  };
}

function downloadJson(filename: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function ErrorNotice({
  error,
}: {
  error: { message: string; reference: string | null };
}) {
  return (
    <div className="error-notice" role="alert">
      <strong>Request not completed</strong>
      <p>{error.message}</p>
      {error.reference ? (
        <small>Request reference: {error.reference}</small>
      ) : null}
    </div>
  );
}

function PendingNotice({ children }: { children: ReactNode }) {
  return (
    <p className="loading-notice" role="status">
      <i aria-hidden="true" /> {children}
    </p>
  );
}

function ResultHeader({
  title,
  onDownload,
}: {
  title: string;
  onDownload: () => void;
}) {
  return (
    <div className="analysis-result-heading" tabIndex={-1}>
      <div>
        <p className="eyebrow">Live API result</p>
        <h3>{title}</h3>
      </div>
      <button className="text-button" onClick={onDownload} type="button">
        Download JSON
      </button>
    </div>
  );
}

export function InventoryAnalysisWorkbench() {
  const [activeTab, setActiveTab] = useState<TabId>("inventory");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function selectTab(index: number) {
    const normalized = (index + tabs.length) % tabs.length;
    setActiveTab(tabs[normalized]!.id);
    tabRefs.current[normalized]?.focus();
  }

  function handleTabs(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === "ArrowRight") selectTab(index + 1);
    else if (event.key === "ArrowLeft") selectTab(index - 1);
    else if (event.key === "Home") selectTab(0);
    else if (event.key === "End") selectTab(tabs.length - 1);
    else return;
    event.preventDefault();
  }

  return (
    <section
      className="analysis-workbench"
      aria-label="Inventory and analysis workbench"
    >
      <div
        className="analysis-tabs"
        role="tablist"
        aria-label="Workbench tools"
      >
        {tabs.map((tab, index) => (
          <button
            aria-controls={`panel-${tab.id}`}
            aria-selected={activeTab === tab.id}
            className={
              activeTab === tab.id ? "analysis-tab is-active" : "analysis-tab"
            }
            id={`tab-${tab.id}`}
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            onKeyDown={(event) => handleTabs(event, index)}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            role="tab"
            tabIndex={activeTab === tab.id ? 0 : -1}
            type="button"
          >
            <span>{String(index + 1).padStart(2, "0")}</span>
            {tab.label}
          </button>
        ))}
      </div>
      <div
        aria-labelledby={`tab-${activeTab}`}
        className="analysis-panel"
        id={`panel-${activeTab}`}
        role="tabpanel"
        tabIndex={0}
      >
        {activeTab === "inventory" ? <InventoryExplorer /> : null}
        {activeTab === "distribution" ? <DistributionLab /> : null}
        {activeTab === "quality" ? <QualityLab /> : null}
        {activeTab === "errors" ? <ErrorRateLab /> : null}
        {activeTab === "trajectory" ? <TrajectoryLab /> : null}
      </div>
    </section>
  );
}

function InventoryExplorer() {
  const [status, setStatus] = useState<PhonologyStatus | null>(null);
  const [statusError, setStatusError] = useState<ReturnType<
    typeof friendlyError
  > | null>(null);
  const [search, setSearch] = useState("");
  const [languages, setLanguages] = useState<LanguageSummary[]>([]);
  const [mappings, setMappings] = useState<EspeakMapping[]>([]);
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [source, setSource] = useState("");
  const [useUnion, setUseUnion] = useState(false);
  const [segmentQuery, setSegmentQuery] = useState("");
  const [segmentClass, setSegmentClass] = useState("all");
  const [marginalOnly, setMarginalOnly] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(
    null,
  );
  const resultRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    analysisApi
      .phonologyStatus(controller.signal)
      .then(setStatus)
      .catch((caught: unknown) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setStatusError(friendlyError(caught));
        }
      });
    return () => controller.abort();
  }, []);

  async function runSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!search.trim()) {
      setError({
        message: "Enter a language name, ISO code, or eSpeak code.",
        reference: null,
      });
      return;
    }
    setPending(true);
    setError(null);
    setInventory(null);
    try {
      const [languagePage, mappingPage] = await Promise.all([
        analysisApi.languages(search.trim()),
        analysisApi.espeakMappings(search.trim()),
      ]);
      setLanguages(languagePage.items);
      setMappings(mappingPage.items);
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setPending(false);
    }
  }

  async function loadInventory(
    identifier: string,
    requestedSource = "",
    union = useUnion,
  ) {
    setPending(true);
    setError(null);
    setInventory(null);
    try {
      const next = await analysisApi.inventory(
        identifier,
        requestedSource || undefined,
        union,
      );
      setInventory(next);
      setSource(next.source);
      requestAnimationFrame(() => resultRef.current?.focus());
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setPending(false);
    }
  }

  if (!status && !statusError)
    return <PendingNotice>Checking local PHOIBLE availability…</PendingNotice>;
  if (statusError) return <ErrorNotice error={statusError} />;
  if (status && !status.cache_available) {
    return (
      <div className="cache-empty" role="status">
        <span aria-hidden="true">/ɸ/</span>
        <div>
          <h2>PHOIBLE data is not provisioned</h2>
          <p>
            Inventory search is safely disabled. This screen never downloads
            datasets or changes the server. Ask an administrator to provision
            the PHOIBLE cache, then reload.
          </p>
          <p>
            eSpeak evaluation can still be used in the{" "}
            <a href="/evaluate">Evaluation Studio</a>.
          </p>
        </div>
      </div>
    );
  }

  const visibleSegments = (inventory?.segments ?? []).filter((segment) => {
    const queryMatches =
      !segmentQuery.trim() ||
      segment.phoneme
        .toLocaleLowerCase()
        .includes(segmentQuery.trim().toLocaleLowerCase()) ||
      segment.features.some((feature) =>
        feature.name
          .toLocaleLowerCase()
          .includes(segmentQuery.trim().toLocaleLowerCase()),
      );
    return (
      queryMatches &&
      (segmentClass === "all" || segment.segment_class === segmentClass) &&
      (!marginalOnly || segment.marginal)
    );
  });

  return (
    <div className="inventory-explorer">
      <header className="tool-heading">
        <div>
          <p className="eyebrow">Provisioned data only</p>
          <h2>Language &amp; segment explorer</h2>
        </div>
        {status?.stats ? (
          <p className="inventory-stats">
            <strong>{status.stats.language_count.toLocaleString()}</strong>{" "}
            languages ·{" "}
            <strong>{status.stats.inventory_count.toLocaleString()}</strong>{" "}
            inventories
          </p>
        ) : null}
      </header>

      <form className="inventory-search" onSubmit={runSearch} role="search">
        <div>
          <label className="field-label" htmlFor="inventory-search">
            Language name or code
          </label>
          <input
            id="inventory-search"
            maxLength={100}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="English, eng, or en-us"
            value={search}
          />
        </div>
        <button
          className="button button-primary"
          disabled={pending}
          type="submit"
        >
          {pending ? "Searching…" : "Search languages"}
        </button>
      </form>

      <div aria-live="polite" className="analysis-announcements">
        {pending ? (
          <PendingNotice>Requesting live phonology data…</PendingNotice>
        ) : null}
        {error ? <ErrorNotice error={error} /> : null}
      </div>

      {!pending && (languages.length || mappings.length) ? (
        <div className="search-results-grid">
          <section aria-labelledby="phoible-results-title">
            <h3 id="phoible-results-title">PHOIBLE languages</h3>
            {languages.length ? (
              <ul className="language-results">
                {languages.map((language) => (
                  <li key={`${language.iso639_3}-${language.glottocode}`}>
                    <div>
                      <strong>{language.language_name}</strong>
                      <small>
                        {language.iso639_3} · {language.glottocode} ·{" "}
                        {language.inventory_count} inventories
                      </small>
                    </div>
                    <button
                      className="text-button"
                      onClick={() => void loadInventory(language.iso639_3)}
                      type="button"
                    >
                      Browse
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted-copy">
                No PHOIBLE languages matched this query.
              </p>
            )}
          </section>
          <section aria-labelledby="espeak-results-title">
            <h3 id="espeak-results-title">eSpeak mappings</h3>
            {mappings.length ? (
              <table className="compact-table">
                <caption className="sr-only">
                  Matching eSpeak to ISO 639-3 mappings
                </caption>
                <thead>
                  <tr>
                    <th scope="col">eSpeak</th>
                    <th scope="col">ISO 639-3</th>
                    <th scope="col">
                      <span className="sr-only">Action</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {mappings.map((mapping) => (
                    <tr key={`${mapping.espeak_code}-${mapping.iso639_3}`}>
                      <td>
                        <code>{mapping.espeak_code}</code>
                      </td>
                      <td>
                        <code>{mapping.iso639_3}</code>
                      </td>
                      <td>
                        <button
                          className="table-action"
                          onClick={() => void loadInventory(mapping.iso639_3)}
                          type="button"
                        >
                          Browse
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted-copy">
                No eSpeak mappings matched this query.
              </p>
            )}
          </section>
        </div>
      ) : null}

      {inventory ? (
        <section className="inventory-result" aria-labelledby="inventory-title">
          <div
            className="analysis-result-heading"
            ref={resultRef}
            tabIndex={-1}
          >
            <div>
              <p className="eyebrow">Live inventory · {inventory.source}</p>
              <h3 id="inventory-title">{inventory.language_name}</h3>
              <p>
                {inventory.iso639_3} · {inventory.glottocode}
                {inventory.specific_dialect
                  ? ` · ${inventory.specific_dialect}`
                  : ""}
              </p>
            </div>
            <button
              className="text-button"
              onClick={() =>
                downloadJson(`${inventory.iso639_3}-inventory.json`, inventory)
              }
              type="button"
            >
              Download JSON
            </button>
          </div>
          <dl className="inventory-counts">
            <div>
              <dt>Total segments</dt>
              <dd>{inventory.size}</dd>
            </div>
            <div>
              <dt>Consonants</dt>
              <dd>{inventory.consonant_count}</dd>
            </div>
            <div>
              <dt>Vowels</dt>
              <dd>{inventory.vowel_count}</dd>
            </div>
            <div>
              <dt>Tones</dt>
              <dd>{inventory.tone_count}</dd>
            </div>
          </dl>
          <details className="inventory-options">
            <summary>Inventory source options</summary>
            <div>
              <label className="field-label" htmlFor="inventory-source">
                Source identifier
              </label>
              <input
                id="inventory-source"
                onChange={(event) => setSource(event.target.value)}
                value={source}
              />
              <label className="check-label">
                <input
                  checked={useUnion}
                  onChange={(event) => setUseUnion(event.target.checked)}
                  type="checkbox"
                />
                Union all inventories for this language
              </label>
              <button
                className="text-button"
                onClick={() =>
                  void loadInventory(inventory.iso639_3, source, useUnion)
                }
                type="button"
              >
                Reload inventory
              </button>
            </div>
          </details>
          <div className="segment-toolbar">
            <div>
              <label className="field-label" htmlFor="segment-filter">
                Filter phoneme or feature
              </label>
              <input
                id="segment-filter"
                onChange={(event) => setSegmentQuery(event.target.value)}
                value={segmentQuery}
              />
            </div>
            <div>
              <label className="field-label" htmlFor="segment-class">
                Segment class
              </label>
              <select
                id="segment-class"
                onChange={(event) => setSegmentClass(event.target.value)}
                value={segmentClass}
              >
                <option value="all">All classes</option>
                <option value="consonant">Consonants</option>
                <option value="vowel">Vowels</option>
                <option value="tone">Tones</option>
              </select>
            </div>
            <label className="check-label segment-check">
              <input
                checked={marginalOnly}
                onChange={(event) => setMarginalOnly(event.target.checked)}
                type="checkbox"
              />
              Marginal only
            </label>
          </div>
          <p className="result-count" aria-live="polite">
            Showing {visibleSegments.length} of {inventory.segments.length}{" "}
            segments.
          </p>
          <div
            className="light-table-scroller"
            role="region"
            aria-label="Scrollable segment inventory"
            tabIndex={0}
          >
            <table className="segment-table">
              <caption className="sr-only">
                Segments in the selected PHOIBLE inventory
              </caption>
              <thead>
                <tr>
                  <th scope="col">Phoneme</th>
                  <th scope="col">Class</th>
                  <th scope="col">Status</th>
                  <th scope="col">Allophones</th>
                  <th scope="col">Features</th>
                </tr>
              </thead>
              <tbody>
                {visibleSegments.map((segment) => (
                  <tr key={`${segment.glyph_id}-${segment.phoneme}`}>
                    <th scope="row" className="phoneme-cell">
                      {segment.phoneme}
                    </th>
                    <td>{segment.segment_class}</td>
                    <td>{segment.marginal ? "Marginal" : "Core"}</td>
                    <td>{segment.allophones.join(", ") || "—"}</td>
                    <td>
                      <details>
                        <summary>{segment.features.length} features</summary>
                        <ul className="feature-list">
                          {segment.features.map((feature) => (
                            <li key={feature.name}>
                              <code>
                                {feature.value}
                                {feature.name}
                              </code>
                            </li>
                          ))}
                        </ul>
                      </details>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {visibleSegments.length === 0 ? (
            <p className="empty-inline">No segments match these filters.</p>
          ) : null}
        </section>
      ) : !pending && languages.length === 0 && mappings.length === 0 ? (
        <p className="tool-empty">
          Search for a language to inspect provisioned PHOIBLE data and eSpeak
          mappings.
        </p>
      ) : null}
    </div>
  );
}

function AnalysisShell({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="analysis-lab">
      <header className="tool-heading">
        <div>
          <p className="eyebrow">Focused diagnostic</p>
          <h2>{title}</h2>
        </div>
        <p>{description}</p>
      </header>
      {children}
    </div>
  );
}

function DistributionLab() {
  const [countText, setCountText] = useState("");
  const [targets, setTargets] = useState("");
  const [references, setReferences] = useState("");
  const [result, setResult] = useState<DistributionMetrics | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(
    null,
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const counts = readPairs(countText, true);
    const reference = references.trim() ? readPairs(references) : [];
    if (!counts?.length || reference === null) {
      setError({
        message: "Use one non-negative unit,value pair per line.",
        reference: null,
      });
      return;
    }
    const targetUnits = uniqueTokens(targets);
    if (!targetUnits.length) {
      setError({ message: "Add at least one target unit.", reference: null });
      return;
    }
    setPending(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await analysisApi.distribution({
          counts: counts.map(({ unit, value }) => ({ unit, count: value })),
          target_units: targetUnits,
          reference_distribution: reference.length
            ? reference.map(({ unit, value }) => ({ unit, weight: value }))
            : null,
        }),
      );
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <AnalysisShell
      title="Distribution balance"
      description="Measure how evenly observed units occupy a target inventory."
    >
      <form className="lab-form" onSubmit={submit}>
        <div className="lab-fields two-columns">
          <div>
            <label className="field-label" htmlFor="distribution-counts">
              Observed counts <span>unit,count per line</span>
            </label>
            <textarea
              id="distribution-counts"
              onChange={(e) => setCountText(e.target.value)}
              placeholder={"p,12\nb,8\nt,5"}
              value={countText}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="distribution-targets">
              Target units <span>space or comma separated</span>
            </label>
            <textarea
              id="distribution-targets"
              onChange={(e) => setTargets(e.target.value)}
              placeholder="p, b, t, d, k"
              value={targets}
            />
          </div>
        </div>
        <details className="optional-input">
          <summary>Optional reference distribution</summary>
          <label className="field-label" htmlFor="distribution-reference">
            Reference weights <span>unit,weight per line</span>
          </label>
          <textarea
            id="distribution-reference"
            onChange={(e) => setReferences(e.target.value)}
            value={references}
          />
        </details>
        <button
          className="button button-primary"
          disabled={pending}
          type="submit"
        >
          {pending ? "Analyzing…" : "Analyze distribution"}
        </button>
      </form>
      <LabState pending={pending} error={error} />
      {result ? <DistributionResult result={result} /> : <ToolEmpty />}
    </AnalysisShell>
  );
}

function DistributionResult({ result }: { result: DistributionMetrics }) {
  const metrics = [
    ["Normalized entropy", result.normalized_entropy],
    ["JSD vs uniform", result.jsd_uniform],
    ["Coefficient of variation", result.coefficient_of_variation],
    ["PCD uniform", result.pcd_uniform],
  ] as const;
  return (
    <section className="lab-result" aria-label="Distribution result">
      <ResultHeader
        title="Balance profile"
        onDownload={() => downloadJson("distribution-analysis.json", result)}
      />
      <div className="metric-grid">
        {metrics.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value.toFixed(3)}</strong>
            <i
              aria-hidden="true"
              style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }}
            />
          </div>
        ))}
      </div>
      <dl className="result-details">
        <div>
          <dt>Entropy</dt>
          <dd>{result.entropy.toFixed(3)}</dd>
        </div>
        <div>
          <dt>Count range</dt>
          <dd>
            {result.min_count}–{result.max_count}
          </dd>
        </div>
        <div>
          <dt>Zero-count units</dt>
          <dd>{result.zero_count}</dd>
        </div>
        <div>
          <dt>Reference JSD</dt>
          <dd>{result.jsd_reference?.toFixed(3) ?? "Not computed"}</dd>
        </div>
      </dl>
    </section>
  );
}

function QualityLab() {
  const [sentences, setSentences] = useState("");
  const [sequences, setSequences] = useState("");
  const [result, setResult] = useState<TextQualityMetrics | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(
    null,
  );
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const sentenceRows = lines(sentences);
    const phonemes = tokenLines(sequences);
    if (!sentenceRows.length || sentenceRows.length !== phonemes.length) {
      setError({
        message: "Add matching non-empty sentence and phoneme-sequence lines.",
        reference: null,
      });
      return;
    }
    setPending(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await analysisApi.textQuality({
          sentences: sentenceRows,
          phoneme_sequences: phonemes,
        }),
      );
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setPending(false);
    }
  }
  return (
    <AnalysisShell
      title="Text quality"
      description="Review length, vocabulary, and readability without hiding unavailable scores."
    >
      <PairedTextForm
        leftId="quality-sentences"
        leftLabel="Sentences"
        leftValue={sentences}
        onLeft={setSentences}
        rightId="quality-phonemes"
        rightLabel="Phoneme sequences"
        rightValue={sequences}
        onRight={setSequences}
        onSubmit={submit}
        pending={pending}
        button="Analyze text quality"
      />
      <LabState pending={pending} error={error} />
      {result ? <QualityResult result={result} /> : <ToolEmpty />}
    </AnalysisShell>
  );
}

function QualityResult({ result }: { result: TextQualityMetrics }) {
  return (
    <section className="lab-result" aria-label="Text quality result">
      <ResultHeader
        title="Corpus text profile"
        onDownload={() => downloadJson("text-quality-analysis.json", result)}
      />
      <dl className="large-metric-grid">
        <div>
          <dt>Total words</dt>
          <dd>{result.total_words}</dd>
        </div>
        <div>
          <dt>Unique words</dt>
          <dd>{result.unique_words}</dd>
        </div>
        <div>
          <dt>Type–token ratio</dt>
          <dd>{result.type_token_ratio.toFixed(3)}</dd>
        </div>
        <div>
          <dt>Hapax ratio</dt>
          <dd>{result.hapax_ratio.toFixed(3)}</dd>
        </div>
      </dl>
      <table className="compact-table metrics-table">
        <caption>Sentence length summary</caption>
        <thead>
          <tr>
            <th scope="col">Measure</th>
            <th scope="col">Mean</th>
            <th scope="col">Median</th>
            <th scope="col">Minimum</th>
            <th scope="col">Maximum</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Words</th>
            <td>{result.sentence_length_words_mean.toFixed(2)}</td>
            <td>{result.sentence_length_words_median.toFixed(2)}</td>
            <td>{result.sentence_length_words_min}</td>
            <td>{result.sentence_length_words_max}</td>
          </tr>
          <tr>
            <th scope="row">Phonemes</th>
            <td>{result.sentence_length_phonemes_mean.toFixed(2)}</td>
            <td>{result.sentence_length_phonemes_median.toFixed(2)}</td>
            <td>{result.sentence_length_phonemes_min}</td>
            <td>{result.sentence_length_phonemes_max}</td>
          </tr>
        </tbody>
      </table>
      <p className="result-footnote">
        Flesch reading ease:{" "}
        {result.flesch_reading_ease?.toFixed(1) ?? "Not computed"} ·
        Flesch–Kincaid grade:{" "}
        {result.flesch_kincaid_grade?.toFixed(1) ?? "Not computed"}
      </p>
    </section>
  );
}

function ErrorRateLab() {
  const [references, setReferences] = useState("");
  const [hypotheses, setHypotheses] = useState("");
  const [refPhonemes, setRefPhonemes] = useState("");
  const [hypPhonemes, setHypPhonemes] = useState("");
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [result, setResult] = useState<ErrorRateResult | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(
    null,
  );
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const refs = lines(references);
    const hyps = lines(hypotheses);
    if (!refs.length || refs.length !== hyps.length) {
      setError({
        message:
          "Add the same number of non-empty reference and hypothesis lines.",
        reference: null,
      });
      return;
    }
    const refTokens = refPhonemes.trim() ? tokenLines(refPhonemes) : null;
    const hypTokens = hypPhonemes.trim() ? tokenLines(hypPhonemes) : null;
    if (
      (refTokens === null) !== (hypTokens === null) ||
      (refTokens &&
        (refTokens.length !== refs.length || hypTokens?.length !== refs.length))
    ) {
      setError({
        message:
          "Provide both phoneme columns with one line for each text pair, or leave both empty.",
        reference: null,
      });
      return;
    }
    setPending(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await analysisApi.errorRates({
          references: refs,
          hypotheses: hyps,
          reference_phonemes: refTokens,
          hypothesis_phonemes: hypTokens,
          case_sensitive: caseSensitive,
        }),
      );
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setPending(false);
    }
  }
  return (
    <AnalysisShell
      title="Error rates"
      description="Compare reference and hypothesis text with explicit finite, infinite, and unavailable states."
    >
      <form className="lab-form" onSubmit={submit}>
        <div className="lab-fields two-columns">
          <TextField
            id="error-references"
            label="References"
            value={references}
            onChange={setReferences}
          />
          <TextField
            id="error-hypotheses"
            label="Hypotheses"
            value={hypotheses}
            onChange={setHypotheses}
          />
        </div>
        <label className="check-label">
          <input
            checked={caseSensitive}
            onChange={(e) => setCaseSensitive(e.target.checked)}
            type="checkbox"
          />
          Case-sensitive text comparison
        </label>
        <details className="optional-input">
          <summary>Optional phoneme error rate inputs</summary>
          <div className="lab-fields two-columns">
            <TextField
              id="error-reference-phonemes"
              label="Reference phonemes"
              value={refPhonemes}
              onChange={setRefPhonemes}
            />
            <TextField
              id="error-hypothesis-phonemes"
              label="Hypothesis phonemes"
              value={hypPhonemes}
              onChange={setHypPhonemes}
            />
          </div>
        </details>
        <button
          className="button button-primary"
          disabled={pending}
          type="submit"
        >
          {pending ? "Analyzing…" : "Calculate error rates"}
        </button>
      </form>
      <LabState pending={pending} error={error} />
      {result ? <ErrorRateResultView result={result} /> : <ToolEmpty />}
    </AnalysisShell>
  );
}

function rateLabel(rate: RateValue) {
  if (rate.status === "finite") return `${(rate.value * 100).toFixed(2)}%`;
  if (rate.status === "positive_infinity") return "∞";
  return "Not computed";
}
function ErrorRateResultView({ result }: { result: ErrorRateResult }) {
  return (
    <section className="lab-result" aria-label="Error rate result">
      <ResultHeader
        title="Comparison profile"
        onDownload={() => downloadJson("error-rate-analysis.json", result)}
      />
      <dl className="large-metric-grid">
        {(["wer", "cer", "per", "ser"] as const).map((key) => (
          <div key={key}>
            <dt>{key.toUpperCase()}</dt>
            <dd>{rateLabel(result[key])}</dd>
          </div>
        ))}
      </dl>
      <div
        className="light-table-scroller"
        role="region"
        aria-label="Scrollable per-pair error rates"
        tabIndex={0}
      >
        <table className="segment-table">
          <caption>Per-pair error rates</caption>
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Reference</th>
              <th scope="col">Hypothesis</th>
              <th scope="col">WER</th>
              <th scope="col">CER</th>
              <th scope="col">PER</th>
            </tr>
          </thead>
          <tbody>
            {result.details.map((detail) => (
              <tr key={detail.index}>
                <th scope="row">{detail.index + 1}</th>
                <td>{detail.reference}</td>
                <td>{detail.hypothesis}</td>
                <td>{rateLabel(detail.wer)}</td>
                <td>{rateLabel(detail.cer)}</td>
                <td>{rateLabel(detail.per)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TrajectoryLab() {
  const [sequences, setSequences] = useState("");
  const [targets, setTargets] = useState("");
  const [unit, setUnit] = useState<CoverageUnit>("phoneme");
  const [result, setResult] = useState<CoverageTrajectory | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(
    null,
  );
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const rows = tokenLines(sequences);
    const targetUnits = uniqueTokens(targets);
    if (!rows.length || !targetUnits.length) {
      setError({
        message: "Add at least one phoneme sequence and one target unit.",
        reference: null,
      });
      return;
    }
    setPending(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await analysisApi.trajectory({
          phoneme_sequences: rows,
          target_units: targetUnits,
          unit,
        }),
      );
    } catch (caught) {
      setError(friendlyError(caught));
    } finally {
      setPending(false);
    }
  }
  return (
    <AnalysisShell
      title="Coverage trajectory"
      description="See when each ordered sentence adds new phonetic coverage."
    >
      <form className="lab-form" onSubmit={submit}>
        <div className="lab-fields two-columns">
          <TextField
            id="trajectory-sequences"
            label="Phoneme sequences"
            value={sequences}
            onChange={setSequences}
          />
          <TextField
            id="trajectory-targets"
            label="Target units"
            value={targets}
            onChange={setTargets}
          />
        </div>
        <label className="field-label" htmlFor="trajectory-unit">
          Coverage unit
        </label>
        <select
          id="trajectory-unit"
          onChange={(e) => setUnit(e.target.value as CoverageUnit)}
          value={unit}
        >
          <option value="phoneme">Phoneme</option>
          <option value="diphone">Diphone</option>
          <option value="triphone">Triphone</option>
        </select>
        <button
          className="button button-primary"
          disabled={pending}
          type="submit"
        >
          {pending ? "Analyzing…" : "Build trajectory"}
        </button>
      </form>
      <LabState pending={pending} error={error} />
      {result ? <TrajectoryResult result={result} /> : <ToolEmpty />}
    </AnalysisShell>
  );
}

function TrajectoryResult({ result }: { result: CoverageTrajectory }) {
  const finalCoverage = result.coverages.at(-1) ?? 0;
  return (
    <section className="lab-result" aria-label="Coverage trajectory result">
      <ResultHeader
        title={`${(finalCoverage * 100).toFixed(1)}% final coverage`}
        onDownload={() => downloadJson("coverage-trajectory.json", result)}
      />
      <div className="trajectory-chart" aria-hidden="true">
        {result.coverages.map((coverage, index) => (
          <i
            key={index}
            style={{ height: `${Math.max(2, coverage * 100)}%` }}
          />
        ))}
      </div>
      <p className="chart-description">
        Visual summary: coverage after each of {result.snapshots.length} ordered
        sentences. Exact values are in the table.
      </p>
      <div
        className="light-table-scroller"
        role="region"
        aria-label="Scrollable coverage trajectory"
        tabIndex={0}
      >
        <table className="segment-table">
          <caption>Coverage after each sentence</caption>
          <thead>
            <tr>
              <th scope="col">Sentence</th>
              <th scope="col">Coverage</th>
              <th scope="col">Covered units</th>
              <th scope="col">Gain</th>
              <th scope="col">New units</th>
            </tr>
          </thead>
          <tbody>
            {result.snapshots.map((snapshot) => (
              <tr key={snapshot.sentence_index}>
                <th scope="row">{snapshot.sentence_index + 1}</th>
                <td>{(snapshot.coverage * 100).toFixed(1)}%</td>
                <td>
                  {snapshot.covered_count} / {result.target_size}
                </td>
                <td>+{snapshot.new_units_count}</td>
                <td>
                  <code>{snapshot.new_units.join(" ") || "—"}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LabState({
  pending,
  error,
}: {
  pending: boolean;
  error: ReturnType<typeof friendlyError> | null;
}) {
  return (
    <div className="analysis-announcements" aria-live="polite">
      {pending ? <PendingNotice>Running a live analysis…</PendingNotice> : null}
      {error ? <ErrorNotice error={error} /> : null}
    </div>
  );
}
function ToolEmpty() {
  return (
    <p className="tool-empty">
      No result has been computed. Submit the form to request a live API
      analysis.
    </p>
  );
}
function TextField({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <label className="field-label" htmlFor={id}>
        {label} <span>one pair per line</span>
      </label>
      <textarea
        id={id}
        onChange={(event) => onChange(event.target.value)}
        value={value}
      />
    </div>
  );
}
function PairedTextForm({
  leftId,
  leftLabel,
  leftValue,
  onLeft,
  rightId,
  rightLabel,
  rightValue,
  onRight,
  onSubmit,
  pending,
  button,
}: {
  leftId: string;
  leftLabel: string;
  leftValue: string;
  onLeft: (value: string) => void;
  rightId: string;
  rightLabel: string;
  rightValue: string;
  onRight: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  pending: boolean;
  button: string;
}) {
  return (
    <form className="lab-form" onSubmit={onSubmit}>
      <div className="lab-fields two-columns">
        <TextField
          id={leftId}
          label={leftLabel}
          value={leftValue}
          onChange={onLeft}
        />
        <TextField
          id={rightId}
          label={rightLabel}
          value={rightValue}
          onChange={onRight}
        />
      </div>
      <button
        className="button button-primary"
        disabled={pending}
        type="submit"
      >
        {pending ? "Analyzing…" : button}
      </button>
    </form>
  );
}
