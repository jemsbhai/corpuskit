"use client";

import { type FormEvent, useState } from "react";

import { describeRequestError } from "@/lib/api-client";
import {
  coverageLabApi,
  type CoverageLabResult,
  type CoverageUnit,
  type ExportedReport,
  type RenderedReport,
  type TargetSpaceEstimate,
  type WeightSet,
  type WeightStrategy,
} from "@/lib/coverage-lab";
import {
  nonEmptyLines,
  phonemeRows,
  saveBytes,
  uniqueTokens,
  weightedUnits,
} from "@/lib/workbench-input";

type LabTab = "coverage" | "weights" | "reports";

export function CoverageWeightingLab() {
  const [tab, setTab] = useState<LabTab>("coverage");
  return (
    <section
      className="workbench-shell"
      aria-label="Coverage and weighting tools"
    >
      <div
        className="workbench-tabs"
        role="tablist"
        aria-label="Coverage lab sections"
      >
        {(["coverage", "weights", "reports"] as const).map((item) => (
          <button
            aria-controls={`coverage-panel-${item}`}
            aria-selected={tab === item}
            className={tab === item ? "is-active" : ""}
            id={`coverage-tab-${item}`}
            key={item}
            onClick={() => setTab(item)}
            role="tab"
            type="button"
          >
            {item === "coverage"
              ? "Target & tracking"
              : item === "weights"
                ? "Weight strategies"
                : "Reports & exports"}
          </button>
        ))}
      </div>
      <div
        aria-labelledby={`coverage-tab-${tab}`}
        id={`coverage-panel-${tab}`}
        role="tabpanel"
      >
        {tab === "coverage" ? <CoverageTrackerPanel /> : null}
        {tab === "weights" ? <WeightPanel /> : null}
        {tab === "reports" ? <ReportPanel /> : null}
      </div>
    </section>
  );
}

