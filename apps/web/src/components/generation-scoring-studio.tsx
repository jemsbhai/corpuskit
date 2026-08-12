"use client";

import Link from "next/link";
import { type FormEvent, useState } from "react";

import { describeRequestError } from "@/lib/api-client";
import {
  generationApi,
  type CompositeScoringResult,
  type PhonotacticArtifact,
  type PhonotacticScoreResult,
  type ReadabilityResult,
  type RepositoryGenerationResult,
} from "@/lib/generation";
import {
  nonEmptyLines,
  phonemeRows,
  saveJson,
  uniqueTokens,
} from "@/lib/workbench-input";

type StudioTab =
  "repository" | "composite" | "phonotactics" | "readability" | "models";

export function GenerationScoringStudio() {
  const [tab, setTab] = useState<StudioTab>("repository");
  const labels: Record<StudioTab, string> = {
    repository: "Repository preview",
    composite: "Composite scoring",
    phonotactics: "Phonotactics",
    readability: "Readability",
    models: "Model validation",
  };
  return (
    <section
      className="workbench-shell"
      aria-label="Generation and scoring tools"
    >
      <div
        className="workbench-tabs"
        role="tablist"
        aria-label="Generation Studio sections"
      >
        {(Object.keys(labels) as StudioTab[]).map((item) => (
          <button
            aria-controls={`generation-panel-${item}`}
            aria-selected={tab === item}
            className={tab === item ? "is-active" : ""}
            id={`generation-tab-${item}`}
            key={item}
            onClick={() => setTab(item)}
            role="tab"
            type="button"
          >
            {labels[item]}
          </button>
        ))}
      </div>
      <div
        aria-labelledby={`generation-tab-${tab}`}
        id={`generation-panel-${tab}`}
        role="tabpanel"
      >
        {tab === "repository" ? <RepositoryPreview /> : null}
        {tab === "composite" ? <CompositeScoring /> : null}
        {tab === "phonotactics" ? <Phonotactics /> : null}
        {tab === "readability" ? <Readability /> : null}
        {tab === "models" ? <ModelValidation /> : null}
      </div>
    </section>
  );
}

