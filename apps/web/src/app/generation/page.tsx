import type { Metadata } from "next";

import { GenerationScoringStudio } from "@/components/generation-scoring-studio";

export const metadata: Metadata = {
  title: "Generation & Scoring Studio",
  description:
    "Bounded repository previews and deterministic CorpusGen scoring workbenches.",
};

export default function GenerationPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">Preview · Score · Validate</p>
          <h1>Generation & Scoring Studio</h1>
        </div>
        <p>
          Exercise the production-safe repository and scoring surfaces with
          finite budgets, integrity-bound artifacts, and explicit worker
          boundaries.
        </p>
      </header>
      <GenerationScoringStudio />
    </div>
  );
}
