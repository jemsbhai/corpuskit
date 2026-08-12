import type { Metadata } from "next";

import { CoverageWeightingLab } from "@/components/coverage-weighting-lab";

export const metadata: Metadata = {
  title: "Coverage & Weighting Lab",
  description:
    "Estimate target spaces, track coverage, compute weights, and export canonical reports.",
};

export default function CoveragePage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">State, priority, provenance</p>
          <h1>Coverage &amp; Weighting Lab</h1>
        </div>
        <p>
          Estimate Cartesian targets before allocation, inspect every ordered
          gain and source, compute engine-backed priorities, and export human or
          canonical reports.
        </p>
      </header>
      <CoverageWeightingLab />
    </div>
  );
}
