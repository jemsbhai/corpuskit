import type { Metadata } from "next";

import { SelectionStudio } from "@/components/selection-studio";

export const metadata: Metadata = {
  title: "Selection Studio",
  description:
    "Compare six normalized CorpusGen sentence-selection algorithms without fabricated results.",
};

export default function SelectionPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">Optimize · Compare · Export</p>
          <h1>Selection Studio</h1>
        </div>
        <p>
          Configure all six selector families, bounded budgets, explicit weights
          and distributions, then retain up to four real results for accessible
          comparison.
        </p>
      </header>
      <SelectionStudio />
    </div>
  );
}