function RepositoryPreview() {
  const [language, setLanguage] = useState("en-us");
  const [texts, setTexts] = useState(
    "Pack my box with five dozen liquor jugs.\nSphinx of black quartz, judge my vow.",
  );
  const [targets, setTargets] = useState("p b t d k ɡ");
  const [unit, setUnit] = useState<"phoneme" | "diphone" | "triphone">(
    "phoneme",
  );
  const [coverage, setCoverage] = useState("1");
  const [sentences, setSentences] = useState("50");
  const [iterations, setIterations] = useState("25");
  const [timeout, setTimeoutValue] = useState("5");
  const [result, setResult] = useState<RepositoryGenerationResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const rows = nonEmptyLines(texts, 2_000);
      setResult(
        await generationApi.preview({
          source: {
            kind: "raw_text",
            language,
            entries: rows.map((text, index) => ({
              source_id: `row-${index + 1}`,
              text,
            })),
          },
          target: { phonemes: uniqueTokens(targets), unit },
          stopping: {
            target_coverage: Number(coverage),
            max_sentences: Number(sentences),
            max_iterations: Number(iterations),
            timeout_seconds: Number(timeout),
          },
          scoring: {
            weights: {
              coverage: 1,
              phonotactic: 0,
              readability: 0,
              fluency: 0,
            },
            phonotactic_artifact: null,
            readability_target: null,
            readability_filter: null,
          },
          candidates_per_iteration: 5,
          activity_timeout_seconds: 30,
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
      <form className="workbench-form" onSubmit={(event) => void submit(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Bounded synchronous contract</p>
            <h2>Repository candidate preview</h2>
          </div>
          <span className="status-pill status-degraded">Preview only</span>
        </div>
        <p className="form-status">
          This executes a finite repository-source preview. Durable repository
          generation remains worker-only and is not registered in the default
          batch worker.
        </p>
        <div className="form-grid three-columns">
          <div>
            <label className="field-label" htmlFor="repository-language">
              eSpeak language
            </label>
            <input
              id="repository-language"
              maxLength={32}
              onChange={(event) => setLanguage(event.target.value)}
              value={language}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="repository-unit">
              Coverage unit
            </label>
            <select
              id="repository-unit"
              onChange={(event) => setUnit(event.target.value as typeof unit)}
              value={unit}
            >
              <option value="phoneme">Phoneme</option>
              <option value="diphone">Diphone</option>
              <option value="triphone">Triphone</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="repository-target">
              Target phonemes
            </label>
            <input
              id="repository-target"
              onChange={(event) => setTargets(event.target.value)}
              value={targets}
            />
          </div>
        </div>
        <label className="field-label" htmlFor="repository-texts">
          Raw repository rows · one sentence per line
        </label>
        <textarea
          dir="auto"
          id="repository-texts"
          onChange={(event) => setTexts(event.target.value)}
          rows={7}
          value={texts}
        />
        <fieldset>
          <legend>Finite stopping criteria</legend>
          <div className="form-grid four-columns">
            <Numeric
              id="repository-coverage"
              label="Target coverage"
              max="1"
              min="0"
              set={setCoverage}
              step="0.01"
              value={coverage}
            />
            <Numeric
              id="repository-sentences"
              label="Max sentences"
              max="250"
              min="1"
              set={setSentences}
              value={sentences}
            />
            <Numeric
              id="repository-iterations"
              label="Max iterations"
              max="1000"
              min="1"
              set={setIterations}
              value={iterations}
            />
            <Numeric
              id="repository-timeout"
              label="Timeout seconds"
              max="30"
              min="0.1"
              set={setTimeoutValue}
              step="0.1"
              value={timeout}
            />
          </div>
        </fieldset>
        <button className="button button-primary" disabled={busy} type="submit">
          {busy ? "Running bounded preview…" : "Run repository preview"}
        </button>
        {error ? (
          <ErrorNotice title="Preview not completed" message={error} />
        ) : null}
      </form>
      {result ? (
        <RepositoryResult result={result} />
      ) : (
        <p className="workbench-empty">No backend result yet.</p>
      )}
    </div>
  );
}

function RepositoryResult({
  result,
}: {
  readonly result: RepositoryGenerationResult;
}) {
  return (
    <div className="workbench-results">
      <div className="result-toolbar">
        <div>
          <p className="eyebrow">Backend-returned manifest</p>
          <h2>{(result.coverage * 100).toFixed(2)}% coverage</h2>
        </div>
        <button
          onClick={() => saveJson("repository-preview.json", result)}
          type="button"
        >
          Export JSON
        </button>
      </div>
      <dl className="inline-metadata">
        <div>
          <dt>Execution</dt>
          <dd>{result.execution_mode.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Stop reason</dt>
          <dd>{result.stop_reason.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Iterations</dt>
          <dd>{result.iterations}</dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>{result.elapsed_seconds.toFixed(3)}s</dd>
        </div>
      </dl>
      <div
        className="table-scroller"
        role="region"
        aria-label="Accepted repository candidates"
        tabIndex={0}
      >
        <table className="workbench-table">
          <caption>Accepted rows and provenance in backend order</caption>
          <thead>
            <tr>
              <th scope="col">Iteration</th>
              <th scope="col">Source</th>
              <th scope="col">Text</th>
              <th scope="col">IPA sequence</th>
              <th scope="col">Gain</th>
            </tr>
          </thead>
          <tbody>
            {result.accepted.map((item) => (
              <tr key={item.source_id}>
                <td>{item.iteration}</td>
                <td>{item.source_id}</td>
                <td dir="auto">{item.text}</td>
                <td className="ipa-cell" dir="ltr" lang="und-fonipa">
                  {item.phonemes.join(" ")}
                </td>
                <td>{item.coverage_gain}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p>
        <strong>Missing units:</strong>{" "}
        <span className="ipa-cell" lang="und-fonipa">
          {result.missing_units.join(" · ") || "None"}
        </span>
      </p>
    </div>
  );
}

function CompositeScoring() {
  const [texts, setTexts] = useState("Pack my box.\nJudge my vow.");
  const [phonemes, setPhonemes] = useState(
    "p æ k m aɪ b ɒ k s\ndʒ ʌ dʒ m aɪ v aʊ",
  );
  const [targets, setTargets] = useState("p æ k m aɪ b ɒ s dʒ ʌ v aʊ");
  const [topK, setTopK] = useState("2");
  const [coverageWeight, setCoverageWeight] = useState("1");
  const [readabilityWeight, setReadabilityWeight] = useState("0");
  const [readabilityMinimum, setReadabilityMinimum] = useState("40");
  const [readabilityMaximum, setReadabilityMaximum] = useState("80");
  const [result, setResult] = useState<CompositeScoringResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const textRows = nonEmptyLines(texts, 250);
      const phoneRows = phonemeRows(phonemes, 250);
      if (textRows.length !== phoneRows.length)
        throw new TypeError(
          "Text and phoneme inputs must contain the same number of rows.",
        );
      const coverage = Number(coverageWeight);
      const readability = Number(readabilityWeight);
      const readabilityMin = Number(readabilityMinimum);
      const readabilityMax = Number(readabilityMaximum);
      if (
        !Number.isFinite(coverage) ||
        !Number.isFinite(readability) ||
        coverage < 0 ||
        readability < 0 ||
        coverage > 1_000 ||
        readability > 1_000 ||
        coverage + readability === 0
      )
        throw new TypeError(
          "Enable at least one finite coverage or readability weight between 0 and 1,000.",
        );
      if (
        readability > 0 &&
        (!Number.isFinite(readabilityMin) ||
          !Number.isFinite(readabilityMax) ||
          readabilityMin < 0 ||
          readabilityMax > 100 ||
          readabilityMin > readabilityMax)
      )
        throw new TypeError(
          "Readability targets must be ordered values between 0 and 100.",
        );
      setResult(
        await generationApi.composite({
          target: { phonemes: uniqueTokens(targets), unit: "phoneme" },
          candidates: textRows.map((text, index) => ({
            source_id: `candidate-${index + 1}`,
            text,
            phonemes: phoneRows[index],
          })),
          state: { covered_sequences: [], accepted_source_ids: [] },
          options: {
            weights: {
              coverage,
              phonotactic: 0,
              readability,
              fluency: 0,
            },
            phonotactic_artifact: null,
            readability_target:
              readability > 0
                ? { minimum: readabilityMin, maximum: readabilityMax }
                : null,
            readability_filter: null,
          },
          top_k: Number(topK),
          commit_source_id: null,
        }),
      );
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
    <div className="workbench-panel">
      <form onSubmit={(event) => void submit(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Deterministic · No model inference</p>
            <h2>Composite candidate ranking</h2>
          </div>
        </div>
        <div className="form-grid two-columns">
          <div>
            <label className="field-label" htmlFor="score-texts">
              Candidate text rows
            </label>
            <textarea
              dir="auto"
              id="score-texts"
              onChange={(event) => setTexts(event.target.value)}
              rows={6}
              value={texts}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="score-phonemes">
              Matching phoneme rows
            </label>
            <textarea
              className="ipa-input"
              dir="ltr"
              id="score-phonemes"
              lang="und-fonipa"
              onChange={(event) => setPhonemes(event.target.value)}
              rows={6}
              value={phonemes}
            />
          </div>
        </div>
        <div className="form-grid four-columns">
          <Numeric
            id="score-coverage-weight"
            label="Coverage weight"
            max="1000"
            min="0"
            set={setCoverageWeight}
            step="0.1"
            value={coverageWeight}
          />
          <Numeric
            id="score-readability-weight"
            label="Readability weight"
            max="1000"
            min="0"
            set={setReadabilityWeight}
            step="0.1"
            value={readabilityWeight}
          />
          <Numeric
            disabled={Number(readabilityWeight) <= 0}
            id="score-readability-minimum"
            label="Readability target minimum"
            max="100"
            min="0"
            set={setReadabilityMinimum}
            step="0.1"
            value={readabilityMinimum}
          />
          <Numeric
            disabled={Number(readabilityWeight) <= 0}
            id="score-readability-maximum"
            label="Readability target maximum"
            max="100"
            min="0"
            set={setReadabilityMaximum}
            step="0.1"
            value={readabilityMaximum}
          />
        </div>
        <p className="field-help">
          Readability targets apply only when its weight is non-zero. This
          synchronous surface never loads a language model, so fluency stays at
          zero. Use the Advanced fluency and perplexity run for allowlisted,
          offline model scoring.
        </p>
        <div className="form-grid two-columns">
          <div>
            <label className="field-label" htmlFor="score-targets">
              Target phonemes
            </label>
            <input
              className="ipa-input"
              id="score-targets"
              lang="und-fonipa"
              onChange={(event) => setTargets(event.target.value)}
              value={targets}
            />
          </div>
          <Numeric
            id="score-top-k"
            label="Return top K"
            max="250"
            min="1"
            set={setTopK}
            value={topK}
          />
        </div>
        <button className="button button-primary" disabled={busy} type="submit">
          Rank candidates
        </button>
        {error ? (
          <ErrorNotice title="Scoring not completed" message={error} />
        ) : null}
      </form>
      {result ? (
        <div
          className="table-scroller"
          role="region"
          aria-label="Composite scoring results"
          tabIndex={0}
        >
          <table className="workbench-table">
            <caption>
              Ranked synchronous results; model-backed fluency is worker-only
            </caption>
            <thead>
              <tr>
                <th scope="col">Rank</th>
                <th scope="col">Candidate</th>
                <th scope="col">Composite</th>
                <th scope="col">Coverage gain</th>
                <th scope="col">Phonotactic</th>
                <th scope="col">Fluency</th>
                <th scope="col">Readability</th>
                <th scope="col">New units</th>
              </tr>
            </thead>
            <tbody>
              {result.ranked.map((item, index) => (
                <tr key={item.source_id}>
                  <td>{index + 1}</td>
                  <td dir="auto">{item.text}</td>
                  <td>{item.composite_score.toFixed(4)}</td>
                  <td>{item.coverage_gain}</td>
                  <td>{item.phonotactic_score.toFixed(4)}</td>
                  <td>{item.fluency_score.toFixed(4)}</td>
                  <td>
                    {item.readability_score === null
                      ? item.readability_status
                      : item.readability_score.toFixed(4)}
                  </td>
                  <td className="ipa-cell" lang="und-fonipa">
                    {item.new_units.join(" · ") || "None"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="workbench-empty">Run scoring to see real rankings.</p>
      )}
    </div>
  );
}

function Phonotactics() {
  const [mode, setMode] = useState<"inventory_derived" | "corpus_trained">(
    "inventory_derived",
  );
  const [inventory, setInventory] = useState("p b t d k ɡ m n a i u");
  const [training, setTraining] = useState("p a t\nb a d\nk a t");
  const [sequences, setSequences] = useState("p a t\nt a p");
  const [artifact, setArtifact] = useState<PhonotacticArtifact | null>(null);
  const [scores, setScores] = useState<PhonotacticScoreResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function train(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setScores(null);
    try {
      setArtifact(
        await generationApi.trainScorer({
          mode,
          n: 2,
          phonemes: mode === "inventory_derived" ? uniqueTokens(inventory) : [],
          sequences:
            mode === "corpus_trained"
              ? phonemeRows(training).map((phonemes) => ({ phonemes }))
              : [],
        }),
      );
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }
  async function score() {
    if (!artifact) return;
    setBusy(true);
    setError(null);
    try {
      setScores(
        await generationApi.scorePhonotactics({
          artifact,
          sequences: phonemeRows(sequences, 250).map((phonemes) => ({
            phonemes,
          })),
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
      <form onSubmit={(event) => void train(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Integrity-bound JSON artifact</p>
            <h2>Train and apply an n-gram scorer</h2>
          </div>
        </div>
        <label className="field-label" htmlFor="phonotactic-mode">
          Training mode
        </label>
        <select
          id="phonotactic-mode"
          onChange={(event) => {
            setMode(event.target.value as typeof mode);
            setArtifact(null);
            setScores(null);
          }}
          value={mode}
        >
          <option value="inventory_derived">Inventory-derived</option>
          <option value="corpus_trained">Corpus-trained sequences</option>
        </select>
        {mode === "inventory_derived" ? (
          <>
            <label className="field-label" htmlFor="phonotactic-inventory">
              Unique phonemes
            </label>
            <textarea
              className="ipa-input"
              id="phonotactic-inventory"
              lang="und-fonipa"
              onChange={(event) => setInventory(event.target.value)}
              rows={4}
              value={inventory}
            />
          </>
        ) : (
          <>
            <label className="field-label" htmlFor="phonotactic-training">
              Training sequences
            </label>
            <textarea
              className="ipa-input"
              id="phonotactic-training"
              lang="und-fonipa"
              onChange={(event) => setTraining(event.target.value)}
              rows={5}
              value={training}
            />
          </>
        )}
        <button className="button button-primary" disabled={busy} type="submit">
          Build scorer artifact
        </button>
      </form>
      {artifact ? (
        <div className="nested-result">
          <div className="result-toolbar">
            <div>
              <h3>Verified scorer artifact</h3>
              <small>SHA-256 {artifact.content_sha256}</small>
            </div>
            <button
              onClick={() => saveJson("phonotactic-scorer.json", artifact)}
              type="button"
            >
              Export artifact
            </button>
          </div>
          <label className="field-label" htmlFor="phonotactic-sequences">
            Sequences to score
          </label>
          <textarea
            className="ipa-input"
            id="phonotactic-sequences"
            lang="und-fonipa"
            onChange={(event) => setSequences(event.target.value)}
            rows={4}
            value={sequences}
          />
          <button
            className="button button-secondary"
            disabled={busy}
            onClick={() => void score()}
            type="button"
          >
            Score sequences
          </button>
          {scores ? (
            <ol className="score-list">
              {scores.scores.map((value, index) => (
                <li key={index}>
                  <span className="ipa-cell" lang="und-fonipa">
                    {phonemeRows(sequences)[index]?.join(" ")}
                  </span>
                  <strong>{value.toFixed(4)}</strong>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : (
        <p className="workbench-empty">Build an artifact before scoring.</p>
      )}
      {error ? (
        <ErrorNotice
          title="Phonotactic operation not completed"
          message={error}
        />
      ) : null}
    </div>
  );
}

function Readability() {
  const [texts, setTexts] = useState(
    "The cat sat on the mat.\nSesquipedalian terminology complicates comprehension.",
  );
  const [minimum, setMinimum] = useState("40");
  const [maximum, setMaximum] = useState("80");
  const [results, setResults] = useState<readonly ReadabilityResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResults([]);
    try {
      const range = { minimum: Number(minimum), maximum: Number(maximum) };
      setResults(
        await generationApi.readability({
          texts: nonEmptyLines(texts, 250),
          target_range: range,
          filter_range: range,
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
      <form onSubmit={(event) => void submit(event)}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Honest availability states</p>
            <h2>Flesch readability scoring</h2>
          </div>
        </div>
        <label className="field-label" htmlFor="readability-texts">
          Text rows
        </label>
        <textarea
          dir="auto"
          id="readability-texts"
          onChange={(event) => setTexts(event.target.value)}
          rows={6}
          value={texts}
        />
        <div className="form-grid two-columns">
          <Numeric
            id="readability-min"
            label="Minimum score"
            max="100"
            min="0"
            set={setMinimum}
            value={minimum}
          />
          <Numeric
            id="readability-max"
            label="Maximum score"
            max="100"
            min="0"
            set={setMaximum}
            value={maximum}
          />
        </div>
        <button className="button button-primary" disabled={busy} type="submit">
          Analyze readability
        </button>
        {error ? (
          <ErrorNotice title="Readability not completed" message={error} />
        ) : null}
      </form>
      {results.length ? (
        <div
          className="table-scroller"
          role="region"
          aria-label="Readability results"
          tabIndex={0}
        >
          <table className="workbench-table">
            <caption>
              Unavailable means the backend could not compute a meaningful value
            </caption>
            <thead>
              <tr>
                <th scope="col">Text</th>
                <th scope="col">Status</th>
                <th scope="col">Flesch ease</th>
                <th scope="col">Normalized score</th>
                <th scope="col">Filter</th>
              </tr>
            </thead>
            <tbody>
              {results.map((item, index) => (
                <tr key={index}>
                  <td dir="auto">{item.text}</td>
                  <td>{item.status}</td>
                  <td>{item.flesch_reading_ease ?? "Not computed"}</td>
                  <td>{item.score ?? "Not computed"}</td>
                  <td>
                    {item.accepted_by_filter === null
                      ? "Not computed"
                      : item.accepted_by_filter
                        ? "Accepted"
                        : "Excluded"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="workbench-empty">No readability results yet.</p>
      )}
    </div>
  );
}

function ModelValidation() {
  return (
    <div className="workbench-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">No secrets in the browser</p>
          <h2>Model runtime contracts</h2>
        </div>
        <span className="status-pill status-available">Advanced workbench</span>
      </div>
      <div className="availability-grid">
        <article className="availability-card">
          <h3>Hosted provider validation</h3>
          <p>
            Validate an allowlisted provider/model and opaque server-side
            connection reference, inspect a bounded cost estimate, then queue
            the unchanged specification on the external-provider worker.
          </p>
          <Link className="button button-secondary" href="/advanced">
            Open hosted configuration
          </Link>
        </article>
        <article className="availability-card">
          <h3>Local model validation</h3>
          <p>
            Validate an immutable model revision, verified snapshot, device,
            quantization, output cap, and deadline before submitting it to the
            configured GPU inference worker.
          </p>
          <Link className="button button-secondary" href="/advanced">
            Open local configuration
          </Link>
        </article>
        <article className="availability-card">
          <h3>Phon-DATG and Phon-RL</h3>
          <p>
            Inspect bounded DATG and PPO/reward primitives, validate exact
            runtime policy, and submit configured DATG or Phon-RL jobs through
            the durable control plane.
          </p>
          <Link className="button button-secondary" href="/advanced">
            Open advanced tools
          </Link>
        </article>
      </div>
      <p className="form-status">
        No provider key, model credential, token, or free-form secret field is
        rendered or stored by this application. Unconfigured worker policy
        remains default-deny.
      </p>
    </div>
  );
}

function Numeric({
  id,
  label,
  value,
  set,
  min,
  max,
  step = "1",
  disabled = false,
}: {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly set: (value: string) => void;
  readonly min: string;
  readonly max: string;
  readonly step?: string;
  readonly disabled?: boolean;
}) {
  return (
    <div>
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <input
        disabled={disabled}
        id={id}
        max={max}
        min={min}
        onChange={(event) => set(event.target.value)}
        step={step}
        type="number"
        value={value}
      />
    </div>
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