function CoverageTrackerPanel() {
  const [targets, setTargets] = useState("p b t d k ɡ");
  const [unit, setUnit] = useState<CoverageUnit>("phoneme");
  const [sequences, setSequences] = useState("p b t\nd k ɡ\np b d ɡ");
  const [weights, setWeights] = useState("");
  const [estimate, setEstimate] = useState<TargetSpaceEstimate | null>(null);
  const [result, setResult] = useState<CoverageLabResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function changeInput(update: () => void) {
    update();
    setEstimate(null);
    setResult(null);
  }

  async function estimateTarget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setEstimate(await coverageLabApi.estimate(uniqueTokens(targets), unit));
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function track() {
    setBusy(true);
    setError(null);
    try {
      setResult(
        await coverageLabApi.track({
          target_phonemes: uniqueTokens(targets),
          unit,
          phoneme_sequences: phonemeRows(sequences),
          weights: weights.trim() ? weightedUnits(weights) : [],
          next_targets_limit: 20,
        }),
      );
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workbench-panel">
      <form onSubmit={(event) => void estimateTarget(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Bounded Cartesian target</p>
            <h2>Estimate before construction</h2>
          </div>
        </div>
        <div className="form-grid three-columns">
          <div>
            <label className="field-label" htmlFor="coverage-targets">
              Target phonemes
            </label>
            <textarea
              id="coverage-targets"
              onChange={(event) =>
                changeInput(() => setTargets(event.target.value))
              }
              rows={4}
              value={targets}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="coverage-unit">
              Unit
            </label>
            <select
              id="coverage-unit"
              onChange={(event) =>
                changeInput(() => setUnit(event.target.value as CoverageUnit))
              }
              value={unit}
            >
              <option value="phoneme">Phoneme (n)</option>
              <option value="diphone">Diphone (n²)</option>
              <option value="triphone">Triphone (n³)</option>
            </select>
            <small className="field-help">
              Diphone and triphone spaces are full Cartesian products.
            </small>
          </div>
          <div>
            <label className="field-label" htmlFor="coverage-weights">
              Optional priority weights
            </label>
            <textarea
              id="coverage-weights"
              onChange={(event) => setWeights(event.target.value)}
              placeholder="p,2\nb,1"
              rows={4}
              value={weights}
            />
          </div>
        </div>
        <button className="button button-primary" disabled={busy} type="submit">
          Estimate target size
        </button>
      </form>
      {estimate ? (
        <div
          className={
            estimate.within_limit
              ? "estimate-card is-valid"
              : "estimate-card is-blocked"
          }
          role="status"
        >
          <strong>
            {estimate.estimated_target_size.toLocaleString()} {estimate.unit}{" "}
            targets
          </strong>
          <p>
            {estimate.phoneme_count} phonemes raised to exponent{" "}
            {estimate.exponent}; synchronous limit{" "}
            {estimate.max_target_size.toLocaleString()}.
          </p>
          <span>
            {estimate.within_limit
              ? "Within synchronous limit"
              : "Too large — reduce the target before tracking"}
          </span>
        </div>
      ) : null}
      <div className="form-grid two-columns">
        <div>
          <label className="field-label" htmlFor="coverage-sequences">
            Ordered phoneme sequences · one sentence per line
          </label>
          <textarea
            id="coverage-sequences"
            onChange={(event) => setSequences(event.target.value)}
            rows={8}
            value={sequences}
          />
        </div>
        <div className="tracking-explainer">
          <strong>One request, complete state</strong>
          <p>
            Each row updates the tracker in order. The result includes step
            gains, final counts and sentence provenance, weighted next targets,
            and a verified reset snapshot.
          </p>
          <button
            className="button button-primary"
            disabled={busy || !estimate?.within_limit}
            onClick={() => void track()}
            type="button"
          >
            Track ordered coverage
          </button>
        </div>
      </div>
      {error ? (
        <div className="error-notice" role="alert">
          <strong>Coverage not computed</strong>
          <p>{error}</p>
        </div>
      ) : null}
      {result ? (
        <CoverageResultView result={result} />
      ) : (
        <p className="workbench-empty">
          Estimate a safe target, then track ordered sequences.
        </p>
      )}
    </div>
  );
}

function CoverageResultView({
  result,
}: {
  readonly result: CoverageLabResult;
}) {
  const counts = new Map(
    result.final.unit_counts.map((item) => [item.unit, item.count]),
  );
  const sources = new Map(
    result.final.unit_sources.map((item) => [item.unit, item.sentence_indices]),
  );
  return (
    <section className="nested-result" aria-labelledby="coverage-result-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Deterministic tracker result</p>
          <h3 id="coverage-result-title">
            {(result.final.coverage * 100).toFixed(1)}% covered
          </h3>
        </div>
        <button
          className="text-action"
          onClick={() =>
            saveBytes(
              "coverage-result.json",
              JSON.stringify(result, null, 2),
              "application/json",
            )
          }
          type="button"
        >
          Download result
        </button>
      </div>
      <dl className="metric-grid">
        <div>
          <dt>Target</dt>
          <dd>{result.final.target_size}</dd>
        </div>
        <div>
          <dt>Covered</dt>
          <dd>{result.final.covered_count}</dd>
        </div>
        <div>
          <dt>Missing</dt>
          <dd>{result.final.missing_units.length}</dd>
        </div>
        <div>
          <dt>After reset</dt>
          <dd>{(result.after_reset.coverage * 100).toFixed(1)}%</dd>
        </div>
      </dl>
      <div
        className="table-scroller"
        role="region"
        aria-label="Coverage steps"
        tabIndex={0}
      >
        <table className="workbench-table">
          <caption>Sentence-by-sentence gains and provenance</caption>
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">Coverage</th>
              <th scope="col">New units</th>
            </tr>
          </thead>
          <tbody>
            {result.steps.map((step) => (
              <tr key={step.sentence_index}>
                <th scope="row">{step.sentence_index + 1}</th>
                <td>{(step.coverage * 100).toFixed(2)}%</td>
                <td className="ipa-text">
                  {step.new_units.join(" ") || "No gain"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div
        className="table-scroller"
        role="region"
        aria-label="Unit counts and sources"
        tabIndex={0}
      >
        <table className="workbench-table">
          <caption>
            Target counts and zero-based source sentence indices
          </caption>
          <thead>
            <tr>
              <th scope="col">Unit</th>
              <th scope="col">Count</th>
              <th scope="col">Sources</th>
            </tr>
          </thead>
          <tbody>
            {result.final.target_units.map((item) => (
              <tr key={item}>
                <th className="ipa-text" scope="row">
                  {item}
                </th>
                <td>{counts.get(item) ?? 0}</td>
                <td>{sources.get(item)?.join(", ") || "Not covered"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p>
        <strong>Next weighted targets:</strong>{" "}
        <span className="ipa-text">
          {result.next_targets.join(" ") || "Target complete"}
        </span>
      </p>
    </section>
  );
}

function WeightPanel() {
  const [strategy, setStrategy] = useState<WeightStrategy | "custom">(
    "uniform",
  );
  const [targets, setTargets] = useState("p b t d k ɡ");
  const [corpus, setCorpus] = useState("p b p\nt d k");
  const [custom, setCustom] = useState("p,2\nb,1");
  const [vowel, setVowel] = useState("2");
  const [consonant, setConsonant] = useState("1");
  const [result, setResult] = useState<WeightSet | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice("");
    try {
      if (strategy === "custom") {
        const weights = weightedUnits(custom);
        const validated = await coverageLabApi.validateWeights("unit", weights);
        setResult({
          weights,
          count: validated.count,
          total: weights.reduce((sum, item) => sum + item.weight, 0),
          mean: weights.length
            ? weights.reduce((sum, item) => sum + item.weight, 0) /
              weights.length
            : 0,
        });
        setNotice("Custom unit weights passed the engine-domain validator.");
      } else {
        const classWeights =
          strategy === "linguistic_class"
            ? [
                { unit: "vowel", weight: Number(vowel) },
                { unit: "consonant", weight: Number(consonant) },
              ]
            : [];
        setResult(
          await coverageLabApi.computeWeights({
            strategy,
            target_units: uniqueTokens(targets, 20_000),
            unit: "phoneme",
            corpus_phonemes:
              strategy === "inverse_frequency" ? phonemeRows(corpus) : [],
            class_weights: classWeights,
          }),
        );
        setNotice("Weights were computed by the connected CorpusGen runtime.");
      }
    } catch (caught) {
      setResult(null);
      setError(describeRequestError(caught));
    }
  }

  return (
    <div className="workbench-panel">
      <form onSubmit={(event) => void submit(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Uniform · inverse · class · custom</p>
            <h2>Weight strategy builder</h2>
          </div>
        </div>
        <div className="form-grid three-columns">
          <div>
            <label className="field-label" htmlFor="weight-strategy">
              Strategy
            </label>
            <select
              id="weight-strategy"
              onChange={(event) =>
                setStrategy(event.target.value as WeightStrategy | "custom")
              }
              value={strategy}
            >
              <option value="uniform">Uniform</option>
              <option value="inverse_frequency">Inverse frequency</option>
              <option value="linguistic_class">Linguistic class</option>
              <option value="custom">Validate custom units</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="weight-targets">
              Target units
            </label>
            <textarea
              id="weight-targets"
              onChange={(event) => setTargets(event.target.value)}
              rows={4}
              value={targets}
            />
          </div>
          {strategy === "inverse_frequency" ? (
            <div>
              <label className="field-label" htmlFor="weight-corpus">
                Corpus phonemes
              </label>
              <textarea
                id="weight-corpus"
                onChange={(event) => setCorpus(event.target.value)}
                rows={4}
                value={corpus}
              />
            </div>
          ) : strategy === "linguistic_class" ? (
            <div>
              <label className="field-label" htmlFor="vowel-weight">
                Vowel weight
              </label>
              <input
                id="vowel-weight"
                min="0.000001"
                onChange={(event) => setVowel(event.target.value)}
                step="any"
                type="number"
                value={vowel}
              />
              <label className="field-label" htmlFor="consonant-weight">
                Consonant weight
              </label>
              <input
                id="consonant-weight"
                min="0.000001"
                onChange={(event) => setConsonant(event.target.value)}
                step="any"
                type="number"
                value={consonant}
              />
            </div>
          ) : strategy === "custom" ? (
            <div>
              <label className="field-label" htmlFor="custom-weights">
                Unit,weight rows
              </label>
              <textarea
                id="custom-weights"
                onChange={(event) => setCustom(event.target.value)}
                rows={4}
                value={custom}
              />
            </div>
          ) : (
            <div className="provenance-note">
              <strong>Equal priority</strong>
              <small>
                Every supplied target receives the same engine-validated weight.
              </small>
            </div>
          )}
        </div>
        <button className="button button-primary" type="submit">
          {strategy === "custom" ? "Validate weights" : "Compute weights"}
        </button>
        <span aria-live="polite" className="form-status">
          {notice}
        </span>
      </form>
      {error ? (
        <div className="error-notice" role="alert">
          <strong>Weights not available</strong>
          <p>{error}</p>
        </div>
      ) : null}
      {result ? (
        <div className="nested-result">
          <dl className="metric-grid">
            <div>
              <dt>Count</dt>
              <dd>{result.count}</dd>
            </div>
            <div>
              <dt>Total</dt>
              <dd>{result.total.toFixed(3)}</dd>
            </div>
            <div>
              <dt>Mean</dt>
              <dd>{result.mean.toFixed(3)}</dd>
            </div>
            <div>
              <dt>Highest</dt>
              <dd>
                {Math.max(
                  0,
                  ...result.weights.map((item) => item.weight),
                ).toFixed(3)}
              </dd>
            </div>
          </dl>
          <div
            className="table-scroller"
            role="region"
            aria-label="Computed weights"
            tabIndex={0}
          >
            <table className="workbench-table">
              <thead>
                <tr>
                  <th scope="col">Unit</th>
                  <th scope="col">Weight</th>
                </tr>
              </thead>
              <tbody>
                {result.weights.map((item) => (
                  <tr key={item.unit}>
                    <th className="ipa-text" scope="row">
                      {item.unit}
                    </th>
                    <td>{item.weight}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ReportPanel() {
  const [sentences, setSentences] = useState(
    "Pack my box with five dozen liquor jugs.\nThe quick brown fox jumps over the lazy dog.",
  );
  const [language, setLanguage] = useState("en-us");
  const [unit, setUnit] = useState<CoverageUnit>("phoneme");
  const [targetMode, setTargetMode] = useState("derived");
  const [targets, setTargets] = useState("p b t d k ɡ");
  const [verbosity, setVerbosity] = useState<"minimal" | "normal" | "verbose">(
    "normal",
  );
  const [rendered, setRendered] = useState<RenderedReport | null>(null);
  const [exported, setExported] = useState<ExportedReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  function baseRequest() {
    return {
      sentences: nonEmptyLines(sentences, 500),
      language,
      unit,
      target:
        targetMode === "explicit"
          ? { mode: "explicit", phonemes: uniqueTokens(targets) }
          : { mode: targetMode, phonemes: [] },
    };
  }

  async function render(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      setRendered(await coverageLabApi.render({ ...baseRequest(), verbosity }));
    } catch (caught) {
      setError(describeRequestError(caught));
    }
  }

  async function exportReport(format: "json" | "jsonld") {
    setError(null);
    try {
      const next = await coverageLabApi.export({
        ...baseRequest(),
        format,
        indent: 2,
      });
      setExported(next);
      saveBytes(
        format === "json" ? "coverage-report.json" : "coverage-report.jsonld",
        next.canonical_json,
        next.media_type,
      );
    } catch (caught) {
      setError(describeRequestError(caught));
    }
  }

  return (
    <div className="workbench-panel">
      <form onSubmit={(event) => void render(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Human and canonical output</p>
            <h2>Evaluation report renderer</h2>
          </div>
        </div>
        <div className="form-grid three-columns">
          <div>
            <label className="field-label" htmlFor="report-language">
              Language
            </label>
            <input
              id="report-language"
              maxLength={32}
              onChange={(event) => setLanguage(event.target.value)}
              value={language}
            />
            <label className="field-label" htmlFor="report-unit">
              Unit
            </label>
            <select
              id="report-unit"
              onChange={(event) => setUnit(event.target.value as CoverageUnit)}
              value={unit}
            >
              <option value="phoneme">Phoneme</option>
              <option value="diphone">Diphone</option>
              <option value="triphone">Triphone</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="report-target-mode">
              Target mode
            </label>
            <select
              id="report-target-mode"
              onChange={(event) => setTargetMode(event.target.value)}
              value={targetMode}
            >
              <option value="derived">Observed inventory discovery</option>
              <option value="explicit">Explicit phonemes</option>
              <option value="phoible">PHOIBLE inventory</option>
            </select>
            <label className="field-label" htmlFor="report-targets">
              Explicit phonemes
            </label>
            <input
              disabled={targetMode !== "explicit"}
              id="report-targets"
              onChange={(event) => setTargets(event.target.value)}
              value={targets}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="report-verbosity">
              Render level
            </label>
            <select
              id="report-verbosity"
              onChange={(event) =>
                setVerbosity(
                  event.target.value as "minimal" | "normal" | "verbose",
                )
              }
              value={verbosity}
            >
              <option value="minimal">Minimal</option>
              <option value="normal">Normal</option>
              <option value="verbose">Verbose</option>
            </select>
            <small className="field-help">
              Derived means observed-inventory discovery and necessarily reports
              100%; it is not target completeness.
            </small>
          </div>
        </div>
        <label className="field-label" htmlFor="report-sentences">
          Sentences
        </label>
        <textarea
          dir="auto"
          id="report-sentences"
          onChange={(event) => setSentences(event.target.value)}
          rows={7}
          value={sentences}
        />
        <div className="form-actions">
          <button className="button button-primary" type="submit">
            Render report
          </button>
          <button
            className="text-action"
            onClick={() => void exportReport("json")}
            type="button"
          >
            Download canonical JSON
          </button>
          <button
            className="text-action"
            onClick={() => void exportReport("jsonld")}
            type="button"
          >
            Download canonical JSON-LD
          </button>
        </div>
      </form>
      {error ? (
        <div className="error-notice" role="alert">
          <strong>Report not available</strong>
          <p>{error}</p>
        </div>
      ) : null}
      {rendered ? (
        <pre className="report-output" tabIndex={0}>
          {rendered.content}
        </pre>
      ) : (
        <p className="workbench-empty">
          Choose a render level or a canonical export.
        </p>
      )}
      {exported ? (
        <p aria-live="polite" className="field-help">
          Downloaded backend-canonical {exported.format.toUpperCase()} as{" "}
          {exported.media_type}.
        </p>
      ) : null}
    </div>
  );
}
