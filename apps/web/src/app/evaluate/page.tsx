import type { Metadata } from "next";

import { EvaluationStudio } from "@/components/evaluation-studio";

export const metadata: Metadata = {
  title: "Evaluation Studio",
  description:
    "Evaluate real sentence text for phonetic coverage with CorpusGen.",
};

export default function EvaluatePage() {
  return (
    <div className="evaluate-page">
      <section
        className="section-frame studio-intro"
        aria-labelledby="studio-title"
      >
        <div>
          <p className="eyebrow">Real workflow · CorpusGen evaluation</p>
          <h1 id="studio-title">Evaluation Studio</h1>
        </div>
        <p>
          Submit sentence text to the connected API for grapheme-to-phoneme
          conversion and coverage analysis. CorpusKit validates every response
          before displaying it.
        </p>
      </section>
      <div className="section-frame">
        <EvaluationStudio />
      </div>
    </div>
  );
}
