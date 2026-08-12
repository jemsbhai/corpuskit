"use client";

import {
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  useRef,
  useState,
} from "react";

import {
  evaluateCorpus,
  EvaluationApiError,
  EvaluationContractError,
  type EvaluationResult,
  type TargetMode,
} from "@/lib/evaluation";

const previewSentences = [
  "The quick brown fox jumps over the lazy dog.",
  "She sells seashells by the seashore.",
  "Pack my box with five dozen liquor jugs.",
];

const languageOptions = [
  { value: "en-us", label: "English (United States)" },
  { value: "en-gb", label: "English (Great Britain)" },
  { value: "es", label: "Spanish" },
  { value: "fr-fr", label: "French" },
  { value: "de", label: "German" },
  { value: "hi", label: "Hindi" },
] as const;

const defaultMaximumUploadBytes = 10 * 1024 * 1024;
const languageCodePattern = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$/u;

function splitSentences(value: string): string[] {
  return value
    .split(/\r?\n/u)
    .map((sentence) => sentence.trim())
    .filter(Boolean);
}

function splitPhonemes(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,\r\n]+/u)
        .map((phoneme) => phoneme.trim())
        .filter(Boolean),
    ),
  );
}

function validateInput(options: {
  sentences: string[];
  language: string;
  targetMode: TargetMode;
  targetPhonemes: string[];
}): string | null {
  if (options.sentences.length === 0)
    return "Add at least one non-empty sentence.";
  if (options.sentences.length > 500)
    return "Use no more than 500 sentences per evaluation.";
  if (options.sentences.some((sentence) => sentence.length > 2_000)) {
    return "Each sentence must contain 2,000 characters or fewer.";
  }
  if (
    new TextEncoder().encode(options.sentences.join("\n")).byteLength >
    defaultMaximumUploadBytes
  ) {
    return "The combined UTF-8 sentence text must be no larger than 10 MiB.";
  }
  if (
    options.language.trim().length > 32 ||
    !languageCodePattern.test(options.language.trim())
  ) {
    return "Enter a valid language code such as en, en-us, or fr-fr.";
  }
  if (
    options.targetMode === "explicit" &&
    options.targetPhonemes.length === 0
  ) {
    return "Add at least one target phoneme for an explicit inventory.";
  }
  if (options.targetPhonemes.length > 256)
    return "Use no more than 256 explicit phonemes.";
  return null;
}

function readableError(error: unknown): {
  message: string;
  reference: string | null;
} {
  if (error instanceof EvaluationApiError) {
    return { message: error.message, reference: error.requestId };
  }
  if (error instanceof EvaluationContractError) {
    return {
      message:
        "The API response did not match the supported evaluation contract. No result was shown.",
      reference: null,
    };
  }
  return {
    message:
      "The evaluation API is unavailable. No result was computed or substituted.",
    reference: null,
  };
}

function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      if (typeof reader.result === "string") resolve(reader.result);
      else reject(new Error("The selected file is not readable text."));
    });
    reader.addEventListener("error", () =>
      reject(reader.error ?? new Error("File read failed.")),
    );
    reader.readAsText(file, "UTF-8");
  });
}

