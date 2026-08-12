"use client";

import { type FormEvent, useEffect, useState } from "react";

import { describeRequestError } from "@/lib/api-client";
import {
  fetchCapabilityCatalog,
  type CapabilityCatalog,
} from "@/lib/capabilities";
import type { CoverageUnit } from "@/lib/coverage-lab";
import {
  selectionApi,
  type CorpusSelection,
  type SelectionAlgorithm,
} from "@/lib/selection";
import {
  nonEmptyLines,
  saveJson,
  uniqueTokens,
  weightedUnits,
} from "@/lib/workbench-input";

interface SavedResult {
  readonly id: string;
  readonly label: string;
  readonly result: CorpusSelection;
}

const algorithmHelp: Record<SelectionAlgorithm, string> = {
  greedy: "Deterministic weighted set cover.",
  celf: "Lazy-greedy evaluation reduction.",
  stochastic: "Seeded stochastic greedy with epsilon sampling.",
  distribution: "Matches an explicit desired unit distribution.",
  ilp: "Optional exact optimization with a hard solver deadline.",
  nsga2: "Optional seeded multi-objective Pareto search.",
};

export function SelectionStudio() {
  const [capabilities, setCapabilities] = useState<CapabilityCatalog | null>(
    null,
  );
  const [algorithm, setAlgorithm] = useState<SelectionAlgorithm>("greedy");
  const [candidates, setCandidates] = useState(
    "Pack my box with five dozen liquor jugs.\nThe quick brown fox jumps over the lazy dog.\nSphinx of black quartz, judge my vow.",
  );
  const [language, setLanguage] = useState("en-us");
  const [unit, setUnit] = useState<CoverageUnit>("phoneme");
  const [targetMode, setTargetMode] = useState<
    "derived" | "explicit" | "phoible"
  >("explicit");
  const [targetPhonemes, setTargetPhonemes] = useState("p b t d k ɡ");
  const [maxSentences, setMaxSentences] = useState("");
  const [targetCoverage, setTargetCoverage] = useState("1");
  const [weights, setWeights] = useState("");
  const [distribution, setDistribution] = useState("p,1\nb,1");
  const [epsilon, setEpsilon] = useState("0.1");
  const [seed, setSeed] = useState("42");
  const [ilpLimit, setIlpLimit] = useState("10");
  const [population, setPopulation] = useState("50");
  const [generations, setGenerations] = useState("100");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<SavedResult[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchCapabilityCatalog(controller.signal).then(setCapabilities, () =>
      setCapabilities(null),
    );
    return () => controller.abort();
  }, []);

  const optimization = capabilities?.capabilities.find(
    (item) => item.id === "optimization",
  );
  const phoible = capabilities?.capabilities.find(
    (item) => item.id === "phoible",
  );
  const optimizationAvailable = optimization?.status === "available";
  const requiresOptimization = algorithm === "ilp" || algorithm === "nsga2";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requiresOptimization && !optimizationAvailable) {
      setError(
        optimization?.reason ??
          "Install the reviewed optimization worker profile before using ILP or NSGA-II.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await selectionApi.select({
        candidates: nonEmptyLines(candidates, 2_000),
        language,
        unit,
        target: {
          mode: targetMode,
          phonemes:
            targetMode === "explicit" ? uniqueTokens(targetPhonemes) : [],
        },
        options: {
          algorithm,
          max_sentences: maxSentences ? Number(maxSentences) : null,
          target_coverage: Number(targetCoverage),
          weights: weights.trim() ? weightedUnits(weights) : [],
          epsilon: Number(epsilon),
          seed: seed ? Number(seed) : null,
          target_distribution:
            algorithm === "distribution" ? weightedUnits(distribution) : [],
          ilp_time_limit_seconds: Number(ilpLimit),
          population_size: Number(population),
          generations: Number(generations),
        },
      });
      const saved = {
        id: crypto.randomUUID(),
        label: `${result.algorithm} · ${(result.coverage * 100).toFixed(1)}%`,
        result,
      };
      setResults((current) => [saved, ...current].slice(0, 6));
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workbench-shell">
      <form className="workbench-form" onSubmit={(event) => void submit(event)}>
        <div className="workbench-form-heading">
          <div>
            <p className="eyebrow">Six normalized algorithms · Real API only</p>
            <h2>Selection specification</h2>
          </div>
          <span
            className={`status-pill ${requiresOptimization && !optimizationAvailable ? "status-unavailable" : "status-available"}`}
          >
            {requiresOptimization
              ? optimizationAvailable
                ? "Optimization ready"
                : "Optimization gated"
              : "Core selector"}
          </span>
        </div>
        <div className="form-grid three-columns">
          <div>
            <label className="field-label" htmlFor="selection-algorithm">
              Algorithm
            </label>
            <select
              id="selection-algorithm"
              onChange={(event) =>
                setAlgorithm(event.target.value as SelectionAlgorithm)
              }
              value={algorithm}
            >
              {(Object.keys(algorithmHelp) as SelectionAlgorithm[]).map(
                (item) => (
                  <option
                    disabled={
                      (item === "ilp" || item === "nsga2") &&
                      !optimizationAvailable
                    }
                    key={item}
                    value={item}
                  >
                    {item === "nsga2" ? "NSGA-II" : item.toUpperCase()}
                  </option>
                ),
              )}
            </select>
            <small className="field-help">{algorithmHelp[algorithm]}</small>
          </div>
          <div>
            <label className="field-label" htmlFor="selection-language">
              Language
            </label>
            <input
              id="selection-language"
              maxLength={32}
              onChange={(event) => setLanguage(event.target.value)}
              value={language}
            />
            <label className="field-label" htmlFor="selection-unit">
              Unit
            </label>
            <select
              id="selection-unit"
              onChange={(event) => setUnit(event.target.value as CoverageUnit)}
              value={unit}
            >
              <option value="phoneme">Phoneme</option>
              <option value="diphone">Diphone</option>
              <option value="triphone">Triphone</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="selection-target-mode">
              Target source
            </label>
            <select
              id="selection-target-mode"
              onChange={(event) =>
                setTargetMode(event.target.value as typeof targetMode)
              }
              value={targetMode}
            >
              <option value="derived">Observed / derived</option>
              <option value="explicit">Explicit phonemes</option>
              <option
                disabled={phoible?.status !== "available"}
                value="phoible"
              >
                PHOIBLE inventory
              </option>
            </select>
            <label className="field-label" htmlFor="selection-targets">
              Explicit target
            </label>
            <input
              disabled={targetMode !== "explicit"}
              id="selection-targets"
              onChange={(event) => setTargetPhonemes(event.target.value)}
              value={targetPhonemes}
            />
          </div>
        </div>
        <label className="field-label" htmlFor="selection-candidates">
          Candidate sentences · one per line
        </label>
        <textarea
          dir="auto"
          id="selection-candidates"
          onChange={(event) => setCandidates(event.target.value)}
          rows={9}
          value={candidates}
        />
        <div className="form-grid four-columns">
          <div>
            <label className="field-label" htmlFor="selection-budget">
              Maximum sentences <span>optional</span>
            </label>
            <input
              id="selection-budget"
              max="2000"
              min="1"
              onChange={(event) => setMaxSentences(event.target.value)}
              type="number"
              value={maxSentences}
            />
            <small className="field-help">
              Leave blank to let the coverage target or candidate count stop
              selection.
            </small>
          </div>
          <div>
            <label className="field-label" htmlFor="selection-coverage">
              Target coverage
            </label>
            <input
              id="selection-coverage"
              max="1"
              min="0"
              onChange={(event) => setTargetCoverage(event.target.value)}
              step="0.01"
              type="number"
              value={targetCoverage}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="selection-seed">
              Seed
            </label>
            <input
              id="selection-seed"
              min="0"
              onChange={(event) => setSeed(event.target.value)}
              type="number"
              value={seed}
            />
            <small className="field-help">
              Required for replayable stochastic and NSGA-II durable jobs.
            </small>
          </div>
          <div>
            <label className="field-label" htmlFor="selection-weights">
              Optional unit,weight rows
            </label>
            <textarea
              id="selection-weights"
              onChange={(event) => setWeights(event.target.value)}
              placeholder="p,2\nb,1"
              rows={3}
              value={weights}
            />
          </div>
        </div>
        {algorithm === "stochastic" ? (
          <div>
            <label className="field-label" htmlFor="selection-epsilon">
              Sampling epsilon
            </label>
            <input
              id="selection-epsilon"
              max="1"
              min="0.000001"
              onChange={(event) => setEpsilon(event.target.value)}
              step="any"
              type="number"
              value={epsilon}
            />
          </div>
        ) : null}
        {algorithm === "distribution" ? (
          <div>
            <label className="field-label" htmlFor="selection-distribution">
              Required target distribution · unit,weight
            </label>
            <textarea
              id="selection-distribution"
              onChange={(event) => setDistribution(event.target.value)}
              rows={4}
              value={distribution}
            />
          </div>
        ) : null}
        {algorithm === "ilp" ? (
          <div>
            <label className="field-label" htmlFor="selection-ilp-limit">
              ILP time limit seconds
            </label>
            <input
              id="selection-ilp-limit"
              max="30"
              min="0.1"
              onChange={(event) => setIlpLimit(event.target.value)}
              step="0.1"
              type="number"
              value={ilpLimit}
            />
          </div>
        ) : null}
        {algorithm === "nsga2" ? (
          <div className="form-grid two-columns">
            <div>
              <label className="field-label" htmlFor="selection-population">
                Population
              </label>
              <input
                id="selection-population"
                max="200"
                min="2"
                onChange={(event) => setPopulation(event.target.value)}
                type="number"
                value={population}
              />
            </div>
            <div>
              <label className="field-label" htmlFor="selection-generations">
                Generations
              </label>
              <input
                id="selection-generations"
                max="200"
                min="1"
                onChange={(event) => setGenerations(event.target.value)}
                type="number"
                value={generations}
              />
            </div>
          </div>
        ) : null}
        {requiresOptimization && !optimizationAvailable ? (
          <div className="availability-card">
            <strong>This algorithm is unavailable</strong>
            <p>
              {optimization?.reason ??
                "Provision the optimization dependency profile and refresh capability status."}
            </p>
          </div>
        ) : null}
        <div className="form-actions">
          <button
            className="button button-primary"
            disabled={busy || (requiresOptimization && !optimizationAvailable)}
            type="submit"
          >
            {busy ? "Selecting…" : "Run selection"}
          </button>
          <span className="form-status">
            Only backend-returned results enter the comparison tray.
          </span>
        </div>
        {error ? (
          <div className="error-notice" role="alert">
            <strong>Selection not completed</strong>
            <p>{error}</p>
          </div>
        ) : null}
      </form>
      <SelectionResults
        results={results}
        onRemove={(id) =>
          setResults((current) => current.filter((item) => item.id !== id))
        }
      />
    </section>
  );
}

