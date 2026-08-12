import Link from "next/link";

import { GuidedDemo } from "@/components/guided-demo";
import { Waveform } from "@/components/waveform";

const productSurfaces = [
  {
    eyebrow: "01 · Understand",
    title: "Inventory & analysis",
    description:
      "Inspect PHOIBLE segments and measure phoneme, diphone, or triphone coverage with source-level provenance.",
    detail: "Available workbenches",
    href: "/inventory",
  },
  {
    eyebrow: "02 · Decide",
    title: "Optimization studio",
    description:
      "Compare six sentence-selection strategies and see exactly what each sentence contributes.",
    detail: "Six-way comparison available",
    href: "/selection",
  },
  {
    eyebrow: "03 · Create",
    title: "Guided generation",
    description:
      "Target missing units with repository, hosted, or local generation and transparent scoring controls.",
    detail: "Scoring plus durable worker submission",
    href: "/generation",
  },
] as const;

export default function HomePage() {
  return (
    <>
      <section className="hero section-frame" aria-labelledby="hero-title">
        <div className="hero-copy">
          <p className="eyebrow">
            <span aria-hidden="true">◆</span> Corpus design workbench
          </p>
          <h1 id="hero-title">
            Design speech corpora with evidence, not guesswork.
          </h1>
          <p className="hero-lede">
            CorpusKit turns phonetic coverage, selection, and generation into a
            traceable workflow—so every sentence earns its place.
          </p>
          <div className="hero-actions">
            <Link className="button button-primary" href="/evaluate">
              Open Evaluation Studio
            </Link>
            <a className="button button-secondary" href="#guided-demo">
              Explore the fixed walkthrough
            </a>
            <Link className="button button-secondary" href="/capabilities">
              Check capability status
            </Link>
          </div>
          <p className="honesty-note">
            <strong>Alpha application.</strong> The workbenches use the
            connected API; the walkthrough below stays deliberately fixed and
            illustrative. Optional model, provider, and GPU workflows remain
            capability-gated by each deployment.
          </p>
        </div>

        <div
          className="hero-instrument"
          aria-label="Illustrative phonetic coverage display"
        >
          <div className="instrument-topline">
            <span>Riverbend demo</span>
            <span className="live-dot-label">
              <i aria-hidden="true" /> Illustrative
            </span>
          </div>
          <div className="coverage-orbit">
            <div className="coverage-value">
              <strong>84%</strong>
              <span>phoneme coverage</span>
            </div>
            <span className="orbit-token token-one">ʃ</span>
            <span className="orbit-token token-two">ŋ</span>
            <span className="orbit-token token-three">ð</span>
            <span className="orbit-token token-four">ʒ</span>
          </div>
          <Waveform />
          <div className="instrument-footer">
            <span>32 / 38 units</span>
            <span>12 sentences</span>
            <span>en-us</span>
          </div>
        </div>
      </section>

      <section className="proof-strip" aria-label="Product principles">
        <div className="section-frame proof-grid">
          <p>
            <strong>Immutable</strong>
            <span>versioned corpus history</span>
          </p>
          <p>
            <strong>Explainable</strong>
            <span>sentence-level provenance</span>
          </p>
          <p>
            <strong>Reproducible</strong>
            <span>engine and model manifests</span>
          </p>
          <p>
            <strong>Language-aware</strong>
            <span>PHOIBLE + eSpeak mapping</span>
          </p>
        </div>
      </section>

      <GuidedDemo />

      <section
        className="section-frame capability-preview"
        aria-labelledby="preview-title"
      >
        <div className="section-heading-row">
          <div>
            <p className="eyebrow">One connected laboratory</p>
            <h2 id="preview-title">From inventory to a defensible corpus.</h2>
          </div>
          <Link className="text-link" href="/capabilities">
            View implementation status <span aria-hidden="true">↗</span>
          </Link>
        </div>
        <div className="surface-grid">
          {productSurfaces.map((surface) => (
            <article className="surface-card" key={surface.title}>
              <p className="surface-number">{surface.eyebrow}</p>
              <h3>{surface.title}</h3>
              <p>{surface.description}</p>
              <span className="planned-label">{surface.detail}</span>
              <Link className="text-link" href={surface.href}>
                Open {surface.title} <span aria-hidden="true">↗</span>
              </Link>
            </article>
          ))}
        </div>
      </section>

      <section
        className="closing-callout section-frame"
        aria-labelledby="closing-title"
      >
        <div>
          <p className="eyebrow">Built for accountable research</p>
          <h2 id="closing-title">Every run should be explainable tomorrow.</h2>
        </div>
        <p>
          CorpusKit records immutable inputs, explicit defaults, durable job
          events, and downloadable manifests—so the evidence survives beyond a
          progress bar.
        </p>
      </section>
    </>
  );
}
