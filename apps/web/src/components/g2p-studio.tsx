"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import { describeRequestError } from "@/lib/api-client";
import { g2pApi, type G2PTranscription } from "@/lib/g2p";
import { orderedLines, saveBytes, saveJson } from "@/lib/workbench-input";

type InputMode = "single" | "batch";

export function G2PStudio() {
  const [mode, setMode] = useState<InputMode>("single");
  const [language, setLanguage] = useState("en-us");
  const [languages, setLanguages] = useState<string[]>([]);
  const [text, setText] = useState(
    "The quick brown fox jumps over the lazy dog.",
  );
  const [results, setResults] = useState<G2PTranscription[]>([]);
  const [variants, setVariants] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const resultRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void g2pApi.languages(controller.signal).then(
      (catalog) => setLanguages(catalog.languages),
      (caught: unknown) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setNotice(
            "The language catalog is unavailable; enter a known eSpeak code.",
          );
        }
      },
    );
    return () => controller.abort();
  }, []);

  async function run(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      const next = variants
        ? (
            await g2pApi.variants(
              mode === "batch" ? (orderedLines(text)[0] ?? "") : text,
              language,
            )
          ).variants
        : mode === "batch"
          ? await g2pApi.batch(orderedLines(text), language)
          : [await g2pApi.single(text, language)];
      setResults(next);
      setNotice(
        `${next.length} ordered transcription${next.length === 1 ? "" : "s"} ready.`,
      );
      requestAnimationFrame(() => resultRef.current?.focus());
    } catch (caught) {
      setResults([]);
      setError(describeRequestError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function copyResults() {
    try {
      await navigator.clipboard.writeText(
        results.map((item) => `${item.text}\t${item.ipa}`).join("\n"),
      );
      setNotice("IPA rows copied to the clipboard.");
    } catch {
      setNotice("Clipboard access was denied; use the TSV download instead.");
    }
  }

  return (
    <section className="workbench-shell" aria-labelledby="g2p-form-title">
      <form className="workbench-form" onSubmit={run}>
        <div className="workbench-form-heading">
          <div>
            <p className="eyebrow">eSpeak backend · Ordered Unicode output</p>
            <h2 id="g2p-form-title">Transcription controls</h2>
          </div>
          <fieldset className="segmented-control">
            <legend className="sr-only">Input mode</legend>
            {(["single", "batch"] as const).map((value) => (
              <label key={value}>
                <input
                  checked={mode === value}
                  name="g2p-mode"
                  onChange={() => {
                    setMode(value);
                    setVariants(false);
                    setResults([]);
                  }}
                  type="radio"
                  value={value}
                />
                {value === "single" ? "Single" : "Batch"}
              </label>
            ))}
          </fieldset>
        </div>

        <div className="form-grid two-columns">
          <div>
            <label className="field-label" htmlFor="g2p-language">
              Language or variant
            </label>
            <input
              aria-describedby="g2p-language-help"
              id="g2p-language"
              list="g2p-languages"
              maxLength={32}
              onChange={(event) => setLanguage(event.target.value)}
              required
              value={language}
            />
            <datalist id="g2p-languages">
              {languages.map((item) => (
                <option key={item} value={item} />
              ))}
            </datalist>
            <small className="field-help" id="g2p-language-help">
              {languages.length
                ? `${languages.length.toLocaleString()} supported eSpeak language codes loaded.`
                : "Use an installed eSpeak language code, such as en-us."}
            </small>
          </div>
          <label className="check-card">
            <input
              checked={variants}
              disabled={mode === "batch"}
              onChange={(event) => setVariants(event.target.checked)}
              type="checkbox"
            />
            <span>
              <strong>Compare known dialect variants</strong>
              <small>
                Available for one input. eSpeak may return one canonical result.
              </small>
            </span>
          </label>
        </div>

        <div>
          <label className="field-label" htmlFor="g2p-input">
            {mode === "single" ? "Text" : "Texts · one ordered item per line"}
          </label>
          <textarea
            dir="auto"
            id="g2p-input"
            maxLength={mode === "single" ? 20_000 : 200_000}
            onChange={(event) => setText(event.target.value)}
            rows={mode === "single" ? 5 : 9}
            value={text}
          />
          <small className="field-help">
            Blank batch rows remain ordered inputs. IPA is rendered with
            automatic bidirectional isolation and is never interpreted as
            markup.
          </small>
        </div>

        <div className="form-actions">
          <button
            className="button button-primary"
            disabled={busy}
            type="submit"
          >
            {busy
              ? "Transcribing…"
              : variants
                ? "Compare variants"
                : "Transcribe"}
          </button>
          <span aria-live="polite" className="form-status" role="status">
            {notice}
          </span>
        </div>
        {error ? (
          <div className="error-notice" role="alert">
            <strong>Transcription not completed</strong>
            <p>{error}</p>
          </div>
        ) : null}
      </form>

      <div className="workbench-results" ref={resultRef} tabIndex={-1}>
        <div className="result-toolbar">
          <div>
            <p className="eyebrow">Live API result</p>
            <h2>Ordered transcription output</h2>
          </div>
          <div className="compact-actions">
            <button
              disabled={!results.length}
              onClick={() => void copyResults()}
              type="button"
            >
              Copy IPA
            </button>
            <button
              disabled={!results.length}
              onClick={() => saveJson("corpuskit-g2p.json", results)}
              type="button"
            >
              Export JSON
            </button>
            <button
              disabled={!results.length}
              onClick={() =>
                saveBytes(
                  "corpuskit-g2p.tsv",
                  `text\tlanguage\tipa\tphonemes\tdiphones\ttriphones\n${results
                    .map((item) =>
                      [
                        item.text,
                        item.language,
                        item.ipa,
                        item.phonemes.join(" "),
                        item.diphones.join(" "),
                        item.triphones.join(" "),
                      ]
                        .map((cell) =>
                          cell.replaceAll("\t", " ").replaceAll("\n", " "),
                        )
                        .join("\t"),
                    )
                    .join("\n")}`,
                  "text/tab-separated-values;charset=utf-8",
                )
              }
              type="button"
            >
              Export TSV
            </button>
          </div>
        </div>

        {results.length ? (
          <div
            className="table-scroller"
            role="region"
            aria-label="G2P results"
            tabIndex={0}
          >
            <table className="workbench-table">
              <caption>
                Ordered IPA and derived consecutive phonetic units
              </caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Text</th>
                  <th scope="col">IPA</th>
                  <th scope="col">Phonemes</th>
                  <th scope="col">Diphones</th>
                  <th scope="col">Triphones</th>
                </tr>
              </thead>
              <tbody>
                {results.map((item, index) => (
                  <tr key={`${index}-${item.language}-${item.text}`}>
                    <th scope="row">{index + 1}</th>
                    <td>
                      <bdi dir="auto">{item.text || "Blank input"}</bdi>
                    </td>
                    <td className="ipa-text" lang={item.language}>
                      <bdi>{item.ipa || "—"}</bdi>
                    </td>
                    <td className="ipa-text" lang={item.language}>
                      {item.phonemes.join(" ") || "—"}
                    </td>
                    <td className="ipa-text" lang={item.language}>
                      {item.diphones.join(" ") || "—"}
                    </td>
                    <td className="ipa-text" lang={item.language}>
                      {item.triphones.join(" ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="workbench-empty">
            Submit text to receive real eSpeak output.
          </p>
        )}
      </div>
    </section>
  );
}