function SelectionResults({
  results,
  onRemove,
}: {
  readonly results: readonly SavedResult[];
  readonly onRemove: (id: string) => void;
}) {
  if (!results.length)
    return (
      <p className="workbench-empty">
        Run a supported selector to begin a side-by-side comparison.
      </p>
    );
  return (
    <div className="workbench-results">
      <div className="result-toolbar">
        <div>
          <p className="eyebrow">Client-side comparison · All six algorithms</p>
          <h2>Verified selection results</h2>
        </div>
      </div>
      <div
        className="table-scroller"
        role="region"
        aria-label="Selection comparison"
        tabIndex={0}
      >
        <table className="workbench-table">
          <caption>Each column comes from one API response</caption>
          <thead>
            <tr>
              <th scope="col">Measure</th>
              {results.map((item) => (
                <th key={item.id} scope="col">
                  {item.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Coverage</th>
              {results.map((item) => (
                <td key={item.id}>
                  {(item.result.coverage * 100).toFixed(2)}%
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">Selected</th>
              {results.map((item) => (
                <td key={item.id}>{item.result.selected_indices.length}</td>
              ))}
            </tr>
            <tr>
              <th scope="row">Missing units</th>
              {results.map((item) => (
                <td key={item.id}>{item.result.missing_units.length}</td>
              ))}
            </tr>
            <tr>
              <th scope="row">Elapsed</th>
              {results.map((item) => (
                <td key={item.id}>{item.result.elapsed_seconds.toFixed(3)}s</td>
              ))}
            </tr>
            <tr>
              <th scope="row">Iterations</th>
              {results.map((item) => (
                <td key={item.id}>{item.result.iterations}</td>
              ))}
            </tr>
            <tr>
              <th scope="row">Actions</th>
              {results.map((item) => (
                <td key={item.id}>
                  <div className="compact-actions">
                    <button
                      onClick={() =>
                        saveJson(
                          `selection-${item.result.algorithm}.json`,
                          item.result,
                        )
                      }
                      type="button"
                    >
                      Export
                    </button>
                    <button onClick={() => onRemove(item.id)} type="button">
                      Remove
                    </button>
                  </div>
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      {results.map((item) => (
        <section className="nested-result" key={item.id}>
          <h3>{item.label}</h3>
          <ol>
            {item.result.selected_sentences.map((sentence, index) => (
              <li
                dir="auto"
                key={`${item.id}-${item.result.selected_indices[index]}`}
              >
                {sentence}
              </li>
            ))}
          </ol>
          <dl className="inline-metadata">
            <div>
              <dt>Evaluations</dt>
              <dd>{item.result.metadata.evaluations ?? "Not reported"}</dd>
            </div>
            <div>
              <dt>Sample size</dt>
              <dd>{item.result.metadata.sample_size ?? "Not reported"}</dd>
            </div>
            <div>
              <dt>KL divergence</dt>
              <dd>{item.result.metadata.kl_divergence ?? "Not computed"}</dd>
            </div>
            <div>
              <dt>Solver</dt>
              <dd>{item.result.metadata.solver_status ?? "Not applicable"}</dd>
            </div>
          </dl>
          {item.result.metadata.pareto_front.length ? (
            <div
              className="table-scroller"
              role="region"
              aria-label={`${item.label} Pareto front`}
              tabIndex={0}
            >
              <table className="workbench-table">
                <caption>Backend-returned NSGA-II Pareto solutions</caption>
                <thead>
                  <tr>
                    <th scope="col">Coverage</th>
                    <th scope="col">Sentences</th>
                    <th scope="col">KL divergence</th>
                    <th scope="col">Indices</th>
                  </tr>
                </thead>
                <tbody>
                  {item.result.metadata.pareto_front.map((solution, index) => (
                    <tr key={`${item.id}-pareto-${index}`}>
                      <td>{(solution.coverage * 100).toFixed(2)}%</td>
                      <td>{solution.sentence_count}</td>
                      <td>{solution.kl_divergence ?? "Not computed"}</td>
                      <td>{solution.selected_indices.join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      ))}
    </div>
  );
}
