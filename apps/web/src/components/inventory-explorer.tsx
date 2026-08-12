"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  analysisApi,
  AnalysisApiError,
  AnalysisContractError,
  type EspeakMapping,
  type FeatureCatalog,
  type Inventory,
  type LanguageSummary,
  type PageResult,
  type PhonologyStatus,
  type Segment,
} from "@/lib/analysis";
import { saveJson } from "@/lib/workbench-input";

type InventoryMode = "best" | "source" | "all" | "union";

const emptyLanguagePage: PageResult<LanguageSummary> = {
  items: [],
  total: 0,
  offset: 0,
  limit: 20,
};
const emptyMappingPage: PageResult<EspeakMapping> = {
  items: [],
  total: 0,
  offset: 0,
  limit: 20,
};
const emptySegmentPage: PageResult<Segment> = {
  items: [],
  total: 0,
  offset: 0,
  limit: 50,
};

function inventoryError(error: unknown): string {
  if (error instanceof AnalysisApiError) {
    return `${error.message}${error.requestId ? ` Reference ${error.requestId}.` : ""}`;
  }
  if (error instanceof AnalysisContractError) {
    return "The service returned an incompatible inventory response.";
  }
  return "Inventory data is temporarily unavailable. No data was substituted.";
}

export function InventoryExplorer() {
  const [status, setStatus] = useState<PhonologyStatus | null>(null);
  const [catalog, setCatalog] = useState<FeatureCatalog>({ names: [] });
  const [search, setSearch] = useState("");
  const [searchOffset, setSearchOffset] = useState(0);
  const [languages, setLanguages] = useState(emptyLanguagePage);
  const [mappings, setMappings] = useState(emptyMappingPage);
  const [identifier, setIdentifier] = useState("");
  const [mode, setMode] = useState<InventoryMode>("best");
  const [sources, setSources] = useState<string[]>([]);
  const [source, setSource] = useState("");
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [allInventories, setAllInventories] =
    useState<PageResult<Inventory> | null>(null);
  const [allOffset, setAllOffset] = useState(0);
  const [segments, setSegments] = useState(emptySegmentPage);
  const [segmentOffset, setSegmentOffset] = useState(0);
  const [segmentClass, setSegmentClass] = useState("");
  const [marginality, setMarginality] = useState("");
  const [featureName, setFeatureName] = useState("");
  const [featureValue, setFeatureValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resultRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      analysisApi.phonologyStatus(controller.signal),
      analysisApi.featureCatalog(controller.signal),
    ]).then(
      ([nextStatus, nextCatalog]) => {
        setStatus(nextStatus);
        setCatalog(nextCatalog);
      },
      (caught: unknown) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(inventoryError(caught));
        }
      },
    );
    return () => controller.abort();
  }, []);

  async function runSearch(event?: FormEvent<HTMLFormElement>, offset = 0) {
    event?.preventDefault();
    const query = search.trim();
    if (!query) {
      setError(
        "Enter a language name, ISO 639-3 code, Glottocode, or eSpeak code.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const [languagePage, mappingPage] = await Promise.all([
        analysisApi.languages(query, undefined, offset, 20),
        analysisApi.espeakMappings(query, undefined, offset, 20),
      ]);
      setLanguages(languagePage);
      setMappings(mappingPage);
      setSearchOffset(offset);
    } catch (caught) {
      setError(inventoryError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function loadDataset() {
    setBusy(true);
    setError(null);
    try {
      setStatus(await analysisApi.loadPhonology());
    } catch (caught) {
      setError(inventoryError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function openInventory(
    nextIdentifier: string,
    nextMode: InventoryMode = "best",
    nextSource = "",
  ) {
    setBusy(true);
    setError(null);
    setIdentifier(nextIdentifier);
    setMode(nextMode);
    setSegmentOffset(0);
    try {
      const sourceResult = await analysisApi.inventorySources(nextIdentifier);
      setSources(sourceResult.sources);
      const resolvedSource = nextSource || sourceResult.sources[0] || "";
      setSource(resolvedSource);
      if (nextMode === "all") {
        const page = await analysisApi.allInventories(nextIdentifier, 0, 5);
        setAllInventories(page);
        setAllOffset(0);
        setInventory(null);
        setSegments(emptySegmentPage);
      } else {
        const union = nextMode === "union";
        const requestedSource =
          nextMode === "source" ? resolvedSource : undefined;
        const [nextInventory, nextSegments] = await Promise.all([
          analysisApi.inventory(nextIdentifier, requestedSource, union),
          analysisApi.segments(nextIdentifier, {
            source: requestedSource,
            union,
            offset: 0,
            limit: 50,
          }),
        ]);
        setInventory(nextInventory);
        setSegments(nextSegments);
        setAllInventories(null);
      }
      requestAnimationFrame(() => resultRef.current?.focus());
    } catch (caught) {
      setInventory(null);
      setAllInventories(null);
      setSegments(emptySegmentPage);
      setError(inventoryError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function paginateAll(offset: number) {
    if (!identifier) return;
    setBusy(true);
    setError(null);
    try {
      setAllInventories(
        await analysisApi.allInventories(identifier, offset, 5),
      );
      setAllOffset(offset);
    } catch (caught) {
      setError(inventoryError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function filterSegments(
    event?: FormEvent<HTMLFormElement>,
    offset = 0,
  ) {
    event?.preventDefault();
    if (!identifier || mode === "all") return;
    setBusy(true);
    setError(null);
    try {
      const page = await analysisApi.segments(identifier, {
        source: mode === "source" ? source : undefined,
        union: mode === "union",
        segmentClass:
          segmentClass === "consonant" ||
          segmentClass === "vowel" ||
          segmentClass === "tone"
            ? segmentClass
            : undefined,
        marginal:
          marginality === "marginal"
            ? true
            : marginality === "core"
              ? false
              : undefined,
        featureName: featureName || undefined,
        featureValue: featureName && featureValue ? featureValue : undefined,
        offset,
        limit: 50,
      });
      setSegments(page);
      setSegmentOffset(offset);
    } catch (caught) {
      setError(inventoryError(caught));
    } finally {
      setBusy(false);
    }
  }

  if (status && !status.cache_available) {
    return (
      <section className="workbench-shell">
        <div className="availability-card" role="status">
          <strong>PHOIBLE data is not provisioned</strong>
          <p>
            Inventory controls are disabled. Ask an operator to run the pinned
            PHOIBLE provisioner; this browser never downloads or mutates the
            dataset.
          </p>
        </div>
      </section>
    );
  }

  const searchTotal = Math.max(languages.total, mappings.total);
  return (
    <section
      className="workbench-shell"
      aria-label="PHOIBLE inventory explorer"
    >
      <div className="provenance-banner">
        <div>
          <strong>PHOIBLE-backed and source explicit</strong>
          <p>
            {status?.stats
              ? `${status.stats.language_count.toLocaleString()} languages · ${status.stats.inventory_count.toLocaleString()} inventories · ${status.stats.segment_count.toLocaleString()} segments.`
              : "Checking the provisioned PHOIBLE snapshot."}
          </p>
          {status ? (
            <small className="provenance-detail">
              Snapshot <code>{status.revision}</code> · SHA-256{" "}
              <abbr title={status.sha256}>{status.sha256.slice(0, 16)}…</abbr>
            </small>
          ) : null}
        </div>
        <span
          className={
            catalog.names.length === 38
              ? "status-pill status-available"
              : "status-pill status-degraded"
          }
        >
          {catalog.names.length} / 38 features
        </span>
        {status && !status.loaded ? (
          <button
            className="button button-secondary"
            disabled={busy}
            onClick={() => void loadDataset()}
            type="button"
          >
            {busy ? "Loading PHOIBLE…" : "Load PHOIBLE statistics"}
          </button>
        ) : null}
      </div>

      <form
        className="workbench-form"
        onSubmit={(event) => void runSearch(event, 0)}
        role="search"
      >
        <div className="workbench-form-heading">
          <div>
            <p className="eyebrow">Language discovery · Paginated</p>
            <h2>Find PHOIBLE and eSpeak identifiers</h2>
          </div>
        </div>
        <div className="form-grid two-columns">
          <div>
            <label className="field-label" htmlFor="inventory-query">
              Language or code
            </label>
            <input
              id="inventory-query"
              maxLength={100}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="English, eng, glottocode, or en-us"
              value={search}
            />
          </div>
          <div className="form-actions align-end">
            <button
              className="button button-primary"
              disabled={busy}
              type="submit"
            >
              {busy ? "Loading…" : "Search"}
            </button>
          </div>
        </div>
        {error ? (
          <div className="error-notice" role="alert">
            <strong>Request not completed</strong>
            <p>{error}</p>
          </div>
        ) : null}
      </form>

      {languages.items.length || mappings.items.length ? (
        <div className="workbench-panel split-panel">
          <section aria-labelledby="language-results-heading">
            <h2 id="language-results-heading">PHOIBLE languages</h2>
            <ul className="record-list">
              {languages.items.map((language) => (
                <li key={`${language.iso639_3}-${language.glottocode}`}>
                  <div>
                    <strong>{language.language_name}</strong>
                    <small>
                      {language.iso639_3} · {language.glottocode} ·{" "}
                      {language.inventory_count} sources
                    </small>
                  </div>
                  <button
                    className="text-action"
                    onClick={() => void openInventory(language.iso639_3)}
                    type="button"
                  >
                    Explore
                  </button>
                </li>
              ))}
            </ul>
          </section>
          <section aria-labelledby="mapping-results-heading">
            <h2 id="mapping-results-heading">eSpeak ↔ ISO mapping</h2>
            <div
              className="table-scroller"
              role="region"
              aria-label="eSpeak mappings"
              tabIndex={0}
            >
              <table className="workbench-table">
                <thead>
                  <tr>
                    <th scope="col">eSpeak</th>
                    <th scope="col">ISO 639-3</th>
                    <th scope="col">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {mappings.items.map((mapping) => (
                    <tr key={`${mapping.espeak_code}-${mapping.iso639_3}`}>
                      <td>
                        <code>{mapping.espeak_code}</code>
                      </td>
                      <td>
                        <code>{mapping.iso639_3}</code>
                      </td>
                      <td>
                        <button
                          className="text-action"
                          onClick={() => void openInventory(mapping.iso639_3)}
                          type="button"
                        >
                          Explore
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <nav className="pagination" aria-label="Language search pages">
            <button
              disabled={busy || searchOffset === 0}
              onClick={() =>
                void runSearch(undefined, Math.max(0, searchOffset - 20))
              }
              type="button"
            >
              Previous
            </button>
            <span>
              Rows {searchTotal ? searchOffset + 1 : 0}–
              {Math.min(searchOffset + 20, searchTotal)} of {searchTotal}
            </span>
            <button
              disabled={busy || searchOffset + 20 >= searchTotal}
              onClick={() => void runSearch(undefined, searchOffset + 20)}
              type="button"
            >
              Next
            </button>
          </nav>
        </div>
      ) : null}

      {identifier ? (
        <div className="workbench-panel" ref={resultRef} tabIndex={-1}>
          <div className="panel-heading">
            <div>
              <p className="eyebrow">PHOIBLE provenance · {identifier}</p>
              <h2>Inventory resolution</h2>
            </div>
            {inventory ? (
              <button
                className="text-action"
                onClick={() =>
                  saveJson(
                    `${inventory.iso639_3}-${inventory.source}-inventory.json`,
                    inventory,
                  )
                }
                type="button"
              >
                Download JSON
              </button>
            ) : null}
          </div>
          <div className="form-grid three-columns">
            <div>
              <label className="field-label" htmlFor="inventory-mode">
                Inventory view
              </label>
              <select
                id="inventory-mode"
                onChange={(event) =>
                  void openInventory(
                    identifier,
                    event.target.value as InventoryMode,
                    source,
                  )
                }
                value={mode}
              >
                <option value="best">Best / largest inventory</option>
                <option value="source">Specific source</option>
                <option value="all">All source inventories</option>
                <option value="union">Synthetic union</option>
              </select>
            </div>
            <div>
              <label className="field-label" htmlFor="inventory-source">
                PHOIBLE source
              </label>
              <select
                disabled={mode !== "source"}
                id="inventory-source"
                onChange={(event) => {
                  setSource(event.target.value);
                  void openInventory(identifier, "source", event.target.value);
                }}
                value={source}
              >
                {sources.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </div>
            <div className="provenance-note">
              <strong>
                {mode === "union"
                  ? "Synthetic union"
                  : mode === "all"
                    ? "Source comparison"
                    : "Recorded inventory"}
              </strong>
              <small>
                {mode === "union"
                  ? "inventory_id=0 and source=union; segment metadata is merged."
                  : "Source and dialect remain visible with every result."}
              </small>
            </div>
          </div>

          {allInventories ? (
            <>
              <div
                className="table-scroller"
                role="region"
                aria-label="All inventory sources"
                tabIndex={0}
              >
                <table className="workbench-table">
                  <caption>
                    PHOIBLE inventories preserve source and dialect provenance
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">ID</th>
                      <th scope="col">Source</th>
                      <th scope="col">Dialect</th>
                      <th scope="col">Segments</th>
                      <th scope="col">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allInventories.items.map((item) => (
                      <tr key={`${item.inventory_id}-${item.source}`}>
                        <td>{item.inventory_id}</td>
                        <td>{item.source}</td>
                        <td>{item.specific_dialect ?? "Not specified"}</td>
                        <td>{item.size}</td>
                        <td>
                          <button
                            className="text-action"
                            onClick={() =>
                              void openInventory(
                                identifier,
                                "source",
                                item.source,
                              )
                            }
                            type="button"
                          >
                            Inspect source
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <nav className="pagination" aria-label="Inventory source pages">
                <button
                  disabled={busy || allOffset === 0}
                  onClick={() => void paginateAll(Math.max(0, allOffset - 5))}
                  type="button"
                >
                  Previous
                </button>
                <span>
                  Rows {allInventories.total ? allOffset + 1 : 0}–
                  {Math.min(allOffset + 5, allInventories.total)} of{" "}
                  {allInventories.total}
                </span>
                <button
                  disabled={busy || allOffset + 5 >= allInventories.total}
                  onClick={() => void paginateAll(allOffset + 5)}
                  type="button"
                >
                  Next
                </button>
              </nav>
            </>
          ) : null}

          {inventory ? (
            <>
              <dl className="metric-grid five-up">
                <div>
                  <dt>Source</dt>
                  <dd>{inventory.source}</dd>
                </div>
                <div>
                  <dt>Total</dt>
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
              <p className="provenance-detail">
                <strong>{inventory.language_name}</strong> · ISO{" "}
                {inventory.iso639_3} · {inventory.glottocode} · inventory{" "}
                {inventory.inventory_id} ·{" "}
                {inventory.specific_dialect ?? "dialect not specified"}
              </p>
              <form
                className="filter-surface"
                onSubmit={(event) => void filterSegments(event, 0)}
              >
                <div className="form-grid four-columns">
                  <div>
                    <label
                      className="field-label"
                      htmlFor="segment-class-filter"
                    >
                      Class
                    </label>
                    <select
                      id="segment-class-filter"
                      onChange={(event) => setSegmentClass(event.target.value)}
                      value={segmentClass}
                    >
                      <option value="">All classes</option>
                      <option value="consonant">Consonant</option>
                      <option value="vowel">Vowel</option>
                      <option value="tone">Tone</option>
                    </select>
                  </div>
                  <div>
                    <label
                      className="field-label"
                      htmlFor="segment-marginality"
                    >
                      Marginality
                    </label>
                    <select
                      id="segment-marginality"
                      onChange={(event) => setMarginality(event.target.value)}
                      value={marginality}
                    >
                      <option value="">Core and marginal</option>
                      <option value="core">Core only</option>
                      <option value="marginal">Marginal only</option>
                    </select>
                  </div>
                  <div>
                    <label className="field-label" htmlFor="segment-feature">
                      Distinctive feature
                    </label>
                    <select
                      id="segment-feature"
                      onChange={(event) => setFeatureName(event.target.value)}
                      value={featureName}
                    >
                      <option value="">Any of 38 features</option>
                      {catalog.names.map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label
                      className="field-label"
                      htmlFor="segment-feature-value"
                    >
                      Value / contour
                    </label>
                    <input
                      disabled={!featureName}
                      id="segment-feature-value"
                      onChange={(event) => setFeatureValue(event.target.value)}
                      pattern="[+\-0](,[+\-0])*"
                      placeholder="+, -, 0, or +,-"
                      value={featureValue}
                    />
                  </div>
                </div>
                <button
                  className="button button-primary"
                  disabled={busy}
                  type="submit"
                >
                  Apply server filters
                </button>
              </form>
              <p aria-live="polite" className="result-count">
                Showing {segments.items.length} of {segments.total} matching
                segments.
              </p>
              <div
                className="table-scroller"
                role="region"
                aria-label="Filtered PHOIBLE segments"
                tabIndex={0}
              >
                <table className="workbench-table segment-grid-table">
                  <thead>
                    <tr>
                      <th scope="col">Segment</th>
                      <th scope="col">Class</th>
                      <th scope="col">Status</th>
                      <th scope="col">Allophones</th>
                      <th scope="col">38-feature values</th>
                    </tr>
                  </thead>
                  <tbody>
                    {segments.items.map((segment) => (
                      <tr key={segment.glyph_id}>
                        <th className="ipa-text" scope="row">
                          <bdi>{segment.phoneme}</bdi>
                        </th>
                        <td>{segment.segment_class}</td>
                        <td>{segment.marginal ? "Marginal" : "Core"}</td>
                        <td className="ipa-text">
                          {segment.allophones.join(" ") || "None recorded"}
                        </td>
                        <td>
                          <details>
                            <summary>
                              {segment.features.length} recorded values
                            </summary>
                            <dl className="feature-grid">
                              {segment.features.map((feature) => (
                                <div key={feature.name}>
                                  <dt>{feature.name}</dt>
                                  <dd>
                                    <code>{feature.value}</code>
                                  </dd>
                                </div>
                              ))}
                            </dl>
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <nav className="pagination" aria-label="Segment result pages">
                <button
                  disabled={busy || segmentOffset === 0}
                  onClick={() =>
                    void filterSegments(
                      undefined,
                      Math.max(0, segmentOffset - 50),
                    )
                  }
                  type="button"
                >
                  Previous
                </button>
                <span>
                  Rows {segments.total ? segmentOffset + 1 : 0}–
                  {Math.min(segmentOffset + 50, segments.total)} of{" "}
                  {segments.total}
                </span>
                <button
                  disabled={busy || segmentOffset + 50 >= segments.total}
                  onClick={() =>
                    void filterSegments(undefined, segmentOffset + 50)
                  }
                  type="button"
                >
                  Next
                </button>
              </nav>
            </>
          ) : null}
        </div>
      ) : (
        <p className="workbench-empty">
          Search and choose a language to resolve a real PHOIBLE inventory.
        </p>
      )}
    </section>
  );
}