export function EvaluationStudio() {
  const [text, setText] = useState("");
  const [language, setLanguage] = useState("en-us");
  const [unit, setUnit] = useState<"phoneme" | "diphone" | "triphone">(
    "phoneme",
  );
  const [targetMode, setTargetMode] = useState<TargetMode>("derived");
  const [explicitPhonemes, setExplicitPhonemes] = useState("");
  const [result, setResult] = useState<EvaluationResult | null>(null);
  const [error, setError] = useState<{
    message: string;
    reference: string | null;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const resultRef = useRef<HTMLDivElement | null>(null);

  const sentences = splitSentences(text);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const targetPhonemes =
      targetMode === "explicit" ? splitPhonemes(explicitPhonemes) : [];
    const inputError = validateInput({
      sentences,
      language,
      targetMode,
      targetPhonemes,
    });
    if (inputError) {
      setError({ message: inputError, reference: null });
      return;
    }

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setPending(true);
    setError(null);
    setNotice(null);
    setResult(null);

    try {
      const nextResult = await evaluateCorpus(
        {
          sentences,
          language: language.trim(),
          unit,
          target: { mode: targetMode, phonemes: targetPhonemes },
        },
        controller.signal,
      );
      setResult(nextResult);
      window.requestAnimationFrame(() => resultRef.current?.focus());
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        setNotice("Evaluation cancelled. No result was computed.");
      } else {
        setError(readableError(caught));
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setPending(false);
      }
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  function loadPreviewInput() {
    setText(previewSentences.join("\n"));
    setLanguage("en-us");
    setUnit("phoneme");
    setTargetMode("derived");
    setExplicitPhonemes("");
    setResult(null);
    setError(null);
    setNotice(
      "Preview input loaded. Submit it to request a real API evaluation.",
    );
  }

  async function importTextFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > defaultMaximumUploadBytes) {
      setError({
        message: "Choose a plain-text file no larger than 10 MiB.",
        reference: null,
      });
      event.target.value = "";
      return;
    }
    try {
      const imported = await readFileAsText(file);
      setText(imported);
      setResult(null);
      setError(null);
      setNotice(
        `${file.name} loaded into the form. It has not been evaluated yet.`,
      );
    } catch {
      setError({
        message: "The selected text file could not be read.",
        reference: null,
      });
    } finally {
      event.target.value = "";
    }
  }

  function handleEditorShortcut(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <div className="evaluation-studio">
      <form className="evaluation-form" onSubmit={submit} noValidate>
        <section
          className="studio-panel sentence-panel"
          aria-labelledby="sentences-title"
        >
          <div className="panel-heading">
            <div>
              <span className="step-marker">01</span>
              <h2 id="sentences-title">Add sentence text</h2>
            </div>
            <span className="sentence-counter" aria-live="polite">
              {sentences.length} / 500 sentences
            </span>
          </div>
          <label className="field-label" htmlFor="evaluation-sentences">
            Sentences <span>one per line</span>
          </label>
          <textarea
            aria-describedby="sentence-help"
            id="evaluation-sentences"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={handleEditorShortcut}
            placeholder={
              "The cat sat on the mat.\nBright stars shimmer above the quiet lake."
            }
            rows={12}
            value={text}
          />
          <p className="field-help" id="sentence-help">
            Paste UTF-8 text, or import a plain-text file. Maximum 500 lines and
            2,000 characters per sentence. Press Ctrl/⌘ + Enter to evaluate.
          </p>
          <div className="input-actions">
            <label className="file-button" htmlFor="sentence-file">
              Import .txt
            </label>
            <input
              accept=".txt,text/plain"
              className="visually-hidden-input"
              id="sentence-file"
              onChange={(event) => void importTextFile(event)}
              type="file"
            />
            <button
              className="text-button"
              onClick={loadPreviewInput}
              type="button"
            >
              Load preview input
            </button>
          </div>
          <p className="preview-disclosure">
            “Load preview input” only fills the form. It never creates or
            displays a fake result.
          </p>
        </section>

        <section
          className="studio-panel configuration-panel"
          aria-labelledby="configuration-title"
        >
          <div className="panel-heading">
            <div>
              <span className="step-marker">02</span>
              <h2 id="configuration-title">Configure evaluation</h2>
            </div>
          </div>

          <div className="field-group">
            <label className="field-label" htmlFor="evaluation-language">
              Language code
            </label>
            <input
              id="evaluation-language"
              list="language-codes"
              maxLength={32}
              onChange={(event) => setLanguage(event.target.value)}
              required
              value={language}
            />
            <datalist id="language-codes">
              {languageOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </datalist>
            <p className="field-help">
              Use an eSpeak language or voice code, such as en-us.
            </p>
          </div>

          <fieldset className="choice-fieldset">
            <legend>Coverage unit</legend>
            <div className="segmented-options">
              {(["phoneme", "diphone", "triphone"] as const).map((option) => (
                <label key={option}>
                  <input
                    checked={unit === option}
                    name="coverage-unit"
                    onChange={() => setUnit(option)}
                    type="radio"
                    value={option}
                  />
                  <span>{option}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="choice-fieldset target-fieldset">
            <legend>Target inventory</legend>
            <div className="stacked-options">
              <label>
                <input
                  checked={targetMode === "derived"}
                  name="target-mode"
                  onChange={() => setTargetMode("derived")}
                  type="radio"
                />
                <span>
                  <strong>Derived from input</strong>
                  <small>Discover the units present in these sentences.</small>
                </span>
              </label>
              <label>
                <input
                  checked={targetMode === "phoible"}
                  name="target-mode"
                  onChange={() => setTargetMode("phoible")}
                  type="radio"
                />
                <span>
                  <strong>PHOIBLE inventory</strong>
                  <small>
                    Compare against the mapped inventory for this language.
                  </small>
                </span>
              </label>
              <label>
                <input
                  checked={targetMode === "explicit"}
                  name="target-mode"
                  onChange={() => setTargetMode("explicit")}
                  type="radio"
                />
                <span>
                  <strong>Explicit IPA list</strong>
                  <small>
                    Provide up to 256 comma- or line-separated units.
                  </small>
                </span>
              </label>
            </div>
          </fieldset>

          {targetMode === "explicit" ? (
            <div className="field-group explicit-targets">
              <label className="field-label" htmlFor="explicit-phonemes">
                Target phonemes
              </label>
              <textarea
                id="explicit-phonemes"
                onChange={(event) => setExplicitPhonemes(event.target.value)}
                placeholder="p, b, t, d, k, ɡ"
                rows={3}
                value={explicitPhonemes}
              />
            </div>
          ) : null}

          <div className="evaluation-submit-row">
            <button
              className="button button-primary"
              disabled={pending}
              type="submit"
            >
              {pending ? "Evaluating…" : "Evaluate corpus"}
            </button>
            {pending ? (
              <button
                className="button button-secondary"
                onClick={cancel}
                type="button"
              >
                Cancel
              </button>
            ) : null}
          </div>
        </section>
      </form>

      <div
        className="studio-announcements"
        aria-atomic="true"
        aria-live="polite"
      >
        {pending ? (
          <p className="loading-notice" role="status">
            <i aria-hidden="true" /> Running a live evaluation…
          </p>
        ) : null}
        {notice ? (
          <p className="info-notice" role="status">
            {notice}
          </p>
        ) : null}
        {error ? (
          <div className="error-notice" role="alert">
            <strong>Evaluation not completed</strong>
            <p>{error.message}</p>
            {error.reference ? (
              <small>Request reference: {error.reference}</small>
            ) : null}
          </div>
        ) : null}
      </div>

      {result ? (
        <EvaluationResults ref={resultRef} result={result} />
      ) : !pending && !error ? (
        <section
          className="results-empty"
          aria-labelledby="empty-results-title"
        >
          <span aria-hidden="true">ə</span>
          <div>
            <h2 id="empty-results-title">
              Results will appear here after a live API evaluation.
            </h2>
            <p>
              No result has been computed yet. Configure the form and submit
              when ready.
            </p>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function EvaluationResults({
  result,
  ref,
}: {
  result: EvaluationResult;
  ref: React.Ref<HTMLDivElement>;
}) {
  const covered = result.covered_units.length;
  const total = result.target_units.length;
  const percentage = result.coverage * 100;

  return (
    <section
      className="evaluation-results"
      aria-labelledby="evaluation-results-title"
    >
      <div className="results-heading" ref={ref} tabIndex={-1}>
        <div>
          <p className="eyebrow">Live API result</p>
          <h2 id="evaluation-results-title">
            {percentage.toFixed(1)}% coverage
          </h2>
          <p>
            {result.total_sentences} sentences · {result.language} ·{" "}
            {result.unit} · {result.target_mode} target
          </p>
        </div>
        <div
          className="coverage-gauge"
          style={
            {
              "--coverage-angle": `${percentage * 3.6}deg`,
            } as React.CSSProperties
          }
        >
          <span>
            <strong>{covered}</strong>
            <small>of {total} units</small>
          </span>
        </div>
      </div>

      <div className="result-summary-grid">
        <article className="result-card missing-card">
          <div className="result-card-heading">
            <h3>Missing units</h3>
            <span>{result.missing_units.length}</span>
          </div>
          {result.missing_units.length ? (
            <ul className="unit-chip-list" aria-label="Missing target units">
              {result.missing_units.map((missingUnit) => (
                <li key={missingUnit}>{missingUnit}</li>
              ))}
            </ul>
          ) : (
            <p className="complete-message">Every target unit is covered.</p>
          )}
        </article>

        <article className="result-card">
          <div className="result-card-heading">
            <h3>Distribution</h3>
          </div>
          {result.distribution ? (
            <dl className="metric-list">
              <div>
                <dt>Normalized entropy</dt>
                <dd>{result.distribution.normalized_entropy.toFixed(3)}</dd>
              </div>
              <div>
                <dt>JSD vs uniform</dt>
                <dd>{result.distribution.jsd_uniform.toFixed(3)}</dd>
              </div>
              <div>
                <dt>PCD score</dt>
                <dd>{result.distribution.pcd_uniform.toFixed(3)}</dd>
              </div>
            </dl>
          ) : (
            <p>Distribution metrics were not returned for this run.</p>
          )}
        </article>
      </div>

      <section
        className="contribution-section"
        aria-labelledby="contribution-title"
      >
        <div className="result-section-heading">
          <div>
            <p className="eyebrow">Sentence provenance</p>
            <h3 id="contribution-title">What each sentence contributed</h3>
          </div>
          <p>
            “New units” are targets first covered at that position in the
            submitted order.
          </p>
        </div>
        <div
          className="table-scroller"
          tabIndex={0}
          role="region"
          aria-label="Scrollable sentence contributions"
        >
          <table className="contribution-table">
            <caption className="sr-only">
              Sentence-level phoneme and new target-unit contributions
            </caption>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Sentence</th>
                <th scope="col">Phonemes</th>
                <th scope="col">New units</th>
              </tr>
            </thead>
            <tbody>
              {result.sentence_details.map((detail) => (
                <tr key={detail.index}>
                  <th scope="row">{detail.index + 1}</th>
                  <td>{detail.text}</td>
                  <td>
                    <span className="phoneme-sequence">
                      {detail.all_phonemes.join(" ") || "None"}
                    </span>
                    <small>{detail.phoneme_count} phonemes</small>
                  </td>
                  <td>
                    {detail.new_units.length ? (
                      <span className="new-unit-list">
                        {detail.new_units.join(" · ")}
                      </span>
                    ) : (
                      <span className="muted-result">No new units</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}
